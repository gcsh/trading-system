"""MITS Phase 0 — historical replay (yfinance → bars → detectors → DB).

`bootstrap_ticker(ticker)` is the single public entry point. It:

  1. Fetches yfinance daily bars (`daily_lookback_years`, default 10).
  2. Fetches yfinance 1h intraday bars (`intraday_lookback_days`, default 180).
  3. Runs every registered detector against each bar series.
  4. Optionally fetches an IV history series from `iv_history` for the
     options-intel detectors. Gracefully degrades if missing.
  5. Bulk-inserts unique observations (UniqueConstraint on
     `(ticker, pattern, timestamp, timeframe)` makes the operation
     idempotent — re-runs skip dups).
  6. Updates the `corpus_status` row for the ticker.

Returns a stats dict.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from backend.bot.detectors import detect_all
from backend.db import session_scope
from backend.models.corpus_status import CorpusStatus
from backend.models.market_observation import MarketObservation

logger = logging.getLogger(__name__)


# ── yfinance fetch helpers ────────────────────────────────────────────


def _fetch_daily_bars(ticker: str, lookback_years: int):
    """Return a DataFrame of daily bars or None."""
    try:
        import yfinance as yf
    except Exception:
        logger.warning("yfinance unavailable")
        return None
    period = f"{max(1, int(lookback_years))}y"
    try:
        df = yf.download(
            ticker, period=period, interval="1d",
            progress=False, auto_adjust=False, threads=False,
        )
    except Exception:
        logger.exception("yfinance daily fetch failed for %s", ticker)
        return None
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "get_level_values"):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def _fetch_intraday_bars(ticker: str, lookback_days: int):
    """Return a DataFrame of 1h bars or None.

    yfinance caps 1h intraday lookback at ~730 days. We request the
    operator-supplied window, clipping to 720 to stay inside the cap.
    """
    try:
        import yfinance as yf
    except Exception:
        return None
    period_days = max(1, min(720, int(lookback_days)))
    try:
        df = yf.download(
            ticker, period=f"{period_days}d", interval="1h",
            progress=False, auto_adjust=False, threads=False,
        )
    except Exception:
        logger.debug("yfinance intraday fetch failed for %s",
                            ticker, exc_info=True)
        return None
    if df is None or df.empty:
        return None
    if hasattr(df.columns, "get_level_values"):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


# ── MITS Phase 2 — intraday IV sampling (ThetaData straddle inversion) ─


# Density cap: one IV sample per ~30 min of bar timestamps. Avoids
# hammering ThetaData with 78 hits per ticker per day (6.5h × 5min bars)
# while still resolving intraday IV moves within a session. The replay
# fills in the gaps by carrying forward — see `_intraday_iv_series` below.
_INTRADAY_IV_SAMPLE_MINUTES = 30


def _intraday_iv_series(ticker: str, bars,
                              fallback_daily: Optional[List[float]] = None,
                              ) -> Optional[List[float]]:
    """Build a per-bar IV series for intraday bars using the Phase 2
    ThetaData straddle workaround.

    Strategy:
      - Walk the bars in chronological order.
      - Sample IV at the first bar of every 30-minute slot via
        ``compute_intraday_iv_at`` (cached after first hit).
      - For bars between samples, carry the most recent sample forward.
      - When a sample fails (no quote, etc.) we keep carrying the prior
        value. If no sample has succeeded yet, fall back to the daily
        IV carry-forward series the caller provides (so the bar still
        sees the daily-resolution IV at minimum).

    Returns ``None`` when no usable IV could be produced for ANY bar
    (e.g. ThetaData unreachable + no daily IV available). The
    options-intel detectors silently skip in that case.
    """
    if bars is None or len(bars) == 0:
        return None
    try:
        from backend.bot.data.thetadata import compute_intraday_iv_at
    except Exception:
        return fallback_daily

    series: List[Optional[float]] = []
    carry: Optional[float] = None
    last_sample_ts: Optional[datetime] = None
    spot_col = None
    try:
        cols = {str(c).lower(): c for c in bars.columns}
        for cand in ("close", "Close"):
            key = cand.lower()
            if key in cols:
                spot_col = cols[key]
                break
    except Exception:
        spot_col = None

    for i, ts in enumerate(bars.index):
        try:
            ts_py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        except Exception:
            ts_py = ts
        # Tick the sample clock when this timestamp is in a new 30-min slot.
        should_sample = False
        if isinstance(ts_py, datetime):
            if last_sample_ts is None:
                should_sample = True
            else:
                delta = (ts_py - last_sample_ts).total_seconds()
                if delta >= _INTRADAY_IV_SAMPLE_MINUTES * 60:
                    should_sample = True
        if should_sample and isinstance(ts_py, datetime):
            spot_val = None
            try:
                if spot_col is not None:
                    spot_val = float(bars[spot_col].iloc[i])
            except Exception:
                spot_val = None
            sampled = None
            try:
                sampled = compute_intraday_iv_at(
                    ticker, ts_py, spot=spot_val,
                )
            except Exception:
                sampled = None
            if sampled is not None and sampled > 0:
                carry = float(sampled)
            last_sample_ts = ts_py
        # When intraday sampling hasn't produced anything yet, surface
        # the daily-IV fallback at this index so the detector sees at
        # least the daily-resolution value.
        if carry is None and fallback_daily and i < len(fallback_daily):
            fb = fallback_daily[i]
            if fb is not None:
                series.append(float(fb))
                continue
        series.append(carry)

    if all(v is None for v in series):
        # Last-ditch: hand back the daily series if we have one.
        if fallback_daily and any(v is not None for v in fallback_daily):
            return fallback_daily
        return None
    return series


# ── IV time-series fetch (graceful degradation) ───────────────────────


def _fetch_iv_series(ticker: str, bars) -> Optional[List[float]]:
    """Map iv_history → list aligned to bars.index.

    Returns None if no IV data exists for the ticker (the options-intel
    detectors then skip).

    Carry-forward semantics: when a bar's date has no IV row, we use the
    last observed IV (the daily IV doesn't change intraday at the
    granularity the IVExpansionDetector cares about, so carrying yesterday's
    value into today's pre-IV-fetch bars is a reasonable null fill).
    For intraday bars (1h), every bar on the same calendar day shares the
    daily IV value — this is the documented degraded-mode behavior: see
    `bootstrap_ticker` docstring.
    """
    try:
        from backend.models.iv_history import IVHistory
    except Exception:
        return None
    try:
        index_dates = []
        for ts in bars.index:
            try:
                index_dates.append(ts.date() if hasattr(ts, "date") else None)
            except Exception:
                index_dates.append(None)
        with session_scope() as s:
            rows = s.execute(
                select(IVHistory.date, IVHistory.iv_atm)
                .where(IVHistory.ticker == ticker)
                .where(IVHistory.iv_atm.is_not(None))
            ).all()
        if not rows:
            return None
        lookup = {}
        for d, iv in rows:
            try:
                lookup[d.date() if hasattr(d, "date") else d] = float(iv)
            except Exception:
                continue
        if not lookup:
            return None
        # Carry-forward fill: walk the bar index, using the last known IV
        # value when an exact-date hit is missing. Bars before the first
        # IV row remain None (cannot extrapolate backward without
        # introducing look-ahead).
        sorted_iv_dates = sorted(lookup.keys())
        series: List[Optional[float]] = []
        carry: Optional[float] = None
        sorted_set = set(sorted_iv_dates)
        for d in index_dates:
            if d is None:
                series.append(carry)
                continue
            if d in sorted_set:
                carry = lookup[d]
            series.append(carry)
        if all(v is None for v in series):
            return None
        return series
    except Exception:
        return None


def _fetch_gex_series(ticker: str, bars) -> Optional[List[float]]:
    """Map gex_history → list aligned to bars.index (carry-forward fill).

    Returns None if no rows for the ticker.

    MITS Phase 2: reads the new `net_gex_scalar` column on
    `GexRegimeHistory` (backfilled from dealer_regime + distance-to-flip
    when not directly populated by a vendor). GEX is a slow-moving signal
    so the carry-forward strategy is acceptable for daily bars; for
    intraday bars every bar on the same calendar day shares the most
    recent snapshot's value, which is the same degraded-mode fallback the
    IV path uses until ThetaData Pro intraday endpoints become available.
    """
    try:
        from backend.models import gex_history as _gex_mod
        GexModel = None
        for name in ("GexRegimeHistory", "GEXHistory", "GexHistory"):
            GexModel = getattr(_gex_mod, name, None)
            if GexModel is not None:
                break
        if GexModel is None:
            return None
    except Exception:
        return None
    try:
        index_dates = []
        for ts in bars.index:
            try:
                index_dates.append(ts.date() if hasattr(ts, "date") else None)
            except Exception:
                index_dates.append(None)
        lookup: Dict[Any, float] = {}
        try:
            with session_scope() as s:
                rows = s.execute(
                    select(GexModel).where(GexModel.ticker == ticker)
                ).scalars().all()
                # Snapshot raw (ts, val) tuples WHILE the session is
                # still open — accessing attrs after commit triggers
                # DetachedInstanceError on lazy-loaded columns. Sort
                # ascending so the latest value per date wins on overlap.
                def _row_ts(r):
                    return (getattr(r, "captured_at", None)
                                  or getattr(r, "timestamp", None)
                                  or getattr(r, "date", None)
                                  or datetime.min)
                rows_sorted = sorted(rows, key=_row_ts)
                for r in rows_sorted:
                    try:
                        raw_ts = (getattr(r, "captured_at", None)
                                        or getattr(r, "timestamp", None)
                                        or getattr(r, "date", None))
                        d = (raw_ts.date()
                                  if hasattr(raw_ts, "date") else raw_ts)
                    except Exception:
                        d = None
                    if d is None:
                        continue
                    val = None
                    # MITS P2 — `net_gex_scalar` is the canonical column.
                    # Other field names retained for forward-compat with a
                    # potential vendor-supplied direct net-GEX column.
                    for fld in ("net_gex_scalar", "net_gex", "gex_total",
                                      "total_gex", "gex_dollar_billion",
                                      "gex_value"):
                        v = getattr(r, fld, None)
                        if v is not None:
                            try:
                                val = float(v)
                                break
                            except Exception:
                                continue
                    if val is None:
                        continue
                    lookup[d] = val
        except Exception:
            return None
        if not lookup:
            return None
        sorted_set = set(lookup.keys())
        series: List[Optional[float]] = []
        carry: Optional[float] = None
        for d in index_dates:
            if d is None:
                series.append(carry)
                continue
            if d in sorted_set:
                carry = lookup[d]
            series.append(carry)
        if all(v is None for v in series):
            return None
        return series
    except Exception:
        return None


# ── persistence helpers ───────────────────────────────────────────────


def _existing_signature_set(ticker: str) -> set:
    """Return a set of (pattern, timestamp_iso, timeframe) tuples for
    observations already in the DB. Cheap dedupe shortcut even though
    the unique constraint also enforces it."""
    out = set()
    try:
        with session_scope() as s:
            rows = s.execute(
                select(MarketObservation.pattern, MarketObservation.timestamp,
                              MarketObservation.timeframe)
                .where(MarketObservation.ticker == ticker)
            ).all()
            for pattern, ts, tf in rows:
                key = (pattern, ts.isoformat() if hasattr(ts, "isoformat") else str(ts), tf)
                out.add(key)
    except Exception:
        logger.debug("existing-sig lookup failed", exc_info=True)
    return out


def _persist_observations(observations: List[Any]) -> Dict[str, int]:
    """Persist a list of `Observation` dataclasses. Skips duplicates
    silently (relying on UniqueConstraint). Returns stats."""
    stats = {"inserted": 0, "skipped": 0, "errors": 0}
    if not observations:
        return stats
    try:
        from sqlalchemy.exc import IntegrityError
    except Exception:
        return stats

    by_ticker: Dict[str, set] = {}
    for obs in observations:
        if obs.ticker not in by_ticker:
            by_ticker[obs.ticker] = _existing_signature_set(obs.ticker)
    for obs in observations:
        key = (obs.pattern, obs.timestamp.isoformat() if obs.timestamp else "",
                  obs.timeframe)
        if key in by_ticker.get(obs.ticker, set()):
            stats["skipped"] += 1
            continue
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
                    source=obs.source,
                    direction=getattr(obs, "direction", None),
                )
                s.add(row)
            stats["inserted"] += 1
            by_ticker[obs.ticker].add(key)
        except IntegrityError:
            stats["skipped"] += 1
        except Exception:
            stats["errors"] += 1
            logger.debug("observation persist failed", exc_info=True)
    return stats


def _update_corpus_status(ticker: str, *, status: str,
                                 observation_count: int = 0,
                                 outcome_count: int = 0,
                                 cell_count: int = 0,
                                 error: Optional[str] = None) -> None:
    try:
        with session_scope() as s:
            row = s.execute(
                select(CorpusStatus).where(CorpusStatus.ticker == ticker)
            ).scalar_one_or_none()
            if row is None:
                row = CorpusStatus(ticker=ticker)
                s.add(row)
                s.flush()
            row.status = status
            if observation_count:
                row.observation_count = observation_count
            if outcome_count:
                row.outcome_count = outcome_count
            if cell_count:
                row.cell_count = cell_count
            if status == "ready":
                row.last_built_at = datetime.utcnow()
                row.error = None
            if error is not None:
                row.error = error[:500]
    except Exception:
        logger.debug("corpus_status update failed for %s", ticker, exc_info=True)


def _count_observations(ticker: str) -> int:
    try:
        with session_scope() as s:
            return int(s.execute(
                select(func.count(MarketObservation.id))
                .where(MarketObservation.ticker == ticker)
            ).scalar_one() or 0)
    except Exception:
        return 0


# ── public entry point ───────────────────────────────────────────────


def bootstrap_ticker(ticker: str, *,
                          daily_lookback_years: int = 10,
                          intraday_lookback_days: int = 180,
                          bars_daily=None,
                          bars_intraday=None,
                          iv_series_daily: Optional[List[float]] = None,
                          iv_series_intraday: Optional[List[float]] = None,
                          gex_series_daily: Optional[List[float]] = None,
                          gex_series_intraday: Optional[List[float]] = None,
                          ) -> Dict[str, Any]:
    """Run the full historical-replay pipeline for one ticker.

    The `bars_daily` / `bars_intraday` / `iv_series_*` / `gex_series_*`
    kwargs are test-injection points: callers can supply pre-built bar
    frames + series and skip the network call. Production callers pass
    none and the function fetches via yfinance + the local iv_history /
    gex_history tables.

    IV / GEX intraday fidelity (MITS Phase 2 — Standard-tier workaround):
      ThetaData Standard tier does NOT expose a dedicated intraday IV
      endpoint. P2 works around this by sampling ATM straddle quotes
      via the historical chain-quote endpoint (which Standard DOES
      expose) once every ~30 minutes and inverting to IV via
      Brenner-Subrahmanyam (`compute_intraday_iv_at`). The resulting
      series is cached in `intraday_iv_cache` so re-runs are free.
      Bars between samples carry the most recent IV forward, and bars
      whose sample failed (no quote available) fall back to the
      daily-IV carry-forward series. Net effect: intraday IV resolves
      at ~30-min granularity (a 13x improvement over Phase 1's
      daily-only resolution) and degrades gracefully to the Phase 1
      behaviour whenever ThetaData is unreachable.

      GEX intraday remains at daily resolution (the gex_history table
      now exposes `net_gex_scalar` for the corpus path, but no
      intraday net-GEX endpoint exists on Standard tier).
    """
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return {"status": "error", "error": "missing ticker"}
    _update_corpus_status(ticker, status="building")
    stats: Dict[str, Any] = {
        "ticker": ticker,
        "daily": {"inserted": 0, "skipped": 0, "errors": 0, "bars": 0},
        "intraday": {"inserted": 0, "skipped": 0, "errors": 0, "bars": 0},
        "status": "ready",
        "errors": [],
    }

    # --- daily replay ----
    daily = bars_daily if bars_daily is not None else _fetch_daily_bars(
        ticker, daily_lookback_years)
    if daily is not None and len(daily) >= 30:
        stats["daily"]["bars"] = len(daily)
        iv = iv_series_daily if iv_series_daily is not None else _fetch_iv_series(
            ticker, daily)
        gex = gex_series_daily if gex_series_daily is not None else _fetch_gex_series(
            ticker, daily)
        try:
            obs = detect_all(ticker, daily, iv_series=iv, gex_series=gex)
            persist_stats = _persist_observations(obs)
            stats["daily"].update(persist_stats)
            stats["daily"]["iv_aligned"] = bool(iv)
            stats["daily"]["gex_aligned"] = bool(gex)
        except Exception as e:
            stats["errors"].append(f"daily replay: {e}")
            logger.exception("daily replay failed for %s", ticker)
    else:
        stats["errors"].append("no daily bars")

    # --- intraday replay ----
    intraday = bars_intraday if bars_intraday is not None else _fetch_intraday_bars(
        ticker, intraday_lookback_days)
    if intraday is not None and len(intraday) >= 30:
        stats["intraday"]["bars"] = len(intraday)
        # MITS Phase 2 — try the ThetaData straddle-inversion workaround
        # FIRST. When a caller supplies `iv_series_intraday` directly
        # (tests, pre-built series) we honor that. Otherwise we sample
        # one straddle every 30 minutes via the historical chain quote
        # endpoint and invert to IV. The function falls back to the
        # daily-IV carry-forward series for any bar where the sample
        # fails — the Phase 1 degraded-mode behaviour is preserved as
        # the floor.
        intraday_iv = iv_series_intraday
        if intraday_iv is None:
            daily_carry = _fetch_iv_series(ticker, intraday)
            intraday_iv = _intraday_iv_series(
                ticker, intraday,
                fallback_daily=daily_carry,
            )
            if intraday_iv is None:
                intraday_iv = daily_carry
        intraday_gex = (gex_series_intraday
                              if gex_series_intraday is not None
                              else _fetch_gex_series(ticker, intraday))
        try:
            obs = detect_all(ticker, intraday,
                                  iv_series=intraday_iv,
                                  gex_series=intraday_gex)
            persist_stats = _persist_observations(obs)
            stats["intraday"].update(persist_stats)
            stats["intraday"]["iv_aligned"] = bool(intraday_iv)
            stats["intraday"]["gex_aligned"] = bool(intraday_gex)
        except Exception as e:
            stats["errors"].append(f"intraday replay: {e}")
            logger.debug("intraday replay failed for %s", ticker, exc_info=True)

    total_inserted = stats["daily"]["inserted"] + stats["intraday"]["inserted"]
    total_obs = _count_observations(ticker)
    final_status = "ready" if total_obs > 0 else "insufficient"
    if stats["errors"] and total_obs == 0:
        final_status = "error"
    _update_corpus_status(ticker, status=final_status,
                                 observation_count=total_obs,
                                 error=("; ".join(stats["errors"]) if stats["errors"] else None))
    stats["status"] = final_status
    stats["observation_count"] = total_obs
    stats["total_inserted"] = total_inserted
    return stats
