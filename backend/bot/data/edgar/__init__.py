"""Stage-18a — SEC EDGAR filings ingest.

Most retail bots miss the things that actually move stocks: 8-K item
2.02 (results announcement) at 16:05 ET, 8-K item 5.02 (officer/director
change), Form 4 (insider purchases). All of these are free + official
via data.sec.gov.

Two endpoints we use:

  • ``/submissions/CIK##########.json`` — recent filings list per company
  • ``/files/company_tickers.json``      — ticker→CIK lookup

The ticker→CIK map is cached in-process (it changes rarely). The
filings cache lives in SQLite (``edgar_filings`` table).

SEC requires a ``User-Agent`` header identifying the operator
(``TB_SEC_USER_AGENT`` env var). Without it we degrade gracefully —
fetch returns empty and the rest of the bot is unaffected.

Rate limit: SEC allows ≤10 req/sec. We fetch one ticker at a time so
worst-case is well under that even if a future caller iterates the
watchlist.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import desc, select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.edgar_filing import EdgarFiling

logger = logging.getLogger(__name__)


# Forms we ingest by default. 8-K = material event, 10-Q/10-K = periodic
# results, 4 = insider transaction, S-3/S-1 = registration / secondary,
# SC 13D/G = beneficial ownership >5%.
DEFAULT_FORMS: Tuple[str, ...] = (
    "8-K", "10-Q", "10-K", "4", "S-3", "S-1", "SC 13D", "SC 13G",
)


# Material 8-K item codes worth flagging in the engine event log.
# (https://www.sec.gov/forms — "Form 8-K Items")
MATERIAL_8K_ITEMS: Tuple[str, ...] = (
    "1.01",   # entry into material agreement
    "1.02",   # termination of material agreement
    "2.01",   # acquisition / disposition
    "2.02",   # results of operations (earnings release)
    "2.05",   # exit or disposal activities
    "3.02",   # unregistered sales of equity
    "5.02",   # officer / director changes
    "7.01",   # Regulation FD disclosure
    "8.01",   # other events
)


# ── client ──────────────────────────────────────────────────────────────


@dataclass
class FilingRow:
    accession_number: str
    form: str
    filed_at: datetime
    primary_document: Optional[str] = None
    items: Optional[str] = None
    reporter: Optional[str] = None
    is_insider_buy: bool = False
    is_insider_sell: bool = False


_TICKER_MAP_CACHE: Dict[str, str] = {}    # ticker → CIK (10-digit, zero-padded)


def _default_get(url: str, *, user_agent: str) -> bytes:
    """Hit a URL with the SEC-required User-Agent. Uses ``requests``
    (which auto-bundles certifi's CA store) rather than
    ``urllib.request`` — the latter fails on macOS Python 3.14 with
    SSL: CERTIFICATE_VERIFY_FAILED."""
    import requests
    resp = requests.get(url, headers={
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
    }, timeout=15)
    resp.raise_for_status()
    return resp.content


class EdgarClient:
    """SEC EDGAR client. Injectable HTTP fetcher for testing."""

    def __init__(self, *, user_agent: Optional[str] = None,
                    getter: Optional[Callable[..., bytes]] = None) -> None:
        self._user_agent = user_agent
        self._getter = getter or _default_get

    def _ua(self) -> str:
        # SEC asks for an email so they can contact heavy users. Matches
        # the memo / narrative / brain pattern: explicit None falls
        # through to TUNABLES; an explicit "" overrides to disable.
        if self._user_agent is not None:
            return self._user_agent
        return getattr(TUNABLES, "sec_user_agent", "") or ""

    @property
    def available(self) -> bool:
        return bool(self._ua())

    def ticker_to_cik(self, ticker: str) -> Optional[str]:
        """Return zero-padded 10-digit CIK for a ticker, or None when
        we can't resolve it."""
        tk = (ticker or "").upper().strip()
        if not tk:
            return None
        if tk in _TICKER_MAP_CACHE:
            return _TICKER_MAP_CACHE[tk]
        if not self.available:
            return None
        try:
            raw = self._getter(
                "https://www.sec.gov/files/company_tickers.json",
                user_agent=self._ua(),
            )
            data = json.loads(raw)
            for _, entry in data.items():
                t = str(entry.get("ticker") or "").upper()
                cik = entry.get("cik_str")
                if t and cik:
                    _TICKER_MAP_CACHE[t] = str(cik).zfill(10)
            return _TICKER_MAP_CACHE.get(tk)
        except Exception:
            logger.warning("EDGAR ticker map fetch failed", exc_info=True)
            return None

    def recent_filings(self, ticker: str, *,
                          forms: Tuple[str, ...] = DEFAULT_FORMS,
                          limit: int = 40) -> List[FilingRow]:
        """Return the most-recent filings for ``ticker``. Filtered to
        ``forms`` and capped at ``limit`` records."""
        if not self.available:
            return []
        cik = self.ticker_to_cik(ticker)
        if cik is None:
            return []
        try:
            raw = self._getter(
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                user_agent=self._ua(),
            )
            data = json.loads(raw)
        except Exception:
            logger.warning("EDGAR submissions fetch failed for %s", ticker,
                              exc_info=True)
            return []
        recent = ((data.get("filings") or {}).get("recent") or {})
        accs = recent.get("accessionNumber") or []
        forms_arr = recent.get("form") or []
        dates = recent.get("filingDate") or []
        prim_docs = recent.get("primaryDocument") or []
        items_arr = recent.get("items") or []
        out: List[FilingRow] = []
        for i in range(min(len(accs), limit * 5)):
            form = forms_arr[i] if i < len(forms_arr) else ""
            if form not in forms:
                continue
            try:
                filed = datetime.strptime(dates[i], "%Y-%m-%d")
            except Exception:
                continue
            row = FilingRow(
                accession_number=accs[i],
                form=form, filed_at=filed,
                primary_document=prim_docs[i] if i < len(prim_docs) else None,
                items=items_arr[i] if i < len(items_arr) else None,
            )
            out.append(row)
            if len(out) >= limit:
                break
        return out


# ── insider classification ──────────────────────────────────────────────


def _classify_insider_form4(filing: FilingRow,
                                client: Optional[EdgarClient] = None
                                ) -> FilingRow:
    """Form 4 filings carry transaction codes (P = purchase, S = sale,
    A = grant). Determining buy vs sell requires fetching the XML
    primary document. To keep things light, we don't parse the XML
    here — we leave both flags False and let downstream consumers
    decide whether to make a follow-up call.

    Future hook: parse the actual XML to set ``is_insider_buy`` /
    ``is_insider_sell``. Marked as a TODO so it isn't lost."""
    return filing


# ── cache / refresh ─────────────────────────────────────────────────────


def _upsert_filings(ticker: str, cik: str,
                       filings: List[FilingRow]) -> int:
    if not filings:
        return 0
    inserted = 0
    try:
        with session_scope() as session:
            existing = set(session.execute(
                select(EdgarFiling.accession_number)
                .where(EdgarFiling.cik == cik)
            ).scalars().all())
            for f in filings:
                if f.accession_number in existing:
                    continue
                session.add(EdgarFiling(
                    cik=cik, ticker=ticker.upper(),
                    accession_number=f.accession_number,
                    form=f.form, filed_at=f.filed_at,
                    primary_document=f.primary_document,
                    items=f.items, reporter=f.reporter,
                    is_insider_buy=f.is_insider_buy,
                    is_insider_sell=f.is_insider_sell,
                ))
                inserted += 1
    except Exception:
        logger.exception("EDGAR upsert failed for %s", ticker)
    return inserted


def refresh_ticker(ticker: str, *,
                      client: Optional[EdgarClient] = None,
                      forms: Tuple[str, ...] = DEFAULT_FORMS,
                      limit: int = 40) -> Dict[str, Any]:
    """Pull a ticker's recent filings into the cache."""
    cl = client or EdgarClient()
    if not cl.available:
        return {"ticker": ticker.upper(), "available": False,
                  "reason": "no TB_SEC_USER_AGENT configured"}
    cik = cl.ticker_to_cik(ticker)
    if cik is None:
        return {"ticker": ticker.upper(), "available": True,
                  "inserted": 0, "reason": "ticker not in SEC map"}
    filings = cl.recent_filings(ticker, forms=forms, limit=limit)
    inserted = _upsert_filings(ticker, cik, filings)
    # MITS Phase 8.2 — capture EDGAR filings to bronze.
    try:
        from backend.bot.data import lake as _lake
        _lake.write_bronze(
            "edgar", "filings",
            [{
                "ticker": ticker.upper(),
                "cik": cik,
                "accession_number": f.accession_number,
                "form": f.form,
                "filed_at": (f.filed_at.isoformat() if f.filed_at else ""),
                "primary_document": f.primary_document or "",
                "items": f.items or "",
            } for f in filings],
            ticker=ticker,
            request_url=f"sec://edgar/{cik}",
            source_version=__name__,
        )
    except Exception:
        pass
    return {"ticker": ticker.upper(), "cik": cik,
              "fetched": len(filings), "inserted": inserted}


def refresh_universe(tickers: List[str], *,
                        client: Optional[EdgarClient] = None,
                        forms: Tuple[str, ...] = DEFAULT_FORMS,
                        limit_per_ticker: int = 20,
                        delay_seconds: float = 0.12,
                        ) -> Dict[str, Any]:
    """Refresh every ticker, respecting the SEC's 10 req/sec rate limit
    by sleeping between calls (default 120ms ≈ 8 req/sec)."""
    cl = client or EdgarClient()
    if not cl.available:
        return {"available": False, "reason": "no TB_SEC_USER_AGENT configured"}
    total_inserted = 0
    results: Dict[str, Any] = {}
    for t in tickers:
        r = refresh_ticker(t, client=cl, forms=forms, limit=limit_per_ticker)
        results[t.upper()] = r
        total_inserted += int(r.get("inserted") or 0)
        if delay_seconds:
            time.sleep(delay_seconds)
    return {"available": True, "total_inserted": total_inserted,
              "results": results}


# ── helpers (consumed by agents / event_risk / narrative / endpoints) ─


def recent_filings_cached(ticker: str, *, limit: int = 20,
                              forms: Optional[Tuple[str, ...]] = None
                              ) -> List[Dict[str, Any]]:
    """Read the cache only — no network call."""
    try:
        with session_scope() as session:
            q = (select(EdgarFiling)
                  .where(EdgarFiling.ticker == ticker.upper())
                  .order_by(desc(EdgarFiling.filed_at))
                  .limit(limit))
            if forms:
                q = q.where(EdgarFiling.form.in_(list(forms)))
            rows = list(session.execute(q).scalars().all())
            return [r.to_dict() for r in rows]
    except Exception:
        return []


def has_material_event(ticker: str, *, within_hours: int = 48) -> bool:
    """True if the ticker has a recent 8-K with a material item code, or
    a 10-Q/10-K/4 in the window. Used by event_risk to widen its
    pre/post-event no-go window beyond just earnings dates."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=within_hours)
    try:
        with session_scope() as session:
            rows = list(session.execute(
                select(EdgarFiling)
                .where(EdgarFiling.ticker == ticker.upper())
                .where(EdgarFiling.filed_at >= cutoff)
                .order_by(desc(EdgarFiling.filed_at))
                .limit(20)
            ).scalars().all())
            for r in rows:
                if r.form in ("10-Q", "10-K", "S-3", "S-1"):
                    return True
                if r.form == "8-K":
                    items = r.items or ""
                    if any(it in items for it in MATERIAL_8K_ITEMS):
                        return True
            return False
    except Exception:
        return False


def insider_activity_summary(ticker: str, *, days: int = 30) -> Dict[str, Any]:
    """Lightweight Form-4 activity rollup over a window. Returns counts
    only — buy/sell classification requires XML parsing which is left
    as a follow-up."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    try:
        with session_scope() as session:
            rows = list(session.execute(
                select(EdgarFiling)
                .where(EdgarFiling.ticker == ticker.upper())
                .where(EdgarFiling.form == "4")
                .where(EdgarFiling.filed_at >= cutoff)
            ).scalars().all())
            return {
                "ticker": ticker.upper(),
                "form4_count": len(rows),
                "buys": sum(1 for r in rows if r.is_insider_buy),
                "sells": sum(1 for r in rows if r.is_insider_sell),
                "window_days": days,
            }
    except Exception:
        return {"ticker": ticker.upper(), "form4_count": 0,
                "buys": 0, "sells": 0, "window_days": days}
