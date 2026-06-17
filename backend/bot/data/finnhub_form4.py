"""MITS Phase 12.3 — Finnhub-backed Form 4 insider transaction ingest.

Drop-in alternate to :mod:`backend.bot.data.edgar_form4` for when SEC
EDGAR is IP-blocking us (status 403 on
``data.sec.gov/submissions/CIK*.json``). Finnhub's
``/stock/insider-transactions`` endpoint parses every Form 4 filing on
the server side and returns the transaction rows directly, so we get
identical fields (insider name, transaction code, shares, price, filing
date, transaction date) without ever hitting an SEC IP.

Activated when ``TB_USE_FINNHUB_FORM4=true`` is set. The orchestrator
registers this callback under the SAME source key ``edgar_form4`` so
all downstream consumers (delta_sync, scorecards, knowledge_graph) keep
working with no schema changes.

Rate limit: Finnhub Free is 60 req/min. We respect that with a small
in-process bucket. Universe sweep at 40 tickers fits comfortably.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.bot.data.sync_orchestrator import CallbackResult
from backend.db import session_scope
from backend.models.insider_trade import InsiderTrade

logger = logging.getLogger(__name__)


FINNHUB_BASE = "https://finnhub.io/api/v1"


# ── shared per-process token bucket ───────────────────────────────────
class _Bucket:
    def __init__(self, per_minute: int = 55) -> None:
        # 55/min keeps us under Finnhub Free 60/min ceiling.
        self.rate_per_sec = max(0.1, per_minute / 60.0)
        self.capacity = float(per_minute)
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


_BUCKET = _Bucket(per_minute=55)


def _api_key() -> str:
    """Finnhub key from env. Empty → caller skips fetch."""
    return os.environ.get("FINNHUB_API_KEY", "").strip()


def _http_get_json(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    _BUCKET.acquire()
    headers = {"User-Agent": "trading-bot/1.0 (mits-p12.3)"}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    if resp.status_code == 429:
        # Defensive — back off and retry once.
        time.sleep(2.0)
        _BUCKET.acquire()
        resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── Form 4 transaction shape — matches edgar_form4.Form4Transaction ──
@dataclass
class Form4Transaction:
    ticker: str
    cik: str
    accession_number: str
    filing_date: date
    transaction_date: date
    insider_name: str
    insider_role: Optional[str]
    transaction_code: str
    shares: Optional[float]
    price: Optional[float]
    total_value: Optional[float]
    is_director: bool
    is_officer: bool
    is_10pct_owner: bool
    source_url: Optional[str]


def _parse_date(s: Any) -> Optional[date]:
    if not s:
        return None
    try:
        s = str(s).strip()
        # Finnhub returns "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS".
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# Finnhub uses lower-case CIK-less ids in the `id` field. We synthesise
# a deterministic ``accession_number`` from the Finnhub id when present
# so the (cik, accession_number, ...) unique key in InsiderTrade still
# de-dups across reruns.
def _accession_for(finnhub_id: Any, ticker: str, filing_dt: date,
                              insider: str) -> str:
    if finnhub_id:
        return str(finnhub_id)
    # Fallback: synthesise from (ticker, date, insider) so the unique
    # constraint still holds.
    safe_ins = "".join(c for c in insider.upper() if c.isalnum())[:20]
    return f"FINNHUB-{ticker}-{filing_dt.isoformat()}-{safe_ins}"


def fetch_finnhub_insider_transactions(
    ticker: str, since_date: date, until_date: date,
) -> List[Form4Transaction]:
    """Pull insider transactions for ``ticker`` in
    ``[since_date, until_date]``. Returns parsed Form4Transaction rows.
    """
    key = _api_key()
    if not key:
        raise RuntimeError(
            "finnhub_form4: FINNHUB_API_KEY missing; cannot ingest")
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return []
    params = {
        "symbol": ticker,
        "from": since_date.isoformat(),
        "to": until_date.isoformat(),
        "token": key,
    }
    payload = _http_get_json(
        f"{FINNHUB_BASE}/stock/insider-transactions", params)
    raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    out: List[Form4Transaction] = []
    for item in raw:
        try:
            filing_dt = _parse_date(item.get("filingDate"))
            txn_dt = _parse_date(item.get("transactionDate"))
            if not filing_dt or not txn_dt:
                continue
            if filing_dt < since_date or filing_dt > until_date:
                continue
            insider = str(item.get("name") or "").strip()
            if not insider:
                continue
            # Finnhub `share` field is the post-transaction holding;
            # `change` is the signed share delta. We use abs(change) as
            # the per-line ``shares`` so the downstream "is this a buy
            # or sell" logic still works off transaction_code.
            change = item.get("change")
            shares = float(abs(change)) if change is not None else None
            price = item.get("transactionPrice")
            try:
                price = float(price) if price not in (None, "") else None
            except Exception:
                price = None
            code = str(item.get("transactionCode") or "").strip().upper()[:4]
            if not code:
                code = "?"
            total_value = (
                (shares or 0.0) * (price or 0.0) if shares and price else None
            )
            # Finnhub does not return CIK directly; we leave as the
            # numeric `cik` field if present, else fall back to ticker.
            cik = str(item.get("cik") or "").strip() or ticker
            acc = _accession_for(item.get("id"), ticker, filing_dt, insider)
            out.append(Form4Transaction(
                ticker=ticker,
                cik=cik,
                accession_number=acc,
                filing_date=filing_dt,
                transaction_date=txn_dt,
                insider_name=insider,
                insider_role=None,  # Finnhub doesn't expose role on free tier
                transaction_code=code,
                shares=shares,
                price=price,
                total_value=total_value,
                is_director=False,
                is_officer=False,
                is_10pct_owner=False,
                source_url=(
                    f"{FINNHUB_BASE}/stock/insider-transactions"
                    f"?symbol={ticker}&id={item.get('id', '')}"
                ),
            ))
        except Exception:
            logger.debug(
                "finnhub_form4: parse failed for %s row", ticker,
                exc_info=True,
            )
    return out


def write_insider_trades(rows: List[Form4Transaction]) -> int:
    """Persist Form 4 transaction rows. INSERT OR IGNORE semantics via
    the unique constraint on the table. Returns inserted count."""
    if not rows:
        return 0
    inserted = 0
    with session_scope() as s:
        for r in rows:
            try:
                row = InsiderTrade(
                    ticker=r.ticker,
                    cik=r.cik,
                    accession_number=r.accession_number,
                    filing_date=r.filing_date,
                    transaction_date=r.transaction_date,
                    insider_name=r.insider_name,
                    insider_role=r.insider_role,
                    transaction_code=r.transaction_code,
                    shares=r.shares,
                    price=r.price,
                    total_value=r.total_value,
                    is_director=r.is_director,
                    is_officer=r.is_officer,
                    is_10pct_owner=r.is_10pct_owner,
                    source_url=r.source_url,
                )
                s.add(row)
                s.flush()
                inserted += 1
            except IntegrityError:
                s.rollback()
                continue
            except Exception:
                s.rollback()
                logger.debug(
                    "finnhub_form4: insert failed for %s %s",
                    r.ticker, r.accession_number, exc_info=True,
                )
    return inserted


def finnhub_form4_backfill_callback(
    ticker: str, chunk_start: date, chunk_end: date,
) -> CallbackResult:
    """Orchestrator callback signature matching edgar_form4."""
    if not _api_key():
        raise RuntimeError(
            "finnhub_form4: FINNHUB_API_KEY missing")
    rows = fetch_finnhub_insider_transactions(
        ticker, chunk_start, chunk_end)
    if not rows:
        return CallbackResult(
            last_completed_date=chunk_end, rows_written=0,
            metadata={"reason": "no_filings_in_window",
                          "source": "finnhub_insider_transactions"},
        )
    inserted = write_insider_trades(rows)
    last_dt = max(r.filing_date for r in rows)
    return CallbackResult(
        last_completed_date=min(chunk_end, last_dt),
        rows_written=inserted,
        metadata={"transactions_parsed": len(rows),
                      "source": "finnhub_insider_transactions"},
    )


__all__ = [
    "Form4Transaction",
    "fetch_finnhub_insider_transactions",
    "write_insider_trades",
    "finnhub_form4_backfill_callback",
]
