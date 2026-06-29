"""Stochastic %K / %D — MITS Phase 10 theory module (P10.2 history-walk).

Citation:

  * George C. Lane, "Investment Educators Tape Service" lectures
    (1957-58). Lane introduced the Stochastic Oscillator as a measure
    of where the close sits within the recent high–low range. The
    canonical 14-period %K with a 3-period %D smoothing is Lane's
    original parameterisation.
  * George C. Lane, "Lane's Stochastics" (Investment Educators, 1984).

    %K = 100 · (C − LL(n)) / (HH(n) − LL(n))
    %D = SMA(%K, 3)        — "fast" stochastic
    %D-slow = SMA(%D, 3)    — "slow" stochastic

Lane's signal grammar (MITS-P10.2 walks every cross, not just last):

  * BUY  on every bull cross (%K crosses above %D) while %K < 20.
  * SELL on every bear cross (%K crosses below %D) while %K > 80.
  * WATCH on neutral crosses (between 20 and 80) — Lane's "noisy zone".
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._indicators import stochastic
from .schema import (
    Line, Marker, Signal, TheoryAnnotation,
    bar_close, bar_ts,
)
from .signal_promote import promote_all


MAX_SIGNALS_PER_THEORY = 25


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    k_period = int(params.get("k_period", 14))
    d_period = int(params.get("d_period", 3))
    oversold = float(params.get("oversold", 20.0))
    overbought = float(params.get("overbought", 80.0))
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})

    ann = TheoryAnnotation(
        theory="stochastic",
        params={"k_period": k_period, "d_period": d_period,
                "oversold": oversold, "overbought": overbought,
                "promote_options": promote_options},
        citation=(
            "Lane, 'Investment Educators Tape Service' (1957-58); "
            "Lane, 'Lane's Stochastics' (Investment Educators 1984)."
        ),
    )
    if len(bars) < k_period + d_period + 5:
        ann.notes.append("Not enough bars for Stochastic.")
        return ann

    k, d = stochastic(bars, k_period=k_period, d_period=d_period)

    # MITS Phase 10.1 — %K / %D as ``series`` lines with panel="stoch".
    def _push_series(values: List[Optional[float]], color: str, label: str,
                     style: str = "solid", width: int = 1) -> None:
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
            meta={"kind": "stochastic", "panel": "stoch"},
            points=points,
        ))

    _push_series(k, "#1f6feb", "%K", style="solid", width=1)
    _push_series(d, "#ff9f1c", "%D", style="solid", width=1)

    # Walk every cross.
    crosses: List[Dict[str, Any]] = []
    for i in range(1, len(k)):
        k0, k1 = k[i - 1], k[i]
        d0, d1 = d[i - 1], d[i]
        if None in (k0, k1, d0, d1):
            continue
        if k0 <= d0 and k1 > d1:
            crosses.append({"i": i, "kind": "bull_cross", "k": k1, "d": d1})
        elif k0 >= d0 and k1 < d1:
            crosses.append({"i": i, "kind": "bear_cross", "k": k1, "d": d1})

    for c in crosses[-30:]:
        i = c["i"]
        ann.markers.append(Marker(
            ts=bar_ts(bars[i]), price=float(bar_close(bars[i])),
            label=f"Stoch {('↑' if c['kind'] == 'bull_cross' else '↓')}",
            color=("#26d07c" if c["kind"] == "bull_cross" else "#ff5a5f"),
            shape=("arrow_up" if c["kind"] == "bull_cross" else "arrow_down"),
        ))

    # P10.2 — emit one Signal per cross over the full series.
    sigs: List[Signal] = []
    for c in crosses:
        i = c["i"]
        kv, dv = c["k"], c["d"]
        ts = bar_ts(bars[i])
        px = bar_close(bars[i])
        if c["kind"] == "bull_cross" and kv < oversold:
            sigs.append(Signal(
                action="BUY",
                ts=ts, price=float(px), confidence=0.65,
                reasoning=(
                    f"%K ({kv:.0f}) crossed above %D ({dv:.0f}) with both "
                    f"in oversold (<{oversold:.0f}) — Lane's canonical "
                    "bull cross."
                ),
                instrument="stock",
                theory_anchor={"cross": "bull", "k": kv, "d": dv, "i": i},
            ))
        elif c["kind"] == "bear_cross" and kv > overbought:
            sigs.append(Signal(
                action="SELL",
                ts=ts, price=float(px), confidence=0.65,
                reasoning=(
                    f"%K ({kv:.0f}) crossed below %D ({dv:.0f}) with both "
                    f"in overbought (>{overbought:.0f}) — Lane's canonical "
                    "bear cross."
                ),
                instrument="stock",
                theory_anchor={"cross": "bear", "k": kv, "d": dv, "i": i},
            ))
        # Suppress mid-zone WATCH crosses — too noisy on a multi-year window.

    if len(sigs) > MAX_SIGNALS_PER_THEORY:
        sigs = sigs[-MAX_SIGNALS_PER_THEORY:]

    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.78
    last_k = k[-1] if k[-1] is not None else 0.0
    last_d = d[-1] if d[-1] is not None else 0.0
    ann.primer = {
        "what_it_measures": (
            "Stochastic %K measures where today's close sits inside the "
            "trailing 14-bar high-low range, scaled 0–100. %D is a "
            "3-period SMA of %K — Lane added it to filter noise. "
            "Readings > 80 = closes near the recent top; < 20 = near "
            "the recent bottom."
        ),
        "how_to_read": (
            "Lane's canonical signals: %K crosses up through %D when both "
            f"< {oversold:.0f} → BUY; %K crosses down through %D when both > "
            f"{overbought:.0f} → SELL. Crosses in the mid-zone are noise. "
            "Trending markets can sit at 80+ for weeks — don't fade trend; "
            "wait for divergence."
        ),
        "key_levels_now": (
            f"%K {last_k:.1f}  ·  %D {last_d:.1f}  ·  "
            f"Regime: "
            f"{'OVERBOUGHT' if last_k >= overbought else ('OVERSOLD' if last_k <= oversold else 'NEUTRAL')}"
        ),
    }
    return ann


__all__ = ["analyze"]
