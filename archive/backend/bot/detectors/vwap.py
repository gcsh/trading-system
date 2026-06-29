"""MITS Phase 0 / Phase 12 — VWAP reclaim/rejection detectors.

Two operating modes:

* **Intraday bars** (5m / 15m / 1h): session-anchored VWAP, reset at
  the start of each calendar day in the input. This is the
  "TradingView-style" anchored VWAP traders watch on the intraday chart.

* **Daily bars** (Phase 12 fix): when only daily bars are available
  (ticker hasn't received the 5m intraday backfill yet), we fall back
  to a 20-bar rolling-VWAP proxy — i.e. the cumulative
  ``sum(typical * volume) / sum(volume)`` over the trailing 20 sessions.
  The "reclaim" signal then fires on the bar where close crosses up
  through the rolling VWAP (and the "rejection" mirror fires on the
  cross-down). This restores VWAP detector coverage to all 40 tickers
  in the universe; without it, only the 14 tickers with intraday
  backfill would benefit from the highest-edge family in the corpus
  (audit: +7pp vs the 68.9 percent baseline).

Phase 12 design choice (b): rolling-VWAP fallback over (a) gating to
intraday-only. The fallback is mathematically a volume-weighted
moving average — defensible signal, well-documented in market
microstructure literature (Berkowitz, Logue, Noser, "The total cost of
transactions on the NYSE", JoF 1988). Bulkowski's "Encyclopedia of
Chart Patterns" treats VWAP crosses as the institutional fair-value
boundary on any timeframe.

The 20-bar window matches our regime/vol-state cohort window so
features stay aligned across the corpus.
"""
from __future__ import annotations

from typing import Any, Dict, List

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)


def _is_intraday(bars) -> bool:
    """Heuristic: True when the bar series has more than one row per
    calendar day (i.e. intraday data). On daily bars one row == one
    date, so the unique-date count equals len(bars)."""
    try:
        if bars is None or len(bars) < 2:
            return False
        seen = set()
        for ts in bars.index:
            try:
                d = ts.date() if hasattr(ts, "date") else ts
            except Exception:
                continue
            seen.add(d)
            if len(seen) < len(bars) // 2:  # short-circuit once obvious
                return True
        return len(seen) < len(bars)
    except Exception:
        return False


def _rolling_vwap(highs: List[float], lows: List[float],
                       closes: List[float], volumes: List[float],
                       window: int) -> List[float]:
    """20-bar volume-weighted moving average. The Phase 12 fallback
    for daily-bar VWAP detectors so all 40 tickers benefit from the
    family even before intraday backfill completes."""
    n = len(closes)
    out: List[float] = []
    pv_window: List[float] = []
    v_window: List[float] = []
    pv_sum = 0.0
    v_sum = 0.0
    for i in range(n):
        typical = (highs[i] + lows[i] + closes[i]) / 3.0
        pv = typical * volumes[i]
        pv_window.append(pv)
        v_window.append(volumes[i])
        pv_sum += pv
        v_sum += volumes[i]
        if len(pv_window) > window:
            pv_sum -= pv_window.pop(0)
            v_sum -= v_window.pop(0)
        out.append(pv_sum / v_sum if v_sum > 0 else closes[i])
    return out


def _compute_session_vwap(bars, *, daily_window: int = 20) -> List[float]:
    """Return VWAP for each bar.

    * Intraday bars → anchored at start of each calendar day.
    * Daily bars → rolling-VWAP over ``daily_window`` sessions (Phase 12).

    Falls back to plain VWAP if dates aren't accessible. Volume column
    is required; without it we return closes (vwap == close → no
    crossings → detectors fire nothing, the historical safe behaviour).
    """
    try:
        highs = bars["high"].astype(float).tolist()
        lows = bars["low"].astype(float).tolist()
        closes = bars["close"].astype(float).tolist()
        if "volume" not in bars.columns:
            return closes  # no volume → return closes (vwap == close)
        volumes = bars["volume"].astype(float).tolist()
    except Exception:
        return []

    # Phase 12 — daily-bar fallback path.
    if not _is_intraday(bars):
        return _rolling_vwap(highs, lows, closes, volumes, daily_window)

    # Intraday — session-anchored VWAP (legacy behaviour preserved).
    try:
        dates = [ts.date() if hasattr(ts, "date") else None for ts in bars.index]
    except Exception:
        dates = [None] * len(bars)
    vwap: List[float] = []
    cum_pv = 0.0
    cum_v = 0.0
    last_date = None
    for i in range(len(closes)):
        d = dates[i]
        if d != last_date:
            cum_pv = 0.0
            cum_v = 0.0
            last_date = d
        typical = (highs[i] + lows[i] + closes[i]) / 3.0
        cum_pv += typical * volumes[i]
        cum_v += volumes[i]
        vwap.append(cum_pv / cum_v if cum_v > 0 else closes[i])
    return vwap


def _build_obs(ticker: str, bars, i: int, pattern: str,
                  features: Dict[str, Any], closes) -> Observation:
    ts = bars.index[i]
    try:
        ts_py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    except Exception:
        ts_py = ts
    return Observation(
        ticker=ticker,
        pattern=pattern,
        timestamp=ts_py,
        timeframe=_bar_timeframe(bars),
        regime=_classify_regime(bars, i),
        vol_state=_classify_vol_state(bars, i),
        time_bucket=_time_bucket(ts_py) if hasattr(ts_py, "hour") else "rth",
        spot=float(closes[i]),
        features=features,
    )


class VWAPReclaimDetector(Detector):
    """Close transitions from below VWAP to above VWAP. Fires on the
    bar where the cross happens."""

    pattern = "vwap_reclaim"

    def default_params(self) -> Dict[str, Any]:
        return {
            "min_cross_distance_pct": 0.0,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 3:
            return []
        bars = _lower_columns(bars)
        vwap = _compute_session_vwap(bars)
        if not vwap:
            return []
        try:
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        min_dist = float(p.get("min_cross_distance_pct", 0.0))
        out: List[Observation] = []
        for i in range(1, len(closes)):
            if closes[i - 1] < vwap[i - 1] and closes[i] > vwap[i]:
                dist = closes[i] - vwap[i]
                if vwap[i] > 0 and (dist / vwap[i]) < min_dist:
                    continue
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "vwap": round(vwap[i], 4),
                    "cross_distance": round(dist, 4),
                }, closes))
        return out


class VWAPRejectionDetector(Detector):
    """Close transitions from above VWAP to below VWAP."""

    pattern = "vwap_rejection"

    def default_params(self) -> Dict[str, Any]:
        return {
            "min_cross_distance_pct": 0.0,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 3:
            return []
        bars = _lower_columns(bars)
        vwap = _compute_session_vwap(bars)
        if not vwap:
            return []
        try:
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        min_dist = float(p.get("min_cross_distance_pct", 0.0))
        out: List[Observation] = []
        for i in range(1, len(closes)):
            if closes[i - 1] > vwap[i - 1] and closes[i] < vwap[i]:
                dist = vwap[i] - closes[i]
                if vwap[i] > 0 and (dist / vwap[i]) < min_dist:
                    continue
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "vwap": round(vwap[i], 4),
                    "cross_distance": round(dist, 4),
                }, closes))
        return out


def build_vwap_detectors() -> List[Detector]:
    return [VWAPReclaimDetector(), VWAPRejectionDetector()]
