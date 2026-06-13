"""Fair Value Gaps (FVG) — MITS Phase 10 theory module.

Citation:

  * Michael J. Huddleston (ICT), "ICT Mentorship Core Content" (self-
    published video curriculum, 2015-2022). FVG (also known as an
    Imbalance) is one of the two foundational SMC primitives along
    with the Order Block.
  * Steven J. Soroka, "Smart Money Concepts" (Self-published, 2023).

Definition: a 3-candle pattern (c1, c2, c3) creating a price-axis
gap that the bar series has NOT yet traded back into:

  * **Bullish FVG** when ``c1.high < c3.low``. The gap is c1.high → c3.low.
  * **Bearish FVG** when ``c1.low > c3.high``. The gap is c3.high → c1.low.

The gap remains an "open imbalance" until price returns to fill it.
ICT teaches that "the market is drawn to fill un-mitigated FVGs" —
which provides a high-probability target on either side.

Signals:

  * BUY  when a bullish FVG gets filled (price returns to the bottom
    of the gap from above) — long-side discount entry.
  * SELL when a bearish FVG gets filled.
  * WATCH when an un-mitigated FVG is visible nearby (target candidate).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .schema import (
    Marker, Signal, TheoryAnnotation, Zone,
    bar_close, bar_high, bar_low, bar_ts,
)


def _find_fvgs(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i in range(2, len(bars)):
        c1 = bars[i - 2]; c2 = bars[i - 1]; c3 = bars[i]
        c1_h = bar_high(c1); c1_l = bar_low(c1)
        c3_h = bar_high(c3); c3_l = bar_low(c3)
        if c1_h < c3_l:
            out.append({
                "kind": "bullish",
                "i": i,
                "x1": bar_ts(c1), "x2": bar_ts(c3),
                "y_low": c1_h, "y_high": c3_l,
                "mid": (c1_h + c3_l) / 2.0,
            })
        elif c1_l > c3_h:
            out.append({
                "kind": "bearish",
                "i": i,
                "x1": bar_ts(c1), "x2": bar_ts(c3),
                "y_low": c3_h, "y_high": c1_l,
                "mid": (c3_h + c1_l) / 2.0,
            })
    return out


def _mitigated(fvg: Dict[str, Any], bars: List[Dict[str, Any]]) -> bool:
    """Has price returned into the gap after creation?"""
    for j in range(fvg["i"] + 1, len(bars)):
        bj = bars[j]
        h = bar_high(bj); l = bar_low(bj)
        if l <= fvg["y_high"] and h >= fvg["y_low"]:
            return True
    return False


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    max_fvgs = int(params.get("max_fvgs", 8))

    ann = TheoryAnnotation(
        theory="fair_value_gaps",
        params={"max_fvgs": max_fvgs},
        citation=(
            "Huddleston (ICT), 'ICT Mentorship Core Content' (2015-2022); "
            "Soroka, 'Smart Money Concepts' (2023)."
        ),
    )
    if len(bars) < 10:
        ann.notes.append("Not enough bars for FVG scan.")
        return ann

    fvgs = _find_fvgs(bars)
    if not fvgs:
        ann.notes.append("No FVG / imbalance detected.")
        return ann

    last_ts = bar_ts(bars[-1])
    last_close = bar_close(bars[-1])

    unmitigated = [f for f in fvgs if not _mitigated(f, bars)]
    # Keep recent unmitigated + the last few mitigated for context.
    recent = (fvgs[-max_fvgs:] if not unmitigated
              else (unmitigated[-max_fvgs:]))
    for fvg in recent:
        color = "#36c26b" if fvg["kind"] == "bullish" else "#ff5a5f"
        opacity = 0.20 if fvg in unmitigated else 0.08
        label = ("Bull FVG" if fvg["kind"] == "bullish" else "Bear FVG") + \
                ("" if fvg in unmitigated else " (filled)")
        ann.zones.append(Zone(
            x1=fvg["x1"], y1=float(fvg["y_low"]),
            x2=last_ts,   y2=float(fvg["y_high"]),
            color=color, opacity=opacity,
            label=label,
        ))
        ann.markers.append(Marker(
            ts=fvg["x2"], price=float(fvg["mid"]),
            label="FVG",
            color=color,
            shape=("arrow_up" if fvg["kind"] == "bullish" else "arrow_down"),
        ))

    # MITS-P10.2 — for EACH FVG, walk bars forward and emit a BUY/SELL
    # at the FIRST mitigation (the close that enters the gap). One
    # signal per FVG, not per bar inside the gap.
    from .signal_promote import promote_all
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})
    sigs: List[Signal] = []
    for fvg in fvgs:
        # Find bar index of fvg.x2 (the 3rd candle).
        fvg_i = None
        for i, b in enumerate(bars):
            if bar_ts(b) == fvg["x2"]:
                fvg_i = i
                break
        if fvg_i is None:
            continue
        for j in range(fvg_i + 1, len(bars)):
            cl = bar_close(bars[j])
            ts_j = bar_ts(bars[j])
            if fvg["y_low"] <= cl <= fvg["y_high"]:
                if fvg["kind"] == "bullish":
                    sigs.append(Signal(
                        action="BUY",
                        ts=ts_j, price=float(cl), confidence=0.62,
                        reasoning=(
                            f"Bullish FVG "
                            f"{fvg['y_low']:.2f}–{fvg['y_high']:.2f} "
                            "mitigated — ICT 'discount-in-premium' long entry."
                        ),
                        target_price=float(fvg["y_high"] + (fvg["y_high"] - fvg["y_low"]) * 3),
                        stop_loss=float(fvg["y_low"] * 0.992),
                        instrument="stock",
                        theory_anchor={"fvg_kind": "bullish", "i": j},
                    ))
                else:
                    sigs.append(Signal(
                        action="SELL",
                        ts=ts_j, price=float(cl), confidence=0.62,
                        reasoning=(
                            f"Bearish FVG "
                            f"{fvg['y_low']:.2f}–{fvg['y_high']:.2f} "
                            "mitigated — ICT short re-entry."
                        ),
                        target_price=float(fvg["y_low"] - (fvg["y_high"] - fvg["y_low"]) * 3),
                        stop_loss=float(fvg["y_high"] * 1.008),
                        instrument="stock",
                        theory_anchor={"fvg_kind": "bearish", "i": j},
                    ))
                break

    if len(sigs) > 25:
        sigs = sigs[-25:]
    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.72
    bull_n = sum(1 for f in unmitigated if f["kind"] == "bullish")
    bear_n = sum(1 for f in unmitigated if f["kind"] == "bearish")
    ann.primer = {
        "what_it_measures": (
            "A Fair Value Gap is a 3-candle imbalance where the middle "
            "candle's move was so violent it left a gap (between c1 and "
            "c3 wicks) un-traded. ICT teaches that the market is drawn "
            "back to fill un-mitigated FVGs — making them high-probability "
            "magnets and re-entry zones."
        ),
        "how_to_read": (
            "Bullish FVG (green) = institutional buying that left a gap "
            "below; expect price to return and fill before continuing "
            "up. Bearish FVG (red) = opposite. Take entries when price "
            "tags the FVG zone — stop just past the far side of the gap."
        ),
        "key_levels_now": (
            f"Un-mitigated FVGs — bullish: {bull_n}, bearish: {bear_n}"
        ),
    }
    return ann


__all__ = ["analyze"]
