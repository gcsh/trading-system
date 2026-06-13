"""MITS Phase 0 — Market structure detectors (BOS, CHOCH).

These are the "smart money concepts" pillars:

  BOS  — Break Of Structure: in an uptrend, price takes out the most
         recent swing-high → trend continuation. Mirror for downtrend.
  CHOCH — Change Of Character: in an uptrend, price takes out the most
          recent swing-low → potential trend reversal.

We define swing high/low as a fractal: a bar whose high (or low) is the
extreme of the surrounding 5-bar window. Look-ahead-safe because at the
time we recognise bar i as a swing, we only need bars[i-2..i+2] up to
the current index — but we only count a swing once both halves of the
window are in the past, so at index i we look at swings from i-2 and
earlier (no future info).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)


def _swing_points(highs: List[float], lows: List[float], up_to: int,
                       k: int = 2) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Return (swing_highs, swing_lows) as (idx, price) lists, only
    counting swings that fall fully before ``up_to`` (look-ahead safe).

    A swing-high at idx j requires highs[j] to be strictly greater than
    highs[j-k..j-1] and highs[j+1..j+k]. So we need j+k <= up_to.
    """
    sh: List[Tuple[int, float]] = []
    sl: List[Tuple[int, float]] = []
    for j in range(k, up_to - k + 1):
        h = highs[j]
        if all(h > highs[m] for m in range(j - k, j)) and all(
                h > highs[m] for m in range(j + 1, j + k + 1)):
            sh.append((j, h))
        l = lows[j]
        if all(l < lows[m] for m in range(j - k, j)) and all(
                l < lows[m] for m in range(j + 1, j + k + 1)):
            sl.append((j, l))
    return sh, sl


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


class BOSDetector(Detector):
    """Break of Structure — close takes out the most recent confirmed
    swing-high (uptrend BOS) or swing-low (downtrend BOS). One
    observation per BOS event; we throttle by tracking the last swing
    we broke so we don't fire on the same swing twice."""

    pattern = "bos"

    def default_params(self) -> Dict[str, Any]:
        return {
            "swing_fractal_k": 2,
            "min_break_pct": 0.0,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 10:
            return []
        bars = _lower_columns(bars)
        try:
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        k = int(p.get("swing_fractal_k", 2))
        min_break = float(p.get("min_break_pct", 0.0))
        out: List[Observation] = []
        last_broken_high_idx: Optional[int] = None
        last_broken_low_idx: Optional[int] = None
        for i in range(4, len(bars)):
            sh, sl = _swing_points(highs, lows, up_to=i, k=k)
            recent_sh = sh[-1] if sh else None
            recent_sl = sl[-1] if sl else None
            # Uptrend BOS: close above most-recent confirmed swing-high.
            if recent_sh and closes[i] > recent_sh[1] * (1.0 + min_break) and \
                  recent_sh[0] != last_broken_high_idx:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "up",
                    "swing_idx": recent_sh[0],
                    "swing_price": round(recent_sh[1], 4),
                    "close": round(closes[i], 4),
                }, closes))
                last_broken_high_idx = recent_sh[0]
            # Downtrend BOS: close below most-recent confirmed swing-low.
            elif recent_sl and closes[i] < recent_sl[1] * (1.0 - min_break) and \
                    recent_sl[0] != last_broken_low_idx:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "down",
                    "swing_idx": recent_sl[0],
                    "swing_price": round(recent_sl[1], 4),
                    "close": round(closes[i], 4),
                }, closes))
                last_broken_low_idx = recent_sl[0]
        return out


class CHOCHDetector(Detector):
    """Change of Character — first break of structure in the OPPOSITE
    direction of the prevailing trend. We approximate "prevailing
    trend" with 20-bar SMA slope: if slope was positive at the
    swing-low's index but the close breaks below that swing-low,
    that's a bearish CHOCH (and vice versa)."""

    pattern = "choch"

    def default_params(self) -> Dict[str, Any]:
        return {
            "swing_fractal_k": 2,
            "sma_window": 20,
            "min_slope": 0.0,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 30:
            return []
        bars = _lower_columns(bars)
        try:
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        k = int(p.get("swing_fractal_k", 2))
        sma_w = int(p.get("sma_window", 20))
        min_slope = float(p.get("min_slope", 0.0))
        out: List[Observation] = []
        last_fired_low_idx: Optional[int] = None
        last_fired_high_idx: Optional[int] = None
        for i in range(25, len(bars)):
            sh, sl = _swing_points(highs, lows, up_to=i, k=k)
            recent_sl = sl[-1] if sl else None
            recent_sh = sh[-1] if sh else None
            # Trend slope: 20-bar SMA at the swing index minus 20-bar SMA
            # five bars earlier — i.e. is the SMA itself rising or falling?
            if recent_sl and recent_sl[0] >= 25:
                j = recent_sl[0]
                sma_now = sum(closes[j - (sma_w - 1):j + 1]) / float(sma_w)
                sma_prev = sum(closes[j - (sma_w + 4):j - 4]) / float(sma_w)
                slope = sma_now - sma_prev
                if slope > min_slope and closes[i] < recent_sl[1] and \
                      recent_sl[0] != last_fired_low_idx:
                    out.append(_build_obs(ticker, bars, i, self.pattern, {
                        "direction": "bearish",
                        "swing_idx": recent_sl[0],
                        "swing_price": round(recent_sl[1], 4),
                    }, closes))
                    last_fired_low_idx = recent_sl[0]
                    continue
            if recent_sh and recent_sh[0] >= 25:
                j = recent_sh[0]
                sma_now = sum(closes[j - (sma_w - 1):j + 1]) / float(sma_w)
                sma_prev = sum(closes[j - (sma_w + 4):j - 4]) / float(sma_w)
                slope = sma_now - sma_prev
                if slope < -min_slope and closes[i] > recent_sh[1] and \
                      recent_sh[0] != last_fired_high_idx:
                    out.append(_build_obs(ticker, bars, i, self.pattern, {
                        "direction": "bullish",
                        "swing_idx": recent_sh[0],
                        "swing_price": round(recent_sh[1], 4),
                    }, closes))
                    last_fired_high_idx = recent_sh[0]
        return out


def build_market_structure_detectors() -> List[Detector]:
    return [BOSDetector(), CHOCHDetector()]
