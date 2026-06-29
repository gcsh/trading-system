"""MACD — MITS Phase 10 theory module (P10.2 history-walk rewrite).

Citation:

  * Gerald Appel, "The Moving Average Convergence-Divergence Trading
    Method" (Signalert, 1979) — original publication. Default
    parameterisation 12/26/9 is Appel's.
  * Gerald Appel, "Technical Analysis: Power Tools for Active Investors"
    (FT Press, 2005) — updated, includes the zero-line cross rule used
    below.

    MACD line = EMA(12) − EMA(26)
    Signal    = EMA(9) of the MACD line
    Histogram = MACD − Signal

Signals — emitted at EVERY cross, not just the latest. The P10.1 bug
where ``analyze()`` only emitted on a cross within the last 5 bars
meant a -14% YTD chart returned zero signals; we now walk the whole
series and emit one Signal per cross (capped to keep the chart legible).

  * BUY  on **bullish cross**  (MACD line crosses above signal line).
                              ``confidence = 0.75`` when also above zero
                              (Appel's strongest setup), else 0.55.
  * SELL on **bearish cross**  (MACD line crosses below signal line).
                              ``confidence = 0.75`` when also below zero,
                              else 0.55.

The histogram is emitted as ``Line(kind="histogram")`` so the frontend
renders it as a true bar series (positive green, negative red) on the
MACD sub-panel — not as an overlay line.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._indicators import closes, macd
from .schema import (
    Line, Marker, Signal, TheoryAnnotation,
    bar_close, bar_ts,
)
from .signal_promote import promote_all


# Cap on signals returned per chart so a 1y SPY view doesn't drop
# 50 BUY/SELL flags on top of each other; the most recent N survive.
MAX_SIGNALS_PER_THEORY = 25


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    fast = int(params.get("fast", 12))
    slow = int(params.get("slow", 26))
    sig_p = int(params.get("signal", 9))
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})

    ann = TheoryAnnotation(
        theory="macd_signal",
        params={"fast": fast, "slow": slow, "signal": sig_p,
                "promote_options": promote_options},
        citation=(
            "Appel, 'The Moving Average Convergence-Divergence Trading "
            "Method' (Signalert 1979); Appel, 'Technical Analysis: Power "
            "Tools for Active Investors' (FT Press 2005)."
        ),
    )
    if len(bars) < slow + sig_p + 5:
        ann.notes.append("Not enough bars for MACD.")
        return ann

    cc = closes(bars)
    macd_line, sig_line, hist = macd(cc, fast, slow, sig_p)

    # MITS Phase 10.1/10.2 — emit MACD line + Signal line as ``series``
    # lines and Histogram as ``histogram`` (true bar series, not a line).
    # All three carry ``meta.panel = "macd"`` so the frontend renders
    # them in a dedicated sub-panel below the candles.
    def _push_series(values, color, label, kind_tag, style="solid", width=1):
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
            meta={"kind": kind_tag, "panel": "macd"},
            points=points,
        ))

    def _push_histogram(values, color, label):
        points = [
            {"ts": bar_ts(bars[i]), "price": float(v)}
            for i, v in enumerate(values) if v is not None
        ]
        if not points:
            return
        ann.lines.append(Line(
            kind="histogram",
            start=points[0],
            end=points[-1],
            color=color, width=1, style="solid",
            label=label,
            meta={"kind": "macd_hist", "panel": "macd"},
            points=points,
        ))

    _push_series(macd_line, "#26d07c", "MACD",   "macd_line",   style="solid",  width=1)
    _push_series(sig_line,  "#ff5a5f", "Signal", "macd_signal", style="solid",  width=1)
    _push_histogram(hist,   "#9aa4b2", "Histogram")

    # Detect every cross over the whole series.
    crosses: List[Dict[str, Any]] = []
    for i in range(1, len(macd_line)):
        m0, m1 = macd_line[i - 1], macd_line[i]
        s0, s1 = sig_line[i - 1], sig_line[i]
        if None in (m0, m1, s0, s1):
            continue
        if m0 <= s0 and m1 > s1:
            crosses.append({
                "i": i, "ts": bar_ts(bars[i]),
                "price": bar_close(bars[i]),
                "kind": "bull_cross",
                "macd": m1, "signal": s1,
                "above_zero": (m1 > 0 and s1 > 0),
            })
        elif m0 >= s0 and m1 < s1:
            crosses.append({
                "i": i, "ts": bar_ts(bars[i]),
                "price": bar_close(bars[i]),
                "kind": "bear_cross",
                "macd": m1, "signal": s1,
                "above_zero": (m1 > 0 and s1 > 0),
            })

    # Marker per cross (cap at last 30 so the chart doesn't choke).
    for c in crosses[-30:]:
        ann.markers.append(Marker(
            ts=c["ts"], price=float(c["price"]),
            label=("MACD↑" if c["kind"] == "bull_cross" else "MACD↓"),
            color=("#26d07c" if c["kind"] == "bull_cross" else "#ff5a5f"),
            shape=("arrow_up" if c["kind"] == "bull_cross" else "arrow_down"),
        ))

    # MITS-P10.2 — emit a Signal per cross over the lookback window.
    sigs: List[Signal] = []
    for c in crosses:
        if c["kind"] == "bull_cross":
            above = c["above_zero"]
            confidence = 0.75 if above else 0.55
            qual = ("ABOVE the zero line — Appel's highest-quality bull "
                    "cross") if above else ("near zero — Appel cautions early "
                                            "crosses can whipsaw; await follow-through")
            sigs.append(Signal(
                action="BUY",
                ts=c["ts"], price=float(c["price"]),
                confidence=confidence,
                reasoning=(
                    f"MACD ({c['macd']:.4f}) crossed UP through Signal "
                    f"({c['signal']:.4f}) {qual}."
                ),
                instrument="stock",
                theory_anchor={"cross": "bull", "above_zero": above, "i": c["i"]},
            ))
        else:
            below = not c["above_zero"]
            confidence = 0.75 if below else 0.55
            qual = ("BELOW the zero line — Appel's highest-quality bear "
                    "cross") if below else ("near zero — Appel cautions early "
                                            "crosses can whipsaw; await follow-through")
            sigs.append(Signal(
                action="SELL",
                ts=c["ts"], price=float(c["price"]),
                confidence=confidence,
                reasoning=(
                    f"MACD ({c['macd']:.4f}) crossed DOWN through Signal "
                    f"({c['signal']:.4f}) {qual}."
                ),
                instrument="stock",
                theory_anchor={"cross": "bear", "above_zero": c["above_zero"], "i": c["i"]},
            ))

    # Trim to last MAX_SIGNALS_PER_THEORY to keep the chart readable.
    if len(sigs) > MAX_SIGNALS_PER_THEORY:
        sigs = sigs[-MAX_SIGNALS_PER_THEORY:]

    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.80
    last_macd = macd_line[-1] if macd_line[-1] is not None else 0.0
    last_sig = sig_line[-1] if sig_line[-1] is not None else 0.0
    last_hist = hist[-1] if hist[-1] is not None else 0.0
    ann.primer = {
        "what_it_measures": (
            "MACD = EMA(12) − EMA(26) — the difference between a short "
            "and long exponential moving average. The 9-period signal "
            "line is an EMA of that difference. Histogram = MACD − "
            "Signal. Appel's 1979 indicator is the single most-watched "
            "momentum tool on every charting platform."
        ),
        "how_to_read": (
            "MACD above Signal AND both above zero = uptrend bull cross. "
            "MACD below Signal AND both below zero = downtrend bear "
            "cross. Crosses near zero are noisy — wait for confirmation. "
            "Divergence between MACD and price is a second-order edge "
            "(see RSI Divergence theory)."
        ),
        "key_levels_now": (
            f"MACD {last_macd:+.4f}  ·  Signal {last_sig:+.4f}  ·  "
            f"Hist {last_hist:+.4f}  ·  Crosses in window: {len(crosses)}"
        ),
    }
    return ann


__all__ = ["analyze"]
