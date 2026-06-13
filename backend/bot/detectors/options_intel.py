"""MITS Phase 0 — options-intel detectors (IV expansion / compression, GEX).

These detectors operate on a *parallel* IV (and optionally GEX) time
series rather than on price bars. Two execution modes:

  1. ``detect`` called with an ``iv_series`` kwarg (list of floats
     aligned to ``bars.index``) — fires based on percentile movement.
  2. ``detect`` called without ``iv_series`` — gracefully returns [].
     The historical replay framework passes IV when ThetaData has the
     data, otherwise skips these detectors.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)


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


class IVExpansionDetector(Detector):
    """IV jumps >20% above the 20-bar trailing mean. Fires on the
    bar where the threshold is first crossed (no repeat-fire on the
    same expansion run)."""

    pattern = "iv_expansion"

    def default_params(self) -> Dict[str, Any]:
        return {
            "lookback_bars": 20,
            "expansion_multiplier": 1.20,
            "reset_multiplier": 1.05,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        iv_series: Optional[List[float]] = kwargs.get("iv_series")
        if bars is None or len(bars) < 25:
            return []
        if not iv_series or len(iv_series) != len(bars):
            return []
        bars = _lower_columns(bars)
        try:
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        lookback = int(p.get("lookback_bars", 20))
        exp_mult = float(p.get("expansion_multiplier", 1.20))
        reset_mult = float(p.get("reset_multiplier", 1.05))
        out: List[Observation] = []
        in_expansion = False
        for i in range(lookback, len(bars)):
            window = iv_series[i - lookback:i]
            window = [x for x in window if x is not None and x > 0]
            if len(window) < 10:
                continue
            mean = sum(window) / len(window)
            current = iv_series[i]
            if current is None or current <= 0:
                continue
            threshold = exp_mult * mean
            if current > threshold and not in_expansion:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "iv_now": round(current, 4),
                    "iv_mean_20": round(mean, 4),
                    "iv_jump_pct": round((current - mean) / mean, 4),
                }, closes))
                in_expansion = True
            elif current < reset_mult * mean:
                in_expansion = False
        return out


class IVCompressionDetector(Detector):
    """IV drops below 80% of the 20-bar trailing mean."""

    pattern = "iv_compression"

    def default_params(self) -> Dict[str, Any]:
        return {
            "lookback_bars": 20,
            "compression_multiplier": 0.80,
            "reset_multiplier": 0.95,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        iv_series: Optional[List[float]] = kwargs.get("iv_series")
        if bars is None or len(bars) < 25:
            return []
        if not iv_series or len(iv_series) != len(bars):
            return []
        bars = _lower_columns(bars)
        try:
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        lookback = int(p.get("lookback_bars", 20))
        comp_mult = float(p.get("compression_multiplier", 0.80))
        reset_mult = float(p.get("reset_multiplier", 0.95))
        out: List[Observation] = []
        in_compression = False
        for i in range(lookback, len(bars)):
            window = iv_series[i - lookback:i]
            window = [x for x in window if x is not None and x > 0]
            if len(window) < 10:
                continue
            mean = sum(window) / len(window)
            current = iv_series[i]
            if current is None or current <= 0:
                continue
            threshold = comp_mult * mean
            if current < threshold and not in_compression:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "iv_now": round(current, 4),
                    "iv_mean_20": round(mean, 4),
                    "iv_drop_pct": round((mean - current) / mean, 4),
                }, closes))
                in_compression = True
            elif current > reset_mult * mean:
                in_compression = False
        return out


class GEXAccelerationDetector(Detector):
    """GEX shifts state by >2x the 20-bar standard deviation. Mirrors
    the IV detectors but reads from ``gex_series`` kwarg (list of
    floats: net dealer gamma)."""

    pattern = "gex_acceleration"

    def default_params(self) -> Dict[str, Any]:
        return {
            "lookback_bars": 20,
            "z_threshold": 2.0,
            "cooldown_bars": 3,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        gex_series: Optional[List[float]] = kwargs.get("gex_series")
        if bars is None or len(bars) < 25:
            return []
        if not gex_series or len(gex_series) != len(bars):
            return []
        bars = _lower_columns(bars)
        try:
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        lookback = int(p.get("lookback_bars", 20))
        z_thresh = float(p.get("z_threshold", 2.0))
        cooldown = int(p.get("cooldown_bars", 3))
        out: List[Observation] = []
        last_fire = -1
        for i in range(lookback, len(bars)):
            window = gex_series[i - lookback:i]
            window = [x for x in window if x is not None]
            if len(window) < 10:
                continue
            try:
                std = statistics.pstdev(window)
                mean = statistics.fmean(window)
            except Exception:
                continue
            if std <= 0:
                continue
            current = gex_series[i]
            if current is None:
                continue
            delta = abs(current - mean) / std
            if delta > z_thresh and (i - last_fire) > cooldown:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "gex_now": round(float(current), 4),
                    "gex_mean_20": round(mean, 4),
                    "gex_std_20": round(std, 4),
                    "z_score": round(delta, 4),
                    "direction": ("positive" if current > mean else "negative"),
                }, closes))
                last_fire = i
        return out


def build_options_intel_detectors() -> List[Detector]:
    return [
        IVExpansionDetector(),
        IVCompressionDetector(),
        GEXAccelerationDetector(),
    ]
