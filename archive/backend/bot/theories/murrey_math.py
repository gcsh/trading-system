"""Murrey Math 1/8 Levels — MITS Phase 10 theory module.

Citation:

  * T. Henning Murrey, "Murrey Math Trading System for All Traded
    Markets" (T.H. Murrey, 1995). Murrey's framework derives 13 levels
    (0/8 through 8/8 plus two each above and below) from W.D. Gann's
    1/8 retracement geometry. Each level has a Murrey-specific
    interpretation:

      * 0/8, 8/8 — "ultimate" support / resistance
      * 1/8, 7/8 — "weak, stall, reverse"
      * 2/8, 6/8 — "pivot, reverse"
      * 3/8, 5/8 — "top / bottom of trading range"
      * 4/8       — "major support / resistance" (the "magnet")

    Murrey's spec: compute a Square Of Nine (SoN) "set up number" close
    to the recent range, then quantise the range into 1/8 buckets.

Approach here:

  * Use the recent N-bar high/low (default N=64 bars, Murrey's typical
    "trading frame").
  * Round to the nearest power-of-10 multiple to align with Murrey's
    "octave" octaves; then carve into eighths.

Signals:

  * BUY  when price bounces from 0/8 or 1/8.
  * SELL when price rejects 7/8 or 8/8.
  * WATCH when price oscillates around 4/8 (the magnet) — Murrey says
    that's the high-probability tradeable range.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from .schema import (
    Line, Signal, TheoryAnnotation,
    bar_close, bar_high, bar_low, bar_ts,
)


MURREY_INTERPRETATIONS = {
    0: "0/8 ultimate support",
    1: "1/8 weak, stall, reverse",
    2: "2/8 pivot, reverse",
    3: "3/8 bottom of trading range",
    4: "4/8 magnet (major S/R)",
    5: "5/8 top of trading range",
    6: "6/8 pivot, reverse",
    7: "7/8 weak, stall, reverse",
    8: "8/8 ultimate resistance",
}

COLORS = {
    0: "#9aa4b2", 1: "#7fc8a9", 2: "#36c26b",
    3: "#1f6feb", 4: "#ffd166", 5: "#1f6feb",
    6: "#36c26b", 7: "#ff9f1c", 8: "#ff5a5f",
}


def _quantise_range(hi: float, lo: float) -> tuple:
    """Murrey rounding: snap to the nearest power-of-10 'octave'."""
    span = hi - lo
    if span <= 0:
        return hi, lo
    mag = math.floor(math.log10(span)) if span > 0 else 0
    # An "octave" — round to a multiple that subdivides cleanly into 8.
    base = 10 ** mag
    # Quantise lo down to nearest base/8, hi up to nearest base/8.
    step = base / 8.0
    lo_q = math.floor(lo / step) * step
    hi_q = math.ceil(hi / step) * step
    if hi_q - lo_q <= 0:
        return hi, lo
    return hi_q, lo_q


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    lookback = int(params.get("lookback", 64))
    density = str(params.get("density", "normal")).lower()
    if density not in ("simple", "normal", "detailed"):
        density = "normal"

    ann = TheoryAnnotation(
        theory="murrey_math",
        params={"lookback": lookback, "density": density, "stepped": True},
        citation=(
            "T. H. Murrey, 'Murrey Math Trading System for All Traded "
            "Markets' (1995). Inherits Gann's 1/8 retracement geometry."
        ),
    )
    if len(bars) < 30:
        ann.notes.append("Not enough bars for Murrey Math.")
        return ann

    # MITS-P10.3.2 — STEPPED OCTAVES.
    #
    # Murrey's "trading frame" is 64 bars (his typical cycle). On a 1y
    # daily chart that's ~3 frames; on a 5y chart it's 20. Walk the
    # window in non-overlapping frames; for each frame, quantise the
    # frame's own HL into an octave and emit each /8 level as a
    # stepped trendline segment spanning that frame's bar range.
    #
    # Density adaptation:
    #
    #   * simple    → only 0/8, 4/8, 8/8 (the "ultimate" + magnet trio)
    #   * normal    → 0, 2, 4, 6, 8 (skip the weak/stall lines)
    #   * detailed  → all 9 levels
    SIMPLE_LEVELS = {0, 4, 8}
    NORMAL_LEVELS = {0, 2, 4, 6, 8}

    def _allowed_level(n: int) -> bool:
        if density == "simple":
            return n in SIMPLE_LEVELS
        if density == "normal":
            return n in NORMAL_LEVELS
        return True

    # Track levels of the most recent frame for the right-axis labels
    # and the "key levels now" primer.
    last_levels: Dict[int, float] = {}
    n_frames = max(1, len(bars) // lookback)
    frames: List[List[Dict[str, Any]]] = []
    # Build non-overlapping frames anchored at the RIGHT edge so the
    # final frame includes the most recent bars (not leftovers at the
    # start of the window).
    cursor = len(bars)
    while cursor > 0 and len(frames) < 30:
        start = max(0, cursor - lookback)
        if cursor - start < max(20, lookback // 3):
            break
        frames.append(bars[start:cursor])
        cursor = start
    frames.reverse()  # chronological order
    if not frames:
        ann.notes.append("Window too short for a Murrey frame.")
        return ann

    total_segments = 0
    for frame_idx, frame in enumerate(frames):
        f_hi = max(bar_high(b) for b in frame)
        f_lo = min(bar_low(b) for b in frame if bar_low(b) > 0)
        if f_hi <= f_lo:
            continue
        hi_q, lo_q = _quantise_range(f_hi, f_lo)
        span = hi_q - lo_q
        if span <= 0:
            continue
        step = span / 8.0
        first_ts = bar_ts(frame[0])
        last_ts = bar_ts(frame[-1])
        if not first_ts or not last_ts:
            continue
        for n in range(0, 9):
            if not _allowed_level(n):
                continue
            p = lo_q + step * n
            ann.lines.append(Line(
                kind="trendline",
                start={"ts": first_ts, "price": float(p)},
                end={"ts": last_ts, "price": float(p)},
                color=COLORS.get(n, "#9aa4b2"),
                width=(2 if n in (0, 4, 8) else 1),
                style=("solid" if n in (0, 4, 8) else "dashed"),
                label=None,  # right-axis stays clean; final frame labels emitted below
                meta={
                    "level": f"{n}/8", "n": n,
                    "priority": 1 if n == 4 else (2 if n in (0, 8) else 3),
                    "interpretation": MURREY_INTERPRETATIONS[n],
                    "frame_idx": frame_idx,
                    "stepped": True,
                },
            ))
            total_segments += 1
            if frame_idx == len(frames) - 1:
                last_levels[n] = p

    # Final-frame right-axis labels — one per allowed level.
    for n, p in last_levels.items():
        ann.lines.append(Line(
            kind="horizontal",
            start={"ts": "", "price": float(p)},
            end={"ts": "", "price": float(p)},
            color=COLORS.get(n, "#9aa4b2"),
            width=1,
            style="dotted",
            label=f"{n}/8 {p:.2f}",
            meta={"level": f"{n}/8", "n": n,
                  "priority": 1 if n == 4 else (2 if n in (0, 8) else 3),
                  "label_only": True,
                  "interpretation": MURREY_INTERPRETATIONS[n]},
        ))

    ann.notes.append(
        f"Murrey: {total_segments} stepped octave segments across "
        f"{len(frames)} frames of ~{lookback} bars."
    )

    # Build the "levels" dict that the rest of the code reads. Use the
    # most-recent frame's levels for the magnet / signal logic.
    levels: Dict[int, float] = dict(last_levels)
    if not levels:
        ann.notes.append("Could not compute any Murrey levels.")
        return ann
    # Re-derive step / lo_q for the most recent frame.
    final_frame = frames[-1]
    f_hi = max(bar_high(b) for b in final_frame)
    f_lo = min(bar_low(b) for b in final_frame if bar_low(b) > 0)
    hi_q, lo_q = _quantise_range(f_hi, f_lo)
    step = (hi_q - lo_q) / 8.0

    last_close = bar_close(bars[-1])
    pos = (last_close - lo_q) / step if step > 0 else 0
    closest_n = int(round(pos))
    closest_n = max(0, min(8, closest_n))
    # Ensure levels has 0..8 for signal emit (use computed even if not
    # rendered under current density).
    for n in range(0, 9):
        if n not in levels:
            levels[n] = lo_q + step * n

    # MITS-P10.2 — walk every bar; emit Signals as price enters Murrey
    # bands. To avoid 200 flags we only emit on TRANSITIONS into the
    # extreme bands (0-1/8 or 7-8/8), not on every bar sitting there.
    from .signal_promote import promote_all
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})
    sigs: List[Signal] = []
    if step > 0:
        prev_band = None
        for i in range(len(bars)):
            cl = bar_close(bars[i])
            ts = bar_ts(bars[i])
            p = (cl - lo_q) / step
            n = max(0, min(8, int(round(p))))
            # Only emit on band TRANSITION to avoid spamming.
            if n != prev_band:
                if n <= 1:
                    sigs.append(Signal(
                        action="BUY",
                        ts=ts, price=float(cl), confidence=0.55,
                        reasoning=(
                            f"Spot ({cl:.2f}) entered the {n}/8 Murrey "
                            "'ultimate support' band — high-probability "
                            f"bounce. Magnet: 4/8 = {levels[4]:.2f}."
                        ),
                        target_price=float(levels[4]),
                        stop_loss=float(levels[0]),
                        instrument="stock",
                        theory_anchor={"level": f"{n}/8", "i": i},
                    ))
                elif n >= 7:
                    sigs.append(Signal(
                        action="SELL",
                        ts=ts, price=float(cl), confidence=0.55,
                        reasoning=(
                            f"Spot ({cl:.2f}) entered the {n}/8 Murrey "
                            "'ultimate resistance' band — high-probability "
                            "rejection."
                        ),
                        target_price=float(levels[4]),
                        stop_loss=float(levels[8]),
                        instrument="stock",
                        theory_anchor={"level": f"{n}/8", "i": i},
                    ))
                elif n == 4 and prev_band is not None:
                    # Magnet — emit WATCH on midpoint cross.
                    sigs.append(Signal(
                        action="WATCH",
                        ts=ts, price=float(cl), confidence=0.40,
                        reasoning=(
                            f"Spot tagged the 4/8 magnet — Murrey's high-"
                            "probability tradeable zone, direction-agnostic."
                        ),
                        instrument="stock",
                        theory_anchor={"level": "4/8", "i": i},
                    ))
            prev_band = n

    if len(sigs) > 25:
        sigs = sigs[-25:]
    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.65  # Murrey is interpretive; lower confidence.
    ann.primer = {
        "what_it_measures": (
            "Murrey Math projects a quantised 1/8 retracement grid onto "
            "the recent trading range, then assigns each level a "
            "behavioural label inherited from Gann. The 4/8 is the "
            "'magnet' — Murrey claims price gravitates to it; 0/8 + 8/8 "
            "are 'ultimate' S/R that rarely break in calm regimes."
        ),
        "how_to_read": (
            "Buy near 0/8 or 1/8 with stop below; sell near 7/8 or 8/8 "
            "with stop above. The 4/8 is a magnet — fades toward it from "
            "outside, follows-through past it from inside. Murrey "
            "explicitly notes this works on every market when the "
            "set-up number matches the instrument's typical octave."
        ),
        "key_levels_now": (
            f"Spot {last_close:.2f} ≈ {closest_n}/8 "
            f"({MURREY_INTERPRETATIONS[closest_n]})  ·  "
            f"4/8 magnet: {levels[4]:.2f}"
        ),
    }
    return ann


__all__ = ["analyze"]
