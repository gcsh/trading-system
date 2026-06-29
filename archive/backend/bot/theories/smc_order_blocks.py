"""SMC Order Blocks — MITS Phase 10 theory module.

Citation:

  * Michael J. Huddleston (a.k.a. "The Inner Circle Trader" / ICT),
    "ICT Mentorship Core Content" (self-published video curriculum,
    2015-2022). Huddleston coined the SMC ("Smart Money Concepts")
    formalisation of the "order block" — the last bearish (or bullish)
    candle that printed BEFORE the impulse move that broke market
    structure.

  * Steven J. Soroka, "Smart Money Concepts: The Mechanical Trader's
    Edge" (Self-published, 2023) — modern practitioner reference for
    the OB/FVG/BoS taxonomy.

Definitions:

  * **Bullish Order Block** = the last DOWN candle (close < open)
    before a strong UP impulse that breaks a recent swing high (Break
    of Structure / BoS).
  * **Bearish Order Block** = the last UP candle before a strong DOWN
    impulse that breaks a recent swing low.

The "impulse" qualifier we use:

  * The candle's range is ≥ 1.5 × ATR(14).
  * The close exceeds the prior 10-bar high (bullish) or low (bearish).

OB zones serve as high-probability re-entry levels on the retracement
back to them.

Signals:

  * BUY  when price returns to a bullish OB zone (from above).
  * SELL when price returns to a bearish OB zone (from below).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._indicators import atr, highs, lows
from .schema import (
    Marker, Signal, TheoryAnnotation, Zone,
    bar_close, bar_high, bar_low, bar_open, bar_ts,
)


def _find_order_blocks(
    bars: List[Dict[str, Any]], atr_mult: float = 1.5, bos_lookback: int = 10,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    atr_series = atr(bars, 14)
    hh = highs(bars); ll = lows(bars)
    for i in range(bos_lookback + 1, len(bars) - 1):
        a = atr_series[i]
        if a is None or a <= 0:
            continue
        bar = bars[i]
        o = bar_open(bar); c = bar_close(bar)
        h = bar_high(bar); l = bar_low(bar)
        rng = h - l
        if rng < atr_mult * a:
            continue
        # Bullish impulse: candle closes up, exceeds prior 10-bar high.
        if c > o and c > max(hh[i - bos_lookback:i]):
            # Find last DOWN candle before i.
            for j in range(i - 1, max(0, i - bos_lookback), -1):
                pj = bars[j]
                if bar_close(pj) < bar_open(pj):
                    out.append({
                        "kind": "bullish",
                        "i": j,
                        "ts": bar_ts(pj),
                        "y_low": bar_low(pj),
                        "y_high": bar_high(pj),
                        "impulse_i": i,
                    })
                    break
        # Bearish impulse.
        elif c < o and c < min(ll[i - bos_lookback:i]):
            for j in range(i - 1, max(0, i - bos_lookback), -1):
                pj = bars[j]
                if bar_close(pj) > bar_open(pj):
                    out.append({
                        "kind": "bearish",
                        "i": j,
                        "ts": bar_ts(pj),
                        "y_low": bar_low(pj),
                        "y_high": bar_high(pj),
                        "impulse_i": i,
                    })
                    break
    return out


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    atr_mult = float(params.get("atr_mult", 1.5))
    bos_lookback = int(params.get("bos_lookback", 10))
    max_obs = int(params.get("max_obs", 6))

    ann = TheoryAnnotation(
        theory="smc_order_blocks",
        params={"atr_mult": atr_mult, "bos_lookback": bos_lookback,
                "max_obs": max_obs},
        citation=(
            "Huddleston (ICT), 'ICT Mentorship Core Content' (2015-2022); "
            "Soroka, 'Smart Money Concepts' (2023)."
        ),
    )
    if len(bars) < 30:
        ann.notes.append("Not enough bars for Order Block scan.")
        return ann

    obs = _find_order_blocks(bars, atr_mult=atr_mult, bos_lookback=bos_lookback)
    last_ts = bar_ts(bars[-1])
    last_close = bar_close(bars[-1])

    # Keep only the most-recent ``max_obs`` OBs.
    obs = obs[-max_obs:]
    for ob in obs:
        color = "#36c26b" if ob["kind"] == "bullish" else "#ff5a5f"
        ann.zones.append(Zone(
            x1=ob["ts"], y1=float(ob["y_low"]),
            x2=last_ts,  y2=float(ob["y_high"]),
            color=color, opacity=0.18,
            label=f"{ob['kind'].title()} OB",
        ))
        ann.markers.append(Marker(
            ts=ob["ts"], price=float((ob["y_low"] + ob["y_high"]) / 2.0),
            label=f"OB-{ob['kind'][:3]}",
            color=color,
            shape=("arrow_up" if ob["kind"] == "bullish" else "arrow_down"),
        ))

    # MITS-P10.2 — walk every bar AFTER each OB is formed; emit a Signal
    # the FIRST time price re-enters the OB zone (one Signal per OB
    # retest). Per Soroka / ICT, the *first* mitigation is the high-
    # probability re-entry; subsequent retests degrade in quality.
    from .signal_promote import promote_all
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})
    sigs: List[Signal] = []
    for ob in obs:
        # Find index of the OB bar in `bars`.
        ob_i = None
        for i, b in enumerate(bars):
            if bar_ts(b) == ob["ts"]:
                ob_i = i
                break
        if ob_i is None:
            continue
        # Walk forward; emit at first close inside the zone.
        for j in range(ob_i + 1, len(bars)):
            cl = bar_close(bars[j])
            ts_j = bar_ts(bars[j])
            if ob["y_low"] <= cl <= ob["y_high"]:
                if ob["kind"] == "bullish":
                    sigs.append(Signal(
                        action="BUY",
                        ts=ts_j, price=float(cl), confidence=0.65,
                        reasoning=(
                            f"Price ({cl:.2f}) returned to a bullish "
                            f"Order Block at "
                            f"{ob['y_low']:.2f}–{ob['y_high']:.2f}. "
                            "Smart-money cost basis defended — long re-entry."
                        ),
                        target_price=float(ob["y_high"] + (ob["y_high"] - ob["y_low"]) * 3),
                        stop_loss=float(ob["y_low"] * 0.992),
                        instrument="stock",
                        theory_anchor={"ob_kind": "bullish",
                                       "zone": [ob["y_low"], ob["y_high"]],
                                       "i": j},
                    ))
                else:
                    sigs.append(Signal(
                        action="SELL",
                        ts=ts_j, price=float(cl), confidence=0.65,
                        reasoning=(
                            f"Price ({cl:.2f}) returned to a bearish "
                            f"Order Block at "
                            f"{ob['y_low']:.2f}–{ob['y_high']:.2f}. "
                            "Smart-money supply defended — short re-entry."
                        ),
                        target_price=float(ob["y_low"] - (ob["y_high"] - ob["y_low"]) * 3),
                        stop_loss=float(ob["y_high"] * 1.008),
                        instrument="stock",
                        theory_anchor={"ob_kind": "bearish",
                                       "zone": [ob["y_low"], ob["y_high"]],
                                       "i": j},
                    ))
                break  # one signal per OB

    if len(sigs) > 25:
        sigs = sigs[-25:]
    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.75
    bullish_n = sum(1 for o in obs if o["kind"] == "bullish")
    bearish_n = sum(1 for o in obs if o["kind"] == "bearish")
    ann.primer = {
        "what_it_measures": (
            "An SMC Order Block is the last opposite-coloured candle "
            "before a strong impulse that breaks recent structure (BoS). "
            "It represents the price zone where 'smart money' built the "
            "position that drove the move — a high-probability re-entry "
            "level on the retracement back to it."
        ),
        "how_to_read": (
            "Bullish OB (green zone) = re-test from above = BUY. "
            "Bearish OB (red zone) = re-test from below = SELL. The "
            "tighter the impulse (more ATR/bar), the higher the OB's "
            "quality. Combine with FVG (Fair Value Gaps) for the canonical "
            "ICT entry stack."
        ),
        "key_levels_now": (
            f"Order Blocks in window — bullish: {bullish_n}, bearish: {bearish_n}"
        ),
    }
    return ann


__all__ = ["analyze"]
