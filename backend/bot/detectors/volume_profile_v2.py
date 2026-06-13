"""MITS Phase 12.D — Volume Profile v2 detectors.

Replaces the legacy ``hvn_acceptance`` and ``lvn_rejection`` detectors
(both at negative edge in the audit) with a proper Market Profile
implementation.

Three detectors:

  * poc_retest          — Point of Control retest after a >=1 ATR move
                          away from it. The fair-value snapback trade.
  * value_area_rejection — Price tests VAH or VAL (the 70 percent
                          value-area edges) and is rejected with a
                          reversal candle. The institutional fade.
  * composite_value_area — Multi-period overlap: when 5d / 20d / 60d
                          value areas all share a common slice and
                          price sits inside it, we tag the bar as
                          high-confluence fair value.

Citations:

  * Steidlmayer, J. P. (1989). "Steidlmayer on Markets — Trading with
    Market Profile" (Wiley). Foundational text on POC / VAH / VAL.
  * Dalton, J. F. et al. (1990). "Mind Over Markets — Power Trading
    with Market-Generated Information" (Probus). Practical playbook
    for value-area trades.
  * Hawkins, J. (2003). "Steidlmayer on Markets: Trading with Market
    Profile" 2nd ed. (Wiley). Refined POC computation.

Implementation
==============

Volume Profile = histogram of volume distributed across price levels.
A typical session has thousands of ticks; we work from OHLCV bars so we
distribute each bar's volume across the [low, high] range proportional
to a uniform density (matches what TPO-based profile vendors emit when
fed bar data, and what TradingView's Volume Profile indicator does).

* POC (Point of Control) = price level with the most volume.
* Value Area = contiguous range around the POC that contains 70 percent
  of total volume (configurable). VAH / VAL = the high / low edges.

Both daily and intraday bars supported. Bin count defaults to 50 — a
balance between resolution and bin-size noise on small ranges. We
recompute the profile in a rolling 20-bar window for poc_retest and
value_area_rejection; the composite detector uses 5 / 20 / 60.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)


VOLUME_PROFILE_V2_FAMILY = "volume_profile_v2"


def _build_obs(ticker: str, bars, i: int, pattern: str,
                  features: Dict[str, Any]) -> Observation:
    closes = bars["close"].astype(float).tolist()
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


def _compute_profile(highs: List[float], lows: List[float],
                          volumes: List[float],
                          start: int, end_exclusive: int,
                          bins: int = 50,
                          value_area_pct: float = 0.70,
                          ) -> Optional[Tuple[float, float, float, List[float],
                                                       float]]:
    """Compute (POC, VAL, VAH, histogram, bin_size) for bars in
    [start, end_exclusive).

    Volume per bar is distributed uniformly across [low, high] then
    binned. Returns None when the window has zero range or zero
    aggregate volume."""
    if end_exclusive - start <= 0:
        return None
    window_low = min(lows[start:end_exclusive])
    window_high = max(highs[start:end_exclusive])
    rng = window_high - window_low
    if rng <= 0:
        return None
    bin_size = rng / bins
    if bin_size <= 0:
        return None
    histogram = [0.0] * bins
    for k in range(start, end_exclusive):
        bar_rng = highs[k] - lows[k]
        if bar_rng <= 0:
            # Single-point bar: dump everything in one bin.
            b = min(bins - 1, max(0, int((closes_safe := highs[k]) - window_low) // 1))
            # Use proportional placement.
            idx = int((highs[k] - window_low) / bin_size)
            idx = min(bins - 1, max(0, idx))
            histogram[idx] += volumes[k]
            continue
        density = volumes[k] / bar_rng
        lo_idx = int((lows[k] - window_low) / bin_size)
        hi_idx = int((highs[k] - window_low) / bin_size)
        lo_idx = max(0, lo_idx)
        hi_idx = min(bins - 1, hi_idx)
        for b in range(lo_idx, hi_idx + 1):
            bin_low = window_low + b * bin_size
            bin_high = bin_low + bin_size
            overlap = max(0.0, min(bin_high, highs[k])
                                     - max(bin_low, lows[k]))
            histogram[b] += density * overlap
    total_vol = sum(histogram)
    if total_vol <= 0:
        return None
    poc_bin = max(range(bins), key=lambda b: histogram[b])
    poc_price = window_low + (poc_bin + 0.5) * bin_size
    # Value-area expansion outward from POC until 70 percent vol covered.
    target = total_vol * value_area_pct
    accumulated = histogram[poc_bin]
    lo_b = hi_b = poc_bin
    while accumulated < target and (lo_b > 0 or hi_b < bins - 1):
        # Expand on the side with more volume.
        next_lo = histogram[lo_b - 1] if lo_b > 0 else -1.0
        next_hi = histogram[hi_b + 1] if hi_b < bins - 1 else -1.0
        if next_hi >= next_lo and hi_b < bins - 1:
            hi_b += 1
            accumulated += histogram[hi_b]
        elif lo_b > 0:
            lo_b -= 1
            accumulated += histogram[lo_b]
        else:
            break
    val = window_low + lo_b * bin_size
    vah = window_low + (hi_b + 1) * bin_size
    return poc_price, val, vah, histogram, bin_size


# ── 1. POC retest ─────────────────────────────────────────────────────


class POCRetestDetector(Detector):
    """POC retest — after price moves >=1 ATR away from the trailing
    20-day POC, emit when it returns to within ``retest_tolerance_pct``
    of the POC."""

    pattern = "poc_retest"
    family = VOLUME_PROFILE_V2_FAMILY
    description = (
        "Point of Control retest after a >=1 ATR excursion. The "
        "fair-value snapback. Cited: Steidlmayer 1989, Dalton 'Mind "
        "Over Markets' 1990."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "window": 20,
            "atr_window": 14,
            "min_excursion_atr": 1.0,
            "retest_tolerance_pct": 0.002,
            "bins": 50,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 35:
            return []
        bars = _lower_columns(bars)
        try:
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
            volumes = (bars["volume"].astype(float).tolist()
                          if "volume" in bars.columns else [0.0] * len(bars))
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        window = int(p.get("window", 20))
        atr_w = int(p.get("atr_window", 14))
        min_excursion = float(p.get("min_excursion_atr", 1.0))
        tol_pct = float(p.get("retest_tolerance_pct", 0.002))
        bins = int(p.get("bins", 50))
        n = len(bars)
        out: List[Observation] = []
        last_excursion_idx = -10
        last_emit_idx = -10
        for i in range(window + atr_w, n):
            profile = _compute_profile(
                highs, lows, volumes, i - window, i, bins=bins,
            )
            if profile is None:
                continue
            poc, _val, _vah, _hist, _bs = profile
            # ATR for excursion measurement.
            trs = []
            for j in range(i - atr_w + 1, i + 1):
                tr = max(highs[j] - lows[j],
                              abs(highs[j] - closes[j - 1]),
                              abs(lows[j] - closes[j - 1]))
                trs.append(tr)
            atr = sum(trs) / max(1, len(trs))
            if atr <= 0:
                continue
            # Track excursion — last bar where |close - poc| >= 1 ATR.
            if abs(closes[i] - poc) >= min_excursion * atr:
                last_excursion_idx = i
                continue
            # Retest — close within tol_pct of POC AND we had an excursion
            # within the last 10 bars.
            if abs(closes[i] - poc) / max(1e-9, poc) <= tol_pct \
                  and i - last_excursion_idx <= 10 \
                  and last_excursion_idx > 0 \
                  and i - last_emit_idx >= 5:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "poc": round(poc, 4),
                    "spot": round(closes[i], 4),
                    "atr": round(atr, 4),
                    "excursion_lag_bars": i - last_excursion_idx,
                }))
                last_emit_idx = i
        return out


# ── 2. Value-area rejection ───────────────────────────────────────────


class ValueAreaRejectionDetector(Detector):
    """VAH / VAL rejection — emit when price tests VAH or VAL from the
    inside and is rejected with a reversal candle (close back inside
    the value area, in the OPPOSITE direction of the test)."""

    pattern = "value_area_rejection"
    family = VOLUME_PROFILE_V2_FAMILY
    description = (
        "Reversal candle at VAH or VAL — institutional fade of the "
        "value-area boundary. Replaces lvn_rejection (-2.9pp). Cited: "
        "Dalton 'Mind Over Markets' 1990."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "window": 20,
            "tolerance_pct": 0.001,
            "bins": 50,
            "value_area_pct": 0.70,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 30:
            return []
        bars = _lower_columns(bars)
        try:
            opens = bars["open"].astype(float).tolist()
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
            volumes = (bars["volume"].astype(float).tolist()
                          if "volume" in bars.columns else [0.0] * len(bars))
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        window = int(p.get("window", 20))
        tol = float(p.get("tolerance_pct", 0.001))
        bins = int(p.get("bins", 50))
        va_pct = float(p.get("value_area_pct", 0.70))
        n = len(bars)
        out: List[Observation] = []
        for i in range(window, n):
            profile = _compute_profile(
                highs, lows, volumes, i - window, i, bins=bins,
                value_area_pct=va_pct,
            )
            if profile is None:
                continue
            poc, val, vah, _, _ = profile
            # Bearish rejection at VAH: bar wicked above VAH, closed back
            # below VAH, and closed below open.
            if (highs[i] >= vah * (1 - tol)
                    and closes[i] < vah
                    and closes[i] < opens[i]):
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "bearish",
                    "vah": round(vah, 4),
                    "val": round(val, 4),
                    "poc": round(poc, 4),
                    "test_high": round(highs[i], 4),
                }))
                continue
            # Bullish rejection at VAL.
            if (lows[i] <= val * (1 + tol)
                    and closes[i] > val
                    and closes[i] > opens[i]):
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "bullish",
                    "vah": round(vah, 4),
                    "val": round(val, 4),
                    "poc": round(poc, 4),
                    "test_low": round(lows[i], 4),
                }))
        return out


# ── 3. Composite value area ───────────────────────────────────────────


class CompositeValueAreaDetector(Detector):
    """Composite VA — emit when price sits inside the overlap region of
    the 5d, 20d, and 60d value areas. High-confluence fair-value zone
    institutional desks treat as the "trade-the-range" comfort zone."""

    pattern = "composite_value_area"
    family = VOLUME_PROFILE_V2_FAMILY
    description = (
        "Multi-period value-area overlap (5d ∩ 20d ∩ 60d). Replaces "
        "hvn_acceptance (-1.7pp). Cited: Dalton 'Markets in Profile' "
        "2007."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "short_window": 5,
            "med_window": 20,
            "long_window": 60,
            "bins": 50,
            "value_area_pct": 0.70,
            "emit_throttle_bars": 5,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 70:
            return []
        bars = _lower_columns(bars)
        try:
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
            volumes = (bars["volume"].astype(float).tolist()
                          if "volume" in bars.columns else [0.0] * len(bars))
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        sw = int(p.get("short_window", 5))
        mw = int(p.get("med_window", 20))
        lw = int(p.get("long_window", 60))
        bins = int(p.get("bins", 50))
        va_pct = float(p.get("value_area_pct", 0.70))
        throttle = int(p.get("emit_throttle_bars", 5))
        n = len(bars)
        out: List[Observation] = []
        last_emit = -throttle
        for i in range(lw + 1, n):
            short = _compute_profile(highs, lows, volumes, i - sw, i,
                                                bins=bins, value_area_pct=va_pct)
            med = _compute_profile(highs, lows, volumes, i - mw, i,
                                             bins=bins, value_area_pct=va_pct)
            long_ = _compute_profile(highs, lows, volumes, i - lw, i,
                                               bins=bins, value_area_pct=va_pct)
            if not (short and med and long_):
                continue
            _, val_s, vah_s, _, _ = short
            _, val_m, vah_m, _, _ = med
            _, val_l, vah_l, _, _ = long_
            overlap_low = max(val_s, val_m, val_l)
            overlap_high = min(vah_s, vah_m, vah_l)
            if overlap_high <= overlap_low:
                continue
            if not (overlap_low <= closes[i] <= overlap_high):
                continue
            if i - last_emit < throttle:
                continue
            out.append(_build_obs(ticker, bars, i, self.pattern, {
                "overlap_low": round(overlap_low, 4),
                "overlap_high": round(overlap_high, 4),
                "spot": round(closes[i], 4),
                "vah_5d": round(vah_s, 4),
                "val_5d": round(val_s, 4),
                "vah_20d": round(vah_m, 4),
                "val_20d": round(val_m, 4),
                "vah_60d": round(vah_l, 4),
                "val_60d": round(val_l, 4),
            }))
            last_emit = i
        return out


def build_volume_profile_v2_detectors() -> List[Detector]:
    return [
        POCRetestDetector(),
        ValueAreaRejectionDetector(),
        CompositeValueAreaDetector(),
    ]
