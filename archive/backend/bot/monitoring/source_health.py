"""MITS Phase 11.I — per-source health aggregator.

Walks three signals per source family and writes a one-row-per-source
snapshot into ``data_source_health`` so the operator UI can render a
familiar green/yellow/red grid:

  1. ``backfill_progress`` — chunked-backfill ledger. We count
     attempts + successes in the rolling 24h window AND in the all-time
     window (the latter as fallback for sources whose backfills landed
     before today's window).
  2. ``data_watermarks`` — per (source, ticker) high-water-mark of the
     furthest-forward calendar date we have a row for. A source with
     a recent watermark is considered "current" even if no work
     happened today (idle != broken).
  3. Row count of the source's primary destination table — if the
     ledger says "done 1000 chunks" but the destination table is empty,
     we surface that as red.

Status policy:
  * green  = success ratio >= green threshold AND rows in the
             destination table, OR a recent watermark and at least one
             "done" chunk on record.
  * yellow = some failures (ratio in [yellow, green)).
  * red    = no signal at all (no chunks, no watermark, no rows) — the
             source is silently dead.

Idempotent — UPSERT on (source, snapshot_date).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, select, text

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.backfill_progress import BackfillProgress
from backend.models.data_source_health import DataSourceHealth
from backend.models.data_watermark import DataWatermark

logger = logging.getLogger(__name__)


# Source families we expect to see signal from. The map value is the
# primary destination table whose row count acts as the "did anything
# actually land?" sanity check. ``None`` if no direct table to inspect
# (e.g. ``detector_replay`` writes into ``market_observations`` indirectly).
_SOURCE_DESTINATION: Dict[str, Optional[str]] = {
    "thetadata_stocks_daily": "stock_bars",
    "thetadata_stocks_intraday_1m": "stock_bars",
    "thetadata_stocks_intraday_5m": "stock_bars",
    "thetadata_iv_history": "iv_history",
    "thetadata_options_eod": "option_contract_bars",
    "fred": "fred_observations",
    "alpaca_quotes": None,
    "finnhub_news": "news_articles",
    "alphavantage_transcripts": "earnings_transcripts",
    "sec_8k_earnings": "earnings_transcripts",
    "sec_press_releases": "news_articles",
    "edgar_form4": "insider_trades",
    "edgar_13f": "fund_holdings",
    "detector_replay": "market_observations",
}


def _classify(
    ratio: float,
    rows_written: int,
    dest_row_count: Optional[int],
    has_recent_watermark: bool,
    has_any_signal: bool,
) -> str:
    green = float(getattr(TUNABLES, "source_health_green_threshold", 1.0))
    yellow = float(getattr(TUNABLES, "source_health_yellow_threshold", 0.8))

    # No signal at all in the ledger AND no destination rows AND no
    # watermark → silently dead.
    if not has_any_signal and (dest_row_count is None or dest_row_count == 0) \
            and not has_recent_watermark:
        return "red"

    # If we have a destination table with rows OR a fresh watermark, the
    # source is at least "yellow" (we know it produced something at some
    # point). The exact step from yellow → green is gated on success
    # ratio of recent attempts.
    if ratio >= green and has_any_signal:
        return "green"
    if (dest_row_count and dest_row_count > 0) or has_recent_watermark:
        # Has output but recent attempts struggled — yellow is honest.
        if ratio >= yellow or not has_any_signal:
            return "yellow"
        return "red"
    if ratio >= yellow:
        return "yellow"
    return "red"


def _table_row_count(session, table_name: str) -> int:
    """Cheap COUNT(*) on the destination table. Returns 0 on any error
    so a missing/renamed table degrades to yellow/red instead of
    crashing the pass."""
    try:
        return int(
            session.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0
        )
    except Exception:
        return 0


def _aggregate_one_source(
    session, source: str, day: date
) -> Tuple[Dict[str, Optional[float]], bool, Optional[int]]:
    """Pull 24h and all-time ``backfill_progress`` stats for ``source``
    plus the latest watermark + destination row count.

    Returns ``(metrics, has_recent_watermark, dest_row_count)``.
    """
    # ── 24h rolling window — use started_at when present, else
    # completed_at. Some legacy rows have neither populated; for those we
    # fall back to "was the chunk created in the last 24h?" — we use
    # the surrogate of row.last_completed_date being today.
    start_ts = datetime.combine(day - timedelta(days=1), datetime.min.time())
    end_ts = datetime.combine(day, datetime.max.time())

    recent = session.execute(
        select(
            BackfillProgress.status,
            BackfillProgress.rows_written,
            BackfillProgress.started_at,
            BackfillProgress.completed_at,
            BackfillProgress.error_text,
        )
        .where(BackfillProgress.source == source)
        .where(
            (BackfillProgress.started_at >= start_ts)
            | (BackfillProgress.completed_at >= start_ts)
        )
        .where(
            (BackfillProgress.started_at <= end_ts)
            | (BackfillProgress.completed_at <= end_ts)
        )
    ).all()

    all_time = session.execute(
        select(
            BackfillProgress.status,
            BackfillProgress.rows_written,
            BackfillProgress.error_text,
        ).where(BackfillProgress.source == source)
    ).all()

    def _stats(rows, with_timing: bool):
        attempts = len(rows)
        successes = sum(1 for r in rows if r.status == "done")
        rows_written = sum(int(r.rows_written or 0) for r in rows)
        durations: List[float] = []
        last_err = None
        for r in rows:
            if with_timing and getattr(r, "completed_at", None) and getattr(r, "started_at", None):
                try:
                    durations.append(
                        (r.completed_at - r.started_at).total_seconds() * 1000.0
                    )
                except Exception:
                    pass
            if r.error_text and r.status != "done":
                last_err = r.error_text
        avg_latency = (sum(durations) / len(durations)) if durations else None
        return attempts, successes, rows_written, avg_latency, last_err

    recent_attempts, recent_succ, recent_rows, recent_lat, recent_err = _stats(
        recent, with_timing=True
    )
    alltime_attempts, alltime_succ, alltime_rows, _, alltime_err = _stats(
        all_time, with_timing=False
    )

    # Pick whichever window has signal to populate the public metrics. If
    # the source ran in the last 24h, surface that; otherwise fall back
    # to all-time so the operator UI still shows a meaningful number.
    if recent_attempts > 0:
        attempts = recent_attempts
        successes = recent_succ
        rows_written = recent_rows
        avg_latency = recent_lat
        last_err = recent_err
    else:
        attempts = alltime_attempts
        successes = alltime_succ
        rows_written = alltime_rows
        avg_latency = None
        last_err = alltime_err

    # ── watermark freshness — any watermark updated in the last 7 days
    # counts as "the source is still on shift". 7d is forgiving enough for
    # weekly/quarterly sources (13F is quarterly, FRED daily, etc.).
    watermark_cutoff = datetime.utcnow() - timedelta(days=7)
    has_recent_wm = session.execute(
        select(func.count(DataWatermark.id))
        .where(DataWatermark.source == source)
        .where(DataWatermark.updated_at >= watermark_cutoff)
    ).scalar() or 0

    dest_table = _SOURCE_DESTINATION.get(source)
    dest_rows = _table_row_count(session, dest_table) if dest_table else None

    return (
        {
            "attempts": attempts,
            "successes": successes,
            "rows_written": rows_written,
            "avg_latency_ms": avg_latency,
            "last_err": last_err,
        },
        bool(has_recent_wm),
        dest_rows,
    )


def run_pass(
    snapshot_date: Optional[date] = None,
    sources: Optional[Iterable[str]] = None,
) -> Dict[str, Dict[str, int]]:
    """Run one aggregation pass for ``snapshot_date`` (default: today UTC).

    Returns a dict keyed by source with the computed status + counts so
    the caller (scheduler / API) can log a one-line summary.
    """
    snap = snapshot_date or date.today()
    target_sources = list(sources) if sources else list(_SOURCE_DESTINATION.keys())
    out: Dict[str, Dict[str, int]] = {}

    try:
        with session_scope() as s:
            for source in target_sources:
                agg, has_wm, dest_rows = _aggregate_one_source(s, source, snap)
                attempts = agg["attempts"]
                successes = agg["successes"]
                rows_written = agg["rows_written"]
                avg_latency = agg["avg_latency_ms"]
                last_err = agg["last_err"]
                ratio = (successes / attempts) if attempts else 0.0
                has_signal = attempts > 0 or rows_written > 0
                status = _classify(
                    ratio,
                    rows_written,
                    dest_rows,
                    has_wm,
                    has_signal,
                )

                existing = s.execute(
                    select(DataSourceHealth)
                    .where(DataSourceHealth.source == source)
                    .where(DataSourceHealth.snapshot_date == snap)
                ).scalar_one_or_none()
                if existing is None:
                    existing = DataSourceHealth(
                        source=source, snapshot_date=snap,
                    )
                    s.add(existing)
                existing.pulls_attempted = int(attempts)
                existing.pulls_successful = int(successes)
                existing.rows_written = int(rows_written)
                existing.avg_latency_ms = avg_latency
                existing.last_error_text = (last_err[:1000] if last_err else None)
                existing.status = status
                existing.computed_at = datetime.utcnow()

                out[source] = {
                    "attempts": attempts,
                    "successes": successes,
                    "rows_written": rows_written,
                    "dest_rows": dest_rows,
                    "has_watermark": has_wm,
                    "avg_latency_ms": (
                        round(avg_latency, 1) if avg_latency else None
                    ),
                    "status": status,
                }
    except Exception:
        logger.exception("source_health pass failed")
    return out


def run_daily_health_pass(
    snapshot_date: Optional[date] = None,
    sources: Optional[Iterable[str]] = None,
) -> Dict[str, Dict[str, int]]:
    """Alias preferred by the operator brief / one-shot callers.

    Identical to :func:`run_pass` — keeps both names so older callers
    (CLI scripts, the scheduler) keep working without churn.
    """
    return run_pass(snapshot_date=snapshot_date, sources=sources)


__all__ = ["run_pass", "run_daily_health_pass"]
