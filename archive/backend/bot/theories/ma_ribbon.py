"""Fibonacci EMA Ribbon — MITS Phase 10 theory module.

Citation:

  * Daryl Guppy, "Trading Tactics" (Wrightbooks, 1997) — Guppy
    Multiple Moving Average (GMMA). Guppy uses a 6-EMA "short" group
    (3/5/8/10/12/15) and a 6-EMA "long" group (30/35/40/45/50/60).
  * The Fibonacci variant used here (5/8/13/21/34/55/89/144) is a
    common modern derivative documented by Steve Nison (candlestick
    school) and adopted by many TradingView Public Library scripts.
    The eight periods are the Fibonacci numbers from F5 to F12 — a
    spread that exposes regime structure across 1-week to 7-month
    horizons on daily bars.

Compression / expansion of the ribbon is the read:

  * **Compressed + flat** = no trend, fade extremes.
  * **Compressed → fanning UP** = uptrend birth (BUY).
  * **Compressed → fanning DOWN** = downtrend birth (SELL).
  * **Fully fanned out** = mature trend — let it run.

Signals:

  * BUY  when price closes above ALL 8 EMAs AND the ribbon was
    compressed within the last 5 bars (recent fan-out).
  * SELL when price closes below ALL 8 EMAs AND the ribbon was
    compressed within the last 5 bars.
  * EXIT_LONG when price crosses below the F21 EMA (the trend-anchor).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._indicators import closes, ema
from .schema import (
    Line, Signal, TheoryAnnotation,
    bar_close, bar_ts,
)


FIB_PERIODS = [5, 8, 13, 21, 34, 55, 89, 144]
RIBBON_COLORS = [
    "#ff5a5f",  # 5
    "#ff8a3d",  # 8
    "#ffd166",  # 13
    "#9be38e",  # 21
    "#36c26b",  # 34
    "#3fb6e3",  # 55
    "#7a85ff",  # 89
    "#b87cff",  # 144
]


def _ribbon_spread(ribbon: List[List[Optional[float]]], i: int) -> Optional[float]:
    vals = [r[i] for r in ribbon if r[i] is not None]
    if not vals:
        return None
    return max(vals) - min(vals)


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    periods: List[int] = list(params.get("periods") or FIB_PERIODS)
    if len(periods) != 8:
        periods = FIB_PERIODS

    ann = TheoryAnnotation(
        theory="ma_ribbon",
        params={"periods": periods},
        citation=(
            "Guppy, 'Trading Tactics' (Wrightbooks 1997); Fibonacci "
            "8-EMA ribbon (5/8/13/21/34/55/89/144) — TradingView Public "
            "Library reference scripts."
        ),
    )
    # MITS-P10.1 — when the window is short (weekly/monthly aggregation),
    # drop the EMAs we can't compute instead of returning empty. Keeps any
    # EMA whose period × 2 fits in the bar count.
    max_fit = len(bars) // 2 if bars else 0
    periods = [p for p in periods if p <= max_fit]
    if not periods:
        ann.notes.append(
            f"Not enough bars ({len(bars)}) for any ribbon EMA."
        )
        return ann
    if len(periods) < 8:
        ann.notes.append(
            f"Short window: rendering {len(periods)}/8 EMAs "
            f"({','.join(str(p) for p in periods)}). "
            "Use window=1y or lower for the full 8-EMA ribbon."
        )

    cc = closes(bars)
    ribbon: List[List[Optional[float]]] = [ema(cc, p) for p in periods]
    # MITS Phase 10.1 — one ``series`` Line per EMA (8 total) instead of
    # ~2000 trendline segments (8 EMAs × ~250 bars).
    # MITS-P10.3.4 — priority tier per EMA period:
    #   * 5, 21, 55  → priority 1 (Simple)
    #   * 8, 13, 34, 89 → priority 2 (Normal)
    #   * 144 + warm-ups → priority 3 (Detailed)
    SIMPLE_EMAS = {5, 21, 55}
    NORMAL_EMAS = {5, 8, 13, 21, 34, 55, 89}

    def _priority_for(period: int) -> int:
        if period in SIMPLE_EMAS:
            return 1
        if period in NORMAL_EMAS:
            return 2
        return 3

    for ridx, series in enumerate(ribbon):
        color = RIBBON_COLORS[ridx % len(RIBBON_COLORS)]
        label_name = f"EMA-{periods[ridx]}"
        points = [
            {"ts": bar_ts(bars[i]), "price": float(v)}
            for i, v in enumerate(series) if v is not None
        ]
        if not points:
            continue
        ann.lines.append(Line(
            kind="series",
            start=points[0],
            end=points[-1],
            color=color, width=1, style="solid",
            label=label_name,
            meta={"kind": "ribbon", "period": periods[ridx],
                  "priority": _priority_for(periods[ridx])},
            points=points,
        ))

    last = len(bars) - 1
    last_close = bar_close(bars[last])
    last_ts = bar_ts(bars[last])
    last_vals = [r[last] for r in ribbon if r[last] is not None]

    if not last_vals or len(last_vals) < 4:
        ann.notes.append("Ribbon still warming up.")
        return ann

    all_above = all(last_close > v for v in last_vals)
    all_below = all(last_close < v for v in last_vals)

    spreads = [
        _ribbon_spread(ribbon, last - k) for k in range(0, 6)
        if last - k >= 0
    ]
    spreads = [s for s in spreads if s is not None]
    recent_compression = False
    if spreads:
        # Compression = the *minimum* spread in the last 5 bars was
        # less than 0.5% of the latest close. This is a loose threshold
        # — Guppy's traditional read is visual.
        min_spread = min(spreads)
        recent_compression = (min_spread <= 0.005 * last_close)

    # MITS-P10.2 — walk every bar; emit BUY on each reclaim of F21
    # (trend-anchor) from below, SELL on each loss from above. F21 is
    # Daryl Guppy's "trader's group" trend filter — the canonical
    # GMMA short-side anchor.
    from .signal_promote import promote_all
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})
    sigs: List[Signal] = []
    f21_series = ribbon[3]
    for i in range(1, len(bars)):
        f21 = f21_series[i]
        f21_prev = f21_series[i - 1]
        if f21 is None or f21_prev is None:
            continue
        cl = bar_close(bars[i])
        prev = bar_close(bars[i - 1])
        ts = bar_ts(bars[i])
        if prev < f21_prev and cl > f21:
            sigs.append(Signal(
                action="BUY",
                ts=ts, price=float(cl), confidence=0.60,
                reasoning=(
                    f"Close ({cl:.2f}) reclaimed F21 EMA ({f21:.2f}) — "
                    "Guppy GMMA trend-anchor reclaim, long bias on."
                ),
                stop_loss=float(f21),
                instrument="stock",
                theory_anchor={"trigger": "f21_reclaim", "i": i},
            ))
        elif prev > f21_prev and cl < f21:
            sigs.append(Signal(
                action="SELL",
                ts=ts, price=float(cl), confidence=0.60,
                reasoning=(
                    f"Close ({cl:.2f}) lost F21 EMA ({f21:.2f}) — Guppy "
                    "GMMA trend-anchor loss, short bias on."
                ),
                stop_loss=float(f21),
                instrument="stock",
                theory_anchor={"trigger": "f21_loss", "i": i},
            ))

    if len(sigs) > 25:
        sigs = sigs[-25:]
    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.80
    ann.primer = {
        "what_it_measures": (
            "The Fibonacci EMA Ribbon plots 8 EMAs spanning the "
            "Fibonacci numbers 5 → 144. Their compression / expansion "
            "geometry reveals regime: tightly stacked = no-trend, "
            "fanning out = strong trend, crossing through each other = "
            "transition. Guppy's GMMA is the conceptual parent."
        ),
        "how_to_read": (
            "Watch the ribbon, not any single line. Compression → fan-out "
            "is a trend birth; fan compression mid-trend warns of an "
            "exhausted move. F21 is the trend-anchor — losing it is the "
            "first sign the trend is breaking down."
        ),
        "key_levels_now": (
            f"F21 anchor: {ribbon[3][-1]:.2f}  ·  F34: {ribbon[4][-1]:.2f}"
            if ribbon[3][-1] is not None and ribbon[4][-1] is not None
            else "Ribbon warming up."
        ),
    }
    return ann


__all__ = ["analyze", "FIB_PERIODS"]
