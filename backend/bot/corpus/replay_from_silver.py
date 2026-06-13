"""MITS Phase 11.H — corpus replay driven by silver-layer ``stock_bars``.

This is the Phase-11 successor to ``historical_replay.bootstrap_ticker``,
which pulled bars from yfinance on every run. Now that Phase 11.B.1 has
landed a typed silver layer (``stock_bars`` table) populated by the
ThetaData backfill, the corpus replay reads from there directly:

  * No vendor round-trip per ticker.
  * Same bar fidelity as the live engine path.
  * Walk-forward by calendar day so detectors only see strictly-prior
    bars (no look-ahead).
  * Persists into the same ``market_observations`` table the live engine
    writes to — outcome_linker + knowledge_aggregator unchanged.

Skips the intraday-aware detectors for tickers whose intraday backfill
is still in progress (so we don't double-fire when intraday completes).
The watermark check uses ``data_watermarks.last_synced_through_date``
for the relevant intraday source.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.bot.detectors import all_detectors, detect_all
from backend.db import session_scope
from backend.models.corpus_status import CorpusStatus
from backend.models.data_watermark import DataWatermark
from backend.models.market_observation import MarketObservation
from backend.models.stock_bar import StockBar

logger = logging.getLogger(__name__)


# Detectors that read intraday-only context (IV/GEX intraday series).
# Skip these when the ticker's intraday backfill hasn't completed yet
# so we don't fire twice when intraday eventually lands.
#
# MITS Phase 12 — VWAP family removed from this set. The vwap detector
# module now ships a daily-bar fallback (rolling 20-bar VWAP proxy) so
# the family produces meaningful signals on daily data while preserving
# the session-anchored behaviour on intraday data. Without this fix,
# only 9 of 40 universe tickers got VWAP coverage, even though VWAP is
# the top-edge family (+7pp vs the 68.9 percent baseline).
_INTRADAY_ONLY_FAMILIES = {"flow_intel"}


@dataclass
class ReplaySummary:
    ticker: str
    bars_read: int
    observations_emitted: int
    observations_inserted: int
    observations_skipped: int
    errors: int
    duration_sec: float
    detector_counts: Dict[str, int]


# ── helpers ───────────────────────────────────────────────────────────


def _fetch_silver_bars(ticker: str, interval: str,
                            start_date: date, end_date: date):
    """Load stock_bars into a pandas DataFrame indexed by bar_ts.

    Returns None when no rows landed yet (the ThetaData stock backfill
    is still tier-gated for some operators) so the caller can fall back
    or skip cleanly.
    """
    try:
        import pandas as pd  # type: ignore
    except Exception:
        return None
    try:
        with session_scope() as s:
            rows = s.execute(
                select(StockBar.bar_ts, StockBar.open, StockBar.high,
                            StockBar.low, StockBar.close, StockBar.volume)
                .where(StockBar.ticker == ticker)
                .where(StockBar.interval == interval)
                .where(StockBar.bar_ts >= datetime.combine(start_date, datetime.min.time()))
                .where(StockBar.bar_ts <= datetime.combine(end_date, datetime.max.time()))
                .order_by(StockBar.bar_ts.asc())
            ).all()
    except Exception:
        logger.exception("silver-bars load failed for %s @ %s", ticker, interval)
        return None
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=[
        "bar_ts", "open", "high", "low", "close", "volume",
    ])
    df = df.set_index("bar_ts")
    df.index = pd.to_datetime(df.index)
    return df


def _intraday_backfill_done(ticker: str, intraday_source: str,
                                  required_through: date) -> bool:
    """Return True only when the intraday backfill is at or past
    ``required_through`` for this ticker. False on missing watermark
    so the replay errs on the side of "skip intraday detectors"."""
    try:
        with session_scope() as s:
            row = s.execute(
                select(DataWatermark)
                .where(DataWatermark.source == intraday_source)
                .where(DataWatermark.ticker == ticker)
            ).scalar_one_or_none()
            if row is None or not row.last_synced_through_date:
                return False
            try:
                wm = datetime.strptime(
                    row.last_synced_through_date, "%Y-%m-%d"
                ).date()
            except Exception:
                return False
            return wm >= required_through
    except Exception:
        return False


def _fetch_iv_series_for_bars(ticker: str, bars) -> Optional[List[float]]:
    """Map iv_history → carry-forward series aligned to bars.index."""
    try:
        from backend.models.iv_history import IVHistory
    except Exception:
        return None
    try:
        with session_scope() as s:
            rows = s.execute(
                select(IVHistory.date, IVHistory.iv_atm)
                .where(IVHistory.ticker == ticker)
                .where(IVHistory.iv_atm.is_not(None))
            ).all()
    except Exception:
        return None
    if not rows:
        return None
    lookup: Dict[Any, float] = {}
    for d, iv in rows:
        try:
            key = d.date() if hasattr(d, "date") else d
            lookup[key] = float(iv)
        except Exception:
            continue
    if not lookup:
        return None
    series: List[Optional[float]] = []
    carry: Optional[float] = None
    for ts in bars.index:
        try:
            day = ts.date() if hasattr(ts, "date") else ts
        except Exception:
            day = None
        if day is not None and day in lookup:
            carry = lookup[day]
        series.append(carry)
    if all(v is None for v in series):
        return None
    return series


def _detector_emits_intraday_only(det) -> bool:
    """True if the detector reads intraday-only series (VWAP, flow)."""
    fam = getattr(det, "family", "") or ""
    return fam in _INTRADAY_ONLY_FAMILIES


def _existing_obs_keys(ticker: str) -> set:
    """Pull the (pattern, ts.isoformat(), timeframe) signatures already in
    market_observations for this ticker. Used as a fast in-memory dedup
    before the per-row INSERT path."""
    out: set = set()
    try:
        with session_scope() as s:
            rows = s.execute(
                select(MarketObservation.pattern,
                              MarketObservation.timestamp,
                              MarketObservation.timeframe)
                .where(MarketObservation.ticker == ticker)
            ).all()
            for pattern, ts, tf in rows:
                key = (
                    pattern,
                    ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                    tf,
                )
                out.add(key)
    except Exception:
        logger.debug("existing-obs lookup failed", exc_info=True)
    return out


def _persist_observation(obs, *, ticker_dedupe: set,
                                stats: Dict[str, int]) -> None:
    """Persist a single Observation with INSERT OR IGNORE semantics.

    Records dedup + insert + error counts on the supplied ``stats`` dict.
    """
    key = (
        obs.pattern,
        obs.timestamp.isoformat() if obs.timestamp else "",
        obs.timeframe,
    )
    if key in ticker_dedupe:
        stats["observations_skipped"] += 1
        return
    try:
        with session_scope() as s:
            row = MarketObservation(
                ticker=obs.ticker,
                pattern=obs.pattern,
                timestamp=obs.timestamp,
                timeframe=obs.timeframe,
                regime=obs.regime,
                vol_state=obs.vol_state,
                time_bucket=obs.time_bucket,
                spot=obs.spot,
                iv_rank=obs.iv_rank,
                gex_state=obs.gex_state,
                features=json.dumps(obs.features or {}, default=str),
                source=obs.source or "historical_replay",
            )
            s.add(row)
        stats["observations_inserted"] += 1
        ticker_dedupe.add(key)
    except IntegrityError:
        stats["observations_skipped"] += 1
    except Exception:
        stats["errors"] += 1
        logger.debug("observation persist failed", exc_info=True)


# ── public entry point ────────────────────────────────────────────────


def replay_ticker(
    ticker: str,
    *,
    start_date: date,
    end_date: date,
    intraday_source: str = "thetadata_stocks_intraday_5m",
    daily_min_bars: int = 30,
    pattern_filter: Optional[Sequence[str]] = None,
) -> ReplaySummary:
    """Walk-forward detector replay for one ticker from silver bars.

    Reads daily bars from ``stock_bars`` (interval='1d') over the
    [start_date, end_date] window and runs the full detector battery
    against the resulting frame. INSERT OR IGNORE on
    ``market_observations`` via the existing
    ``(ticker, pattern, timestamp, timeframe)`` unique constraint.

    Intraday-only detectors (VWAP, flow_intel) are skipped when the
    ticker's intraday watermark hasn't reached ``end_date`` yet — they
    re-fire on a later replay once intraday completes.

    Returns ReplaySummary so the launcher can render a per-ticker line.
    """
    t0 = datetime.utcnow()
    ticker = (ticker or "").upper().strip()
    summary = ReplaySummary(
        ticker=ticker, bars_read=0, observations_emitted=0,
        observations_inserted=0, observations_skipped=0, errors=0,
        duration_sec=0.0, detector_counts={},
    )
    if not ticker:
        return summary

    daily = _fetch_silver_bars(ticker, "1d", start_date, end_date)
    if daily is None or len(daily) < daily_min_bars:
        logger.info("replay %s: skipped (only %s daily bars in silver)",
                            ticker,
                            0 if daily is None else len(daily))
        summary.duration_sec = (datetime.utcnow() - t0).total_seconds()
        return summary
    summary.bars_read = len(daily)

    intraday_ready = _intraday_backfill_done(
        ticker, intraday_source, end_date,
    )
    iv_series = _fetch_iv_series_for_bars(ticker, daily)

    # Detector skipping: intraday-only families only fire when intraday
    # backfill has caught up. We achieve this by reusing the existing
    # `detect_all` (which iterates every enabled detector) but with a
    # post-filter on the resulting observations. Cleaner than forking
    # detect_all itself.
    try:
        obs_list = detect_all(ticker, daily, iv_series=iv_series)
    except Exception:
        logger.exception("detect_all crashed for %s", ticker)
        summary.errors += 1
        obs_list = []

    # Build pattern → family map once.
    family_by_pattern = {
        d.pattern: getattr(d, "family", "") or "" for d in all_detectors()
    }

    if not intraday_ready:
        before = len(obs_list)
        obs_list = [o for o in obs_list
                       if family_by_pattern.get(o.pattern, "") not in
                       _INTRADAY_ONLY_FAMILIES]
        deferred = before - len(obs_list)
        if deferred:
            logger.info(
                "replay %s: deferred %d intraday-only observations "
                "(intraday backfill not at %s)",
                ticker, deferred, end_date,
            )

    # Phase 12.2 — operator-requested detector subset (force-replay path).
    if pattern_filter:
        allowed = {p.strip() for p in pattern_filter if p and p.strip()}
        if allowed:
            before = len(obs_list)
            obs_list = [o for o in obs_list if o.pattern in allowed]
            dropped = before - len(obs_list)
            if dropped:
                logger.info(
                    "replay %s: filtered to %d detector(s); dropped %d "
                    "obs for unrelated patterns",
                    ticker, len(allowed), dropped,
                )

    summary.observations_emitted = len(obs_list)
    if not obs_list:
        summary.duration_sec = (datetime.utcnow() - t0).total_seconds()
        return summary

    # Per-ticker dedup keys keep the persist path O(N) instead of O(N²).
    ticker_dedupe = _existing_obs_keys(ticker)
    persist_stats = {
        "observations_inserted": 0,
        "observations_skipped": 0,
        "errors": 0,
    }
    detector_counts: Dict[str, int] = {}
    for obs in obs_list:
        _persist_observation(obs, ticker_dedupe=ticker_dedupe,
                                 stats=persist_stats)
        detector_counts[obs.pattern] = detector_counts.get(obs.pattern, 0) + 1
    summary.observations_inserted = persist_stats["observations_inserted"]
    summary.observations_skipped = persist_stats["observations_skipped"]
    summary.errors = persist_stats["errors"]
    summary.detector_counts = detector_counts
    summary.duration_sec = (datetime.utcnow() - t0).total_seconds()

    # Update corpus_status with the new observation count.
    try:
        from sqlalchemy import func
        with session_scope() as s:
            row = s.execute(
                select(CorpusStatus).where(CorpusStatus.ticker == ticker)
            ).scalar_one_or_none()
            if row is None:
                row = CorpusStatus(ticker=ticker)
                s.add(row)
                s.flush()
            row.observation_count = int(s.execute(
                select(func.count(MarketObservation.id))
                .where(MarketObservation.ticker == ticker)
            ).scalar_one() or 0)
            row.status = "ready"
            row.last_built_at = datetime.utcnow()
    except Exception:
        logger.debug("corpus_status update failed for %s", ticker, exc_info=True)

    return summary


def replay_universe(
    tickers: Sequence[str],
    *,
    start_date: date,
    end_date: date,
    intraday_source: str = "thetadata_stocks_intraday_5m",
    progress_cb=None,
    pattern_filter: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Replay every ticker in ``tickers`` in order. Per-ticker stats
    bubble up so the launcher can render a row each iteration.

    ``progress_cb(idx, total, ticker, summary)`` is called after each
    ticker finishes — used by the CLI to log a per-ticker line.
    """
    grand = {
        "tickers": 0,
        "bars_read": 0,
        "observations_emitted": 0,
        "observations_inserted": 0,
        "observations_skipped": 0,
        "errors": 0,
        "per_detector": {},
        "duration_sec": 0.0,
    }
    t0 = datetime.utcnow()
    for idx, ticker in enumerate(tickers, start=1):
        summary = replay_ticker(
            ticker,
            start_date=start_date,
            end_date=end_date,
            intraday_source=intraday_source,
            pattern_filter=pattern_filter,
        )
        grand["tickers"] += 1
        grand["bars_read"] += summary.bars_read
        grand["observations_emitted"] += summary.observations_emitted
        grand["observations_inserted"] += summary.observations_inserted
        grand["observations_skipped"] += summary.observations_skipped
        grand["errors"] += summary.errors
        for pattern, count in summary.detector_counts.items():
            grand["per_detector"][pattern] = (
                grand["per_detector"].get(pattern, 0) + count
            )
        if progress_cb is not None:
            try:
                progress_cb(idx, len(tickers), ticker, summary)
            except Exception:
                logger.debug("progress_cb failed", exc_info=True)
    grand["duration_sec"] = (datetime.utcnow() - t0).total_seconds()
    return grand


__all__ = [
    "ReplaySummary",
    "replay_ticker",
    "replay_universe",
]
