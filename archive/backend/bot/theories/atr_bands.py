"""ATR Price Bands — MITS Phase 10 theory module.

Citation:

  * J. Welles Wilder Jr., "New Concepts in Technical Trading Systems"
    (Trend Research, 1978). Chapter 2 defines ATR (Average True Range).
  * Charles LeBeau & David Lucas, "Technical Traders Guide to Computer
    Analysis of the Futures Markets" (McGraw-Hill, 1992) — formalised
    the "Chandelier Exit" concept: chandelier_exit_long = HH(N) − k·ATR.
    Our ATR-band variant is the symmetric form.

    Mid    = close
    Upper  = close + k · ATR(period)
    Lower  = close − k · ATR(period)

Often used for stop placement, not entries. We surface both bands at
the latest bar so the operator can size a trade against true range
(Van Tharp's R-multiple framework).

Signals:

  * BUY  on close above prior bar's Upper band (volatility expansion long).
  * SELL on close below prior bar's Lower band.
  * The bands themselves serve as visible stop-loss zones for any other
    theory's BUY/SELL signal.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._indicators import atr
from .schema import (
    Line, Signal, TheoryAnnotation,
    bar_close, bar_ts,
)


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    period = int(params.get("period", 14))
    mult = float(params.get("mult", 2.0))

    ann = TheoryAnnotation(
        theory="atr_bands",
        params={"period": period, "mult": mult},
        citation=(
            "Wilder, 'New Concepts in Technical Trading Systems' (1978); "
            "LeBeau & Lucas, 'Technical Traders Guide' (1992) — "
            "Chandelier Exit / ATR band lineage."
        ),
    )
    if len(bars) < period + 5:
        ann.notes.append("Not enough bars for ATR bands.")
        return ann

    atr_series = atr(bars, period)
    # MITS Phase 10.1 — emit 5 ATR bands (+2 / +1 / mid / -1 / -2) as
    # ``series`` lines (5 total, was N-1 × 2 trendline segments).
    upper2 = []; upper1 = []; midline = []; lower1 = []; lower2 = []
    for i, a in enumerate(atr_series):
        c = bar_close(bars[i])
        if a is None or c <= 0:
            upper2.append(None); upper1.append(None); midline.append(None)
            lower1.append(None); lower2.append(None)
        else:
            upper2.append(c + mult * a)
            upper1.append(c + (mult / 2.0) * a)
            midline.append(c)
            lower1.append(c - (mult / 2.0) * a)
            lower2.append(c - mult * a)

    def _curve(values, color, label, style="dashed", width=1, priority=2):
        points = [
            {"ts": bar_ts(bars[i]), "price": float(v)}
            for i, v in enumerate(values) if v is not None
        ]
        if not points:
            return
        ann.lines.append(Line(
            kind="series",
            start=points[0],
            end=points[-1],
            color=color, width=width, style=style,
            label=label,
            meta={"kind": "atr_band", "priority": priority},
            points=points,
        ))

    # MITS-P10.3.4 — priority by band severity. Outer ±2-ATR are the
    # actionable extremes (priority 1, always shown). Inner ±1-ATR and
    # the midline are noise on a wide window (priority 3, detailed only).
    _curve(upper2, "#36c26b", f"+{mult:.1f}·ATR",       style="dashed", width=1, priority=1)
    _curve(upper1, "#9be38e", f"+{(mult/2.0):.1f}·ATR", style="dotted", width=1, priority=3)
    _curve(midline, "#ffd166", "Mid (close)",            style="dotted", width=1, priority=2)
    _curve(lower1, "#ff9f9f", f"−{(mult/2.0):.1f}·ATR", style="dotted", width=1, priority=3)
    _curve(lower2, "#ff5a5f", f"−{mult:.1f}·ATR",       style="dashed", width=1, priority=1)

    # Compatibility aliases for the signal logic below — ``upper``/``lower``
    # refer to the outer bands.
    upper = upper2
    lower = lower2

    # MITS-P10.2 — walk every bar; emit WATCH at every ±2-ATR band tag
    # (volatility extreme). LeBeau & Lucas use the band tag as a R-
    # multiple sizing anchor, not a directional entry; we emit WATCH so
    # the operator sees the tag without forcing a trade decision.
    from .signal_promote import promote_all
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})
    sigs: List[Signal] = []
    for i in range(1, len(bars)):
        prior_upper = upper[i - 1]
        prior_lower = lower[i - 1]
        cl = bar_close(bars[i])
        ts = bar_ts(bars[i])
        a_i = atr_series[i] if i < len(atr_series) else None
        if prior_upper is not None and cl > prior_upper:
            sigs.append(Signal(
                action="WATCH",
                ts=ts, price=float(cl), confidence=0.55,
                reasoning=(
                    f"Close ({cl:.2f}) printed above the +{mult:.1f}·ATR "
                    f"band ({prior_upper:.2f}) — volatility extreme. "
                    "Per LeBeau & Lucas, use 1·ATR as the R-multiple stop."
                ),
                target_price=float(cl + (a_i or 0) * 2.0) if a_i else None,
                stop_loss=float(cl - (a_i or 0) * 1.0) if a_i else None,
                instrument="stock",
                theory_anchor={"side": "upper_tag", "atr": a_i, "i": i},
            ))
        elif prior_lower is not None and cl < prior_lower:
            sigs.append(Signal(
                action="WATCH",
                ts=ts, price=float(cl), confidence=0.55,
                reasoning=(
                    f"Close ({cl:.2f}) printed below the −{mult:.1f}·ATR "
                    f"band ({prior_lower:.2f}) — volatility extreme."
                ),
                target_price=float(cl - (a_i or 0) * 2.0) if a_i else None,
                stop_loss=float(cl + (a_i or 0) * 1.0) if a_i else None,
                instrument="stock",
                theory_anchor={"side": "lower_tag", "atr": a_i, "i": i},
            ))

    if len(sigs) > 25:
        sigs = sigs[-25:]

    last = len(bars) - 1
    last_atr = atr_series[last]
    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.75
    ann.primer = {
        "what_it_measures": (
            "ATR (Average True Range) is Wilder's measure of bar-to-bar "
            "price-action volatility. ATR Bands offset the close by "
            "±k·ATR to define meaningful breakout / stop levels — the "
            "same true-range scaling that powers Chandelier Exits and "
            "Van Tharp's R-multiple position sizing."
        ),
        "how_to_read": (
            "ATR bands are LESS useful as entries and MORE useful as "
            "stops + position sizing anchors. 1R = 1 ATR is the canonical "
            "definition. Compare ATR over time to detect volatility "
            "regime shifts — sudden ATR expansion almost always precedes "
            "or accompanies trend changes."
        ),
        "key_levels_now": (
            f"ATR: {last_atr:.2f}  ·  Upper: {upper[last]:.2f}  ·  "
            f"Lower: {lower[last]:.2f}"
            if last_atr and upper[last] and lower[last]
            else "Warm-up incomplete."
        ),
    }
    return ann


__all__ = ["analyze"]
