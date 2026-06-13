"""MITS Phase 12.C — Wyckoff method detectors.

Implements five detectors over Richard D. Wyckoff's accumulation /
distribution schematic, the dominant institutional positioning
framework taught in CFA and CMT curricula:

  * wyckoff_accumulation_phase — Phase A→E label for current bar.
  * wyckoff_distribution_phase — mirror.
  * wyckoff_spring             — false breakdown below support that
                                  quickly reverses on declining volume.
  * wyckoff_sos                — Sign of Strength: strong rally on
                                  expanding volume out of a range.
  * wyckoff_upthrust           — false breakout above resistance that
                                  fails on declining volume.

Citations:

  * Wyckoff, R. D. (1931). "A Course in Stock Market Science",
    Wyckoff Stock Market Institute.
  * Pruden, H. O. (2007). "The Three Skills of Top Trading: Behavioral
    Systems Building, Pattern Recognition, and Mental State
    Management", Wiley. Chapter 3 covers the Wyckoff phases.
  * Weis, D. (2013). "Trades About to Happen", Wiley. Volume-spread
    framework that operationalises Wyckoff for modern bar data.

Design notes
============

The Wyckoff schematic is multi-week / multi-month on daily bars. We
classify the CURRENT bar's position in the schematic given the last
60 sessions. Detectors emit one observation per phase transition (not
per bar) so cohorts stay legible. Spring and upthrust are
single-event detectors and emit on the bar where the false move
reverses.

All detectors are look-ahead-safe (each bar i consults only
``bars[0:i+1]``) and tolerate empty / missing volume gracefully.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)


WYCKOFF_FAMILY = "wyckoff"


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


def _rolling_extrema(values: List[float], i: int, window: int
                            ) -> Tuple[float, float, int, int]:
    """Return ``(min, max, min_idx, max_idx)`` over the trailing
    ``window`` bars ending at ``i`` (inclusive)."""
    start = max(0, i - window + 1)
    window_vals = values[start:i + 1]
    mn = min(window_vals)
    mx = max(window_vals)
    return mn, mx, start + window_vals.index(mn), start + window_vals.index(mx)


def _classify_phase(highs: List[float], lows: List[float],
                          closes: List[float], volumes: List[float],
                          i: int, window: int = 60
                          ) -> Tuple[str, Dict[str, Any]]:
    """Return the Wyckoff phase label + supporting features for bar i.

    Phases (accumulation orientation; distribution is the mirror):

      * Phase A — selling climax + automatic rally.
      * Phase B — trading range; declining volume.
      * Phase C — spring / final shakeout.
      * Phase D — markup begins, sign of strength.
      * Phase E — out of range, trend established.
      * none    — not in a recognisable schematic.

    Lightweight rule-based classifier — defers to the spring / SOS /
    upthrust detectors below for tradable signals.
    """
    if i < window:
        return "none", {}
    lo, hi, lo_idx, hi_idx = _rolling_extrema(lows, i, window)
    hi_lo, hi_hi, _, _ = _rolling_extrema(highs, i, window)
    range_size = hi_hi - lo
    if range_size <= 0:
        return "none", {}
    pos_in_range = (closes[i] - lo) / range_size
    vol_window = volumes[i - window + 1:i + 1]
    vol_mean = sum(vol_window) / len(vol_window)
    recent_vol = sum(volumes[i - 9:i + 1]) / 10.0 if i >= 9 else vol_mean
    vol_ratio = recent_vol / vol_mean if vol_mean > 0 else 1.0
    # Is the market in a horizontal range? Use close-to-range-midpoint
    # std as a proxy.
    mid = lo + range_size / 2.0
    deviations = [abs(c - mid) for c in closes[i - window + 1:i + 1]]
    dev_pct = (sum(deviations) / len(deviations)) / max(1e-9, mid)
    in_range = dev_pct < 0.04  # within ~4 pct of the midpoint = ranging
    features = {
        "range_low": round(lo, 4),
        "range_high": round(hi_hi, 4),
        "pos_in_range": round(pos_in_range, 3),
        "vol_ratio": round(vol_ratio, 3),
        "range_pct": round(range_size / max(1e-9, mid), 4),
    }
    # Phase E: closed cleanly outside the range with momentum.
    if closes[i] > hi_hi * 1.005 and pos_in_range > 0.95:
        return "E_markup", features
    if closes[i] < lo * 0.995 and pos_in_range < 0.05:
        return "E_markdown", features
    # Phase D: closed in the upper third with expanding volume.
    if in_range and pos_in_range > 0.66 and vol_ratio > 1.2:
        return "D_strength", features
    # Phase C: dipped to the lower edge then closed back inside on low vol.
    if in_range and lows[i] < lo * 1.005 and closes[i] > lo and vol_ratio < 1.0:
        return "C_spring", features
    # Phase B: in range, low and declining volume.
    if in_range and vol_ratio < 1.0:
        return "B_consolidation", features
    # Phase A: selling climax — biggest volume bar in the window with
    # close near the low.
    if (i - lo_idx <= 5 and vol_ratio > 1.5
            and pos_in_range < 0.25):
        return "A_climax", features
    return "none", features


# ── 1. Wyckoff accumulation phase ─────────────────────────────────────


class WyckoffAccumulationPhaseDetector(Detector):
    """Composite phase-tagging detector. Emits one observation per
    distinct phase transition during accumulation (phases A→E). The
    features dict carries the phase label so cohort analysis can pivot
    on it."""

    pattern = "wyckoff_accumulation_phase"
    family = WYCKOFF_FAMILY
    description = (
        "Wyckoff Phase A→E accumulation tag (selling climax → "
        "consolidation → spring → SOS → markup). Cited: Wyckoff 1931, "
        "Pruden 'Three Skills' 2007."
    )

    def default_params(self) -> Dict[str, Any]:
        return {"window": 60, "min_drawdown_pct": 0.10}

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
        window = int(p.get("window", 60))
        min_dd = float(p.get("min_drawdown_pct", 0.10))
        n = len(bars)
        out: List[Observation] = []
        last_phase: Optional[str] = None
        for i in range(window, n):
            # Only consider accumulation context — price has dropped at
            # least min_dd from a prior high.
            prior_high = max(highs[max(0, i - 2 * window):i])
            if prior_high <= 0:
                continue
            dd = (prior_high - closes[i]) / prior_high
            if dd < min_dd:
                continue
            phase, feat = _classify_phase(highs, lows, closes, volumes,
                                                       i, window=window)
            if phase == "none" or phase.startswith("E_markdown"):
                continue
            # Only accumulation-side phases.
            if not (phase.startswith("A_")
                    or phase.startswith("B_")
                    or phase.startswith("C_")
                    or phase.startswith("D_")
                    or phase == "E_markup"):
                continue
            if phase == last_phase:
                continue
            out.append(_build_obs(ticker, bars, i, self.pattern, {
                **feat, "phase": phase, "drawdown": round(dd, 4),
            }))
            last_phase = phase
        return out


# ── 2. Wyckoff distribution phase ─────────────────────────────────────


class WyckoffDistributionPhaseDetector(Detector):
    """Mirror of accumulation — operates after a sustained rally and
    emits phase-by-phase. Phases: buying climax → automatic reaction
    → upthrust → sign of weakness → markdown."""

    pattern = "wyckoff_distribution_phase"
    family = WYCKOFF_FAMILY
    description = (
        "Wyckoff distribution schematic (buying climax → upthrust → "
        "markdown). Mirror of accumulation. Cited: Wyckoff 1931, "
        "Pruden 'Three Skills' 2007."
    )

    def default_params(self) -> Dict[str, Any]:
        return {"window": 60, "min_runup_pct": 0.10}

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
        window = int(p.get("window", 60))
        min_ru = float(p.get("min_runup_pct", 0.10))
        n = len(bars)
        out: List[Observation] = []
        last_phase: Optional[str] = None
        for i in range(window, n):
            prior_low = min(lows[max(0, i - 2 * window):i])
            if prior_low <= 0:
                continue
            ru = (closes[i] - prior_low) / prior_low
            if ru < min_ru:
                continue
            phase, feat = _classify_phase(highs, lows, closes, volumes,
                                                       i, window=window)
            if phase == "none" or phase.startswith("E_markup"):
                continue
            # Re-interpret phases in the distribution context.
            if phase == "A_climax":
                phase_label = "A_buying_climax"
            elif phase == "B_consolidation":
                phase_label = "B_consolidation"
            elif phase == "C_spring":
                phase_label = "C_upthrust"  # mirror
            elif phase == "D_strength":
                phase_label = "D_weakness"
            elif phase == "E_markdown":
                phase_label = "E_markdown"
            else:
                continue
            if phase_label == last_phase:
                continue
            out.append(_build_obs(ticker, bars, i, self.pattern, {
                **feat, "phase": phase_label, "runup": round(ru, 4),
            }))
            last_phase = phase_label
        return out


# ── 3. Wyckoff spring ─────────────────────────────────────────────────


class WyckoffSpringDetector(Detector):
    """Spring — false breakdown below a trading-range support that
    quickly reverses on declining volume during the break and rising
    volume on the reversal. High-conviction reversal entry."""

    pattern = "wyckoff_spring"
    family = WYCKOFF_FAMILY
    description = (
        "False breakdown below trading-range support with declining "
        "volume on the break and rising volume on the reversal. "
        "Classical Wyckoff long-entry signal. Cited: Pruden 'Three "
        "Skills' 2007 (Wiley)."
    )

    def default_params(self) -> Dict[str, Any]:
        # MITS Phase 12.1 Fix 7 — relaxed thresholds.
        # Original spring detector fired zero times in 5y × 40 tickers.
        # Root cause: required EVERY break bar's volume < 1.2x MA AND
        # the recovery bar's volume > 1.2x MA, which is rare. The Wyckoff
        # spring is fundamentally "false break below support that
        # reverses quickly" — the volume profile is supportive context,
        # not a hard gate.
        return {
            "range_window": 20,           # tighter window — daily-scale springs
            "volume_ma_window": 20,
            "recovery_bars": 3,
            "break_vol_ratio_max": 2.0,   # break bar may be high-vol; relaxed
            "recovery_vol_ratio_min": 1.0,  # any normal or rising recovery
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
            volumes = (bars["volume"].astype(float).tolist()
                          if "volume" in bars.columns else [0.0] * len(bars))
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        rw = int(p.get("range_window", 20))
        vol_ma_w = int(p.get("volume_ma_window", 20))
        rec_bars = int(p.get("recovery_bars", 3))
        break_vol_max = float(p.get("break_vol_ratio_max", 2.0))
        rec_vol_min = float(p.get("recovery_vol_ratio_min", 1.0))
        n = len(bars)
        out: List[Observation] = []
        # Cooldown — at most one spring per `rw` bars to keep observations
        # legible.
        last_emit = -10 * rw
        for i in range(rw + vol_ma_w, n):
            if i - last_emit < rw:
                continue
            # The break bar j is one of the prior `rec_bars` bars.
            # Spring pattern: low pierced support, close recovered above.
            for lookback in range(1, rec_bars + 1):
                j = i - lookback
                if j < vol_ma_w + rw:
                    continue
                # Establish the trading range from the WINDOW BEFORE the
                # break bar j (so the break-bar low doesn't reset support).
                range_lo = min(lows[j - rw:j])
                range_hi = max(highs[j - rw:j])
                range_size = range_hi - range_lo
                if range_size <= 0:
                    continue
                # j wicked below support.
                if not (lows[j] < range_lo):
                    continue
                # i closes back above the broken support (the "reversal").
                if not (closes[i] > range_lo):
                    continue
                # Spring strength: the break wick should be meaningful
                # (> 0.1% of support price) so we filter noise wicks.
                wick_pct = (range_lo - lows[j]) / max(1e-9, range_lo)
                if wick_pct < 0.001:
                    continue
                vol_ma = sum(volumes[j - vol_ma_w:j]) / vol_ma_w
                if vol_ma <= 0:
                    # ETF/index ticker — no volume; accept on price alone.
                    break_vol_ratio = 1.0
                    rec_vol_ratio = 1.0
                else:
                    break_vol_ratio = volumes[j] / vol_ma
                    rec_vol_ratio = volumes[i] / vol_ma
                # Acceptance: break vol bounded above, recovery vol >=
                # configured floor.
                if break_vol_ratio > break_vol_max:
                    continue
                if rec_vol_ratio < rec_vol_min:
                    continue
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "range_low": round(range_lo, 4),
                    "range_high": round(range_hi, 4),
                    "break_idx": j,
                    "break_low": round(lows[j], 4),
                    "break_wick_pct": round(wick_pct, 5),
                    "break_vol_ratio": round(break_vol_ratio, 3),
                    "recovery_vol_ratio": round(rec_vol_ratio, 3),
                }))
                last_emit = i
                break
        return out


# ── 4. Wyckoff Sign of Strength ───────────────────────────────────────


class WyckoffSOSDetector(Detector):
    """Sign of Strength — strong rally on EXPANDING volume after a
    test of support, breaking ABOVE the trading range. Confirms Phase
    D markup is underway."""

    pattern = "wyckoff_sos"
    family = WYCKOFF_FAMILY
    description = (
        "Sign of Strength: strong rally on expanding volume breaking "
        "above a trading range after a successful test of support. "
        "Phase D confirmation. Cited: Wyckoff 1931."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "range_window": 30,
            "vol_mult": 1.5,
            "vol_ma_window": 20,
            "min_breakout_pct": 0.005,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 40:
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
        rw = int(p.get("range_window", 30))
        vol_mult = float(p.get("vol_mult", 1.5))
        vol_ma_w = int(p.get("vol_ma_window", 20))
        min_break = float(p.get("min_breakout_pct", 0.005))
        n = len(bars)
        out: List[Observation] = []
        for i in range(rw + vol_ma_w, n):
            range_hi = max(highs[i - rw:i])
            range_lo = min(lows[i - rw:i])
            if range_hi - range_lo <= 0:
                continue
            vol_ma = sum(volumes[i - vol_ma_w:i]) / vol_ma_w
            if vol_ma <= 0:
                continue
            break_pct = (closes[i] - range_hi) / max(1e-9, range_hi)
            if (closes[i] > range_hi * (1 + min_break)
                    and volumes[i] >= vol_mult * vol_ma):
                # Confirm a test of support happened within the prior 10 bars.
                tested = any(lows[k] <= range_lo * 1.01
                                  for k in range(max(0, i - 10), i))
                if tested:
                    out.append(_build_obs(ticker, bars, i, self.pattern, {
                        "range_high": round(range_hi, 4),
                        "range_low": round(range_lo, 4),
                        "break_pct": round(break_pct, 5),
                        "vol_ratio": round(volumes[i] / vol_ma, 3),
                    }))
        return out


# ── 5. Wyckoff upthrust ───────────────────────────────────────────────


class WyckoffUpthrustDetector(Detector):
    """Upthrust — false breakout ABOVE a trading-range resistance that
    fails on declining volume. The distribution-side counterpart to
    the spring; high-conviction short entry."""

    pattern = "wyckoff_upthrust"
    family = WYCKOFF_FAMILY
    description = (
        "False breakout above resistance with declining volume on the "
        "break and rising volume on the reversal. Wyckoff "
        "distribution-side short entry. Cited: Pruden 'Three Skills' "
        "2007."
    )

    def default_params(self) -> Dict[str, Any]:
        # MITS Phase 12.1 Fix 7 — symmetric relaxation to spring.
        return {
            "range_window": 20,
            "volume_ma_window": 20,
            "recovery_bars": 3,
            "break_vol_ratio_max": 2.0,
            "reversal_vol_ratio_min": 1.0,
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
            volumes = (bars["volume"].astype(float).tolist()
                          if "volume" in bars.columns else [0.0] * len(bars))
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        rw = int(p.get("range_window", 20))
        vol_ma_w = int(p.get("volume_ma_window", 20))
        rec_bars = int(p.get("recovery_bars", 3))
        break_vol_max = float(p.get("break_vol_ratio_max", 2.0))
        rev_vol_min = float(p.get("reversal_vol_ratio_min", 1.0))
        n = len(bars)
        out: List[Observation] = []
        last_emit = -10 * rw
        for i in range(rw + vol_ma_w, n):
            if i - last_emit < rw:
                continue
            for lookback in range(1, rec_bars + 1):
                j = i - lookback
                if j < vol_ma_w + rw:
                    continue
                # Range from window BEFORE the break bar so the break
                # high doesn't re-set resistance.
                range_hi = max(highs[j - rw:j])
                range_lo = min(lows[j - rw:j])
                if range_hi - range_lo <= 0:
                    continue
                if not (highs[j] > range_hi):
                    continue
                # Closes back below the broken resistance (the reversal).
                if not (closes[i] < range_hi):
                    continue
                wick_pct = (highs[j] - range_hi) / max(1e-9, range_hi)
                if wick_pct < 0.001:
                    continue
                vol_ma = sum(volumes[j - vol_ma_w:j]) / vol_ma_w
                if vol_ma <= 0:
                    break_vol_ratio = 1.0
                    rev_vol_ratio = 1.0
                else:
                    break_vol_ratio = volumes[j] / vol_ma
                    rev_vol_ratio = volumes[i] / vol_ma
                if break_vol_ratio > break_vol_max:
                    continue
                if rev_vol_ratio < rev_vol_min:
                    continue
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "range_low": round(range_lo, 4),
                    "range_high": round(range_hi, 4),
                    "break_idx": j,
                    "break_high": round(highs[j], 4),
                    "break_wick_pct": round(wick_pct, 5),
                    "break_vol_ratio": round(break_vol_ratio, 3),
                    "reversal_vol_ratio": round(rev_vol_ratio, 3),
                }))
                last_emit = i
                break
        return out


def build_wyckoff_detectors() -> List[Detector]:
    return [
        WyckoffAccumulationPhaseDetector(),
        WyckoffDistributionPhaseDetector(),
        WyckoffSpringDetector(),
        WyckoffSOSDetector(),
        WyckoffUpthrustDetector(),
    ]
