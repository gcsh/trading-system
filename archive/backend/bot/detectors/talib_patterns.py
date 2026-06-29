"""MITS Phase 0 — TA-Lib candlestick pattern wrappers.

TA-Lib ships ~60 candlestick pattern functions. We wrap a curated set of
15 that have clear academic / trader-folklore backing and produce one
Observation per bar where the underlying function returns a non-zero
signal.

Gracefully degrades when TA-Lib isn't installed: the detector instance
exists but `detect()` returns []. Tests must skip in that case.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)

logger = logging.getLogger(__name__)


try:
    import talib  # type: ignore
    _TALIB_AVAILABLE = True
except Exception:
    talib = None  # type: ignore
    _TALIB_AVAILABLE = False


# Curated 15-pattern set. Slug values become the `pattern` column in
# market_observations — keep stable; downstream cohort cells are keyed on
# them. Direction key: "bull" | "bear" | "either" (DOJI etc).
TALIB_PATTERN_SPECS: List[Dict[str, str]] = [
    {"func": "CDLENGULFING",      "slug": "talib_engulfing",       "direction": "either"},
    {"func": "CDLHAMMER",         "slug": "talib_hammer",          "direction": "bull"},
    {"func": "CDLDOJI",           "slug": "talib_doji",            "direction": "either"},
    {"func": "CDLEVENINGSTAR",    "slug": "talib_evening_star",    "direction": "bear"},
    {"func": "CDLMORNINGSTAR",    "slug": "talib_morning_star",    "direction": "bull"},
    {"func": "CDLSHOOTINGSTAR",   "slug": "talib_shooting_star",   "direction": "bear"},
    {"func": "CDLHARAMI",         "slug": "talib_harami",          "direction": "either"},
    {"func": "CDLPIERCING",       "slug": "talib_piercing",        "direction": "bull"},
    {"func": "CDLDARKCLOUDCOVER", "slug": "talib_dark_cloud_cover","direction": "bear"},
    {"func": "CDL3WHITESOLDIERS", "slug": "talib_three_white_soldiers", "direction": "bull"},
    {"func": "CDL3BLACKCROWS",    "slug": "talib_three_black_crows",    "direction": "bear"},
    {"func": "CDLHANGINGMAN",     "slug": "talib_hanging_man",     "direction": "bear"},
    {"func": "CDLINVERTEDHAMMER", "slug": "talib_inverted_hammer", "direction": "bull"},
    {"func": "CDLMARUBOZU",       "slug": "talib_marubozu",        "direction": "either"},
    {"func": "CDLSPINNINGTOP",    "slug": "talib_spinning_top",    "direction": "either"},
]


def _run_pattern(func_name: str, opens, highs, lows, closes):
    """Call a TA-Lib pattern function by name. Returns the int8 array
    of signals (-100, 0, +100) — or None if TA-Lib unavailable / errored."""
    if not _TALIB_AVAILABLE or talib is None:
        return None
    fn = getattr(talib, func_name, None)
    if fn is None:
        return None
    try:
        return fn(opens, highs, lows, closes)
    except Exception:
        logger.debug("TA-Lib %s failed", func_name, exc_info=True)
        return None


class TaLibPatternDetector(Detector):
    """Single-function TA-Lib pattern. Instantiated once per spec via
    ``build_talib_detectors()``; consumers usually call
    ``detect_all`` from ``detectors/__init__.py``."""

    def __init__(self, spec: Dict[str, str]) -> None:
        self.func = spec["func"]
        self.pattern = spec["slug"]
        self.direction = spec.get("direction", "either")

    def default_params(self) -> Dict[str, Any]:
        # TA-Lib's candlestick functions take no operator-tunable knobs
        # (the geometry is fixed in the C library) — surface
        # ``min_strength`` so operators can filter weak fires (|s| < 80)
        # without dropping the whole detector.
        return {
            "min_signal_strength": 0,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 5:
            return []
        if not _TALIB_AVAILABLE:
            return []
        try:
            bars = _lower_columns(bars)
            import numpy as np
            opens = bars["open"].astype(float).to_numpy()
            highs = bars["high"].astype(float).to_numpy()
            lows = bars["low"].astype(float).to_numpy()
            closes = bars["close"].astype(float).to_numpy()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        min_strength = int(p.get("min_signal_strength", 0))

        signals = _run_pattern(self.func, opens, highs, lows, closes)
        if signals is None:
            return []

        observations: List[Observation] = []
        timeframe = _bar_timeframe(bars)
        for i, sig in enumerate(signals):
            try:
                s = int(sig)
            except Exception:
                continue
            if s == 0:
                continue
            if min_strength > 0 and abs(s) < min_strength:
                continue
            ts = bars.index[i]
            try:
                ts_py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            except Exception:
                ts_py = ts
            features: Dict[str, Any] = {
                "talib_value": s,
                "direction_hint": ("bull" if s > 0 else "bear"),
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(closes[i]),
            }
            observations.append(Observation(
                ticker=ticker,
                pattern=self.pattern,
                timestamp=ts_py,
                timeframe=timeframe,
                regime=_classify_regime(bars, i),
                vol_state=_classify_vol_state(bars, i),
                time_bucket=_time_bucket(ts_py) if hasattr(ts_py, "hour") else "rth",
                spot=float(closes[i]),
                features=features,
            ))
        return observations


def build_talib_detectors() -> List[TaLibPatternDetector]:
    """Return one detector per TA-Lib spec. Safe to call even when
    TA-Lib isn't installed — the returned detectors just return []."""
    return [TaLibPatternDetector(spec) for spec in TALIB_PATTERN_SPECS]


def talib_available() -> bool:
    return bool(_TALIB_AVAILABLE)
