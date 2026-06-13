"""MITS Phase 0 — outcome linker.

For every `MarketObservation` without `MarketOutcome` rows, fetch the
forward bars and compute return at multiple horizons:

  * Intraday (timeframe in {1m, 5m, 15m, 30m, 1h}): 5min, 30min, 60min
  * Daily   (timeframe == 1d): 1d, 5d, 20d

Horizons whose forward window exceeds the available bar history are
skipped (we'll fill them in on a later run when more bars exist).

Idempotent: each (observation_id, horizon) is uniqued.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.db import session_scope
from backend.models.corpus_status import CorpusStatus
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome

logger = logging.getLogger(__name__)


# Horizons in minutes (canonical). Daily-bar observations use the 1d/5d/20d
# subset; intraday observations use 5min/30min/60min.
HORIZON_MINUTES: Dict[str, int] = {
    "5min": 5,
    "30min": 30,
    "60min": 60,
    "1d": 60 * 24,
    "5d": 60 * 24 * 5,
    "20d": 60 * 24 * 20,
}

INTRADAY_HORIZONS = ["5min", "30min", "60min"]
DAILY_HORIZONS = ["1d", "5d", "20d"]


# MITS Phase 12.1 Fix 3 — direction-aware "winner" definition.
# Neutral threshold: any move >= 0.5% in either direction counts as a
# winner for volatility-regime detectors (gex_acceleration, iv_*).
_NEUTRAL_WIN_THRESHOLD = 0.005


def _compute_winner(direction: Optional[str], return_pct: Optional[float],
                            threshold: float = _NEUTRAL_WIN_THRESHOLD) -> bool:
    """Direction-aware winner check.

    long    → return > 0           (bullish setup, price up = win)
    short   → return < 0           (bearish setup, price down = win)
    neutral → |return| > threshold (vol regime; any meaningful move)
    None    → return > 0           (legacy fallback — bullish bias)
    """
    if return_pct is None:
        return False
    d = (direction or "").lower() if direction else ""
    if d == "long":
        return return_pct > 0
    if d == "short":
        return return_pct < 0
    if d == "neutral":
        return abs(return_pct) > threshold
    # Legacy / unknown — preserve historical behaviour.
    return return_pct > 0


def _is_intraday(timeframe: str) -> bool:
    return timeframe in {"1m", "5m", "15m", "30m", "1h"}


def _yf_ticker_alias(ticker: str) -> str:
    """yfinance accepts class-share tickers with a hyphen instead of a
    dot. ``BRK.B`` chokes ('possibly delisted') but ``BRK-B`` works.
    Apply the same normalisation Phase 9.5's heatseeker fix used."""
    t = (ticker or "").upper().strip()
    if not t:
        return t
    if "." in t:
        return t.replace(".", "-")
    return t


def _bars_from_stock_bars_table(ticker: str, intraday: bool):
    """MITS Phase 12.1 Fix 12 — primary bar source is the local
    ``stock_bars`` table populated by the ThetaData backfill.

    Phase 11 landed 20y of daily + 5y of 1m/5m/60m bars per ticker into
    this table; the outcome linker should reach for that BEFORE punching
    yfinance, especially for class-share tickers (BRK.B) where yfinance
    routinely returns 'possibly delisted'. Returns a pandas DataFrame or
    None when the table is empty for the ticker.
    """
    try:
        import pandas as pd
        from backend.models.stock_bar import StockBar
    except Exception:
        return None
    intervals = ("60m", "30m", "15m", "5m", "1m") if intraday else ("1d",)
    try:
        with session_scope() as s:
            for itv in intervals:
                rows = s.execute(
                    select(StockBar.bar_ts, StockBar.open, StockBar.high,
                              StockBar.low, StockBar.close, StockBar.volume)
                    .where(StockBar.ticker == ticker.upper())
                    .where(StockBar.interval == itv)
                    .order_by(StockBar.bar_ts.asc())
                ).all()
                if not rows:
                    continue
                df = pd.DataFrame(rows, columns=[
                    "ts", "open", "high", "low", "close", "volume",
                ])
                df = df.set_index("ts")
                df.columns = [c.lower() for c in df.columns]
                return df
    except Exception:
        logger.debug("stock_bars fetch failed for %s", ticker, exc_info=True)
    return None


