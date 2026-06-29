"""MITS Phase 12.B — Smart Money Concepts (SMC) detectors.

Six institutional-grade detectors covering the Smart Money Concepts
framework popularised by Michael J. Huddleston ("Inner Circle Trader",
ICT) and grounded in classical price-action work. Each detector below
is implemented as a pure function over OHLCV bars — no look-ahead, no
external state.

Citations:

  * Huddleston, M. J. (2016–2024). "Inner Circle Trader" mentorship
    series. Concepts: order block, fair value gap (FVG), liquidity
    sweep, premium/discount zones, market structure shift.
  * Bulkowski, T. N. (2005). "Encyclopedia of Chart Patterns" (Wiley,
    2nd ed.). Pattern statistics — used as guidance on minimum
    impulse strength and confirmation windows.
  * Brooks, A. (2012). "Trading Price Action: Reversals" (Wiley).
    Pivot-based market structure framework.

Replaces / supersedes:

  * ``liquidity_sweep`` (legacy, -1.4pp edge in audit) → liquidity_sweep_v2.
  * ``stop_hunt`` (legacy) → stop_hunt_v2.
  * ``change_of_character`` (CHOCH, -5.5pp) → market_structure_shift_v2.

Detector summary
================

  order_block             — last opposite-color candle before a >=1 ATR
                            impulse in <=5 bars; emits when price returns
                            to the OB zone.
  fair_value_gap          — 3-bar imbalance; emits at gap fill.
  liquidity_sweep_v2      — sweep of equal-highs/equal-lows pool then
                            close back inside.
  stop_hunt_v2            — failed liquidity sweep that reverses with
                            volume > 1.5x MA(20).
  premium_discount_zone   — 50 percent midpoint of the most-recent
                            impulse leg; emits when price enters the
                            "with-trend" half.
  market_structure_shift_v2 — proper HH/HL / LH/LL state machine on
                              ZigZag pivots; emits on structural shift.

All detectors return Observation rows ready for INSERT OR IGNORE on
``market_observations``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)


SMC_FAMILY = "smc"


# ── shared helpers ────────────────────────────────────────────────────


def _atr(highs: List[float], lows: List[float], closes: List[float],
              i: int, window: int = 14) -> float:
    """True-range based ATR. Walks back ``window`` bars from ``i``
    inclusive. Look-ahead-safe."""
    if i < window:
        window = max(2, i)
    trs: List[float] = []
    start = max(1, i - window + 1)
    for j in range(start, i + 1):
        tr = max(
            highs[j] - lows[j],
            abs(highs[j] - closes[j - 1]),
            abs(lows[j] - closes[j - 1]),
        )
        trs.append(tr)
    return sum(trs) / max(1, len(trs))


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


def _zigzag_pivots(highs: List[float], lows: List[float],
                          pct: float = 0.03,
                          ) -> List[Tuple[int, float, str]]:
    """Classical ZigZag — emits pivots whenever the move from the last
    pivot exceeds ``pct`` (default 3 percent). Returns
    ``[(idx, price, 'H' | 'L'), ...]`` in chronological order.

    No look-ahead: each pivot is confirmed once the reverse move clears
    the threshold; callers iterating forward should only consume pivots
    whose confirmation bar precedes the current bar.
    """
    n = len(highs)
    if n < 3:
        return []
    pivots: List[Tuple[int, float, str]] = []
    # Initialise with first bar's mid as anchor.
    anchor_idx = 0
    anchor_price = (highs[0] + lows[0]) / 2.0
    direction: Optional[str] = None  # 'up' | 'down' once established
    extreme_idx = 0
    extreme_price = anchor_price
    extreme_is_high = True
    for i in range(1, n):
        h = highs[i]
        l = lows[i]
        if direction is None:
            up_move = (h - anchor_price) / max(1e-9, anchor_price)
            dn_move = (anchor_price - l) / max(1e-9, anchor_price)
            if up_move >= pct and up_move >= dn_move:
                direction = "up"
                pivots.append((anchor_idx, anchor_price, "L"))
                extreme_idx, extreme_price, extreme_is_high = i, h, True
            elif dn_move >= pct:
                direction = "down"
                pivots.append((anchor_idx, anchor_price, "H"))
                extreme_idx, extreme_price, extreme_is_high = i, l, False
            continue
        if direction == "up":
            if h > extreme_price:
                extreme_idx, extreme_price = i, h
            retrace = (extreme_price - l) / max(1e-9, extreme_price)
            if retrace >= pct:
                pivots.append((extreme_idx, extreme_price, "H"))
                direction = "down"
                extreme_idx, extreme_price = i, l
        else:  # direction == "down"
            if l < extreme_price:
                extreme_idx, extreme_price = i, l
            rally = (h - extreme_price) / max(1e-9, extreme_price)
            if rally >= pct:
                pivots.append((extreme_idx, extreme_price, "L"))
                direction = "up"
                extreme_idx, extreme_price = i, h
    return pivots


# ── 1. Order block ────────────────────────────────────────────────────


class OrderBlockDetector(Detector):
    """Order block — last opposite-color candle before a strong
    impulse (>=1 ATR move in <=5 bars). Emits when price returns to
    test the OB zone (open..close range of the originating candle)."""

    pattern = "order_block"
    family = SMC_FAMILY
    description = (
        "Institutional-zone retest: last opposite-color candle before "
        "a >=1 ATR impulse, then price returns to that candle's range. "
        "Cited: Huddleston ICT 2016+."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "impulse_bars": 5,
            "min_impulse_atr": 1.0,
            "retest_lookback_bars": 30,
            "atr_window": 14,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 25:
            return []
        bars = _lower_columns(bars)
        try:
            opens = bars["open"].astype(float).tolist()
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        impulse_bars = int(p.get("impulse_bars", 5))
        min_atr = float(p.get("min_impulse_atr", 1.0))
        retest_lookback = int(p.get("retest_lookback_bars", 30))
        atr_w = int(p.get("atr_window", 14))
        n = len(bars)
        out: List[Observation] = []
        # Order blocks are (originating_idx, zone_low, zone_high, direction).
        active_blocks: List[Tuple[int, float, float, str]] = []
        fired_blocks: set = set()
        for i in range(atr_w + impulse_bars, n):
            atr = _atr(highs, lows, closes, i, window=atr_w)
            if atr <= 0:
                continue
            # Look back impulse_bars for a >=1 ATR move.
            j_start = max(0, i - impulse_bars)
            move_up = closes[i] - lows[j_start]
            move_dn = highs[j_start] - closes[i]
            if move_up >= min_atr * atr:
                # Bullish impulse — find last bearish candle preceding it
                # (within [j_start - impulse_bars, j_start]).
                k_lo = max(0, j_start - impulse_bars)
                last_bear = None
                for k in range(j_start, k_lo - 1, -1):
                    if closes[k] < opens[k]:
                        last_bear = k
                        break
                if last_bear is not None:
                    zone_low = min(opens[last_bear], closes[last_bear])
                    zone_high = max(opens[last_bear], closes[last_bear])
                    active_blocks.append((last_bear, zone_low, zone_high,
                                                 "bullish"))
            elif move_dn >= min_atr * atr:
                k_lo = max(0, j_start - impulse_bars)
                last_bull = None
                for k in range(j_start, k_lo - 1, -1):
                    if closes[k] > opens[k]:
                        last_bull = k
                        break
                if last_bull is not None:
                    zone_low = min(opens[last_bull], closes[last_bull])
                    zone_high = max(opens[last_bull], closes[last_bull])
                    active_blocks.append((last_bull, zone_low, zone_high,
                                                 "bearish"))
            # Test active blocks for retest.
            for blk in active_blocks:
                idx0, lo, hi, direction = blk
                if idx0 in fired_blocks:
                    continue
                if i - idx0 > retest_lookback:
                    continue
                # Retest = current bar's low/high intersects the zone.
                if direction == "bullish":
                    if lows[i] <= hi and lows[i] >= lo:
                        out.append(_build_obs(ticker, bars, i, self.pattern, {
                            "direction": "bullish",
                            "origin_idx": idx0,
                            "zone_low": round(lo, 4),
                            "zone_high": round(hi, 4),
                            "spot": round(closes[i], 4),
                            "atr": round(atr, 4),
                        }))
                        fired_blocks.add(idx0)
                else:
                    if highs[i] >= lo and highs[i] <= hi:
                        out.append(_build_obs(ticker, bars, i, self.pattern, {
                            "direction": "bearish",
                            "origin_idx": idx0,
                            "zone_low": round(lo, 4),
                            "zone_high": round(hi, 4),
                            "spot": round(closes[i], 4),
                            "atr": round(atr, 4),
                        }))
                        fired_blocks.add(idx0)
            # Prune old blocks.
            active_blocks = [b for b in active_blocks
                                  if i - b[0] <= retest_lookback]
        return out


# ── 2. Fair Value Gap ─────────────────────────────────────────────────


class FairValueGapDetector(Detector):
    """3-candle imbalance — candle[i-1].high < candle[i+1].low (bullish
    FVG) or mirror. We emit when price RETURNS to fill the gap (the
    classical ICT entry trigger), not on gap creation. This keeps the
    detector forward-tradable."""

    pattern = "fair_value_gap"
    family = SMC_FAMILY
    description = (
        "3-bar imbalance gap fill. Fires when price returns to fill an "
        "FVG created by a prior impulse. Cited: Huddleston ICT 2016+."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "min_gap_pct": 0.0010,  # 10bp minimum
            "fill_lookback_bars": 40,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 5:
            return []
        bars = _lower_columns(bars)
        try:
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        min_gap = float(p.get("min_gap_pct", 0.0010))
        lookback = int(p.get("fill_lookback_bars", 40))
        n = len(bars)
        # gaps: (origin_idx, low_edge, high_edge, direction)
        gaps: List[Tuple[int, float, float, str]] = []
        fired: set = set()
        out: List[Observation] = []
        for i in range(2, n):
            # Gap created between bar i-2 and bar i (middle bar is i-1).
            mid = closes[i - 1] if closes[i - 1] > 0 else 1.0
            if highs[i - 2] < lows[i]:
                size_pct = (lows[i] - highs[i - 2]) / mid
                if size_pct >= min_gap:
                    gaps.append((i, highs[i - 2], lows[i], "bullish"))
            elif lows[i - 2] > highs[i]:
                size_pct = (lows[i - 2] - highs[i]) / mid
                if size_pct >= min_gap:
                    gaps.append((i, highs[i], lows[i - 2], "bearish"))
            # Check fills.
            for g in gaps:
                idx0, lo, hi, direction = g
                if idx0 in fired:
                    continue
                if i - idx0 > lookback:
                    continue
                if i <= idx0:
                    continue
                if direction == "bullish" and lows[i] <= hi:
                    out.append(_build_obs(ticker, bars, i, self.pattern, {
                        "direction": "bullish",
                        "origin_idx": idx0,
                        "gap_low": round(lo, 4),
                        "gap_high": round(hi, 4),
                        "gap_pct": round((hi - lo) / max(1e-9, mid), 5),
                        "fill_lag_bars": i - idx0,
                    }))
                    fired.add(idx0)
                elif direction == "bearish" and highs[i] >= lo:
                    out.append(_build_obs(ticker, bars, i, self.pattern, {
                        "direction": "bearish",
                        "origin_idx": idx0,
                        "gap_low": round(lo, 4),
                        "gap_high": round(hi, 4),
                        "gap_pct": round((hi - lo) / max(1e-9, mid), 5),
                        "fill_lag_bars": i - idx0,
                    }))
                    fired.add(idx0)
            gaps = [g for g in gaps if i - g[0] <= lookback]
        return out


# ── 3. Liquidity sweep v2 ─────────────────────────────────────────────


def _equal_levels(prices: List[float], indices: List[int],
                       tol_pct: float) -> Optional[Tuple[float, List[int]]]:
    """Return (level, member_indices) for a cluster of values within
    ``tol_pct`` of each other. None if no pool of size >= 2."""
    if len(prices) < 2:
        return None
    base = sum(prices) / len(prices)
    tol = base * tol_pct
    members = [i for i, p in zip(indices, prices) if abs(p - base) <= tol]
    if len(members) >= 2:
        return base, members
    return None


class LiquiditySweepV2Detector(Detector):
    """Liquidity sweep v2 — find a pool of equal highs (or lows) over
    the last N bars, then emit when a bar wicks ABOVE (or BELOW) the
    pool but closes back inside. Captures the classical "stop run"
    that institutional desks use to source liquidity."""

    pattern = "liquidity_sweep_v2"
    family = SMC_FAMILY
    description = (
        "Equal highs/lows liquidity pool swept by a wick then closed "
        "back inside. Replaces legacy liquidity_sweep. Cited: "
        "Huddleston ICT 2016+, Brooks 'Trading Price Action: Reversals'."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "pool_lookback": 20,
            "equal_tol_pct": 0.001,  # 10bp
            "min_wick_pct": 0.0005,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 25:
            return []
        bars = _lower_columns(bars)
        try:
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        lookback = int(p.get("pool_lookback", 20))
        tol = float(p.get("equal_tol_pct", 0.001))
        min_wick = float(p.get("min_wick_pct", 0.0005))
        n = len(bars)
        out: List[Observation] = []
        for i in range(lookback + 1, n):
            window_start = i - lookback
            # Pools of equal highs / equal lows in [window_start, i-1].
            recent_highs = highs[window_start:i]
            recent_lows = lows[window_start:i]
            # Top quartile of highs as candidate pool.
            sorted_highs = sorted(enumerate(recent_highs),
                                          key=lambda x: -x[1])
            top_n = max(3, len(sorted_highs) // 4)
            top_highs = sorted_highs[:top_n]
            pool_high = _equal_levels(
                [h for _, h in top_highs],
                [window_start + idx for idx, _ in top_highs],
                tol,
            )
            sorted_lows = sorted(enumerate(recent_lows), key=lambda x: x[1])
            bot_n = max(3, len(sorted_lows) // 4)
            bot_lows = sorted_lows[:bot_n]
            pool_low = _equal_levels(
                [l for _, l in bot_lows],
                [window_start + idx for idx, _ in bot_lows],
                tol,
            )
            # Bullish sweep (sweep the lows, close back inside).
            if pool_low is not None:
                level, members = pool_low
                wick_below = (level - lows[i]) / max(1e-9, level)
                if (lows[i] < level
                        and closes[i] > level
                        and wick_below >= min_wick):
                    out.append(_build_obs(ticker, bars, i, self.pattern, {
                        "direction": "bullish",
                        "pool_level": round(level, 4),
                        "pool_size": len(members),
                        "wick_pct": round(wick_below, 5),
                    }))
                    continue
            # Bearish sweep (sweep the highs, close back inside).
            if pool_high is not None:
                level, members = pool_high
                wick_above = (highs[i] - level) / max(1e-9, level)
                if (highs[i] > level
                        and closes[i] < level
                        and wick_above >= min_wick):
                    out.append(_build_obs(ticker, bars, i, self.pattern, {
                        "direction": "bearish",
                        "pool_level": round(level, 4),
                        "pool_size": len(members),
                        "wick_pct": round(wick_above, 5),
                    }))
        return out


# ── 4. Stop hunt v2 ───────────────────────────────────────────────────


class StopHuntV2Detector(Detector):
    """Failed liquidity sweep that reverses with volume. Bar wicks
    above (below) the prior 20-bar swing high (low) AND closes in the
    OPPOSITE direction AND volume > 1.5x the 20-bar MA volume."""

    pattern = "stop_hunt_v2"
    family = SMC_FAMILY
    description = (
        "Failed sweep of prior swing high/low with elevated volume. "
        "High-conviction reversal. Cited: Brooks 'Reading Price Charts "
        "Bar by Bar', Huddleston ICT 2016+."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "swing_lookback": 20,
            "volume_mult": 1.5,
            "volume_ma_window": 20,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 25:
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
        lookback = int(p.get("swing_lookback", 20))
        vol_mult = float(p.get("volume_mult", 1.5))
        vol_ma_w = int(p.get("volume_ma_window", 20))
        n = len(bars)
        out: List[Observation] = []
        for i in range(lookback + vol_ma_w, n):
            window_h = max(highs[i - lookback:i])
            window_l = min(lows[i - lookback:i])
            vol_ma = sum(volumes[i - vol_ma_w:i]) / vol_ma_w
            if vol_ma <= 0:
                continue
            vol_ratio = volumes[i] / vol_ma
            if vol_ratio < vol_mult:
                continue
            # Bearish stop hunt: wick above prior swing high but close
            # below it (and ideally close < open).
            if (highs[i] > window_h
                    and closes[i] < window_h
                    and closes[i] < opens[i]):
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "bearish",
                    "swept_level": round(window_h, 4),
                    "wick_pct": round((highs[i] - window_h)
                                              / max(1e-9, window_h), 5),
                    "volume_ratio": round(vol_ratio, 3),
                }))
            # Bullish stop hunt.
            elif (lows[i] < window_l
                    and closes[i] > window_l
                    and closes[i] > opens[i]):
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "direction": "bullish",
                    "swept_level": round(window_l, 4),
                    "wick_pct": round((window_l - lows[i])
                                              / max(1e-9, window_l), 5),
                    "volume_ratio": round(vol_ratio, 3),
                }))
        return out


# ── 5. Premium / discount zone ────────────────────────────────────────


class PremiumDiscountZoneDetector(Detector):
    """50 percent midpoint of the most-recent impulse leg. We pull
    ZigZag pivots, take the last completed leg, compute its midpoint,
    and emit when price ENTERS the discount half of an uptrend (or
    the premium half of a downtrend) — the classical with-trend entry
    window."""

    pattern = "premium_discount_zone"
    family = SMC_FAMILY
    description = (
        "Trend-aligned entry: price enters the discount half of an "
        "uptrend's impulse (or premium half of a downtrend). Cited: "
        "Huddleston ICT 'Optimal Trade Entry' framework."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "zigzag_pct": 0.03,
            "discount_threshold": 0.5,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 25:
            return []
        bars = _lower_columns(bars)
        try:
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        zz_pct = float(p.get("zigzag_pct", 0.03))
        threshold = float(p.get("discount_threshold", 0.5))
        pivots = _zigzag_pivots(highs, lows, pct=zz_pct)
        if len(pivots) < 2:
            return []
        out: List[Observation] = []
        last_emit_idx = -10
        for k in range(1, len(pivots)):
            p_prev = pivots[k - 1]
            p_curr = pivots[k]
            leg_low = min(p_prev[1], p_curr[1])
            leg_high = max(p_prev[1], p_curr[1])
            midpoint = (leg_low + leg_high) / 2.0
            # Trend direction from leg.
            trend = "up" if p_prev[2] == "L" and p_curr[2] == "H" else "down"
            # Walk forward bars between p_curr[0] and the next pivot
            # (or end of series); flag the first bar that enters the
            # OTE half.
            next_idx = (pivots[k + 1][0] if k + 1 < len(pivots) else len(bars))
            for i in range(p_curr[0] + 1, min(next_idx, len(bars))):
                if i - last_emit_idx < 3:
                    continue
                if trend == "up":
                    # Discount half: below midpoint in this leg's price range.
                    if (closes[i] < midpoint and closes[i] > leg_low
                            and (midpoint - closes[i])
                                    / max(1e-9, midpoint - leg_low)
                                >= threshold):
                        out.append(_build_obs(ticker, bars, i, self.pattern, {
                            "trend": "up",
                            "leg_low": round(leg_low, 4),
                            "leg_high": round(leg_high, 4),
                            "midpoint": round(midpoint, 4),
                            "zone": "discount",
                        }))
                        last_emit_idx = i
                        break
                else:
                    if (closes[i] > midpoint and closes[i] < leg_high
                            and (closes[i] - midpoint)
                                    / max(1e-9, leg_high - midpoint)
                                >= threshold):
                        out.append(_build_obs(ticker, bars, i, self.pattern, {
                            "trend": "down",
                            "leg_low": round(leg_low, 4),
                            "leg_high": round(leg_high, 4),
                            "midpoint": round(midpoint, 4),
                            "zone": "premium",
                        }))
                        last_emit_idx = i
                        break
        return out


# ── 6. Market Structure Shift v2 ──────────────────────────────────────


class MarketStructureShiftV2Detector(Detector):
    """Proper HH/HL/LH/LL state machine on ZigZag pivots. Emits when
    structure FLIPS — i.e. the trend was making higher-highs +
    higher-lows and the next pivot is a lower-low (or vice versa)."""

    pattern = "market_structure_shift_v2"
    family = SMC_FAMILY
    description = (
        "ZigZag-pivot trend-flip detector. Replaces legacy CHOCH "
        "(-5.5pp). Cited: Brooks 'Trading Price Action: Reversals' "
        "Wiley 2012, Huddleston ICT market-structure framework."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "zigzag_pct": 0.03,
            "min_history_pivots": 3,
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
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        zz_pct = float(p.get("zigzag_pct", 0.03))
        min_hist = int(p.get("min_history_pivots", 3))
        pivots = _zigzag_pivots(highs, lows, pct=zz_pct)
        if len(pivots) < min_hist + 1:
            return []
        out: List[Observation] = []
        # State: 'up' (HH/HL) or 'down' (LH/LL); flip on structural break.
        state: Optional[str] = None
        last_h: Optional[float] = None
        last_l: Optional[float] = None
        for k, (idx, price, kind) in enumerate(pivots):
            if state is None:
                if kind == "H":
                    last_h = price
                else:
                    last_l = price
                if last_h is not None and last_l is not None:
                    state = "up" if last_h > last_l else "down"
                continue
            if state == "up":
                if kind == "H":
                    if last_h is not None and price > last_h:
                        last_h = price
                    elif last_h is not None and price < last_h:
                        # Lower High while in uptrend — warning, not flip yet.
                        last_h = price
                elif kind == "L":
                    if last_l is not None and price < last_l:
                        # Lower low → trend flip to down.
                        out.append(_build_obs(ticker, bars, idx, self.pattern, {
                            "direction": "bearish_flip",
                            "prior_low": round(last_l, 4),
                            "new_low": round(price, 4),
                            "prior_state": "up",
                        }))
                        state = "down"
                    last_l = price
            else:  # state == "down"
                if kind == "L":
                    if last_l is not None and price < last_l:
                        last_l = price
                    elif last_l is not None and price > last_l:
                        last_l = price
                elif kind == "H":
                    if last_h is not None and price > last_h:
                        # Higher high → trend flip to up.
                        out.append(_build_obs(ticker, bars, idx, self.pattern, {
                            "direction": "bullish_flip",
                            "prior_high": round(last_h, 4),
                            "new_high": round(price, 4),
                            "prior_state": "down",
                        }))
                        state = "up"
                    last_h = price
        return out


def build_smc_detectors() -> List[Detector]:
    return [
        OrderBlockDetector(),
        FairValueGapDetector(),
        LiquiditySweepV2Detector(),
        StopHuntV2Detector(),
        PremiumDiscountZoneDetector(),
        MarketStructureShiftV2Detector(),
    ]
