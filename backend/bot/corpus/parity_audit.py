"""MITS Phase 11.J — cross-vendor parity audit.

For every (ticker, date) pair where we have BOTH a ThetaData EOD bar
(silver-layer ``stock_bars`` interval=1d) AND a legacy yfinance close
(pulled fresh from yfinance with a 5y window), compute

    divergence_pct = |close_yf - close_theta| / close_theta

and persist into ``parity_audit_history``. Rows whose divergence
exceeds ``parity_suspect_pct`` (default 0.5%) get severity="suspect"
and we flag every MarketObservation whose timestamp falls on that
calendar day with ``parity_warn=True`` so downstream consumers can
demote them.

Operator framing (per the data-blame principle): vendors must be
clean enough that losses are attributable to agent logic, not "the
feed was bad". This audit is the receipts we keep on file.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, func, select, update

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.market_observation import MarketObservation
from backend.models.parity_audit_history import ParityAuditHistory
from backend.models.stock_bar import StockBar

logger = logging.getLogger(__name__)


# Severity thresholds (config-driven where possible — operator can tune
# `parity_*_pct` via TUNABLES without code changes).
def _warn_pct() -> float:
    return float(getattr(TUNABLES, "parity_warn_pct", 0.005))


def _suspect_pct() -> float:
    return float(getattr(TUNABLES, "parity_suspect_pct", 0.02))


# ── yfinance fetch ────────────────────────────────────────────────────


def _fetch_yfinance_closes(ticker: str, start: date, end: date
                                  ) -> Dict[date, float]:
    """Pull yfinance daily closes for the window. Returns
    ``{date: close}``. Empty dict on any failure — the parity audit
    silently no-ops for tickers yfinance can't serve."""
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        logger.debug("yfinance unavailable; parity audit returns empty")
        return {}
    # yfinance accepts string YYYY-MM-DD. The end date is exclusive in
    # yfinance, so add one day.
    try:
        df = yf.download(
            ticker, start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            interval="1d", progress=False, auto_adjust=False,
            threads=False,
        )
    except Exception:
        logger.debug("yfinance pull failed for %s [%s, %s]",
                            ticker, start, end, exc_info=True)
        return {}
    if df is None or df.empty:
        return {}
    try:
        if hasattr(df.columns, "get_level_values"):
            df.columns = [c[0] if isinstance(c, tuple) else c
                              for c in df.columns]
        df.columns = [str(c).strip().lower() for c in df.columns]
        out: Dict[date, float] = {}
        for ts, row in df.iterrows():
            try:
                d = ts.date() if hasattr(ts, "date") else ts
            except Exception:
                continue
            try:
                close = float(row["close"])
            except Exception:
                continue
            if close > 0:
                out[d] = close
        return out
    except Exception:
        logger.debug("yfinance parse failed for %s", ticker, exc_info=True)
        return {}


def _fetch_silver_closes(ticker: str, start: date, end: date
                                ) -> Dict[date, float]:
    """Pull ThetaData closes from stock_bars (interval=1d) for the
    window. Returns ``{date: close}``."""
    out: Dict[date, float] = {}
    try:
        with session_scope() as s:
            rows = s.execute(
                select(StockBar.bar_ts, StockBar.close)
                .where(StockBar.ticker == ticker)
                .where(StockBar.interval == "1d")
                .where(StockBar.bar_ts >= datetime.combine(start, datetime.min.time()))
                .where(StockBar.bar_ts <= datetime.combine(end, datetime.max.time()))
            ).all()
            for ts, close in rows:
                if close is None or close <= 0:
                    continue
                try:
                    d = ts.date() if hasattr(ts, "date") else ts
                except Exception:
                    continue
                try:
                    out[d] = float(close)
                except Exception:
                    continue
    except Exception:
        logger.debug("stock_bars closes load failed for %s",
                            ticker, exc_info=True)
    return out


# ── core audit ────────────────────────────────────────────────────────


def _classify_severity(divergence: Optional[float]) -> str:
    if divergence is None:
        return "missing"
    if divergence >= _suspect_pct():
        return "suspect"
    if divergence >= _warn_pct():
        return "warn"
    return "ok"


def _persist_row(session, *, ticker: str, audit_date: date,
                     source_a: str, source_b: str,
                     close_a: Optional[float], close_b: Optional[float],
                     divergence: Optional[float],
                     severity: str) -> bool:
    """Idempotent UPSERT on the (ticker, date, source_a, source_b) key.

    Returns True when a new row landed, False when an existing row was
    updated.
    """
    existing = session.execute(
        select(ParityAuditHistory)
        .where(ParityAuditHistory.ticker == ticker)
        .where(ParityAuditHistory.audit_date == audit_date)
        .where(ParityAuditHistory.source_a == source_a)
        .where(ParityAuditHistory.source_b == source_b)
    ).scalar_one_or_none()
    if existing is None:
        existing = ParityAuditHistory(
            ticker=ticker, audit_date=audit_date,
            source_a=source_a, source_b=source_b,
        )
        session.add(existing)
        inserted = True
    else:
        inserted = False
    existing.close_a = close_a
    existing.close_b = close_b
    existing.divergence_pct = divergence
    existing.severity = severity
    existing.audited_at = datetime.utcnow()
    return inserted


