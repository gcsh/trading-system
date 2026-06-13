"""Bollinger Bands — MITS Phase 10 theory module (P10.2 history-walk).

Citation:

  * John Bollinger, "Bollinger on Bollinger Bands" (McGraw-Hill, 2001).
    The canonical reference. Defines:

        Mid   = SMA(close, 20)
        Upper = Mid + 2 · σ(close, 20)
        Lower = Mid − 2 · σ(close, 20)

    Bollinger explicitly recommends RSI thresholds of **35 / 65** for
    band-tag confirmation (not the textbook 30 / 70) — Chapter 9 "How
    to use Bollinger Bands". The 30 / 70 readings are oversold /
    overbought regimes; 35 / 65 are *fade-the-band* triggers.
  * John Bollinger, "Bollinger Bands and the Squeeze" (Stocks &
    Commodities, V.10:1, 1992) — defines the bandwidth (BBW) and the
    Squeeze regime (BBW in the bottom 20–25th percentile of the
    trailing sample), used here as a setup classifier.

Signals (MITS-P10.2):

  * BUY  on every bar where ``low <= lower_band`` AND ``RSI(14) <= 35``.
  * SELL on every bar where ``high >= upper_band`` AND ``RSI(14) >= 65``.
  * WATCH on a bar where bandwidth ≤ 25th-percentile of trailing 120 bars
    (Bollinger Squeeze) — direction-agnostic; arm both-sides orders.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._indicators import bollinger, closes, rsi
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
    mult = float(params.get("mult", 2.0))
    rsi_period = int(params.get("rsi_period", 14))
    # Bollinger's relaxed RSI thresholds (book p.142) — 35/65 not 30/70.
    rsi_buy = float(params.get("rsi_buy_threshold", 35.0))
    rsi_sell = float(params.get("rsi_sell_threshold", 65.0))
    squeeze_pct = float(params.get("squeeze_pct", 25.0))
    squeeze_window = int(params.get("squeeze_window", 120))
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})

    ann = TheoryAnnotation(
        theory="bollinger",
        params={"period": period, "mult": mult, "rsi_period": rsi_period,
                "rsi_buy_threshold": rsi_buy, "rsi_sell_threshold": rsi_sell,
                "squeeze_pct": squeeze_pct, "squeeze_window": squeeze_window,
                "promote_options": promote_options},
        citation=(
            "Bollinger, 'Bollinger on Bollinger Bands' (McGraw-Hill 2001); "
            "Bollinger, 'Bollinger Bands and the Squeeze' (S&C V.10:1, 1992)."
        ),
    )
    if len(bars) < period + 5:
        ann.notes.append("Not enough bars for a Bollinger calculation.")
        return ann
    cc = closes(bars)
    mid, upper, lower = bollinger(cc, period=period, mult=mult)
    rsi_series = rsi(cc, rsi_period)

    # Bandwidth & rolling squeeze classifier (windowed percentile).
    bbw: List[Optional[float]] = []
    for m, u, l in zip(mid, upper, lower):
        if m is None or u is None or l is None or m == 0:
            bbw.append(None)
        else:
            bbw.append((u - l) / m)

    # Per-bar squeeze threshold = `squeeze_pct`-th percentile of the
    # trailing ``squeeze_window`` bbw values.
    def _squeeze_threshold_at(i: int) -> Optional[float]:
        lo = max(0, i - squeeze_window + 1)
        sample = [v for v in bbw[lo:i + 1] if v is not None]
        if len(sample) < 20:
            return None
        sample_sorted = sorted(sample)
        k = max(0, int(len(sample_sorted) * (squeeze_pct / 100.0)) - 1)
        return sample_sorted[k]

    # MITS Phase 10.1 — one ``series`` Line per band (3 total).
    # MITS-P10.3.4 — priority: mid is priority 2 (normal+), upper/lower
    # are priority 1 (always shown — the bands are the whole point).
    def _push_curve(values: List[Optional[float]], color: str, label: str,
                    style: str = "solid", width: int = 1,
                    priority: int = 2) -> None:
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
            meta={"kind": "band", "series": label, "priority": priority},
            points=points,
        ))

    _push_curve(mid,   "#ffd166", "Mid (SMA 20)", style="solid", width=1,
                priority=2)
    _push_curve(upper, "#36c26b", "Upper +2σ",   style="dashed", width=1,
                priority=1)
    _push_curve(lower, "#ff5a5f", "Lower −2σ",   style="dashed", width=1,
                priority=1)

    # ── MITS-P10.2 — walk every bar, emit per-fire Signals.
    sigs: List[Signal] = []
    for i in range(len(bars)):
        if upper[i] is None or lower[i] is None or mid[i] is None:
            continue
        ts = bar_ts(bars[i])
        cl = bar_close(bars[i])
        hi = bar_high(bars[i])
        lo = bar_low(bars[i])
        rs = rsi_series[i] if i < len(rsi_series) else None
        # BUY — close prints below the lower band AND RSI is at/below 35.
        if lo <= lower[i] and rs is not None and rs <= rsi_buy:
            sigs.append(Signal(
                action="BUY",
                ts=ts, price=float(cl), confidence=0.65,
                reasoning=(
                    f"Price tagged the lower Bollinger band "
                    f"({lower[i]:.2f}) with RSI {rs:.0f} ≤ {rsi_buy:.0f} — "
                    "Bollinger's classic oversold mean-reversion setup. "
                    "Target the 20-day midline; stop ~20% below the band."
                ),
                target_price=float(mid[i]),
                stop_loss=float(lower[i] - (upper[i] - lower[i]) * 0.20),
                instrument="stock",
                theory_anchor={"band": "lower", "rsi": rs, "i": i},
            ))
        elif hi >= upper[i] and rs is not None and rs >= rsi_sell:
            sigs.append(Signal(
                action="SELL",
                ts=ts, price=float(cl), confidence=0.65,
                reasoning=(
                    f"Price tagged the upper Bollinger band "
                    f"({upper[i]:.2f}) with RSI {rs:.0f} ≥ {rsi_sell:.0f} — "
                    "Bollinger's classic overbought mean-reversion setup. "
                    "Target the 20-day midline."
                ),
                target_price=float(mid[i]),
                stop_loss=float(upper[i] + (upper[i] - lower[i]) * 0.20),
                instrument="stock",
                theory_anchor={"band": "upper", "rsi": rs, "i": i},
            ))
        else:
            # WATCH — squeeze regime (rolling-window percentile).
            sth = _squeeze_threshold_at(i)
            bw_i = bbw[i]
            if sth is not None and bw_i is not None and bw_i <= sth:
                # Only emit one squeeze WATCH at the START of each squeeze
                # run to avoid 50 yellow flags on a quiet chart.
                prev_bw = bbw[i - 1] if i > 0 else None
                prev_th = _squeeze_threshold_at(i - 1) if i > 0 else None
                already = (prev_bw is not None and prev_th is not None
                           and prev_bw <= prev_th)
                if not already:
                    sigs.append(Signal(
                        action="WATCH",
                        ts=ts, price=float(cl), confidence=0.55,
                        reasoning=(
                            f"Bandwidth ({bw_i*100:.2f}%) entered the "
                            f"bottom {squeeze_pct:.0f}th percentile of the "
                            f"trailing {squeeze_window} bars — Bollinger "
                            "Squeeze. Direction unknown; arm breakout "
                            "orders both sides."
                        ),
                        instrument="stock",
                        theory_anchor={"squeeze": True, "bbw": bw_i,
                                        "threshold": sth, "i": i},
                    ))

    if len(sigs) > MAX_SIGNALS_PER_THEORY:
        sigs = sigs[-MAX_SIGNALS_PER_THEORY:]

    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.85

    last = len(bars) - 1
    last_mid = mid[last]; last_upper = upper[last]; last_lower = lower[last]
    last_bbw = bbw[last]
    ann.primer = {
        "what_it_measures": (
            "Bollinger Bands surround a 20-period moving average with a "
            "±2σ envelope. The bands widen during high-volatility regimes "
            "and contract (the 'Squeeze') during quiet periods. ~95% of "
            "closes statistically fall inside the band — so prints outside "
            "are notable and usually pull back to the mid."
        ),
        "how_to_read": (
            f"Close beyond the upper band + RSI ≥ {rsi_sell:.0f} = "
            "mean-reversion SELL with the 20-period midline as target. "
            f"Close beyond the lower band + RSI ≤ {rsi_buy:.0f} = "
            "mean-reversion BUY. A Squeeze (bandwidth in bottom "
            f"{squeeze_pct:.0f}%) is direction-agnostic — arm both-sides "
            "orders. Trending markets ride the band; do NOT fade a "
            "Bollinger band during a confirmed trend regime."
        ),
        "key_levels_now": (
            f"Mid {last_mid:.2f}  ·  Upper {last_upper:.2f}  ·  "
            f"Lower {last_lower:.2f}  ·  BBW "
            f"{(last_bbw*100):.2f}%" if last_mid and last_upper and last_lower and last_bbw
            else "Insufficient warm-up for level readout."
        ),
    }
    return ann


__all__ = ["analyze"]
