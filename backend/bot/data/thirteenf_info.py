"""MITS Phase 15.followup.1 — 13f.info-backed Form 13F-HR ingest.

Drop-in alternate to :mod:`backend.bot.data.edgar_13f` for when SEC
EDGAR is IP-blocking us (status 403 on both ``data.sec.gov`` and
``www.sec.gov`` Archives). 13f.info mirrors the SEC's Form 13F-HR
universe with a stable per-filing JSON endpoint, so we get the same
fields (CUSIP, issuer name, value, shares, pct of portfolio) without
ever touching an SEC IP.

Activated when ``TB_USE_13F_INFO=true`` is set. The orchestrator
registers this callback under the SAME source key ``edgar_13f`` so
all downstream consumers (smart_money feature layer, scorecards,
analysis composer) keep working with no schema changes.

13f.info shape:

    GET https://13f.info/manager/{cik_padded}-{slug}
        Returns HTML index listing every 13F-HR filing for the manager.
        Each row carries the accession number embedded in the
        ``/13f/{accession}-{slug}`` link target.

    GET https://13f.info/data/13f/{accession_unpadded}
        Returns JSON ``{"data": [[sym, name, class, cusip, value_000,
        pct, shares, principal, option_type], ...]}``. Value is in
        thousands of USD (matches the SEC's pre-2023 unit; we scale to
        full dollars here so the table stays normalized).

    GET https://13f.info/13f/{accession}-{slug}
        Returns the filing detail HTML with a ``dl`` block carrying
        ``Holdings as of`` (period of report) and ``Date filed``.

Idempotent + dedup-aware. Re-running the callback for a fund + window
that's already landed is a no-op at the ``write_fund_holdings`` layer
(unique key on (fund_cik, quarter_end_date, cusip)). We reuse that
persistence path so the CUSIP→ticker map + change-from-prior-qtr logic
stays consistent with the SEC route.

Source of truth: 13f.info publishes its data directly from SEC EDGAR
filings — no enrichment, no synthesis. The robots.txt only disallows
``/search``; ``/data`` and ``/manager`` are crawlable. We send a
clearly-identified User-Agent and rate-limit ourselves with a
per-process token bucket so we don't get rate-limited.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from backend.bot.data.edgar_13f import (
    FundHoldingRow,
    write_13f_bronze,
    write_fund_holdings,
)
from backend.bot.data.sync_orchestrator import CallbackResult
from backend.config import TUNABLES

logger = logging.getLogger(__name__)


THIRTEENF_INFO_BASE = "https://13f.info"


# ── shared per-process token bucket ───────────────────────────────────
class _Bucket:
    def __init__(self, per_second: float = 2.0) -> None:
        self.rate_per_sec = max(0.5, per_second)
        self.capacity = max(2.0, per_second * 2.0)
        self.tokens = self.capacity
        self.last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity,
                    self.tokens + (now - self.last) * self.rate_per_sec,
                )
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = max(0.05, (1.0 - self.tokens) / self.rate_per_sec)
            time.sleep(min(wait, 1.0))


_BUCKET = _Bucket(per_second=2.0)


def _user_agent() -> str:
    """Identifying UA. Fall back to the SEC UA tunable when 13f.info-
    specific override isn't set — 13f.info accepts any clearly-named UA
    and we want operator contact details in the header for transparency.
    """
    return (
        os.environ.get("TB_13FINFO_USER_AGENT", "").strip()
        or (getattr(TUNABLES, "sec_user_agent", "") or "").strip()
        or "trading-bot 13f-research srikant.parimi@gmail.com"
    )


def _http_get(url: str, *, accept: str = "text/html") -> Tuple[int, bytes]:
    _BUCKET.acquire()
    timeout = float(getattr(TUNABLES, "edgar_http_timeout_sec", 30.0))
    resp = requests.get(
        url,
        headers={
            "User-Agent": _user_agent(),
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
        },
        timeout=timeout,
    )
    if resp.status_code == 429:
        # Defensive — back off and retry once.
        time.sleep(2.0)
        _BUCKET.acquire()
        resp = requests.get(
            url,
            headers={
                "User-Agent": _user_agent(),
                "Accept": accept,
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=timeout,
        )
    return resp.status_code, resp.content


# ── manager filings index ─────────────────────────────────────────────


@dataclass
class FilingRef:
    """One row in the manager's filings index. ``accession`` is the
    unpadded SEC accession number (digits only — 13f.info embeds it in
    the URL without the dashes). ``slug`` is everything after the
    accession, used only to rebuild the filing detail URL."""
    accession: str
    slug: str
    quarter_end_date: date


# 13f.info renders the manager page as a Rails app — the table rows
# follow a stable shape:
#   <td class="..." data-order="YYYY-MM-DD">
#     <a href="/13f/{accession}-{slug}">Q{n} YYYY</a>
#   </td>
_INDEX_ROW_RE = re.compile(
    r'data-order="(\d{4}-\d{2}-\d{2})">\s*'
    r'<a href="/13f/([0-9]+)-([a-z0-9-]+)">',
    re.IGNORECASE,
)


def _manager_slug(name: str) -> str:
    """13f.info slugifies fund names the same way Rails ``parameterize``
    does — lowercase, alphanumeric + hyphens. We don't need to perfectly
    match the slug because we have a fallback: hit ``/manager/{cik}`` and
    follow the redirect when the slug guess is wrong."""
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _resolve_manager_url(cik: str, name: str) -> Optional[str]:
    """Return the canonical manager URL. We try ``/manager/{cik}-{slug}``
    first because that's what 13f.info uses in its sitemap; if that 404s
    we fall back to ``/manager/{cik}`` which 13f.info redirects to the
    canonical URL when the CIK is known.
    """
    padded = (cik or "").zfill(10)
    slug = _manager_slug(name)
    candidates = []
    if slug:
        candidates.append(f"{THIRTEENF_INFO_BASE}/manager/{padded}-{slug}")
    candidates.append(f"{THIRTEENF_INFO_BASE}/manager/{padded}")
    for url in candidates:
        status, body = _http_get(url, accept="text/html")
        if status == 200 and body and b"<title>" in body[:2000]:
            return url
        if status in (301, 302):
            # ``requests`` follows redirects by default so a 3xx is rare;
            # treat as a try-next-candidate.
            continue
    return None


def list_manager_filings(cik: str, name: str, since: date
                              ) -> List[FilingRef]:
    """Walk the manager's filings index and return every 13F-HR filing
    with quarter_end_date >= ``since``. ``name`` is used to build the
    URL slug — passing the wrong name still works because we fall back
    to the bare CIK URL.
    """
    url = _resolve_manager_url(cik, name)
    if not url:
        return []
    status, body = _http_get(url, accept="text/html")
    if status != 200 or not body:
        return []
    text = body.decode("utf-8", errors="ignore")
    out: List[FilingRef] = []
    for match in _INDEX_ROW_RE.finditer(text):
        try:
            qend = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except Exception:
            continue
        if qend < since:
            continue
        out.append(FilingRef(
            accession=match.group(2),
            slug=match.group(3),
            quarter_end_date=qend,
        ))
    # Dedupe by accession (a manager that filed an amendment can show
    # the same accession twice in the index — keep the first).
    seen: set = set()
    deduped: List[FilingRef] = []
    for f in out:
        if f.accession in seen:
            continue
        seen.add(f.accession)
        deduped.append(f)
    return deduped


# ── filing detail + holdings JSON ─────────────────────────────────────


# Matches `Date filed` value `M/D/YYYY` in the dl block. Used to read
# the SEC filing_date which the manager index doesn't expose directly.
_DATE_FILED_RE = re.compile(
    r"Date filed\s*</dt>\s*<dd[^>]*>\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4})",
    re.IGNORECASE | re.DOTALL,
)


def _parse_us_date(s: str) -> Optional[date]:
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except Exception:
        return None


def fetch_filing_date(accession: str, slug: str) -> Optional[date]:
    """Read ``Date filed`` off the filing detail HTML. Falls back to
    ``None`` when 13f.info doesn't have it (rare — the field is always
    present in canonical filings)."""
    url = f"{THIRTEENF_INFO_BASE}/13f/{accession}-{slug}"
    status, body = _http_get(url, accept="text/html")
    if status != 200 or not body:
        return None
    text = body.decode("utf-8", errors="ignore")
    m = _DATE_FILED_RE.search(text)
    if not m:
        return None
    return _parse_us_date(m.group(1))


def parse_holdings_json(payload: Dict[str, Any], *, fund_cik: str,
                              fund_name: str, accession: str,
                              filing_date: date, quarter_end_date: date,
                              source_url: str) -> List[FundHoldingRow]:
    """Convert 13f.info's compact array-of-arrays payload into
    :class:`FundHoldingRow` instances. Column order (verified live):

        0 ticker      1 issuer_name  2 class        3 cusip
        4 value_000   5 pct          6 shares       7 principal
        8 option_type

    Value is in thousands of USD — we scale to full dollars to match
    the post-2023 SEC reporting unit (and what ``edgar_13f`` writes for
    modern filings).
    """
    rows_raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows_raw, list):
        return []
    rows: List[FundHoldingRow] = []
    # Total portfolio value for pct recomputation when pct is missing —
    # 13f.info usually ships pct directly but we recompute on the way
    # out so the persisted row matches the SEC parser's invariant.
    total_value = 0.0
    pre: List[Tuple[str, str, Optional[float], Optional[float],
                          Optional[float]]] = []
    for row in rows_raw:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        cusip = str(row[3] or "").strip()
        if not cusip:
            continue
        issuer = str(row[1] or "").strip() or None
        try:
            value_thousands = float(row[4]) if row[4] is not None else None
        except (TypeError, ValueError):
            value_thousands = None
        value_usd = (
            value_thousands * 1000.0 if value_thousands is not None else None
        )
        try:
            pct = float(row[5]) if row[5] is not None else None
        except (TypeError, ValueError):
            pct = None
        try:
            shares = float(row[6]) if row[6] is not None else None
        except (TypeError, ValueError):
            shares = None
        pre.append((cusip, issuer or "", shares, value_usd, pct))
        if value_usd is not None:
            total_value += value_usd
    for cusip, issuer, shares, value_usd, pct in pre:
        if pct is None and value_usd is not None and total_value > 0:
            pct = round(100.0 * (value_usd / total_value), 4)
        rows.append(FundHoldingRow(
            fund_cik=fund_cik,
            fund_name=fund_name,
            quarter_end_date=quarter_end_date,
            cusip=cusip,
            issuer_name=issuer or None,
            ticker=None,
            shares=shares,
            value_usd=value_usd,
            pct_of_portfolio=pct,
            filing_date=filing_date,
            accession_number=accession,
            source_url=source_url,
        ))
    return rows


def fetch_holdings(filing: FilingRef, *, fund_cik: str, fund_name: str
                          ) -> List[FundHoldingRow]:
    json_url = f"{THIRTEENF_INFO_BASE}/data/13f/{filing.accession}"
    status, body = _http_get(json_url, accept="application/json")
    if status != 200 or not body:
        return []
    try:
        import json as _json
        payload = _json.loads(body.decode("utf-8", errors="ignore"))
    except Exception:
        logger.debug(
            "thirteenf_info: JSON parse failed for accession=%s",
            filing.accession, exc_info=True,
        )
        return []
    filing_date = fetch_filing_date(filing.accession, filing.slug)
    if filing_date is None:
        # Heuristic: 13F-HR is due 45 days after the quarter end. Use
        # quarter_end + 45 as a stand-in so downstream code that orders
        # by filing_date doesn't see a None.
        from datetime import timedelta as _td
        filing_date = filing.quarter_end_date + _td(days=45)
    return parse_holdings_json(
        payload,
        fund_cik=fund_cik,
        fund_name=fund_name,
        accession=filing.accession,
        filing_date=filing_date,
        quarter_end_date=filing.quarter_end_date,
        source_url=f"{THIRTEENF_INFO_BASE}/13f/{filing.accession}-{filing.slug}",
    )


# ── orchestrator callback ─────────────────────────────────────────────


def thirteenf_info_backfill_callback(fund_cik: str, chunk_start: date,
                                              chunk_end: date) -> CallbackResult:
    """Match :func:`backend.bot.data.edgar_13f.edgar_13f_backfill_callback`
    so the orchestrator can register us under the ``edgar_13f`` source
    key with no other downstream changes."""
    from backend.bot.data.watched_funds import lookup_fund_name
    fund_cik = (fund_cik or "").strip().zfill(10)
    name = lookup_fund_name(fund_cik) or f"CIK {fund_cik}"
    filings = list_manager_filings(fund_cik, name, chunk_start)
    filings = [f for f in filings if f.quarter_end_date <= chunk_end]
    if not filings:
        return CallbackResult(
            last_completed_date=chunk_end, rows_written=0,
            metadata={"reason": "no_13f_filings_in_window",
                          "source": "13f_info"},
        )
    total_inserted = 0
    flat: List[FundHoldingRow] = []
    for filing in filings:
        holdings = fetch_holdings(filing, fund_cik=fund_cik, fund_name=name)
        if not holdings:
            continue
        total_inserted += write_fund_holdings(holdings)
        flat.extend(holdings)
    if flat:
        write_13f_bronze(fund_cik, flat,
                              chunk_start=chunk_start, chunk_end=chunk_end)
    last_dt = max((f.quarter_end_date for f in filings), default=chunk_end)
    return CallbackResult(
        last_completed_date=min(chunk_end, last_dt),
        rows_written=total_inserted,
        metadata={
            "filings": len(filings),
            "holdings": len(flat),
            "source": "13f_info",
        },
    )


__all__ = [
    "FilingRef",
    "list_manager_filings",
    "fetch_filing_date",
    "parse_holdings_json",
    "fetch_holdings",
    "thirteenf_info_backfill_callback",
]
