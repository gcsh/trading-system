"""Gann fans + time cycles + retracements.

Implements the angles and time cycles described in:

  * W. D. Gann, "How to Make Profits in Commodities" (Lambert-Gann
    Publishing, 1942) — Chapters on geometric angles and Gann lines.
  * W. D. Gann, "45 Years in Wall Street" (1949) — time-cycle days
    (30/60/90/120/144/180/240/270/360).
  * Robert Krausz, "A W. D. Gann Treasure Discovered" (Geometric
    Traders Institute, 1998) — Gann Wheel + the canonical fan ratios
    used by modern platforms (TradingView's Gann fan, MetaTrader's
    GannFan indicator).

Canonical Gann unit (this is the "1×1" line slope) is defined as the
**price-per-bar** baseline that makes the 1×1 angle visually 45° on a
calibrated chart. We compute it from the recent N-bar price range:

    unit = (highest_high - lowest_low) over last N bars  / N

Fan ratios (slope in price-per-bar):

    8×1 = 8·unit   (very steep ↑)
    4×1 = 4·unit
    3×1 = 3·unit
    2×1 = 2·unit
    1×1 = 1·unit   (the "main" 45°)
    1×2 = unit / 2
    1×3 = unit / 3
    1×4 = unit / 4
    1×8 = unit / 8 (very shallow ↑)

For a swing-high pivot the fan slopes downward (negative); for a
swing-low pivot it slopes upward (positive).

Time cycles (vertical lines at bar counts from the pivot): 30, 45, 60,
90, 120, 144, 180, 240, 270, 360, 540, 720.

Retracements (1/8 levels): 12.5, 25, 37.5, 50, 62.5, 75, 87.5 percent
of the swing range.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

from ._zigzag import detect_pivots
from .schema import (
    Line, Marker, TheoryAnnotation, Zone,
    bar_close, bar_high, bar_low, bar_ts,
)


# Fan colour convention matches the operator's Gann reference image:
#   1×1 black, 1×2 / 2×1 red, 1×3 / 3×1 blue, 1×4 / 4×1 black (lighter).
FAN_RATIOS: List[Dict[str, Any]] = [
    {"name": "1x8", "slope_mult": 1.0 / 8.0, "color": "#444",  "width": 1, "rank": 8},
    {"name": "1x4", "slope_mult": 1.0 / 4.0, "color": "#222",  "width": 1, "rank": 4},
    {"name": "1x3", "slope_mult": 1.0 / 3.0, "color": "#1f6feb", "width": 1, "rank": 3},
    {"name": "1x2", "slope_mult": 1.0 / 2.0, "color": "#d63a3a", "width": 1, "rank": 2},
    {"name": "1x1", "slope_mult": 1.0,         "color": "#000",  "width": 2, "rank": 1},
    {"name": "2x1", "slope_mult": 2.0,         "color": "#d63a3a", "width": 1, "rank": 2},
    {"name": "3x1", "slope_mult": 3.0,         "color": "#1f6feb", "width": 1, "rank": 3},
    {"name": "4x1", "slope_mult": 4.0,         "color": "#222",  "width": 1, "rank": 4},
    {"name": "8x1", "slope_mult": 8.0,         "color": "#444",  "width": 1, "rank": 8},
]

TIME_CYCLES = [30, 45, 60, 90, 120, 144, 180, 240, 270, 360, 540, 720]

RETRACE_LEVELS = [0.125, 0.250, 0.375, 0.500, 0.625, 0.750, 0.875]


def _gann_unit(bars: List[Dict[str, Any]], lookback: int) -> float:
    """Price-per-bar baseline. ``(rolling-range) / lookback``."""
    lo = max(0, len(bars) - lookback)
    window = bars[lo:]
    if not window:
        return 0.0
    hi = max(bar_high(b) for b in window)
    lo_ = min(bar_low(b) for b in window if bar_low(b) > 0)
    if hi <= 0 or lo_ <= 0:
        return 0.0
    n = max(1, len(window))
    return float((hi - lo_) / n)


def _auto_pivot(bars: List[Dict[str, Any]], lookback: int,
                  zigzag_pct: float) -> Optional[Dict[str, Any]]:
    """Pick the most recent significant pivot inside ``lookback`` bars.
    Prefers swing low for an uptrending market, swing high otherwise."""
    if not bars:
        return None
    lo = max(0, len(bars) - lookback)
    window = bars[lo:]
    pivots = detect_pivots(window, threshold_pct=zigzag_pct)
    if not pivots:
        # Fall back to the absolute extreme in the window.
        hi_idx = max(range(len(window)), key=lambda i: bar_high(window[i]))
        lo_idx = min(range(len(window)), key=lambda i: bar_low(window[i]))
        if bar_close(window[-1]) > bar_close(window[0]):
            return {"i": lo + lo_idx, "ts": bar_ts(bars[lo + lo_idx]),
                     "price": bar_low(bars[lo + lo_idx]), "type": "low"}
        return {"i": lo + hi_idx, "ts": bar_ts(bars[lo + hi_idx]),
                 "price": bar_high(bars[lo + hi_idx]), "type": "high"}
    # Translate pivot indices back to the global bar index.
    last = pivots[-1]
    last_global = {**last, "i": lo + last["i"]}
    # Prefer a swing-low anchor when the latest series trends up.
    if bar_close(bars[-1]) > bar_close(bars[lo]):
        for p in reversed(pivots):
            if p["type"] == "low":
                return {**p, "i": lo + p["i"]}
    else:
        for p in reversed(pivots):
            if p["type"] == "high":
                return {**p, "i": lo + p["i"]}
    return last_global


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _add_bars(bars: List[Dict[str, Any]], from_idx: int,
                count: int) -> str:
    """Return the ISO timestamp ``count`` bars after ``from_idx``. When
    the target bar is past the end of ``bars`` we extrapolate using the
    median inter-bar spacing."""
    target = from_idx + count
    if 0 <= target < len(bars):
        return bar_ts(bars[target])
    # Extrapolate using the median delta of the trailing bars.
    if len(bars) < 2:
        return bar_ts(bars[-1]) if bars else ""
    tail = bars[-min(50, len(bars)):]
    deltas = []
    for i in range(1, len(tail)):
        a = _parse_ts(bar_ts(tail[i - 1]))
        b = _parse_ts(bar_ts(tail[i]))
        if a and b:
            deltas.append((b - a).total_seconds())
    if not deltas:
        return bar_ts(bars[-1])
    deltas.sort()
    med = deltas[len(deltas) // 2]
    last_ts = _parse_ts(bar_ts(bars[-1])) or datetime.utcnow()
    overshoot = target - (len(bars) - 1)
    out = last_ts + timedelta(seconds=med * overshoot)
    return out.isoformat()


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    lookback = int(params.get("unit_lookback", 60))
    zigzag_pct = float(params.get("zigzag_pct", getattr(TUNABLES, "theory_zigzag_pct", 3.0)))
    show_retracements = bool(params.get("show_retracements", True))
    show_time_cycles = bool(params.get("show_time_cycles", True))
    show_fan = bool(params.get("show_fan", True))

    ann = TheoryAnnotation(
        theory="gann",
        params={
            **params,
            "unit_lookback": lookback,
            "zigzag_pct": zigzag_pct,
            "show_retracements": show_retracements,
            "show_time_cycles": show_time_cycles,
            "show_fan": show_fan,
        },
        citation=(
            "W. D. Gann, 'How to Make Profits in Commodities' (1942); "
            "'45 Years in Wall Street' (1949); "
            "Krausz, 'A W. D. Gann Treasure Discovered' (1998)."
        ),
    )
    if not bars:
        ann.notes.append("No bars supplied.")
        return ann

    unit = _gann_unit(bars, lookback)
    if unit <= 0:
        ann.notes.append("Could not compute a Gann unit (price range is zero).")
        return ann
    ann.params["unit_price_per_bar"] = round(unit, 6)

    # Pivot — operator may pin via params["pivot_index"] / ["pivot_ts"].
    pivot: Optional[Dict[str, Any]] = None
    if params.get("pivot_index") is not None:
        try:
            idx = int(params["pivot_index"])
        except Exception:
            idx = -1
        if 0 <= idx < len(bars):
            b = bars[idx]
            pivot = {
                "i": idx, "ts": bar_ts(b),
                "price": bar_low(b),
                "type": params.get("pivot_type") or "low",
            }
    if pivot is None and params.get("pivot_ts"):
        target = _parse_ts(str(params["pivot_ts"]))
        if target is not None:
            best_i = min(
                range(len(bars)),
                key=lambda i: abs((_parse_ts(bar_ts(bars[i])) or datetime.min) - target),
            )
            b = bars[best_i]
            pivot = {
                "i": best_i, "ts": bar_ts(b),
                "price": bar_low(b) if params.get("pivot_type", "low") == "low" else bar_high(b),
                "type": params.get("pivot_type", "low"),
            }
    if pivot is None:
        pivot = _auto_pivot(bars, lookback, zigzag_pct)
    if pivot is None:
        ann.notes.append("Could not find a swing pivot to anchor the fan.")
        return ann

    ann.params["pivot"] = {
        "i": pivot["i"], "ts": pivot["ts"],
        "price": float(pivot["price"]),
        "type": pivot["type"],
    }
    # Confidence: the fan only "fits" the chart well when the pivot is a
    # genuine extreme. Use the depth of the pivot's price vs the nearby
    # bars as a proxy.
    sign = -1.0 if pivot["type"] == "high" else 1.0

    # ── Fan rays ────────────────────────────────────────────────────
    if show_fan:
        last_i = len(bars) - 1
        # Interpretation matrix: a swing-low pivot's UPWARD fan rays are
        # "support" angles (price stays above them in an uptrend); a
        # swing-high pivot's DOWNWARD fan rays are "resistance" angles.
        direction = "support" if pivot["type"] == "low" else "resistance"
        for fan in FAN_RATIOS:
            slope = unit * fan["slope_mult"] * sign
            end_price = pivot["price"] + slope * (last_i - pivot["i"])
            mult = fan["slope_mult"]
            if mult > 1:
                ratio = f"{fan['rank']}x1"
                interp = (
                    f"steep — strong {direction}"
                    if mult >= 4 else f"steep {direction}"
                )
            elif mult < 1:
                ratio = f"1x{fan['rank']}"
                interp = (
                    f"shallow — weak {direction}"
                    if mult <= 1.0 / 4.0 else f"half-speed {direction}"
                )
            else:
                ratio = "1x1"
                interp = f"45° — neutral {direction}"
            ann.lines.append(Line(
                kind="fan",
                start={"ts": pivot["ts"], "price": float(pivot["price"])},
                end={"ts": bar_ts(bars[last_i]), "price": float(end_price)},
                color=fan["color"], width=int(fan["width"]),
                style="solid",
                label=f"{ratio} ({interp})",
                meta={
                    "ratio": ratio,
                    "direction": direction,
                    "interpretation": interp,
                    "slope_mult": float(mult),
                    "end_price": float(end_price),
                },
            ))
        # Shaded fan area between 1×1 and 1×4 (matches the reference image's
        # "support fan" colouring).
        slope_strong = unit * 1.0 * sign
        slope_weak = unit * (1.0 / 4.0) * sign
        y_strong = pivot["price"] + slope_strong * (last_i - pivot["i"])
        y_weak = pivot["price"] + slope_weak * (last_i - pivot["i"])
        ann.zones.append(Zone(
            x1=pivot["ts"], y1=float(pivot["price"]),
            x2=bar_ts(bars[last_i]), y2=float(y_strong),
            color="#7fc8a9", opacity=0.10,
            label="1:1 ↔ 1:4 fan area",
        ))
        ann.zones.append(Zone(
            x1=pivot["ts"], y1=float(pivot["price"]),
            x2=bar_ts(bars[last_i]), y2=float(y_weak),
            color="#7fc8a9", opacity=0.06,
        ))

    # ── Time cycles ─────────────────────────────────────────────────
    if show_time_cycles:
        for n in TIME_CYCLES:
            target_ts = _add_bars(bars, pivot["i"], n)
            target_i = pivot["i"] + n
            label_dt = ""
            if 0 <= target_i < len(bars):
                t = _parse_ts(bar_ts(bars[target_i]))
                if t:
                    label_dt = t.strftime("%a %d %b %y")
            else:
                t = _parse_ts(target_ts)
                if t:
                    label_dt = t.strftime("%a %d %b %y")
            # Alternate vertical line colour (red ⇆ blue) so adjacent
            # cycles are visually distinguishable (mirrors the reference
            # image).
            color = "#d63a3a" if (TIME_CYCLES.index(n) % 2 == 0) else "#1f6feb"
            ann.lines.append(Line(
                kind="vertical",
                start={"ts": target_ts, "price": float(pivot["price"])},
                end={"ts": target_ts, "price": float(pivot["price"])},
                color=color, width=1, style="dashed",
                label=f"{n} / {label_dt}" if label_dt else f"{n}",
                meta={
                    "bar_count": int(n),
                    "target_ts": target_ts,
                    "target_date": label_dt,
                    "kind": "time_cycle",
                },
            ))

    # ── Retracements ────────────────────────────────────────────────
    if show_retracements:
        last_i = len(bars) - 1
        # Anchor 2 = pivot; anchor 1 = opposite extreme inside the lookback.
        lo = max(0, pivot["i"] - lookback)
        window = bars[lo:pivot["i"] + 1] or bars[:pivot["i"] + 1]
        if pivot["type"] == "low":
            anchor_high = max(bar_high(b) for b in window) if window else pivot["price"]
            anchor_low = pivot["price"]
        else:
            anchor_high = pivot["price"]
            anchor_low = min(bar_low(b) for b in window if bar_low(b) > 0) \
                if window else pivot["price"]
        height = anchor_high - anchor_low
        if height > 0:
            for level in RETRACE_LEVELS:
                price = anchor_low + height * level
                ann.lines.append(Line(
                    kind="horizontal",
                    start={"ts": pivot["ts"], "price": price},
                    end={"ts": bar_ts(bars[last_i]), "price": price},
                    color="#9aa4b2", width=1, style="dotted",
                    label=f"{level*100:.1f}%",
                ))
    ann.markers.append(Marker(
        ts=pivot["ts"], price=float(pivot["price"]),
        label=("Swing low pivot" if pivot["type"] == "low" else "Swing high pivot"),
        color="#ffc107", shape=("arrow_up" if pivot["type"] == "low" else "arrow_down"),
    ))
    ann.confidence = 0.75  # Gann fans are operator-anchored — confidence is a self-report.
    spot = bar_close(bars[-1])
    diff = spot - pivot["price"]
    bars_elapsed = max(1, (len(bars) - 1) - pivot["i"])
    impulse_per_bar = diff / bars_elapsed
    # Which fan ray is the price moving closest to (in absolute terms)?
    if unit > 0:
        ratios = [
            ("1x8", unit / 8.0), ("1x4", unit / 4.0), ("1x3", unit / 3.0),
            ("1x2", unit / 2.0), ("1x1", unit), ("2x1", unit * 2.0),
            ("3x1", unit * 3.0), ("4x1", unit * 4.0), ("8x1", unit * 8.0),
        ]
        nearest = min(ratios, key=lambda r: abs(abs(impulse_per_bar) - r[1]))
        nearest_ratio = nearest[0]
    else:
        nearest_ratio = "—"
    side = "above" if diff > 0 else "below"
    pivot_label = "swing-low" if pivot["type"] == "low" else "swing-high"
    ann.primer = {
        "what_it_measures": (
            "Gann fans translate the famous 1×1 angle (a 45° line where "
            "price moves one unit per one unit of time) into a set of "
            "supporting/resistance rays. The unit comes from the rolling "
            "price range divided by the lookback (so the geometry is "
            "calibrated to *this* market, not an arbitrary scale). Time "
            "cycles are Gann's empirical 'memory bars' — 30/45/60/90/120/"
            "144/180/270/360 days from a major pivot — where reversals "
            "cluster historically."
        ),
        "how_to_read": (
            "The 1×1 ray is the trend's heartbeat. While price respects "
            "the 1×1 from above (after a swing-low pivot), the uptrend is "
            "healthy. A drop below the 1×1 typically migrates the next "
            "support down to 1×2, then 1×3, then 1×4 — each weaker than "
            "the last. Steep rays (2×1, 3×1, 4×1) are blow-off / "
            "capitulation angles — visited briefly, rarely sustained. "
            "When price meets a time-cycle vertical at a fan-ray "
            "horizontal, expect a reversal or pause."
        ),
        "key_levels_now": (
            f"Anchor: {pivot_label} at {pivot['price']:.2f} on "
            f"{pivot['ts'][:10]}. Spot {spot:.2f} is {abs(diff):.2f} "
            f"{side} the pivot, tracking near the {nearest_ratio} ray "
            f"({impulse_per_bar:+.4f}/bar vs unit {unit:.4f}/bar)."
        ),
    }
    return ann


__all__ = ["analyze", "FAN_RATIOS", "TIME_CYCLES", "RETRACE_LEVELS"]
