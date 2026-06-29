"""Price-action chart-pattern detection.

Detects classical chart patterns from a ZigZag pivot series. Pattern
definitions follow:

  * Thomas N. Bulkowski, "Encyclopedia of Chart Patterns" (3rd ed.,
    Wiley, 2021) — taxonomy + tolerance bands for triangles, head &
    shoulders, double tops/bottoms, flags, wedges.
  * John J. Murphy, "Technical Analysis of the Financial Markets"
    (NYIF, 1999), Chapters 6 & 8 — trendline definitions, channel
    construction, breakout-volume confirmation rules.

For every detected pattern we return:

  * ``pattern_name`` — canonical Bulkowski name.
  * ``lines`` — boundary trendlines/horizontals.
  * ``markers`` — breakout point + projected target.
  * ``zones`` — shaded "projected target" area.
  * ``confidence`` — 0..1 score based on (a) fit residuals to the
    boundary lines, (b) symmetry between paired highs/lows.
  * Notes — anything the operator should verify by eye.

We pick the highest-confidence pattern across the latest 4-7 pivots.
If nothing scores above ``min_confidence``, we still return the latest
trend structure (zigzag pivots + last trendline) so the chart isn't
empty.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.config import TUNABLES

from ._zigzag import detect_pivots
from .schema import (
    Line, Marker, TheoryAnnotation, Zone,
    bar_close, bar_high, bar_low, bar_ts, bar_volume,
)


# ── helpers ──────────────────────────────────────────────────────────


def _line_eq(p1: Dict[str, Any], p2: Dict[str, Any]
              ) -> Tuple[float, float]:
    """Return (slope, intercept) for the line through two pivots,
    using bar index as x. ``slope = dprice / dbar``."""
    x1, y1 = float(p1["i"]), float(p1["price"])
    x2, y2 = float(p2["i"]), float(p2["price"])
    if x2 == x1:
        return 0.0, y1
    slope = (y2 - y1) / (x2 - x1)
    intercept = y1 - slope * x1
    return slope, intercept


def _line_at(slope: float, intercept: float, x: float) -> float:
    return slope * x + intercept


def _pct_diff(a: float, b: float) -> float:
    if a == 0:
        return float("inf")
    return abs(a - b) / abs(a)


def _vol_confirmed(bars: List[Dict[str, Any]], break_idx: int,
                    window: int = 20, mult: float = 1.5) -> bool:
    """Did the breakout bar's volume exceed ``mult`` × the trailing
    ``window``-bar average?"""
    if break_idx <= 0 or break_idx >= len(bars):
        return False
    lo = max(0, break_idx - window)
    prior = [bar_volume(b) for b in bars[lo:break_idx] if bar_volume(b) > 0]
    if not prior:
        return False
    avg = sum(prior) / len(prior)
    return bar_volume(bars[break_idx]) >= mult * avg


# ── pattern detectors ─────────────────────────────────────────────────


def _detect_triangle(bars, pivots, lookback=8) -> Optional[Dict[str, Any]]:
    """Detect ascending / descending / symmetric triangles in the
    latest ``lookback`` pivots.

    Bulkowski: a triangle needs at least 2 touches on each boundary; we
    require the latest pivots to alternate high-low-high-low… with the
    highs and lows each having ≥ 2 points to fit a trendline.

    A breakout pivot (well past the established boundary) at the end of
    the series would distort the boundary slope. We strip trailing
    pivots that lie outside the boundary established by the pre-tail
    pivots — Bulkowski calls these "the apex" / "break" pivots, which
    do not belong to the triangle.
    """
    if len(pivots) < 4:
        return None
    # Strip trailing pivots that are obviously the break-out — pivots
    # whose price is more than 5% beyond the median of the prior 4
    # same-typed pivots.
    trimmed = list(pivots)
    while len(trimmed) >= 6:
        last = trimmed[-1]
        same_typed = [p for p in trimmed[:-1] if p["type"] == last["type"]][-4:]
        if len(same_typed) < 2:
            break
        median = sorted(p["price"] for p in same_typed)[len(same_typed) // 2]
        # 3% deviation from the established support/resistance median
        # is plenty large to mark a break-out pivot. Cleaner than 5%
        # which left genuine break-out pivots inside the boundary fit.
        if abs(last["price"] - median) / max(1e-9, median) > 0.03:
            trimmed = trimmed[:-1]
        else:
            break
    tail = trimmed[-lookback:]
    highs = [p for p in tail if p["type"] == "high"]
    lows = [p for p in tail if p["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None

    hi_slope, hi_b = _line_eq(highs[0], highs[-1])
    lo_slope, lo_b = _line_eq(lows[0], lows[-1])

    # Magnitude of slopes relative to price range — anything below the
    # noise floor counts as "flat".
    price_range = max(p["price"] for p in tail) - min(p["price"] for p in tail)
    if price_range <= 0:
        return None
    slope_floor = 0.05 * price_range / max(1, (tail[-1]["i"] - tail[0]["i"]))

    hi_flat = abs(hi_slope) < slope_floor
    lo_flat = abs(lo_slope) < slope_floor
    hi_down = hi_slope < -slope_floor
    hi_up = hi_slope > slope_floor
    lo_up = lo_slope > slope_floor
    lo_down = lo_slope < -slope_floor

    name = None
    if hi_flat and lo_up:
        name = "ascending_triangle"
    elif hi_down and lo_flat:
        name = "descending_triangle"
    elif hi_down and lo_up:
        name = "symmetric_triangle"
    if name is None:
        return None

    # The triangle "apex" — line intersection. If beyond the chart's
    # right edge, the pattern is still valid; if behind us, it's not.
    if hi_slope == lo_slope:
        return None
    apex_x = (lo_b - hi_b) / (hi_slope - lo_slope)
    if apex_x < tail[-1]["i"] - lookback:
        return None

    # Goodness-of-fit: how close each pivot lies to its boundary line.
    def _fit(points, slope, b):
        if len(points) < 2:
            return 0.0
        residuals = [abs(p["price"] - _line_at(slope, b, p["i"]))
                     for p in points]
        rng = max(p["price"] for p in points) - min(p["price"] for p in points)
        if rng <= 0:
            return 0.0
        return 1.0 - min(1.0, (sum(residuals) / len(residuals)) / rng)

    hi_fit = _fit(highs, hi_slope, hi_b)
    lo_fit = _fit(lows, lo_slope, lo_b)
    conf = (hi_fit + lo_fit) / 2.0

    # Breakout: last bar that closed beyond one of the boundaries.
    breakout_idx = None
    breakout_dir = None
    breakout_boundary = None
    for j in range(tail[-1]["i"] + 1, len(bars)):
        upper = _line_at(hi_slope, hi_b, j)
        lower = _line_at(lo_slope, lo_b, j)
        c = bar_close(bars[j])
        if c > upper:
            breakout_idx, breakout_dir = j, "up"
            breakout_boundary = upper
            break
        if c < lower:
            breakout_idx, breakout_dir = j, "down"
            breakout_boundary = lower
            break

    triangle_height = max(p["price"] for p in highs) - min(p["price"] for p in lows)
    target = None
    if breakout_idx is not None and breakout_boundary is not None:
        if breakout_dir == "up":
            target = breakout_boundary + triangle_height
        else:
            target = breakout_boundary - triangle_height

    return {
        "name": name,
        "hi": (hi_slope, hi_b),
        "lo": (lo_slope, lo_b),
        "highs": highs,
        "lows": lows,
        "confidence": round(conf, 3),
        "breakout_idx": breakout_idx,
        "breakout_dir": breakout_dir,
        "breakout_boundary": breakout_boundary,
        "triangle_height": triangle_height,
        "target": target,
    }


def _detect_head_and_shoulders(bars, pivots) -> Optional[Dict[str, Any]]:
    """Detect H&S (or inverse) from the last 5 pivots ordered H-L-H-L-H
    (or L-H-L-H-L for inverse). Bulkowski: middle peak > outer two;
    outer two within 5% of each other; neckline through the two
    intermediate valleys."""
    if len(pivots) < 5:
        return None
    tail = pivots[-5:]
    types = [p["type"] for p in tail]
    if types == ["high", "low", "high", "low", "high"]:
        s1, v1, head, v2, s2 = tail
        if head["price"] <= s1["price"] or head["price"] <= s2["price"]:
            return None
        if _pct_diff(s1["price"], s2["price"]) > 0.05:
            return None
        name = "head_and_shoulders"
        direction = "down"
    elif types == ["low", "high", "low", "high", "low"]:
        s1, v1, head, v2, s2 = tail
        if head["price"] >= s1["price"] or head["price"] >= s2["price"]:
            return None
        if _pct_diff(s1["price"], s2["price"]) > 0.05:
            return None
        name = "inverse_head_and_shoulders"
        direction = "up"
    else:
        return None

    nk_slope, nk_b = _line_eq(v1, v2)
    head_price = head["price"]
    nk_at_head = _line_at(nk_slope, nk_b, head["i"])
    pattern_height = abs(head_price - nk_at_head)

    # Breakout: bar that crosses the neckline beyond the second
    # shoulder.
    breakout_idx = None
    for j in range(s2["i"] + 1, len(bars)):
        nk = _line_at(nk_slope, nk_b, j)
        c = bar_close(bars[j])
        if direction == "down" and c < nk:
            breakout_idx = j
            break
        if direction == "up" and c > nk:
            breakout_idx = j
            break

    target = None
    if breakout_idx is not None:
        nk_at_break = _line_at(nk_slope, nk_b, breakout_idx)
        target = (nk_at_break - pattern_height) if direction == "down" \
            else (nk_at_break + pattern_height)

    # Confidence from shoulder symmetry.
    sym = 1.0 - _pct_diff(s1["price"], s2["price"]) / 0.05
    sym = max(0.0, min(1.0, sym))
    return {
        "name": name,
        "shoulders": (s1, s2),
        "head": head,
        "neckline": (nk_slope, nk_b),
        "valleys": (v1, v2),
        "direction": direction,
        "pattern_height": pattern_height,
        "confidence": round(sym, 3),
        "breakout_idx": breakout_idx,
        "target": target,
    }


def _detect_double_top_bottom(bars, pivots) -> Optional[Dict[str, Any]]:
    """Last 3 pivots: peak-trough-peak (or trough-peak-trough).
    Bulkowski: paired peaks within 3% of each other; intermediate
    retracement > 38.2%.

    If the most recent pivot lies BEYOND the neckline (i.e. the
    break-out has already happened), the relevant double pattern is
    the prior 3 pivots — drop the break-out pivot before classifying.
    """
    if len(pivots) < 3:
        return None
    # Default: examine the last 3 pivots. But if the last pivot looks
    # like the break-out leg (sliding past the neckline established by
    # the prior 3), slide the window back by one so we classify the
    # 3-pivot pattern itself.
    tail = pivots[-3:]
    if len(pivots) >= 4:
        prior = pivots[-4:-1]
        types_prior = [p["type"] for p in prior]
        last = pivots[-1]
        if types_prior == ["high", "low", "high"]:
            mid_price = prior[1]["price"]
            if last["type"] == "low" and last["price"] < mid_price:
                tail = prior
        elif types_prior == ["low", "high", "low"]:
            mid_price = prior[1]["price"]
            if last["type"] == "high" and last["price"] > mid_price:
                tail = prior
    types = [p["type"] for p in tail]
    # Bulkowski's "Encyclopedia of Chart Patterns" requires a clear
    # intermediate pullback between the paired peaks — a 10% retrace
    # from peak to trough is the operative minimum (not 38.2%, which
    # is a Fibonacci ratio that some retail platforms misappropriate).
    retrace_min = 0.10
    if types == ["high", "low", "high"]:
        p1, mid, p2 = tail
        if _pct_diff(p1["price"], p2["price"]) > 0.03:
            return None
        retrace = (max(p1["price"], p2["price"]) - mid["price"]) / max(
            p1["price"], p2["price"]
        )
        if retrace < retrace_min:
            return None
        name = "double_top"
        direction = "down"
        neckline_price = mid["price"]
        height = max(p1["price"], p2["price"]) - mid["price"]
    elif types == ["low", "high", "low"]:
        p1, mid, p2 = tail
        if _pct_diff(p1["price"], p2["price"]) > 0.03:
            return None
        retrace = (mid["price"] - min(p1["price"], p2["price"])) / mid["price"]
        if retrace < retrace_min:
            return None
        name = "double_bottom"
        direction = "up"
        neckline_price = mid["price"]
        height = mid["price"] - min(p1["price"], p2["price"])
    else:
        return None

    breakout_idx = None
    for j in range(p2["i"] + 1, len(bars)):
        c = bar_close(bars[j])
        if direction == "down" and c < neckline_price:
            breakout_idx = j
            break
        if direction == "up" and c > neckline_price:
            breakout_idx = j
            break

    target = None
    if breakout_idx is not None:
        target = (neckline_price - height) if direction == "down" \
            else (neckline_price + height)

    return {
        "name": name,
        "pivots": tail,
        "neckline_price": neckline_price,
        "height": height,
        "direction": direction,
        "confidence": round(
            1.0 - _pct_diff(p1["price"], p2["price"]) / 0.03, 3,
        ),
        "breakout_idx": breakout_idx,
        "target": target,
    }


def _detect_wedge(bars, pivots, lookback=6) -> Optional[Dict[str, Any]]:
    """Bulkowski wedge: both boundaries slope the SAME direction and
    converge.

    We share the triangle classifier's "trim trailing break-out pivot"
    pre-step so a single dramatic break-out bar doesn't tip a
    flat-bottomed triangle into a false falling-wedge.
    """
    if len(pivots) < 4:
        return None
    trimmed = list(pivots)
    while len(trimmed) >= 6:
        last = trimmed[-1]
        same_typed = [p for p in trimmed[:-1] if p["type"] == last["type"]][-4:]
        if len(same_typed) < 2:
            break
        median = sorted(p["price"] for p in same_typed)[len(same_typed) // 2]
        if abs(last["price"] - median) / max(1e-9, median) > 0.03:
            trimmed = trimmed[:-1]
        else:
            break
    tail = trimmed[-lookback:]
    highs = [p for p in tail if p["type"] == "high"]
    lows = [p for p in tail if p["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    hi_slope, hi_b = _line_eq(highs[0], highs[-1])
    lo_slope, lo_b = _line_eq(lows[0], lows[-1])
    # If either boundary is dead-flat the structure is a triangle, not
    # a wedge — Bulkowski reserves "wedge" for two converging same-
    # signed slopes.
    price_range = max(p["price"] for p in tail) - min(p["price"] for p in tail)
    if price_range > 0:
        flat = 0.02 * price_range / max(1, (tail[-1]["i"] - tail[0]["i"]))
        if abs(hi_slope) < flat or abs(lo_slope) < flat:
            return None
    if hi_slope * lo_slope <= 0:  # opposite signs — not a wedge
        return None
    # Convergence: the gap at the right edge must be smaller than the gap at the left.
    left_gap = _line_at(hi_slope, hi_b, tail[0]["i"]) - _line_at(lo_slope, lo_b, tail[0]["i"])
    right_gap = _line_at(hi_slope, hi_b, tail[-1]["i"]) - _line_at(lo_slope, lo_b, tail[-1]["i"])
    if left_gap <= 0 or right_gap <= 0:
        return None
    if right_gap >= left_gap * 0.9:
        return None
    name = "rising_wedge" if hi_slope > 0 else "falling_wedge"
    # Wedges break opposite to their slope direction (rising wedge = bearish).
    direction = "down" if hi_slope > 0 else "up"
    return {
        "name": name,
        "highs": highs, "lows": lows,
        "hi": (hi_slope, hi_b), "lo": (lo_slope, lo_b),
        "direction": direction,
        "confidence": round(min(1.0, 1.0 - right_gap / max(1e-9, left_gap)), 3),
    }


def _detect_channel(bars, pivots, lookback=6) -> Optional[Dict[str, Any]]:
    """Parallel-channel detection. Murphy: the two boundary slopes must
    be within 15% of each other and span the same direction."""
    if len(pivots) < 4:
        return None
    tail = pivots[-lookback:]
    highs = [p for p in tail if p["type"] == "high"]
    lows = [p for p in tail if p["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    hi_slope, hi_b = _line_eq(highs[0], highs[-1])
    lo_slope, lo_b = _line_eq(lows[0], lows[-1])
    if hi_slope == 0 and lo_slope == 0:
        return None
    if hi_slope == 0 or lo_slope == 0:
        return None
    rel = abs(hi_slope - lo_slope) / max(abs(hi_slope), abs(lo_slope))
    if rel > 0.15:
        return None
    if hi_slope * lo_slope <= 0:
        return None
    name = "channel_up" if hi_slope > 0 else "channel_down"
    return {
        "name": name,
        "highs": highs, "lows": lows,
        "hi": (hi_slope, hi_b), "lo": (lo_slope, lo_b),
        "confidence": round(1.0 - rel / 0.15, 3),
    }


def _detect_flag(bars, pivots) -> Optional[Dict[str, Any]]:
    """Bull/bear flag: sharp impulse (>5% in <10 bars) followed by
    counter-trend consolidation with slope opposite the impulse and
    range < 50% of the impulse."""
    if len(pivots) < 3:
        return None
    # Take the largest move in the last 30 bars as the candidate flagpole.
    candidates = []
    for i in range(len(pivots) - 1):
        a, b = pivots[i], pivots[i + 1]
        span = b["i"] - a["i"]
        move_pct = abs(b["price"] - a["price"]) / max(1e-9, a["price"])
        if span <= 10 and move_pct >= 0.05:
            candidates.append((a, b, move_pct))
    if not candidates:
        return None
    a, b, impulse_pct = max(candidates, key=lambda t: t[2])
    if b is not pivots[-1] and b is not pivots[-2]:
        # The flagpole should be near the end of the series.
        return None
    direction = "up" if b["price"] > a["price"] else "down"
    # Subsequent pivots = consolidation; require slope opposite the impulse.
    after = [p for p in pivots if p["i"] > b["i"]]
    if len(after) < 2:
        return None
    slope, b_int = _line_eq(after[0], after[-1])
    if direction == "up" and slope >= 0:
        return None
    if direction == "down" and slope <= 0:
        return None
    cons_range = max(p["price"] for p in after) - min(p["price"] for p in after)
    impulse_range = abs(b["price"] - a["price"])
    if cons_range > 0.5 * impulse_range:
        return None
    name = "bull_flag" if direction == "up" else "bear_flag"
    return {
        "name": name,
        "pole": (a, b),
        "consolidation": after,
        "slope": slope, "intercept": b_int,
        "direction": direction,
        "confidence": round(min(1.0, impulse_pct / 0.10), 3),
    }


def _select_best(*candidates) -> Optional[Dict[str, Any]]:
    """Pick the highest-confidence candidate. Triangles take precedence
    over wedges at equal-confidence ties because a triangle with a flat
    boundary frequently misclassifies as a wedge with same-sign-but-tiny
    slopes — Bulkowski's encyclopedia treats them as distinct
    structures."""
    pool = [c for c in candidates if c]
    if not pool:
        return None
    tri_pref = {"ascending_triangle": 0.05, "descending_triangle": 0.05,
                "symmetric_triangle": 0.05}
    return max(
        pool,
        key=lambda c: (c.get("confidence") or 0.0)
                       + tri_pref.get(c.get("name", ""), 0.0),
    )


# ── builders ──────────────────────────────────────────────────────────


def _render_triangle(pat, bars) -> Tuple[List[Line], List[Marker], List[Zone], List[str], List[Dict[str, Any]]]:
    lines: List[Line] = []
    markers: List[Marker] = []
    zones: List[Zone] = []
    notes: List[str] = []
    extras: List[Dict[str, Any]] = []
    hi_slope, hi_b = pat["hi"]
    lo_slope, lo_b = pat["lo"]
    highs = pat["highs"]
    lows = pat["lows"]
    last_i = len(bars) - 1
    # Upper boundary — "Falling resistance" / "Horizontal resistance".
    upper_label = "Falling resistance" if pat["name"] == "descending_triangle" \
        else ("Horizontal resistance" if pat["name"] == "ascending_triangle"
               else "Upper boundary")
    lower_label = "Horizontal support" if pat["name"] == "descending_triangle" \
        else ("Rising support" if pat["name"] == "ascending_triangle"
               else "Lower boundary")
    lines.append(Line(
        kind="trendline",
        start={"ts": highs[0]["ts"], "price": _line_at(hi_slope, hi_b, highs[0]["i"])},
        end={"ts": bar_ts(bars[last_i]), "price": _line_at(hi_slope, hi_b, last_i)},
        color="#ff5a5f", width=2, style="solid", label=upper_label,
    ))
    lines.append(Line(
        kind="trendline",
        start={"ts": lows[0]["ts"], "price": _line_at(lo_slope, lo_b, lows[0]["i"])},
        end={"ts": bar_ts(bars[last_i]), "price": _line_at(lo_slope, lo_b, last_i)},
        color="#36c26b", width=2, style="solid", label=lower_label,
    ))
    # Callout boxes — frontend draws these on the canvas overlay.
    mid_hi_i = (highs[0]["i"] + highs[-1]["i"]) // 2
    mid_lo_i = (lows[0]["i"] + lows[-1]["i"]) // 2
    mid_hi_i = max(0, min(last_i, mid_hi_i))
    mid_lo_i = max(0, min(last_i, mid_lo_i))
    extras.append({
        "kind": "callout",
        "label": upper_label,
        "anchor_ts": bar_ts(bars[mid_hi_i]),
        "anchor_price": _line_at(hi_slope, hi_b, mid_hi_i),
        "color": "#ff5a5f",
        "placement": "above",
    })
    extras.append({
        "kind": "callout",
        "label": lower_label,
        "anchor_ts": bar_ts(bars[mid_lo_i]),
        "anchor_price": _line_at(lo_slope, lo_b, mid_lo_i),
        "color": "#36c26b",
        "placement": "below",
    })
    if pat["breakout_idx"] is not None:
        bo = pat["breakout_idx"]
        markers.append(Marker(
            ts=bar_ts(bars[bo]), price=bar_close(bars[bo]),
            label=("Sell here" if pat["breakout_dir"] == "down" else "Buy here"),
            color=("#ff5a5f" if pat["breakout_dir"] == "down" else "#36c26b"),
            shape=("arrow_down" if pat["breakout_dir"] == "down" else "arrow_up"),
        ))
        extras.append({
            "kind": "callout",
            "label": ("Sell here" if pat["breakout_dir"] == "down" else "Buy here"),
            "anchor_ts": bar_ts(bars[bo]),
            "anchor_price": float(bar_close(bars[bo])),
            "color": ("#ff5a5f" if pat["breakout_dir"] == "down" else "#36c26b"),
            "placement": ("above" if pat["breakout_dir"] == "down" else "below"),
            "emphasis": "strong",
        })
        if pat["target"] is not None:
            markers.append(Marker(
                ts=bar_ts(bars[last_i]), price=pat["target"],
                label="Projected target", color="#ffc107", shape="text",
            ))
            # Shaded projection zone between breakout and target.
            zones.append(Zone(
                x1=bar_ts(bars[bo]), y1=bar_close(bars[bo]),
                x2=bar_ts(bars[last_i]), y2=pat["target"],
                color="#ffc107", opacity=0.10,
                label="Projected target",
            ))
            extras.append({
                "kind": "callout",
                "label": f"Projected target {pat['target']:.2f}",
                "anchor_ts": bar_ts(bars[last_i]),
                "anchor_price": float(pat["target"]),
                "color": "#ffc107",
                "placement": ("below" if pat["breakout_dir"] == "down" else "above"),
            })
        if _vol_confirmed(bars, bo):
            notes.append("Volume spike with break-out bar")
            extras.append({
                "kind": "volume_callout",
                "label": "Volume spike — break-out confirmed (>1.5× 20-bar avg)",
                "anchor_ts": bar_ts(bars[bo]),
                "color": "#ffc107",
            })
        else:
            notes.append("Break-out detected, but volume not confirmed (<1.5× avg)")
    else:
        notes.append("Pattern still forming — no break-out yet.")
    return lines, markers, zones, notes, extras


def _render_hs(pat, bars) -> Tuple[List[Line], List[Marker], List[Zone], List[str], List[Dict[str, Any]]]:
    lines: List[Line] = []
    markers: List[Marker] = []
    zones: List[Zone] = []
    notes: List[str] = []
    s1, s2 = pat["shoulders"]
    head = pat["head"]
    v1, v2 = pat["valleys"]
    nk_slope, nk_b = pat["neckline"]
    last_i = len(bars) - 1
    # Neckline.
    lines.append(Line(
        kind="trendline",
        start={"ts": v1["ts"], "price": _line_at(nk_slope, nk_b, v1["i"])},
        end={"ts": bar_ts(bars[last_i]), "price": _line_at(nk_slope, nk_b, last_i)},
        color="#ffc107", width=2, style="dashed", label="Neckline",
    ))
    for tag, pt in (("LS", s1), ("Head", head), ("RS", s2)):
        markers.append(Marker(
            ts=pt["ts"], price=pt["price"], label=tag,
            color="#9aa4b2", shape="text",
        ))
    if pat["breakout_idx"] is not None:
        bo = pat["breakout_idx"]
        markers.append(Marker(
            ts=bar_ts(bars[bo]), price=bar_close(bars[bo]),
            label=("Sell here" if pat["direction"] == "down" else "Buy here"),
            color=("#ff5a5f" if pat["direction"] == "down" else "#36c26b"),
            shape=("arrow_down" if pat["direction"] == "down" else "arrow_up"),
        ))
        if pat["target"] is not None:
            markers.append(Marker(
                ts=bar_ts(bars[last_i]), price=pat["target"],
                label="Projected target", color="#ffc107", shape="text",
            ))
        if _vol_confirmed(bars, bo):
            notes.append("Volume spike with break-out bar")
    return lines, markers, zones, notes, []


def _render_double(pat, bars) -> Tuple[List[Line], List[Marker], List[Zone], List[str], List[Dict[str, Any]]]:
    lines: List[Line] = []
    markers: List[Marker] = []
    notes: List[str] = []
    p1, mid, p2 = pat["pivots"]
    last_i = len(bars) - 1
    lines.append(Line(
        kind="horizontal",
        start={"ts": p1["ts"], "price": pat["neckline_price"]},
        end={"ts": bar_ts(bars[last_i]), "price": pat["neckline_price"]},
        color="#ffc107", width=2, style="dashed", label="Neckline",
    ))
    for label, pt in (("Top 1" if pat["direction"] == "down" else "Btm 1", p1),
                       ("Top 2" if pat["direction"] == "down" else "Btm 2", p2)):
        markers.append(Marker(
            ts=pt["ts"], price=pt["price"], label=label,
            color="#9aa4b2", shape="text",
        ))
    if pat["breakout_idx"] is not None:
        bo = pat["breakout_idx"]
        markers.append(Marker(
            ts=bar_ts(bars[bo]), price=bar_close(bars[bo]),
            label=("Sell here" if pat["direction"] == "down" else "Buy here"),
            color=("#ff5a5f" if pat["direction"] == "down" else "#36c26b"),
            shape=("arrow_down" if pat["direction"] == "down" else "arrow_up"),
        ))
        if pat["target"] is not None:
            markers.append(Marker(
                ts=bar_ts(bars[last_i]), price=pat["target"],
                label="Projected target", color="#ffc107", shape="text",
            ))
        if _vol_confirmed(bars, bo):
            notes.append("Volume spike with break-out bar")
    return lines, markers, [], notes, []


def _render_wedge_or_channel(pat, bars) -> Tuple[List[Line], List[Marker], List[Zone], List[str], List[Dict[str, Any]]]:
    lines: List[Line] = []
    notes: List[str] = []
    hi_slope, hi_b = pat["hi"]
    lo_slope, lo_b = pat["lo"]
    highs = pat["highs"]
    lows = pat["lows"]
    last_i = len(bars) - 1
    lines.append(Line(
        kind="trendline",
        start={"ts": highs[0]["ts"], "price": _line_at(hi_slope, hi_b, highs[0]["i"])},
        end={"ts": bar_ts(bars[last_i]), "price": _line_at(hi_slope, hi_b, last_i)},
        color="#ff5a5f", width=2, style="solid",
        label="Upper boundary",
    ))
    lines.append(Line(
        kind="trendline",
        start={"ts": lows[0]["ts"], "price": _line_at(lo_slope, lo_b, lows[0]["i"])},
        end={"ts": bar_ts(bars[last_i]), "price": _line_at(lo_slope, lo_b, last_i)},
        color="#36c26b", width=2, style="solid",
        label="Lower boundary",
    ))
    notes.append(
        "Wedges break opposite to their slope direction (rising→bearish, falling→bullish)."
        if pat["name"].endswith("wedge")
        else "Trade with the channel; reversal candidates at the boundary touches."
    )
    return lines, [], [], notes, []


def _render_flag(pat, bars) -> Tuple[List[Line], List[Marker], List[Zone], List[str], List[Dict[str, Any]]]:
    a, b = pat["pole"]
    after = pat["consolidation"]
    slope, intercept = pat["slope"], pat["intercept"]
    last_i = len(bars) - 1
    lines: List[Line] = [
        Line(
            kind="trendline",
            start={"ts": a["ts"], "price": a["price"]},
            end={"ts": b["ts"], "price": b["price"]},
            color="#ffc107", width=3, style="solid", label="Flagpole",
        ),
        Line(
            kind="trendline",
            start={"ts": after[0]["ts"],
                    "price": _line_at(slope, intercept, after[0]["i"])},
            end={"ts": bar_ts(bars[last_i]),
                  "price": _line_at(slope, intercept, last_i)},
            color="#9aa4b2", width=2, style="dashed", label="Consolidation",
        ),
    ]
    notes = [
        f"{pat['name'].replace('_', ' ').title()} — projected target equals flagpole length added at the breakout."
    ]
    return lines, [], [], notes, []


# ── public entry point ───────────────────────────────────────────────


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    """Run pattern detection on ``bars`` and return an annotation."""
    params = dict(params or {})
    zigzag_pct = float(
        params.get("zigzag_pct", getattr(TUNABLES, "theory_zigzag_pct", 3.0))
    )
    min_confidence = float(params.get("min_confidence", 0.40))
    params.setdefault("zigzag_pct", zigzag_pct)
    params.setdefault("min_confidence", min_confidence)

    ann = TheoryAnnotation(
        theory="price_action",
        params=params,
        citation=(
            "Bulkowski, 'Encyclopedia of Chart Patterns' (3rd ed., Wiley 2021); "
            "Murphy, 'Technical Analysis of the Financial Markets' (NYIF 1999)."
        ),
    )
    if not bars:
        ann.notes.append("No bars supplied.")
        return ann

    pivots = detect_pivots(bars, threshold_pct=zigzag_pct)
    if not pivots:
        ann.notes.append("Not enough price movement to form pivots at the chosen threshold.")
        return ann

    # ZigZag itself — render as thin grey lines so the operator sees the
    # underlying structure even when no pattern matches.
    for i in range(1, len(pivots)):
        a, b = pivots[i - 1], pivots[i]
        ann.lines.append(Line(
            kind="trendline",
            start={"ts": a["ts"], "price": a["price"]},
            end={"ts": b["ts"], "price": b["price"]},
            color="#5b6470", width=1, style="dotted", label=None,
        ))

    triangle = _detect_triangle(bars, pivots)
    hs = _detect_head_and_shoulders(bars, pivots)
    dbl = _detect_double_top_bottom(bars, pivots)
    wedge = _detect_wedge(bars, pivots)
    channel = _detect_channel(bars, pivots)
    flag = _detect_flag(bars, pivots)
    best = _select_best(triangle, hs, dbl, wedge, channel, flag)
    if best is None or (best.get("confidence") or 0.0) < min_confidence:
        ann.notes.append(
            "No high-confidence pattern detected at zigzag {:.1f}%. ZigZag pivots only.".format(zigzag_pct)
        )
        ann.primer = {
            "what_it_measures": (
                "Classical chart patterns are recurring shapes in price "
                "that Bulkowski statistically catalogued. They measure "
                "supply/demand geometry directly — no smoothing, no "
                "indicator lag."
            ),
            "how_to_read": (
                "The ZigZag pivots show the underlying swing structure. "
                "Lower the ZigZag % parameter to find smaller patterns; "
                "raise it to focus on major structural turns. A real "
                "pattern needs at least 2 boundary touches on each side."
            ),
            "key_levels_now": (
                f"No qualifying pattern at the current ZigZag threshold "
                f"({zigzag_pct:.1f}%). Try lowering it or widening the "
                "window."
            ),
        }
        return ann

    ann.pattern_name = best["name"]
    ann.confidence = float(best.get("confidence") or 0.0)
    if best["name"].endswith("triangle"):
        ls, ms, zs, ns, ex = _render_triangle(best, bars)
    elif best["name"].endswith("head_and_shoulders"):
        ls, ms, zs, ns, ex = _render_hs(best, bars)
    elif best["name"] in ("double_top", "double_bottom"):
        ls, ms, zs, ns, ex = _render_double(best, bars)
    elif best["name"].endswith("wedge") or best["name"].startswith("channel"):
        ls, ms, zs, ns, ex = _render_wedge_or_channel(best, bars)
    elif best["name"].endswith("flag"):
        ls, ms, zs, ns, ex = _render_flag(best, bars)
    else:
        ls, ms, zs, ns, ex = ([], [], [], [], [])
    ann.lines.extend(ls)
    ann.markers.extend(ms)
    ann.zones.extend(zs)
    ann.notes.extend(ns)
    ann.extras.extend(ex)
    if ann.confidence < 0.65:
        ann.notes.insert(0, "auto-detected, please verify")

    # Theory primer — pattern-specific where possible, generic otherwise.
    pattern_blurbs = {
        "ascending_triangle": (
            "An ascending triangle has a flat resistance and a rising "
            "support — buyers absorbing supply at one fixed price while "
            "lows climb. Bulkowski's empirical edge: bullish break ~70% "
            "of the time, target = triangle height added at break."
        ),
        "descending_triangle": (
            "A descending triangle has a flat support and a falling "
            "resistance — sellers distributing into one fixed bid while "
            "highs decline. Bulkowski's edge: bearish break ~64% of the "
            "time, target = triangle height subtracted at break."
        ),
        "symmetric_triangle": (
            "A symmetric triangle has both boundaries converging — "
            "compressed volatility before a directional resolution. "
            "Direction of break is the signal; volume confirmation is "
            "essential."
        ),
        "head_and_shoulders": (
            "Three peaks with the middle higher than the outer two; the "
            "valleys between form the neckline. A close below the "
            "neckline triggers a measured-move target equal to the head-"
            "to-neckline height projected down from the break."
        ),
        "inverse_head_and_shoulders": (
            "Bullish mirror of H&S: three troughs with the middle deeper "
            "than the outer two. A close above the neckline triggers a "
            "measured-move up target equal to the head-to-neckline height."
        ),
        "double_top": (
            "Two peaks at near-identical price with a meaningful "
            "intermediate pullback. Confirms with a close below the "
            "intermediate trough (neckline)."
        ),
        "double_bottom": (
            "Two troughs at near-identical price with a meaningful "
            "intermediate rally. Confirms with a close above the "
            "intermediate peak (neckline)."
        ),
        "rising_wedge": (
            "Both boundaries slope up but the upper rises slower than "
            "the lower — supply compressing into the apex. Wedges break "
            "OPPOSITE their slope: rising wedge is bearish."
        ),
        "falling_wedge": (
            "Both boundaries slope down but the lower falls slower than "
            "the upper — demand compressing into the apex. Wedges break "
            "OPPOSITE their slope: falling wedge is bullish."
        ),
        "channel_up": (
            "Parallel up-sloping boundaries. Trade in the direction of "
            "the channel; mean-revert at the boundary touches."
        ),
        "channel_down": (
            "Parallel down-sloping boundaries. Trade in the direction of "
            "the channel; mean-revert at the boundary touches."
        ),
        "bull_flag": (
            "Sharp impulse up (the 'flagpole') followed by a small "
            "counter-trend consolidation (the 'flag'). Continuation "
            "pattern — target = flagpole length added at the flag break."
        ),
        "bear_flag": (
            "Sharp impulse down (the 'flagpole') followed by a small "
            "counter-trend consolidation (the 'flag'). Continuation "
            "pattern — target = flagpole length subtracted at the break."
        ),
    }
    blurb = pattern_blurbs.get(best["name"], "Chart pattern detected.")
    last_close = bar_close(bars[-1])
    breakout_msg = ""
    if best.get("breakout_idx") is not None and best.get("target") is not None:
        breakout_msg = (
            f" Break-out fired at bar {best['breakout_idx']}; projected "
            f"target {best['target']:.2f} vs spot {last_close:.2f}."
        )
    elif best.get("breakout_idx") is not None:
        breakout_msg = f" Break-out fired at bar {best['breakout_idx']}."
    else:
        breakout_msg = " Pattern still forming — no break-out yet."
    ann.primer = {
        "what_it_measures": (
            "Classical chart patterns are recurring shapes in price that "
            "Bulkowski statistically catalogued across 200,000+ historical "
            "patterns. Each pattern has a measurable boundary geometry, a "
            "break-out trigger, and a measured-move target — making them "
            "the most disciplined form of pure-price trading."
        ),
        "how_to_read": (
            blurb +
            " Look for two confirmations: (a) a CLOSE through the boundary "
            "(intraday spikes don't count), and (b) a volume spike on the "
            "break-out bar (>1.5× the 20-bar average). Without volume, "
            "treat the break as suspect."
        ),
        "key_levels_now": (
            f"Detected: {best['name'].replace('_', ' ').title()} "
            f"@ {int(ann.confidence*100)}% confidence.{breakout_msg}"
        ),
    }
    return ann


__all__ = ["analyze"]
