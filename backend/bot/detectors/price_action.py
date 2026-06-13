"""MITS Phase 0 — rule-of-thumb price action detectors.

The operator instruction (locked decision #2): "let the corpus decide".
We do NOT try to enshrine an academic Bull Flag definition here — we
pick a simple rule that fires on the geometry most retail traders call
a Bull Flag, then let the knowledge graph tell us whether it actually
has edge.

Eight detectors here:
  BullFlag, BearFlag, Pennant, Consolidation,
  Breakout, Pullback, FailedBreakout, FailedBreakdown

All return historical observations (every bar where the pattern fires).
No look-ahead — at index i we only consult bars[0..i].

MITS Phase 4 (P4.1): every detector exposes a ``default_params()``
dict and reads tunable knobs out of the ``params`` kwarg so operators
can adjust thresholds from the Configure modal without touching code.
"""
from __future__ import annotations

from typing import Any, Dict, List

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)


# ── shared helpers ────────────────────────────────────────────────────


def _safe_floats(bars):
    """Return (open, high, low, close) as float lists in one pass."""
    return (
        bars["open"].astype(float).tolist(),
        bars["high"].astype(float).tolist(),
        bars["low"].astype(float).tolist(),
        bars["close"].astype(float).tolist(),
    )


def _build_obs(ticker: str, bars, i: int, pattern: str,
                  features: Dict[str, Any], closes: List[float]) -> Observation:
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


# ── flag patterns ─────────────────────────────────────────────────────