def _fetch_bars_for_outcome(ticker: str, intraday: bool, observation_ts: datetime):
    """Return a DataFrame covering the post-observation window.

    Source order:
      1. Local ``stock_bars`` SQLite table (ThetaData backfill).
      2. yfinance with hyphenated class-share alias for BRK.B etc.

    For daily: pull the last 5y of daily bars (idempotent cache call —
    yfinance is reasonably fast and we cache via observation persist).
    For intraday: pull the last 60 days of 1h bars (yfinance limit).
    """
    # Prefer local cache — survives a fresh-start AND handles BRK.B.
    df = _bars_from_stock_bars_table(ticker, intraday)
    if df is not None and not df.empty:
        return df
    try:
        import yfinance as yf
    except Exception:
        return None
    yf_ticker = _yf_ticker_alias(ticker)
    try:
        if intraday:
            df = yf.download(
                yf_ticker, period="60d", interval="1h",
                progress=False, auto_adjust=False, threads=False,
            )
        else:
            df = yf.download(
                yf_ticker, period="5y", interval="1d",
                progress=False, auto_adjust=False, threads=False,
            )
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "get_level_values"):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df.columns = [str(c).strip().lower() for c in df.columns]
        return df
    except Exception:
        logger.debug("outcome bar fetch failed for %s", ticker, exc_info=True)
        return None


def _find_index_at_or_after(bars, ts: datetime) -> Optional[int]:
    """Return the smallest bar index whose timestamp is >= ``ts``."""
    try:
        idx = bars.index
        for i in range(len(idx)):
            tsi = idx[i]
            try:
                tsi_dt = tsi.to_pydatetime() if hasattr(tsi, "to_pydatetime") else tsi
            except Exception:
                tsi_dt = tsi
            # Strip tzinfo for comparison.
            if hasattr(tsi_dt, "replace") and tsi_dt.tzinfo is not None:
                tsi_dt = tsi_dt.replace(tzinfo=None)
            ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts
            if tsi_dt >= ts_naive:
                return i
        return None
    except Exception:
        return None


