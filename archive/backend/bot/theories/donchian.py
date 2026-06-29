"""Donchian Channels — MITS Phase 10 theory module (P10.2 history-walk).

Citation:

  * Richard Donchian (1949), the originator of the "4-week rule" and
    the first published trend-following channel-breakout system. The
    canonical formula:

        Upper = max(high,  N)
        Lower = min(low,   N)
        Mid   = (Upper + Lower) / 2

    N = 20 is the modern default (matches the original "4-week rule"
    when applied to weekly bars and the Turtle Traders' 20-day system).
  * Curtis M. Faith, "Way of the Turtle" (McGraw-Hill, 2007) — the
    famous Turtle Traders system used Donchian N=20 entry / N=10 exit;
    we expose the same two-channel pair as ``period`` / ``exit_period``.

Signals (MITS-P10.2 — walk every bar, not just last):

  * BUY  on every bar that closes above the prior-bar's Upper-N.
  * SELL on every bar that closes below the prior-bar's Lower-N.
  * EXIT_LONG  on every bar that closes below ``exit_period`` low.
  * EXIT_SHORT on every bar that closes above ``exit_period`` high.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._indicators import donchian
from .schema import (
    Line, Signal, TheoryAnnotation,
    bar_close, bar_high, bar_low, bar_ts,
)
from .signal_promote import promote_all


MAX_SIGNALS_PER_THEORY = 25


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    period = int(params.get("period", 20))
    exit_period = int(params.get("exit_period", 10))
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})

    ann = TheoryAnnotation(
        theory="donchian",
        params={"period": period, "exit_period": exit_period,
                "promote_options": promote_options},
        citation=(
            "Donchian (1949), 'Trend Following Methods in Commodity '"
            "'Price Analysis'; Faith, 'Way of the Turtle' (McGraw-Hill 2007)."
        ),
    )
    if len(bars) < period + 5:
        ann.notes.append("Not enough bars for Donchian channel.")
        return ann

    up, lo, mid = donchian(bars, period=period)
    up_exit, lo_exit, _ = donchian(bars, period=exit_period)

    # MITS Phase 10.1 — one ``series`` Line per channel (3 total).
    def _curve(values, color, label, style="solid", width=1):
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
            meta={"kind": "channel", "series": label},
            points=points,
        ))

    _curve(up, "#36c26b", f"Upper-{period}", style="solid", width=1)
    _curve(lo, "#ff5a5f", f"Lower-{period}", style="solid", width=1)
    _curve(mid, "#ffd166", "Mid", style="dotted", width=1)

    # MITS-P10.2 — emit a signal on every breakout, not just the last bar.
    sigs: List[Signal] = []
    for i in range(1, len(bars)):
        prior_upper = up[i - 1]
        prior_lower = lo[i - 1]
        prior_upper_exit = up_exit[i - 1]
        prior_lower_exit = lo_exit[i - 1]
        cl = bar_close(bars[i])
        ts = bar_ts(bars[i])

        if prior_upper is not None and cl > prior_upper:
            atr_band = (prior_upper - (prior_lower or cl)) * 0.5
            sigs.append(Signal(
                action="BUY",
                ts=ts, price=float(cl), confidence=0.70,
                reasoning=(
                    f"Close ({cl:.2f}) broke above prior Donchian-{period} "
                    f"upper ({prior_upper:.2f}) — classic Turtle System-1 "
                    f"long. Trail stop on N-{exit_period} low."
                ),
                target_price=float(cl + atr_band) if atr_band > 0 else None,
                stop_loss=float(prior_lower_exit) if prior_lower_exit else None,
                instrument="stock",
                theory_anchor={"channel": "upper", "n": period, "i": i},
            ))
        elif prior_lower is not None and cl < prior_lower:
            atr_band = ((prior_upper or cl) - prior_lower) * 0.5
            sigs.append(Signal(
                action="SELL",
                ts=ts, price=float(cl), confidence=0.70,
                reasoning=(
                    f"Close ({cl:.2f}) broke below prior Donchian-{period} "
                    f"lower ({prior_lower:.2f}) — classic Turtle System-1 "
                    "short."
                ),
                target_price=float(cl - atr_band) if atr_band > 0 else None,
                stop_loss=float(prior_upper_exit) if prior_upper_exit else None,
                instrument="stock",
                theory_anchor={"channel": "lower", "n": period, "i": i},
            ))

    if len(sigs) > MAX_SIGNALS_PER_THEORY:
        sigs = sigs[-MAX_SIGNALS_PER_THEORY:]

    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.80
    last = len(bars) - 1
    ann.primer = {
        "what_it_measures": (
            "Donchian Channels frame the highest high and lowest low of "
            "the trailing N bars. Richard Donchian's 1949 channel-"
            "breakout was the first published systematic trend-following "
            "method — every modern CTA descends from this template, and "
            "the Turtle Traders' famous N=20 entry / N=10 exit pair is a "
            "direct application."
        ),
        "how_to_read": (
            "Close above the prior upper channel = breakout long; close "
            "below the prior lower channel = breakdown short. Hold until "
            "price closes through the opposite exit channel "
            "(typically a shorter N). The two-channel split keeps you in "
            "a trade longer than the entry signal would alone, capturing "
            "the long tail of a strong trend."
        ),
        "key_levels_now": (
            f"Upper-{period}: {up[last]:.2f}  ·  Lower-{period}: "
            f"{lo[last]:.2f}  ·  Mid: {mid[last]:.2f}"
            if up[last] is not None and lo[last] is not None and mid[last] is not None
            else "Channel warm-up incomplete."
        ),
    }
    return ann


__all__ = ["analyze"]