class BullFlagDetector(Detector):
    """Bull flag: a strong upward thrust (>5% over ~5 bars) followed by
    a 3-7 bar consolidation where the range narrows (consolidation
    range < 50% of the thrust). Fires on the LAST bar of consolidation.
    """

    pattern = "bull_flag"

    def default_params(self) -> Dict[str, Any]:
        return {
            "min_thrust_pct": 0.05,
            "max_tightness_ratio": 0.50,
            "consolidation_bars": 5,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 12:
            return []
        bars = _lower_columns(bars)
        try:
            _, highs, lows, closes = _safe_floats(bars)
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        min_thrust = float(p.get("min_thrust_pct", 0.05))
        max_tight = float(p.get("max_tightness_ratio", 0.50))
        out: List[Observation] = []
        # i is the candidate end-of-flag bar.
        for i in range(11, len(bars)):
            # Thrust: closes[i-10] -> closes[i-5] up >= min_thrust.
            thrust_start = closes[i - 10]
            thrust_end = closes[i - 5]
            if thrust_start <= 0:
                continue
            thrust_pct = (thrust_end - thrust_start) / thrust_start
            if thrust_pct < min_thrust:
                continue
            # Consolidation: range of last 5 bars' (high - low) must be
            # narrower than the thrust range.
            cons_high = max(highs[i - 4:i + 1])
            cons_low = min(lows[i - 4:i + 1])
            cons_range = cons_high - cons_low
            thrust_range = max(highs[i - 10:i - 4]) - min(lows[i - 10:i - 4])
            if thrust_range <= 0 or cons_range <= 0:
                continue
            if cons_range > max_tight * thrust_range:
                continue
            # Consolidation must hold above the thrust midpoint (no deep retracement).
            thrust_mid = (thrust_start + thrust_end) / 2.0
            if cons_low < thrust_mid:
                continue
            out.append(_build_obs(ticker, bars, i, self.pattern, {
                "thrust_pct": round(thrust_pct, 4),
                "cons_range": round(cons_range, 4),
                "thrust_range": round(thrust_range, 4),
                "tightness": round(cons_range / thrust_range, 4),
            }, closes))
        return out


class BearFlagDetector(Detector):
    """Mirror of BullFlagDetector — downward thrust then sideways."""

    pattern = "bear_flag"

    def default_params(self) -> Dict[str, Any]:
        return {
            "min_thrust_pct": 0.05,
            "max_tightness_ratio": 0.50,
            "consolidation_bars": 5,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 12:
            return []
        bars = _lower_columns(bars)
        try:
            _, highs, lows, closes = _safe_floats(bars)
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        min_thrust = float(p.get("min_thrust_pct", 0.05))
        max_tight = float(p.get("max_tightness_ratio", 0.50))
        out: List[Observation] = []
        for i in range(11, len(bars)):
            thrust_start = closes[i - 10]
            thrust_end = closes[i - 5]
            if thrust_start <= 0:
                continue
            thrust_pct = (thrust_end - thrust_start) / thrust_start
            if thrust_pct > -min_thrust:
                continue
            cons_high = max(highs[i - 4:i + 1])
            cons_low = min(lows[i - 4:i + 1])
            cons_range = cons_high - cons_low
            thrust_range = max(highs[i - 10:i - 4]) - min(lows[i - 10:i - 4])
            if thrust_range <= 0 or cons_range <= 0:
                continue
            if cons_range > max_tight * thrust_range:
                continue
            thrust_mid = (thrust_start + thrust_end) / 2.0
            if cons_high > thrust_mid:
                continue
            out.append(_build_obs(ticker, bars, i, self.pattern, {
                "thrust_pct": round(thrust_pct, 4),
                "cons_range": round(cons_range, 4),
                "thrust_range": round(thrust_range, 4),
                "tightness": round(cons_range / thrust_range, 4),
            }, closes))
        return out


class PennantDetector(Detector):
    """Pennant — converging triangle after a thrust. We approximate
    by checking that the last 5 bars' rolling range is monotonically
    shrinking after a sharp move."""

    pattern = "pennant"

    def default_params(self) -> Dict[str, Any]:
        return {
            "min_thrust_pct": 0.04,
            "min_range_compression": 0.20,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 12:
            return []
        bars = _lower_columns(bars)
        try:
            _, highs, lows, closes = _safe_floats(bars)
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        min_thrust = float(p.get("min_thrust_pct", 0.04))
        min_compress = float(p.get("min_range_compression", 0.20))
        out: List[Observation] = []
        for i in range(11, len(bars)):
            thrust_pct = abs(closes[i - 5] - closes[i - 10]) / max(1e-9, closes[i - 10])
            if thrust_pct < min_thrust:
                continue
            r1 = highs[i - 4] - lows[i - 4]
            r2 = highs[i - 3] - lows[i - 3]
            r3 = highs[i - 2] - lows[i - 2]
            r4 = highs[i - 1] - lows[i - 1]
            r5 = highs[i] - lows[i]
            ranges = [r1, r2, r3, r4, r5]
            if any(r <= 0 for r in ranges):
                continue
            # Strictly shrinking is too rare on real bars; require
            # last < first AND average shrinkage > threshold.
            if r5 >= r1:
                continue
            if (r1 - r5) / r1 < min_compress:
                continue
            out.append(_build_obs(ticker, bars, i, self.pattern, {
                "thrust_pct": round(thrust_pct, 4),
                "range_compression": round(1.0 - (r5 / r1), 4),
            }, closes))
        return out


# ── consolidation / breakouts ────────────────────────────────────────


class ConsolidationDetector(Detector):
    """Tight sideways action — 10 bars where (high-low) is < 50% of
    the prior 30-bar median true-range."""

    pattern = "consolidation"

    def default_params(self) -> Dict[str, Any]:
        return {
            "tight_ratio": 0.5,
            "lookback_bars": 10,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 40:
            return []
        bars = _lower_columns(bars)
        try:
            _, highs, lows, closes = _safe_floats(bars)
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        tight = float(p.get("tight_ratio", 0.5))
        out: List[Observation] = []
        for i in range(39, len(bars)):
            recent_range = max(highs[i - 9:i + 1]) - min(lows[i - 9:i + 1])
            prior = []
            for j in range(i - 39, i - 9):
                prior.append(highs[j] - lows[j])
            if not prior:
                continue
            prior_sorted = sorted(prior)
            median = prior_sorted[len(prior_sorted) // 2]
            if median <= 0:
                continue
            if recent_range > tight * 10 * median:
                continue
            out.append(_build_obs(ticker, bars, i, self.pattern, {
                "recent_range": round(recent_range, 4),
                "median_bar_range": round(median, 4),
                "ratio": round(recent_range / (10 * median), 4),
            }, closes))
        return out


class BreakoutDetector(Detector):
    """Close breaks above the prior 20-bar high by at least 0.3% with
    expanding volume (last bar volume > 1.3x 20-bar median)."""

    pattern = "breakout"

    def default_params(self) -> Dict[str, Any]:
        return {
            "lookback_bars": 20,
            "min_breakout_pct": 0.003,
            "volume_multiplier": 1.3,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 22:
            return []
        bars = _lower_columns(bars)
        try:
            _, highs, lows, closes = _safe_floats(bars)
            volumes = bars["volume"].astype(float).tolist() if "volume" in bars.columns else None
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        lookback = int(p.get("lookback_bars", 20))
        min_break = float(p.get("min_breakout_pct", 0.003))
        vol_mult = float(p.get("volume_multiplier", 1.3))
        out: List[Observation] = []
        for i in range(lookback + 1, len(bars)):
            prior_high = max(highs[i - lookback:i])
            if prior_high <= 0:
                continue
            if closes[i] <= prior_high * (1.0 + min_break):
                continue
            vol_ok = True
            if volumes:
                window = sorted(volumes[i - lookback:i])
                med_vol = window[len(window) // 2] if window else 0.0
                vol_ok = volumes[i] >= vol_mult * max(1.0, med_vol)
            if not vol_ok:
                continue
            out.append(_build_obs(ticker, bars, i, self.pattern, {
                "prior_high": round(prior_high, 4),
                "breakout_pct": round((closes[i] - prior_high) / prior_high, 4),
            }, closes))
        return out


class PullbackDetector(Detector):
    """In an uptrend (10-bar SMA rising), a 2-bar dip of >1% with
    the close still above the 20-bar SMA."""

    pattern = "pullback"

    def default_params(self) -> Dict[str, Any]:
        return {
            "min_dip_pct": 0.01,
            "sma_window": 20,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 22:
            return []
        bars = _lower_columns(bars)
        try:
            _, _, _, closes = _safe_floats(bars)
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        min_dip = float(p.get("min_dip_pct", 0.01))
        out: List[Observation] = []
        for i in range(21, len(bars)):
            sma20 = sum(closes[i - 19:i + 1]) / 20.0
            sma10_now = sum(closes[i - 9:i + 1]) / 10.0
            sma10_prev = sum(closes[i - 19:i - 9]) / 10.0
            if sma10_now <= sma10_prev:
                continue
            if closes[i] < sma20:
                continue
            dip = (closes[i] - closes[i - 2]) / max(1e-9, closes[i - 2])
            if dip > -min_dip:
                continue
            out.append(_build_obs(ticker, bars, i, self.pattern, {
                "dip_pct": round(dip, 4),
                "sma20": round(sma20, 4),
            }, closes))
        return out


class FailedBreakoutDetector(Detector):
    """A breakout that closes back below the prior 20-bar high within
    3 bars. Fires on the bar that closes back below."""

    pattern = "failed_breakout"

    def default_params(self) -> Dict[str, Any]:
        return {
            "lookback_bars": 20,
            "min_breakout_pct": 0.003,
            "max_bars_to_fail": 3,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 22:
            return []
        bars = _lower_columns(bars)
        try:
            _, highs, _, closes = _safe_floats(bars)
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        lookback = int(p.get("lookback_bars", 20))
        min_break = float(p.get("min_breakout_pct", 0.003))
        max_bars = int(p.get("max_bars_to_fail", 3))
        out: List[Observation] = []
        for i in range(lookback + 1, len(bars)):
            # Look back up to ``max_bars`` for a breakout.
            for k in range(1, max_bars + 1):
                j = i - k
                if j < lookback:
                    continue
                prior_high = max(highs[j - lookback:j])
                if closes[j] <= prior_high * (1.0 + min_break):
                    continue
                # Did closes[i] revert below prior_high?
                if closes[i] < prior_high:
                    out.append(_build_obs(ticker, bars, i, self.pattern, {
                        "prior_high": round(prior_high, 4),
                        "bars_to_fail": k,
                    }, closes))
                    break
        return out


class FailedBreakdownDetector(Detector):
    """A breakdown that recovers above the prior 20-bar low within 3
    bars."""

    pattern = "failed_breakdown"

    def default_params(self) -> Dict[str, Any]:
        return {
            "lookback_bars": 20,
            "min_breakdown_pct": 0.003,
            "max_bars_to_recover": 3,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 22:
            return []
        bars = _lower_columns(bars)
        try:
            _, _, lows, closes = _safe_floats(bars)
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        lookback = int(p.get("lookback_bars", 20))
        min_down = float(p.get("min_breakdown_pct", 0.003))
        max_bars = int(p.get("max_bars_to_recover", 3))
        out: List[Observation] = []
        for i in range(lookback + 1, len(bars)):
            for k in range(1, max_bars + 1):
                j = i - k
                if j < lookback:
                    continue
                prior_low = min(lows[j - lookback:j])
                if closes[j] >= prior_low * (1.0 - min_down):
                    continue
                if closes[i] > prior_low:
                    out.append(_build_obs(ticker, bars, i, self.pattern, {
                        "prior_low": round(prior_low, 4),
                        "bars_to_recover": k,
                    }, closes))
                    break
        return out


def build_price_action_detectors() -> List[Detector]:
    return [
        BullFlagDetector(),
        BearFlagDetector(),
        PennantDetector(),
        ConsolidationDetector(),
        BreakoutDetector(),
        PullbackDetector(),
        FailedBreakoutDetector(),
        FailedBreakdownDetector(),
    ]