def _compute_outcomes_for_obs(obs, bars,
                                       direction: Optional[str] = None,
                                       ) -> List[Tuple[str, float, float, float, bool]]:
    """Return [(horizon, entry_price, exit_price, return_pct, was_winner), ...]
    for horizons whose forward window fits the bar series.

    MITS Phase 12.1 Fix 3: ``direction`` controls how ``was_winner`` is
    scored. See ``_compute_winner`` for the truth table.
    """
    if bars is None or len(bars) < 2:
        return []
    entry_idx = _find_index_at_or_after(bars, obs.timestamp)
    if entry_idx is None or entry_idx >= len(bars) - 1:
        return []
    try:
        closes = bars["close"].astype(float).tolist()
    except Exception:
        return []
    entry_price = closes[entry_idx]
    if entry_price <= 0:
        return []
    horizons = (INTRADAY_HORIZONS if _is_intraday(obs.timeframe) else DAILY_HORIZONS)
    out: List[Tuple[str, float, float, float, bool]] = []
    for h in horizons:
        mins = HORIZON_MINUTES[h]
        # Translate minutes to bar steps.
        if _is_intraday(obs.timeframe):
            step = max(1, mins // 60)  # 1h bars → 1, 0 for 5min (treat as 1 bar).
            if mins == 5:
                step = 1
            elif mins == 30:
                step = 1
            elif mins == 60:
                step = 1
        else:
            step = max(1, mins // (60 * 24))
        target = entry_idx + step
        if target >= len(closes):
            continue
        exit_price = closes[target]
        ret = (exit_price - entry_price) / entry_price
        out.append((h, entry_price, exit_price, ret,
                       _compute_winner(direction, ret)))
    return out


def _outcome_exists(session, observation_id: int, horizon: str) -> bool:
    return session.execute(
        select(MarketOutcome.id)
        .where(MarketOutcome.observation_id == observation_id)
        .where(MarketOutcome.horizon == horizon)
    ).first() is not None


def link_outcomes_batch(ticker: Optional[str] = None, *,
                            limit: int = 5000) -> Dict[str, Any]:
    """Fill outcomes for observations missing them.

    If ``ticker`` is provided, scope to that ticker. Otherwise process
    up to ``limit`` observations across all tickers (oldest first so
    completed horizons get filled before fresh ones).
    """
    stats = {"observations_processed": 0, "outcomes_inserted": 0,
              "outcomes_skipped": 0, "errors": 0, "tickers": {}}
    try:
        with session_scope() as s:
            q = select(MarketObservation)
            if ticker:
                q = q.where(MarketObservation.ticker == ticker.upper().strip())
            q = q.order_by(MarketObservation.timestamp.asc()).limit(limit)
            obs_rows = list(s.execute(q).scalars().all())
            # Detach: copy fields needed downstream so we can close this session.
            detached = []
            for o in obs_rows:
                detached.append({
                    "id": o.id,
                    "ticker": o.ticker,
                    "timestamp": o.timestamp,
                    "timeframe": o.timeframe,
                    "direction": o.direction,
                })
    except Exception:
        logger.exception("outcome linker: observation fetch failed")
        return stats

    # Group by (ticker, is_intraday) so we fetch bars at most twice per ticker.
    bars_cache: Dict[Tuple[str, bool], Any] = {}
    for obs in detached:
        intraday = _is_intraday(obs["timeframe"])
        key = (obs["ticker"], intraday)
        if key not in bars_cache:
            bars_cache[key] = _fetch_bars_for_outcome(
                obs["ticker"], intraday, obs["timestamp"],
            )
        bars = bars_cache[key]
        if bars is None:
            stats["errors"] += 1
            continue
        # Build a minimal observation shim for compute (we only need the
        # timestamp / timeframe fields).
        shim = type("Shim", (), {})()
        shim.timestamp = obs["timestamp"]
        shim.timeframe = obs["timeframe"]
        outcomes = _compute_outcomes_for_obs(
            shim, bars, direction=obs.get("direction"),
        )
        if not outcomes:
            stats["observations_processed"] += 1
            continue
        try:
            with session_scope() as s2:
                for h, entry, exit_, ret, was_winner in outcomes:
                    if _outcome_exists(s2, obs["id"], h):
                        stats["outcomes_skipped"] += 1
                        continue
                    try:
                        s2.add(MarketOutcome(
                            observation_id=obs["id"],
                            horizon=h,
                            entry_price=entry,
                            exit_price=exit_,
                            return_pct=ret,
                            was_winner=was_winner,
                        ))
                        stats["outcomes_inserted"] += 1
                    except IntegrityError:
                        stats["outcomes_skipped"] += 1
        except Exception:
            stats["errors"] += 1
            logger.debug("outcome insert batch failed", exc_info=True)
        stats["observations_processed"] += 1
        stats["tickers"][obs["ticker"]] = stats["tickers"].get(obs["ticker"], 0) + 1

    # Update corpus_status outcome_count if we scoped to a ticker.
    if ticker:
        try:
            with session_scope() as s3:
                tkr = ticker.upper().strip()
                row = s3.execute(
                    select(CorpusStatus).where(CorpusStatus.ticker == tkr)
                ).scalar_one_or_none()
                if row is None:
                    row = CorpusStatus(ticker=tkr, status="building")
                    s3.add(row)
                    s3.flush()
                row.outcome_count = int(s3.execute(
                    select(func.count(MarketOutcome.id))
                    .join(MarketObservation,
                            MarketObservation.id == MarketOutcome.observation_id)
                    .where(MarketObservation.ticker == tkr)
                ).scalar_one() or 0)
        except Exception:
            logger.debug("corpus_status outcome_count update failed", exc_info=True)
    return stats


# MITS Phase 12.1 Fix 4 — bulk relink for direction-aware re-scoring.


def rescore_winners_in_place(batch_size: int = 5000) -> Dict[str, Any]:
    """Recompute ``was_winner`` on EVERY existing MarketOutcome row using
    the direction-aware rule, WITHOUT re-fetching bars.

    Strategy: stream observations + outcomes joined; for each row, recompute
    ``was_winner = _compute_winner(direction, return_pct)`` and UPDATE.
    No bar fetches, no horizon recompute — just rescore.

    Used after the direction-backfill migration to flip the ~508k legacy
    outcome rows whose was_winner column was direction-unaware.

    Returns ``{processed, flipped, errors}``.
    """
    stats = {"processed": 0, "flipped": 0, "unchanged": 0, "errors": 0}
    offset = 0
    while True:
        try:
            with session_scope() as s:
                # Pull a batch of joined rows.
                rows = s.execute(
                    select(MarketOutcome.id, MarketOutcome.return_pct,
                              MarketOutcome.was_winner,
                              MarketObservation.direction)
                    .join(MarketObservation,
                              MarketObservation.id ==
                                    MarketOutcome.observation_id)
                    .order_by(MarketOutcome.id.asc())
                    .offset(offset)
                    .limit(batch_size)
                ).all()
                if not rows:
                    break
                update_pairs: List[Tuple[int, bool]] = []
                for oc_id, ret_pct, prev_winner, direction in rows:
                    if ret_pct is None:
                        stats["processed"] += 1
                        continue
                    new_winner = _compute_winner(direction, ret_pct)
                    if bool(prev_winner) == bool(new_winner):
                        stats["unchanged"] += 1
                    else:
                        stats["flipped"] += 1
                        update_pairs.append((oc_id, bool(new_winner)))
                    stats["processed"] += 1
                # Bulk-update flipped rows.
                if update_pairs:
                    from sqlalchemy import update as sqla_update
                    for oc_id, new_winner in update_pairs:
                        s.execute(sqla_update(MarketOutcome)
                                       .where(MarketOutcome.id == oc_id)
                                       .values(was_winner=new_winner))
            offset += batch_size
        except Exception:
            stats["errors"] += 1
            logger.exception("rescore_winners_in_place batch failed @ %d",
                                  offset)
            offset += batch_size
            continue
    return stats


def relink_all(ticker: Optional[str] = None) -> Dict[str, Any]:
    """End-to-end re-link entry point.

    1. Re-score existing outcome rows in place (cheap, no network).
    2. Fill any missing (observation, horizon) outcomes (bars-bound).

    Use ``ticker`` to scope; ``None`` = whole corpus.
    """
    summary = {"rescore": {}, "fill_in": {}}
    summary["rescore"] = rescore_winners_in_place()
    # Process new-only fill in batches of 10k to stay memory-bounded.
    processed = 0
    total_inserted = 0
    while True:
        batch = link_outcomes_batch(ticker=ticker, limit=10000)
        if batch["observations_processed"] == 0:
            break
        processed += batch["observations_processed"]
        total_inserted += batch["outcomes_inserted"]
        # Stop when no inserts happened in a full batch — means we're
        # caught up.
        if batch["outcomes_inserted"] == 0 and batch["outcomes_skipped"] > 0:
            break
        # Safety: don't loop forever.
        if processed > 500000:
            break
    summary["fill_in"] = {
        "observations_processed": processed,
        "outcomes_inserted": total_inserted,
    }
    return summary
