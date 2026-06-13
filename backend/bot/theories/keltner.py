"""Keltner Channels — MITS Phase 10 theory module (P10.2 history-walk).

Citation:

  * Chester Keltner, "How to Make Money in Commodities" (1960). Keltner's
    original channel used a 10-bar SMA of the typical price ± a fixed
    fraction of the 10-bar SMA of the daily range. We use the modern
    Linda Bradford Raschke variant adopted by virtually every charting
    platform today:

        Mid   = EMA(close, 20)
        Upper = Mid + 2 · ATR(10)
        Lower = Mid − 2 · ATR(10)

  * Linda Bradford Raschke & Lawrence Connors, "Street Smarts:
    High Probability Short-term Trading Strategies" (M. Gordon, 1996) —
    documents the modern (EMA + ATR) variant. Raschke also documents
    "walking the band" — when price closes above the upper channel for
    5+ consecutive bars, it confirms a strong trend (WATCH signal).

Distinction vs Bollinger: Keltner's bands are driven by True Range
(price-action volatility), Bollinger's by standard deviation (return
volatility). A "squeeze" classically fires when Bollinger contracts
INSIDE Keltner.

Signals (P10.2 — walk every bar, not last):

  * BUY  on every bar that closes above the upper band (trend
         continuation breakout per Raschke).
  * SELL on every bar that closes below the lower band.
  * WATCH when price walks the upper band for 5+ consecutive bars
         (Raschke's "strong trend" confirmation).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._indicators import atr, keltner
from .schema import (
    Line, Signal, TheoryAnnotation,
    bar_close, bar_ts,
)
from .signal_promote import promote_all


MAX_SIGNALS_PER_THEORY = 25


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    ema_period = int(params.get("ema_period", 20))
    atr_period = int(params.get("atr_period", 10))
    mult = float(params.get("mult", 2.0))
    walk_threshold = int(params.get("walk_threshold", 5))
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})

    ann = TheoryAnnotation(
        theory="keltner",
        params={"ema_period": ema_period, "atr_period": atr_period,
                "mult": mult, "walk_threshold": walk_threshold,
                "promote_options": promote_options},
        citation=(
            "Keltner, 'How to Make Money in Commodities' (1960); "
            "Raschke & Connors, 'Street Smarts' (1996)."
        ),
    )
    if len(bars) < max(ema_period, atr_period) + 5:
        ann.notes.append("Not enough bars for a Keltner channel.")
        return ann

    mid, upper, lower = keltner(bars, ema_period=ema_period,
                                  atr_period=atr_period, mult=mult)
    atr_series = atr(bars, atr_period)

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

    _curve(mid,   "#ffd166", "EMA-20",         style="solid",  width=1)
    _curve(upper, "#36c26b", f"+{mult:.1f}·ATR", style="dashed", width=1)
    _curve(lower, "#ff5a5f", f"−{mult:.1f}·ATR", style="dashed", width=1)

    # P10.2 — walk every bar, emit BUY/SELL on breakout + WATCH on walks.
    sigs: List[Signal] = []
    walk_run = 0   # consecutive bars closing above the upper band
    for i in range(len(bars)):
        if upper[i] is None or lower[i] is None or mid[i] is None:
            continue
        cl = bar_close(bars[i])
        ts = bar_ts(bars[i])
        prior_close = bar_close(bars[i - 1]) if i > 0 else None
        a_i = atr_series[i] if i < len(atr_series) else None
        # Trend-continuation breakout BUY.
        if prior_close is not None and prior_close <= upper[i] and cl > upper[i]:
            sigs.append(Signal(
                action="BUY",
                ts=ts, price=float(cl), confidence=0.65,
                reasoning=(
                    f"Close ({cl:.2f}) broke above the Keltner upper "
                    f"band ({upper[i]:.2f}) — trend-continuation breakout "
                    "per Raschke. Riding ATR-bounded volatility expansion."
                ),
                target_price=float(cl + (a_i or 0) * 2.0) if a_i else None,
                stop_loss=float(mid[i]),
                instrument="stock",
                theory_anchor={"band": "upper", "i": i},
            ))
        elif prior_close is not None and prior_close >= lower[i] and cl < lower[i]:
            sigs.append(Signal(
                action="SELL",
                ts=ts, price=float(cl), confidence=0.65,
                reasoning=(
                    f"Close ({cl:.2f}) broke below the Keltner lower "
                    f"band ({lower[i]:.2f}) — trend-continuation breakdown."
                ),
                target_price=float(cl - (a_i or 0) * 2.0) if a_i else None,
                stop_loss=float(mid[i]),
                instrument="stock",
                theory_anchor={"band": "lower", "i": i},
            ))

        # Walk-the-band tracking — Raschke's strong-trend WATCH.
        if cl > upper[i]:
            walk_run += 1
            if walk_run == walk_threshold:
                sigs.append(Signal(
                    action="WATCH",
                    ts=ts, price=float(cl), confidence=0.55,
                    reasoning=(
                        f"Price walked the Keltner upper band "
                        f"({upper[i]:.2f}) for {walk_threshold} consecutive "
                        "bars — Raschke's strong-trend confirmation. Do "
                        "NOT fade; ride the band."
                    ),
                    instrument="stock",
                    theory_anchor={"walk": "upper", "consecutive": walk_run, "i": i},
                ))
        else:
            walk_run = 0

    if len(sigs) > MAX_SIGNALS_PER_THEORY:
        sigs = sigs[-MAX_SIGNALS_PER_THEORY:]

    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.80
    last = len(bars) - 1
    last_mid = mid[last]; last_upper = upper[last]; last_lower = lower[last]
    ann.primer = {
        "what_it_measures": (
            "Keltner Channels surround a 20-period EMA with an ATR-"
            "scaled band, so the envelope breathes with the market's "
            "price-action volatility. Unlike Bollinger (σ of returns), "
            "Keltner is anchored in True Range — more responsive to "
            "gaps and large bars."
        ),
        "how_to_read": (
            "Breakouts beyond the Keltner band are taken AT FACE VALUE — "
            "they are signal, not noise. This is the opposite of "
            "Bollinger, where outside-band prints are fade candidates. "
            "Use the mid EMA as the trailing exit / opposite-side stop."
        ),
        "key_levels_now": (
            f"Mid {last_mid:.2f}  ·  Upper {last_upper:.2f}  ·  "
            f"Lower {last_lower:.2f}"
            if last_mid and last_upper and last_lower
            else "Insufficient warm-up for level readout."
        ),
    }
    return ann


__all__ = ["analyze"]