def _flag_parity_warn(ticker: str, suspect_dates: Iterable[date]
                            ) -> int:
    """Set parity_warn=True on every MarketObservation whose
    timestamp.date() is in ``suspect_dates``. Returns rows updated.

    SQLite UPDATE with date() function is awkward — we walk the
    suspect dates explicitly and bound each UPDATE to a single
    calendar day window so the SQL stays trivial.
    """
    rows_updated = 0
    try:
        with session_scope() as s:
            for d in suspect_dates:
                start_dt = datetime.combine(d, datetime.min.time())
                end_dt = datetime.combine(d, datetime.max.time())
                result = s.execute(
                    update(MarketObservation)
                    .where(MarketObservation.ticker == ticker)
                    .where(MarketObservation.timestamp >= start_dt)
                    .where(MarketObservation.timestamp <= end_dt)
                    .where(MarketObservation.parity_warn.is_(False))
                    .values(parity_warn=True)
                )
                rows_updated += result.rowcount or 0
    except Exception:
        logger.debug("parity_warn flag write failed for %s",
                            ticker, exc_info=True)
    return rows_updated


def audit_ticker(ticker: str, *,
                     start_date: date, end_date: date,
                     source_a: str = "yfinance",
                     source_b: str = "thetadata") -> Dict[str, Any]:
    """Run the full parity audit for one ticker. Returns a stats dict.

    The audit is asymmetric in name (yfinance is source_a, ThetaData
    is source_b) but symmetric in value — divergence is always
    relative to source_b (the new canonical pull).
    """
    ticker = (ticker or "").upper().strip()
    stats: Dict[str, Any] = {
        "ticker": ticker, "rows_audited": 0, "rows_inserted": 0,
        "suspect_dates": 0, "warn_dates": 0, "ok_dates": 0,
        "missing_dates": 0, "obs_flagged": 0,
        "yf_dates": 0, "theta_dates": 0,
    }
    if not ticker:
        return stats
    silver = _fetch_silver_closes(ticker, start_date, end_date)
    if not silver:
        # Nothing to audit against — skip cleanly.
        stats["theta_dates"] = 0
        return stats
    yf_closes = _fetch_yfinance_closes(ticker, start_date, end_date)
    stats["yf_dates"] = len(yf_closes)
    stats["theta_dates"] = len(silver)

    overlap_dates = sorted(set(yf_closes.keys()) & set(silver.keys()))
    if not overlap_dates:
        # No overlap means the audit can't compare anything for this
        # ticker (yfinance returned nothing or ThetaData covers a
        # different window). Stamp one "missing" row per day in the
        # silver window so the parity_audit_history row count tells
        # the audit ran cleanly. Cap at 50 rows so we don't explode
        # the table when yfinance is rate-limited for a long window.
        with session_scope() as s:
            for d in sorted(silver.keys())[:50]:
                inserted = _persist_row(
                    s, ticker=ticker, audit_date=d,
                    source_a=source_a, source_b=source_b,
                    close_a=None, close_b=silver[d],
                    divergence=None, severity="missing",
                )
                stats["rows_audited"] += 1
                if inserted:
                    stats["rows_inserted"] += 1
                stats["missing_dates"] += 1
        return stats

    suspect_dates: List[date] = []
    with session_scope() as s:
        for d in overlap_dates:
            close_yf = yf_closes[d]
            close_th = silver[d]
            try:
                divergence = abs(close_yf - close_th) / close_th
            except Exception:
                divergence = None
            divergence = (min(divergence, 1.0)
                            if divergence is not None else None)
            severity = _classify_severity(divergence)
            inserted = _persist_row(
                s, ticker=ticker, audit_date=d,
                source_a=source_a, source_b=source_b,
                close_a=close_yf, close_b=close_th,
                divergence=divergence, severity=severity,
            )
            stats["rows_audited"] += 1
            if inserted:
                stats["rows_inserted"] += 1
            if severity == "suspect":
                stats["suspect_dates"] += 1
                suspect_dates.append(d)
            elif severity == "warn":
                stats["warn_dates"] += 1
            else:
                stats["ok_dates"] += 1

    if suspect_dates:
        stats["obs_flagged"] = _flag_parity_warn(ticker, suspect_dates)
    return stats


def audit_universe(tickers: Iterable[str], *,
                       start_date: date, end_date: date,
                       progress_cb=None) -> Dict[str, Any]:
    """Audit every ticker; return aggregated stats."""
    grand = {
        "tickers": 0, "rows_audited": 0, "rows_inserted": 0,
        "suspect_dates": 0, "warn_dates": 0, "ok_dates": 0,
        "missing_dates": 0, "obs_flagged": 0,
    }
    for idx, ticker in enumerate(list(tickers), start=1):
        stats = audit_ticker(ticker, start_date=start_date,
                                  end_date=end_date)
        grand["tickers"] += 1
        for k in ("rows_audited", "rows_inserted", "suspect_dates",
                       "warn_dates", "ok_dates", "missing_dates",
                       "obs_flagged"):
            grand[k] += int(stats.get(k, 0) or 0)
        if progress_cb is not None:
            try:
                progress_cb(idx, ticker, stats)
            except Exception:
                logger.debug("parity progress_cb failed", exc_info=True)
    return grand


__all__ = [
    "audit_ticker",
    "audit_universe",
]
