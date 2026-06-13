"""Gann Square of 9 — MITS Phase 10 theory module.

Citation:

  * W. D. Gann, "The Tunnel Thru The Air" (Lambert-Gann, 1927). Gann's
    original Square Of Nine spiral diagram. The number 1 sits at the
    centre; integers spiral outward; every 45° (one half-quarter) marks
    a "harmonic" angle.
  * Jeff Cooper, "Hit and Run Trading II" (M. Gordon, 1998) — Chapter
    10 documents the modern Square Of 9 calculation used by every
    Gann-software vendor:

        For pivot price P, the harmonic prices at angle θ degrees are:

            P_up(θ)   = ( √P + θ/180 ) ²
            P_down(θ) = ( √P − θ/180 ) ²

        The canonical angles are 45° / 90° / 135° / 180° / 225° / 270° /
        315° / 360°. The 90° harmonics (the "cardinal cross") are the
        most-watched.

Signals:

  * BUY  when price tags a Down-side 90° / 180° harmonic and bounces.
  * SELL when price tags an Up-side 90° / 180° harmonic and rejects.
  * The pivot anchor is the recent swing low (default) or swing high.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

from ._zigzag import detect_pivots
from .schema import (
    Line, Marker, Signal, TheoryAnnotation,
    bar_close, bar_high, bar_low, bar_ts,
)


HARMONIC_ANGLES = [45, 90, 135, 180, 225, 270, 315, 360]


def square_of_9_level(pivot_price: float, angle_deg: float,
                         direction: str = "up") -> float:
    """One harmonic price using Cooper's formula."""
    if pivot_price <= 0:
        return pivot_price
    sp = math.sqrt(pivot_price)
    delta = angle_deg / 180.0
    if direction == "up":
        return (sp + delta) ** 2
    return max(0.0, (sp - delta) ** 2)


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    zigzag_pct = float(params.get("zigzag_pct",
                                       getattr(TUNABLES, "theory_zigzag_pct", 3.0)))
    # MITS Phase 10.1 — operator screenshot showed the anchor floored to
    # the lowest of a wide window (the full series min, not a meaningful
    # swing). Tighten the default lookback so the anchor lands on a
    # *recent* significant pivot. Anchor pivot selection below also
    # explicitly picks the MOST SIGNIFICANT recent pivot (by absolute
    # move-size) rather than just the chronologically last one.
    lookback = int(params.get("lookback", 90))
    pivot_type = params.get("pivot_type")  # "high" or "low" or None

    ann = TheoryAnnotation(
        theory="square_of_9",
        params={"zigzag_pct": zigzag_pct, "lookback": lookback,
                "pivot_type": pivot_type},
        citation=(
            "W. D. Gann, 'The Tunnel Thru The Air' (Lambert-Gann 1927); "
            "Cooper, 'Hit and Run Trading II' (M. Gordon 1998), Ch. 10 "
            "(Square Of Nine harmonic formula)."
        ),
    )
    if len(bars) < 30:
        ann.notes.append("Not enough bars for Square of 9.")
        return ann

    win = bars[-lookback:] if len(bars) > lookback else bars[:]
    offset = max(0, len(bars) - lookback)
    pivots = detect_pivots(win, threshold_pct=zigzag_pct)
    if not pivots:
        # Fallback: window extreme.
        hi_i = max(range(len(win)), key=lambda i: bar_high(win[i]))
        lo_i = min(range(len(win)), key=lambda i: bar_low(win[i]))
        is_uptrend = bar_close(bars[-1]) > bar_close(win[0])
        if pivot_type == "high":
            pivot = {"i": hi_i, "ts": bar_ts(win[hi_i]),
                      "price": bar_high(win[hi_i]), "type": "high"}
        elif pivot_type == "low":
            pivot = {"i": lo_i, "ts": bar_ts(win[lo_i]),
                      "price": bar_low(win[lo_i]), "type": "low"}
        else:
            pivot = (
                {"i": lo_i, "ts": bar_ts(win[lo_i]),
                  "price": bar_low(win[lo_i]), "type": "low"}
                if is_uptrend else
                {"i": hi_i, "ts": bar_ts(win[hi_i]),
                  "price": bar_high(win[hi_i]), "type": "high"}
            )
    else:
        # MITS Phase 10.1 — pick the MOST SIGNIFICANT pivot in the window
        # (largest absolute move from the prior pivot), not the
        # chronologically last one. This matches the operator's mental
        # model — a Gann harmonic grid should anchor at the swing point
        # the market actually reacted to, not whichever pivot happens to
        # be most recent (often a noise pivot near current price).
        def _significance(idx: int) -> float:
            p = pivots[idx]
            if idx == 0:
                return abs(p["price"])
            prev = pivots[idx - 1]
            return abs(p["price"] - prev["price"])

        if pivot_type:
            candidates = [(i, p) for i, p in enumerate(pivots)
                          if p["type"] == pivot_type]
            if candidates:
                # Significance-weighted pick among same-type pivots.
                idx_pick = max(candidates, key=lambda kv: _significance(kv[0]))[0]
                pivot = pivots[idx_pick]
            else:
                pivot = pivots[-1]
        else:
            idx_pick = max(range(len(pivots)), key=_significance)
            pivot = pivots[idx_pick]

    pivot_i_global = pivot["i"] + offset
    pivot_price = float(pivot["price"])
    last_ts = bar_ts(bars[-1])
    first_ts = bar_ts(bars[0])

    # For each angle, draw both up and down harmonics.
    up_levels: Dict[float, float] = {}
    down_levels: Dict[float, float] = {}
    for ang in HARMONIC_ANGLES:
        up_p = square_of_9_level(pivot_price, ang, "up")
        dn_p = square_of_9_level(pivot_price, ang, "down")
        up_levels[ang] = up_p
        down_levels[ang] = dn_p
        col_up = ("#ffd166" if ang in (90, 180, 360)
                    else ("#ff9f1c" if ang in (45, 135) else "#9aa4b2"))
        col_dn = ("#1f6feb" if ang in (90, 180, 360)
                    else ("#36c26b" if ang in (45, 135) else "#9aa4b2"))
        width = 2 if ang in (90, 180, 360) else 1
        style = "solid" if ang in (90, 180, 360) else "dashed"
        ann.lines.append(Line(
            kind="horizontal",
            start={"ts": pivot["ts"], "price": float(up_p)},
            end={"ts": last_ts, "price": float(up_p)},
            color=col_up, width=width, style=style,
            label=f"+{ang}° → {up_p:.2f}",
            meta={"side": "up", "angle": ang, "price": up_p},
        ))
        ann.lines.append(Line(
            kind="horizontal",
            start={"ts": pivot["ts"], "price": float(dn_p)},
            end={"ts": last_ts, "price": float(dn_p)},
            color=col_dn, width=width, style=style,
            label=f"−{ang}° → {dn_p:.2f}",
            meta={"side": "down", "angle": ang, "price": dn_p},
        ))

    ann.markers.append(Marker(
        ts=pivot["ts"], price=pivot_price,
        label=f"Anchor: {pivot['type']} pivot",
        color="#ffd166",
        shape=("arrow_down" if pivot["type"] == "high" else "arrow_up"),
    ))

    # MITS-P10.2 — walk every bar after the anchor pivot; emit a Signal
    # when price tags a 45°/90°/180° harmonic (not just last bar).
    from .signal_promote import promote_all
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})
    last_close = bar_close(bars[-1])
    sigs: List[Signal] = []
    tol_pct = 0.005  # 0.5% proximity tolerance
    emitted_levels: set = set()  # dedupe same-bar same-angle
    for i in range(max(pivot_i_global + 1, 1), len(bars)):
        cl = bar_close(bars[i])
        prev = bar_close(bars[i - 1])
        ts = bar_ts(bars[i])
        tol = max(tol_pct * cl, 0.10)
        for ang in (45, 90, 180):
            up_p = up_levels[ang]
            dn_p = down_levels[ang]
            # Tagged from below — BUY (bounce off support harmonic).
            if prev < dn_p and abs(cl - dn_p) <= tol:
                key = ("dn", ang, i)
                if key not in emitted_levels:
                    emitted_levels.add(key)
                    sigs.append(Signal(
                        action="BUY",
                        ts=ts, price=float(cl), confidence=0.55,
                        reasoning=(
                            f"Spot ({cl:.2f}) tagged the Gann Square Of 9 "
                            f"−{ang}° harmonic ({dn_p:.2f}) from the "
                            f"{pivot['type']} anchor at {pivot_price:.2f} — "
                            "bounce candidate."
                        ),
                        target_price=float(pivot_price),
                        stop_loss=float(dn_p * 0.985),
                        instrument="stock",
                        theory_anchor={"angle": -ang, "level": dn_p, "i": i},
                    ))
            # Rejected from above — SELL.
            elif prev > up_p and abs(cl - up_p) <= tol:
                key = ("up", ang, i)
                if key not in emitted_levels:
                    emitted_levels.add(key)
                    sigs.append(Signal(
                        action="SELL",
                        ts=ts, price=float(cl), confidence=0.55,
                        reasoning=(
                            f"Spot ({cl:.2f}) rejected the Gann Square Of 9 "
                            f"+{ang}° harmonic ({up_p:.2f}) from the "
                            f"{pivot['type']} anchor at {pivot_price:.2f} — "
                            "rejection candidate."
                        ),
                        target_price=float(pivot_price),
                        stop_loss=float(up_p * 1.015),
                        instrument="stock",
                        theory_anchor={"angle": +ang, "level": up_p, "i": i},
                    ))

    if len(sigs) > 25:
        sigs = sigs[-25:]
    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.70
    ann.primer = {
        "what_it_measures": (
            "Gann's Square Of 9 spirals integers outward from 1; every "
            "rotation (360°) corresponds to a full mathematical cycle. "
            "Cooper's formula P_up(θ) = (√P + θ/180)² translates that "
            "geometry into price levels above and below a pivot. The "
            "cardinal-cross angles (90°/180°/270°/360°) are the most-"
            "watched harmonics on every Gann-software vendor's chart."
        ),
        "how_to_read": (
            "Watch the cardinal-cross levels (bold solid lines) for "
            "rejections or bounces. A tag of −90° from a swing low is a "
            "high-quality long-side bounce; a tag of +180° is a higher-"
            "level resistance. Combine with time-cycle Gann verticals "
            "(see the Gann theory module) for the strongest reads. "
            "MITS-P10.1 ANCHOR — the harmonic grid is anchored at the "
            "most-significant ZigZag pivot in the last 90 bars (largest "
            "absolute swing), not the chronologically most-recent pivot "
            "and not the absolute min/max of the window — so the grid "
            "reflects the swing the market actually reacted to."
        ),
        "key_levels_now": (
            f"Anchor: {pivot['type']} @ {pivot_price:.2f}  ·  "
            f"+90°: {up_levels[90]:.2f}  ·  −90°: {down_levels[90]:.2f}  ·  "
            f"+180°: {up_levels[180]:.2f}  ·  −180°: {down_levels[180]:.2f}"
        ),
    }
    return ann


__all__ = ["analyze", "square_of_9_level", "HARMONIC_ANGLES"]
