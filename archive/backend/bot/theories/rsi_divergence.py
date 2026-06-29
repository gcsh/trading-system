"""RSI Divergence — MITS Phase 10 theory module (P10.2 history-walk).

Citation:

  * J. Welles Wilder Jr., "New Concepts in Technical Trading Systems"
    (Trend Research, 1978). Chapter 6 defines RSI and chapter 7
    documents bullish/bearish divergence as the *primary* edge of the
    indicator (more so than the 70/30 overbought/oversold readings).

    RSI(n) = 100 − 100 / (1 + RS)
    RS     = avg_gain(n) / avg_loss(n)  with Wilder's RMA smoothing

  * Cardwell, "RSI Edge" (Stocks & Commodities, V.13:4, 1995) —
    refined the divergence read with the "positive reversal" /
    "negative reversal" extension.

Divergence definitions (Wilder, p. 70):

  * **Bullish (regular)**  — price prints lower low, RSI prints higher low.
  * **Bearish (regular)**  — price prints higher high, RSI prints lower high.
  * **Hidden bullish**     — price prints higher low, RSI prints lower low (continuation).
  * **Hidden bearish**     — price prints lower high, RSI prints higher high.

We detect divergences over the swing-pivot grid produced by ZigZag.

Signals (MITS-P10.2 — walk every signal, not just last):

  * BUY  on EACH detected bullish (regular OR hidden) divergence.
  * SELL on EACH detected bearish (regular OR hidden) divergence.
  * BUY  on RSI crossing UP through 30 from oversold (Wilder's threshold).
  * SELL on RSI crossing DOWN through 70 from overbought.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.config import TUNABLES

from ._indicators import closes, rsi
from ._zigzag import detect_pivots
from .schema import (
    Line, Marker, Signal, TheoryAnnotation,
    bar_close, bar_ts,
)
from .signal_promote import promote_all


MAX_SIGNALS_PER_THEORY = 25


def _find_divergences(
    pivots: List[Dict[str, Any]],
    rsi_series: List[Optional[float]],
    bars: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    highs = [p for p in pivots if p["type"] == "high"]
    lows  = [p for p in pivots if p["type"] == "low"]

    def _pair_check(arr: List[Dict[str, Any]], side: str) -> None:
        for i in range(1, len(arr)):
            a, b = arr[i - 1], arr[i]
            ai, bi = a["i"], b["i"]
            if ai >= len(rsi_series) or bi >= len(rsi_series):
                continue
            ar = rsi_series[ai]; br = rsi_series[bi]
            if ar is None or br is None:
                continue
            p_a, p_b = a["price"], b["price"]
            if side == "high":
                if p_b > p_a and br < ar:
                    out.append({
                        "kind": "bearish_regular",
                        "a": a, "b": b, "rsi_a": ar, "rsi_b": br,
                    })
                elif p_b < p_a and br > ar:
                    out.append({
                        "kind": "bearish_hidden",
                        "a": a, "b": b, "rsi_a": ar, "rsi_b": br,
                    })
            else:
                if p_b < p_a and br > ar:
                    out.append({
                        "kind": "bullish_regular",
                        "a": a, "b": b, "rsi_a": ar, "rsi_b": br,
                    })
                elif p_b > p_a and br < ar:
                    out.append({
                        "kind": "bullish_hidden",
                        "a": a, "b": b, "rsi_a": ar, "rsi_b": br,
                    })

    _pair_check(highs, "high")
    _pair_check(lows,  "low")
    return out


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    rsi_period = int(params.get("rsi_period", 14))
    zigzag_pct = float(params.get("zigzag_pct",
                                       getattr(TUNABLES, "theory_zigzag_pct", 3.0)))
    lookback = int(params.get("lookback", 180))
    oversold = float(params.get("oversold", 30.0))
    overbought = float(params.get("overbought", 70.0))
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})

    ann = TheoryAnnotation(
        theory="rsi_divergence",
        params={"rsi_period": rsi_period, "zigzag_pct": zigzag_pct,
                "lookback": lookback, "oversold": oversold,
                "overbought": overbought, "promote_options": promote_options},
        citation=(
            "Wilder, 'New Concepts in Technical Trading Systems' (1978), "
            "Ch. 6–7; Cardwell, 'RSI Edge' (S&C V.13:4, 1995)."
        ),
    )
    if len(bars) < rsi_period + 20:
        ann.notes.append("Not enough bars for RSI divergence.")
        return ann

    rsi_series = rsi(closes(bars), rsi_period)
    win = bars[-lookback:]
    pivots = detect_pivots(win, threshold_pct=zigzag_pct)
    offset = max(0, len(bars) - lookback)
    pivots_global = [{**p, "i": p["i"] + offset} for p in pivots]
    divs = _find_divergences(pivots_global, rsi_series, bars)

    # MITS Phase 10.1 — RSI plot as ``series`` line w/ panel="rsi".
    rsi_points = [
        {"ts": bar_ts(bars[i]), "price": float(v)}
        for i, v in enumerate(rsi_series) if v is not None
    ]
    if rsi_points:
        ann.lines.append(Line(
            kind="series",
            start=rsi_points[0],
            end=rsi_points[-1],
            color="#b87cff", width=1, style="solid",
            label=f"RSI({rsi_period})",
            meta={"kind": "rsi", "panel": "rsi"},
            points=rsi_points,
        ))

    color_for_kind = {
        "bullish_regular": "#36c26b",
        "bullish_hidden":  "#7fc8a9",
        "bearish_regular": "#ff5a5f",
        "bearish_hidden":  "#ff9f9f",
    }
    for d in divs:
        a, b = d["a"], d["b"]
        ann.lines.append(Line(
            kind="trendline",
            start={"ts": a["ts"], "price": float(a["price"])},
            end={"ts": b["ts"], "price": float(b["price"])},
            color=color_for_kind.get(d["kind"], "#9aa4b2"),
            width=2, style="dashed",
            label=d["kind"].replace("_", " "),
            meta={"kind": "divergence", "type": d["kind"],
                   "rsi_a": d["rsi_a"], "rsi_b": d["rsi_b"]},
        ))
        ann.markers.append(Marker(
            ts=b["ts"], price=float(b["price"]),
            label=d["kind"].replace("_", " "),
            color=color_for_kind.get(d["kind"], "#fff"),
            shape="circle",
        ))

    # ── MITS-P10.2 — emit a Signal per divergence + RSI OB/OS crosses.
    sigs: List[Signal] = []

    # 1) Every divergence fires a signal.
    for d in divs:
        kind = d["kind"]
        b = d["b"]
        ts = b["ts"]
        price = float(b["price"])
        if kind.startswith("bullish"):
            sigs.append(Signal(
                action="BUY",
                ts=ts, price=price, confidence=0.65 if "hidden" in kind else 0.70,
                reasoning=(
                    f"{kind.replace('_', ' ').title()} divergence: "
                    f"price {d['a']['price']:.2f} → {d['b']['price']:.2f}, "
                    f"RSI {d['rsi_a']:.1f} → {d['rsi_b']:.1f}. Momentum is "
                    "leaving the downside — Wilder's classic bullish setup."
                ),
                stop_loss=price,
                instrument="stock",
                theory_anchor={"divergence": kind, "i": b["i"]},
            ))
        else:
            sigs.append(Signal(
                action="SELL",
                ts=ts, price=price, confidence=0.65 if "hidden" in kind else 0.70,
                reasoning=(
                    f"{kind.replace('_', ' ').title()} divergence: "
                    f"price {d['a']['price']:.2f} → {d['b']['price']:.2f}, "
                    f"RSI {d['rsi_a']:.1f} → {d['rsi_b']:.1f}. Momentum is "
                    "leaving the upside — Wilder's classic bearish setup."
                ),
                stop_loss=price,
                instrument="stock",
                theory_anchor={"divergence": kind, "i": b["i"]},
            ))

    # 2) RSI OB/OS reclaim — BUY on RSI crossing UP through oversold,
    #    SELL on RSI crossing DOWN through overbought. Wilder's original
    #    Ch.6 use of RSI is as an OB/OS oscillator before being a
    #    divergence tool.
    for i in range(1, len(rsi_series)):
        prev = rsi_series[i - 1]
        cur = rsi_series[i]
        if prev is None or cur is None:
            continue
        ts = bar_ts(bars[i])
        cl = bar_close(bars[i])
        # Reclaim from oversold (RSI < OS → RSI ≥ OS).
        if prev < oversold <= cur:
            sigs.append(Signal(
                action="BUY",
                ts=ts, price=float(cl), confidence=0.60,
                reasoning=(
                    f"RSI ({cur:.0f}) reclaimed the {oversold:.0f} oversold "
                    "threshold from below — Wilder's canonical mean-"
                    "reversion BUY trigger."
                ),
                instrument="stock",
                theory_anchor={"rsi_cross": "oversold_up", "rsi": cur, "i": i},
            ))
        # Reject from overbought (RSI > OB → RSI ≤ OB).
        elif prev > overbought >= cur:
            sigs.append(Signal(
                action="SELL",
                ts=ts, price=float(cl), confidence=0.60,
                reasoning=(
                    f"RSI ({cur:.0f}) lost the {overbought:.0f} overbought "
                    "threshold from above — Wilder's canonical mean-"
                    "reversion SELL trigger."
                ),
                instrument="stock",
                theory_anchor={"rsi_cross": "overbought_down", "rsi": cur, "i": i},
            ))

    if len(sigs) > MAX_SIGNALS_PER_THEORY:
        sigs = sigs[-MAX_SIGNALS_PER_THEORY:]

    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.75
    last_rsi = rsi_series[-1] if rsi_series else None
    ann.primer = {
        "what_it_measures": (
            "RSI divergence is Wilder's primary signal — when price and "
            "RSI disagree about the direction of an extreme. Price prints "
            "a new low but RSI does not = the move is losing momentum "
            "(bullish divergence). Price prints a new high but RSI does "
            "not = the move is running on fumes (bearish divergence)."
        ),
        "how_to_read": (
            "Look for two same-type swing pivots (two highs OR two lows). "
            "Regular divergence = reversal candidate. Hidden divergence = "
            "trend-continuation candidate (the move is pausing, not "
            "ending). Combine with structure: a bullish divergence at a "
            "Fibonacci 61.8% retrace is much higher quality than the same "
            "divergence at a random level."
        ),
        "key_levels_now": (
            f"RSI now: {last_rsi:.1f}  ·  Divergences in window: {len(divs)}"
            if last_rsi is not None else f"Divergences in window: {len(divs)}"
        ),
    }
    return ann


__all__ = ["analyze"]
