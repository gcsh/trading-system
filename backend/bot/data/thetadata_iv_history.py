"""MITS Phase 11.B.3 — ThetaData IV history backfill (5y per ticker).

Thin orchestrator-shaped wrapper around the existing
:func:`backend.bot.data.iv_history.backfill` routine. That function
already knows how to:

  - pick the nearest-30DTE expiration available on each historical date,
  - pull historical EOD straddle (call + put closes),
  - invert via Brenner-Subrahmanyam to compute ATM IV,
  - upsert into ``iv_history`` SQLite + bronze parquet.

We wrap it in a :class:`CallbackResult`-shaped function so the
:class:`SyncOrchestrator` can drive multi-year backfills with progress
ledger, crash-resume, and rate limiting.

The IV-history backfill is naturally chunked (one date per outbound
straddle pull), so a chunk of 180 days = ~180 pulls = ~22s at 8 rps.
The orchestrator's rate-limit and retry envelope cover the rest.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select

from backend.bot.data.sync_orchestrator import CallbackResult
from backend.db import session_scope

logger = logging.getLogger(__name__)


def _max_existing_date(ticker: str, chunk_start: date,
                              chunk_end: date) -> Optional[date]:
    """Latest iv_history date in [chunk_start, chunk_end] — used to set
    the orchestrator's last_completed_date when the chunk had no new
    rows but did have data we kept from a previous attempt."""
    try:
        from backend.models.iv_history import IVHistory
        with session_scope() as s:
            row = s.execute(
                select(IVHistory.date)
                .where(IVHistory.ticker == ticker.upper())
                .where(IVHistory.date >= datetime.combine(chunk_start,
                                                              datetime.min.time()))
                .where(IVHistory.date <= datetime.combine(chunk_end,
                                                              datetime.min.time()))
                .order_by(IVHistory.date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return row.date() if hasattr(row, "date") else row
    except Exception:
        logger.debug("_max_existing_date failed for %s",
                          ticker, exc_info=True)
        return None


def fetch_iv_history(ticker: str, start: date, end: date) -> dict:
    """Pulls IV-rank-grade ATM IV for every trading day in ``[start, end]``.
    Returns the underlying backfill stats dict."""
    from backend.bot.data.iv_history import backfill as _backfill
    # iv_history.backfill is parameterised by lookback_days. Translate
    # our [start, end] into the equivalent lookback window.
    today = date.today()
    lookback_days = max(1, (today - start).days)
    if end < today:
        # The lookback_days arg of iv_history.backfill walks back from
        # today; an upper-bound (`end`) can't be expressed in its API.
        # The function will pull every (ticker,date) ≥ (today-lookback)
        # that isn't already in iv_history, including dates between
        # `end` and today. That's a feature: we get free coverage for
        # the (end, today] tail at no extra orchestrator cost.
        pass
    return _backfill(ticker, lookback_days=lookback_days, target_dte=30,
                          pace_seconds=0.02)


def iv_history_backfill_callback(ticker: str, chunk_start: date,
                                          chunk_end: date) -> CallbackResult:
    """Orchestrator-shaped callback. Pulls the chunk, returns counts.

    Note: the underlying backfill walks ALL dates between today and
    today-lookback, not just the chunk. The orchestrator's chunk
    boundaries are still meaningful — they drive the retry envelope
    and the progress ledger — but the rows written may exceed the
    chunk window.
    """
    today = date.today()
    if chunk_start > today:
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=0,
            metadata={"reason": "future_chunk"},
        )
    # Use an effective start = max(chunk_start, today - 5y) so we never
    # ask iv_history to walk further back than ThetaData Standard
    # actually has data for.
    five_years_ago = today - timedelta(days=5 * 365)
    eff_start = max(chunk_start, five_years_ago)
    lookback_days = max(1, (today - eff_start).days)
    from backend.bot.data.iv_history import backfill as _backfill
    stats = _backfill(ticker, lookback_days=lookback_days, target_dte=30,
                          pace_seconds=0.02)
    inserted = int(stats.get("inserted") or 0)
    last_seen = _max_existing_date(ticker, eff_start, min(chunk_end, today))
    last_complete = last_seen or chunk_end
    return CallbackResult(
        last_completed_date=last_complete,
        rows_written=inserted,
        metadata={
            "backfill_stats": stats,
            "lookback_days": lookback_days,
        },
    )


__all__ = [
    "fetch_iv_history",
    "iv_history_backfill_callback",
]
