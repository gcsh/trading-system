"""ZigZag pivot detector — shared helper for price-action + Gann + Fib.

The algorithm walks the bar series and emits alternating swing-high /
swing-low pivots whenever price reverses by more than ``threshold_pct``
from the running extreme. This is the canonical "ZigZag indicator"
implementation as documented in:

  * John J. Murphy, "Technical Analysis of the Financial Markets"
    (NYIF, 1999), Chapter 14: "Point and Figure Charting" — describes
    the underlying reversal-threshold mechanic that ZigZag formalises.
  * Achelis, "Technical Analysis from A to Z" (McGraw-Hill, 2nd ed.):
    "ZigZag" entry — the standard retail-platform spec used by
    MetaTrader / TradingView's ZigZag indicator.

Returns pivots in chronological order with type ``"high"`` or
``"low"``. Always alternates.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .schema import bar_close, bar_high, bar_low, bar_ts


def detect_pivots(
    bars: List[Dict[str, Any]],
    threshold_pct: float = 3.0,
) -> List[Dict[str, Any]]:
    """Return alternating swing pivots.

    Each pivot: ``{"i": int (index into bars), "ts": iso, "price":
    float, "type": "high"|"low"}``.

    The first pivot is seeded from the first bar based on which extreme
    is crossed first. ``threshold_pct`` is the percentage move that
    qualifies a reversal as a new pivot.
    """
    if not bars or threshold_pct <= 0:
        return []
    pct = float(threshold_pct) / 100.0

    pivots: List[Dict[str, Any]] = []
    # Initialise the "active" pivot to bar 0; direction unknown.
    last_high_i = 0
    last_low_i = 0
    last_high = bar_high(bars[0])
    last_low = bar_low(bars[0])
    direction: Optional[str] = None  # "up" = tracking a swing high; "down" = swing low.

    for i in range(1, len(bars)):
        b = bars[i]
        h = bar_high(b)
        l = bar_low(b)

        if direction is None:
            # Wait for the first qualifying move out of the seed bar.
            if h >= last_low * (1.0 + pct):
                direction = "up"
                last_high = h
                last_high_i = i
                pivots.append({
                    "i": last_low_i, "ts": bar_ts(bars[last_low_i]),
                    "price": last_low, "type": "low",
                })
            elif l <= last_high * (1.0 - pct):
                direction = "down"
                last_low = l
                last_low_i = i
                pivots.append({
                    "i": last_high_i, "ts": bar_ts(bars[last_high_i]),
                    "price": last_high, "type": "high",
                })
            else:
                if h > last_high:
                    last_high, last_high_i = h, i
                if l < last_low:
                    last_low, last_low_i = l, i
            continue

        if direction == "up":
            # Keep updating the running high…
            if h > last_high:
                last_high, last_high_i = h, i
            # …until price reverses ≥ pct from the high.
            if l <= last_high * (1.0 - pct):
                pivots.append({
                    "i": last_high_i, "ts": bar_ts(bars[last_high_i]),
                    "price": last_high, "type": "high",
                })
                direction = "down"
                last_low, last_low_i = l, i
        else:  # direction == "down"
            if l < last_low:
                last_low, last_low_i = l, i
            if h >= last_low * (1.0 + pct):
                pivots.append({
                    "i": last_low_i, "ts": bar_ts(bars[last_low_i]),
                    "price": last_low, "type": "low",
                })
                direction = "up"
                last_high, last_high_i = h, i

    # Tail pivot — append the latest running extreme so the most recent
    # leg is represented in the pattern matcher.
    if direction == "up":
        pivots.append({
            "i": last_high_i, "ts": bar_ts(bars[last_high_i]),
            "price": last_high, "type": "high",
        })
    elif direction == "down":
        pivots.append({
            "i": last_low_i, "ts": bar_ts(bars[last_low_i]),
            "price": last_low, "type": "low",
        })

    # De-duplicate adjacent same-type pivots (rare but possible at boundaries).
    out: List[Dict[str, Any]] = []
    for p in pivots:
        if out and out[-1]["type"] == p["type"]:
            # Keep the more extreme one.
            if p["type"] == "high" and p["price"] > out[-1]["price"]:
                out[-1] = p
            elif p["type"] == "low" and p["price"] < out[-1]["price"]:
                out[-1] = p
            continue
        out.append(p)
    return out


__all__ = ["detect_pivots"]
