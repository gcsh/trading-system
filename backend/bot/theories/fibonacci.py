"""Fibonacci retracements + extensions.

Standard retracement and extension grid between two swing pivots.
Ratios per:

  * Frost & Prechter, "Elliott Wave Principle: Key to Market
    Behavior" (10th ed., New Classics Library, 2005) — Appendix A
    documents the canonical Fibonacci ratios (0.236, 0.382, 0.500,
    0.618, 0.786, 1.272, 1.618, 2.618, 4.236) used by Elliott
    wave-traders and which TradingView's Fibonacci-Retracement tool
    ships as defaults.
  * Robert Fischer, "Fibonacci Applications and Strategies for
    Traders" (Wiley, 1993) — Chapter 3: retracement levels; Chapter 6:
    extension levels.

We auto-pick the largest swing in the recent window unless the
operator passes ``anchor_a_index``/``anchor_b_index`` (or
``anchor_a_ts``/``anchor_b_ts``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

from ._zigzag import detect_pivots
from .schema import (
    Line, Marker, TheoryAnnotation,
    bar_close, bar_high, bar_low, bar_ts,
)


RETRACEMENT_LEVELS = [0.000, 0.236, 0.382, 0.500, 0.618, 0.786, 1.000]
EXTENSION_LEVELS = [1.272, 1.382, 1.618, 2.000, 2.618, 4.236]

LEVEL_COLORS = {
    0.000: "#9aa4b2",
    0.236: "#36c26b",
    0.382: "#1f6feb",
    0.500: "#ffc107",
    0.618: "#ff9f1c",
    0.786: "#d63a3a",
    1.000: "#9aa4b2",
    1.272: "#36c26b",
    1.382: "#1f6feb",
    1.618: "#ff9f1c",
    2.000: "#d63a3a",
    2.618: "#9aa4b2",
    4.236: "#9aa4b2",
}


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _pick_anchors(
    bars: List[Dict[str, Any]],
    zigzag_pct: float,
    lookback: int,
) -> Optional[Dict[str, Any]]:
    """Pick two pivots that span the largest swing in the lookback."""
    lo = max(0, len(bars) - lookback)
    window = bars[lo:]
    pivots = detect_pivots(window, threshold_pct=zigzag_pct)
    if len(pivots) < 2:
        # Fall back to absolute extremes.
        hi_i = max(range(len(window)), key=lambda i: bar_high(window[i]))
        lo_i = min(range(len(window)), key=lambda i: bar_low(window[i]))
        if hi_i == lo_i:
            return None
        if hi_i > lo_i:
            return {
                "a": {"i": lo + lo_i, "price": bar_low(window[lo_i]),
                      "ts": bar_ts(window[lo_i]), "type": "low"},
                "b": {"i": lo + hi_i, "price": bar_high(window[hi_i]),
                      "ts": bar_ts(window[hi_i]), "type": "high"},
            }
        return {
            "a": {"i": lo + hi_i, "price": bar_high(window[hi_i]),
                  "ts": bar_ts(window[hi_i]), "type": "high"},
            "b": {"i": lo + lo_i, "price": bar_low(window[lo_i]),
                  "ts": bar_ts(window[lo_i]), "type": "low"},
        }
    # Largest |Δprice| pair of adjacent pivots.
    best = None
    best_d = 0.0
    for i in range(1, len(pivots)):
        d = abs(pivots[i]["price"] - pivots[i - 1]["price"])
        if d > best_d:
            best_d = d
            best = (pivots[i - 1], pivots[i])
    if best is None:
        return None
    a, b = best
    return {
        "a": {**a, "i": lo + a["i"]},
        "b": {**b, "i": lo + b["i"]},
    }


def _anchor_from_params(
    bars: List[Dict[str, Any]], params: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    a_idx = params.get("anchor_a_index")
    b_idx = params.get("anchor_b_index")
    if a_idx is not None and b_idx is not None:
        try:
            ai = int(a_idx)
            bi = int(b_idx)
        except Exception:
            return None
        if 0 <= ai < len(bars) and 0 <= bi < len(bars):
            a = bars[ai]; b = bars[bi]
            return {
                "a": {"i": ai, "ts": bar_ts(a),
                      "price": bar_low(a) if bar_low(a) < bar_low(b) else bar_high(a),
                      "type": "low" if bar_low(a) < bar_low(b) else "high"},
                "b": {"i": bi, "ts": bar_ts(b),
                      "price": bar_high(b) if bar_high(b) > bar_high(a) else bar_low(b),
                      "type": "high" if bar_high(b) > bar_high(a) else "low"},
            }
    return None


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    zigzag_pct = float(params.get("zigzag_pct", getattr(TUNABLES, "theory_zigzag_pct", 3.0)))
    lookback = int(params.get("lookback", 120))
    show_extensions = bool(params.get("show_extensions", True))

    ann = TheoryAnnotation(
        theory="fibonacci",
        params={**params, "zigzag_pct": zigzag_pct, "lookback": lookback,
                "show_extensions": show_extensions},
        citation=(
            "Frost & Prechter, 'Elliott Wave Principle' (10th ed., 2005); "
            "Fischer, 'Fibonacci Applications and Strategies for Traders' (Wiley 1993)."
        ),
    )
    if not bars:
        ann.notes.append("No bars supplied.")
        return ann

    anchors = _anchor_from_params(bars, params) \
        or _pick_anchors(bars, zigzag_pct, lookback)
    if anchors is None:
        ann.notes.append("Could not identify two pivots for the Fibonacci grid.")
        return ann

    a = anchors["a"]
    b = anchors["b"]
    ann.params["anchor_a"] = {"i": a["i"], "ts": a["ts"], "price": float(a["price"])}
    ann.params["anchor_b"] = {"i": b["i"], "ts": b["ts"], "price": float(b["price"])}

    high = max(a["price"], b["price"])
    low = min(a["price"], b["price"])
    height = high - low
    if height <= 0:
        ann.notes.append("Anchor prices are identical — no range to retrace.")
        return ann

    last_ts = bar_ts(bars[-1])
    # Direction: if anchor B is higher than A, the move is "up" → 0% sits at
    # the low (A) and 100% at the high (B). Retracements measure pullback
    # from B down toward A.
    up_move = b["price"] >= a["price"]
    sig_map = {
        0.000: "swing",
        0.236: "shallow retracement",
        0.382: "shallow retracement",
        0.500: "balanced retracement",
        0.618: "golden retracement",
        0.786: "deep retracement",
        1.000: "full retracement",
    }
    sig_ext = {
        1.272: "extension target",
        1.382: "extension target",
        1.618: "golden extension",
        2.000: "double extension",
        2.618: "fib-cluster outlier",
        4.236: "extreme outlier",
    }
    level_prices: List[Dict[str, Any]] = []
    for level in RETRACEMENT_LEVELS:
        if up_move:
            price = low + height * (1.0 - level)
        else:
            price = low + height * level
        col = LEVEL_COLORS.get(level, "#9aa4b2")
        label = f"{level*100:.1f}% — {sig_map.get(level, 'retracement')} {price:.2f}"
        ann.lines.append(Line(
            kind="horizontal",
            start={"ts": a["ts"], "price": float(price)},
            end={"ts": last_ts, "price": float(price)},
            color=col, width=1, style="solid",
            label=label,
            meta={
                "pct": float(level),
                "kind": "retracement",
                "significance": sig_map.get(level, "retracement"),
                "price": float(price),
            },
        ))
        level_prices.append({"pct": level, "kind": "retracement", "price": float(price)})
    if show_extensions:
        for ext in EXTENSION_LEVELS:
            if up_move:
                price = low + height * ext
            else:
                price = high - height * (ext - 1.0)
            col = LEVEL_COLORS.get(ext, "#9aa4b2")
            label = f"{ext*100:.1f}% — {sig_ext.get(ext, 'extension target')} {price:.2f}"
            ann.lines.append(Line(
                kind="horizontal",
                start={"ts": b["ts"], "price": float(price)},
                end={"ts": last_ts, "price": float(price)},
                color=col, width=1, style="dashed",
                label=label,
                meta={
                    "pct": float(ext),
                    "kind": "extension",
                    "significance": sig_ext.get(ext, "extension target"),
                    "price": float(price),
                },
            ))
            level_prices.append({"pct": ext, "kind": "extension", "price": float(price)})

    # The base swing.
    ann.lines.append(Line(
        kind="trendline",
        start={"ts": a["ts"], "price": float(a["price"])},
        end={"ts": b["ts"], "price": float(b["price"])},
        color="#ffc107", width=2, style="solid",
        label="Anchor swing",
    ))
    ann.markers.append(Marker(
        ts=a["ts"], price=float(a["price"]),
        label="0%" if up_move else "100%",
        color="#9aa4b2", shape="text",
    ))
    ann.markers.append(Marker(
        ts=b["ts"], price=float(b["price"]),
        label="100%" if up_move else "0%",
        color="#9aa4b2", shape="text",
    ))
    ann.confidence = 0.80

    # ── MITS-P10.2 — emit Signals on 61.8% bounce + 161.8% extension hit.
    # Frost & Prechter, Elliott Wave Principle (1978), Ch. 4 — "golden
    # retracement" 61.8% is the highest-quality reversal level; Fischer,
    # The New Fibonacci Trader (2001), Ch. 6 — 161.8% extension is the
    # canonical mean-reversion target.
    from .signal_promote import promote_all
    from .schema import Signal as _Signal
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})
    # Build dict of level → price for the levels of interest.
    fib_lvl = {lv["pct"]: lv["price"] for lv in level_prices}
    sigs = []
    p618 = fib_lvl.get(0.618)
    p1618 = fib_lvl.get(1.618)
    # Find anchor's b index — only walk bars AFTER the swing completes.
    b_i = b["i"] if b.get("i") is not None else 0
    if p618 is not None:
        for i in range(max(1, b_i + 1), len(bars)):
            cl = bar_close(bars[i])
            prev = bar_close(bars[i - 1])
            ts = bar_ts(bars[i])
            # Bounce off 61.8% — close back through after tagging.
            if up_move:
                # Down-retrace into 61.8%; we want prev <= p618 < cl.
                if prev <= p618 < cl:
                    sigs.append(_Signal(
                        action="BUY",
                        ts=ts, price=float(cl), confidence=0.65,
                        reasoning=(
                            f"Close ({cl:.2f}) bounced off the 61.8% golden "
                            f"retracement ({p618:.2f}) — Frost & Prechter's "
                            "highest-quality reversal level."
                        ),
                        target_price=float(b["price"]),
                        stop_loss=float(p618),
                        instrument="stock",
                        theory_anchor={"level": "61.8%", "i": i},
                    ))
            else:
                if prev >= p618 > cl:
                    sigs.append(_Signal(
                        action="SELL",
                        ts=ts, price=float(cl), confidence=0.65,
                        reasoning=(
                            f"Close ({cl:.2f}) rejected the 61.8% golden "
                            f"retracement ({p618:.2f}) from above."
                        ),
                        target_price=float(b["price"]),
                        stop_loss=float(p618),
                        instrument="stock",
                        theory_anchor={"level": "61.8%", "i": i},
                    ))
    if p1618 is not None:
        for i in range(max(1, b_i + 1), len(bars)):
            cl = bar_close(bars[i])
            prev = bar_close(bars[i - 1])
            ts = bar_ts(bars[i])
            if up_move:
                if prev < p1618 <= cl:
                    sigs.append(_Signal(
                        action="SELL",
                        ts=ts, price=float(cl), confidence=0.55,
                        reasoning=(
                            f"Close ({cl:.2f}) tagged the 161.8% Fibonacci "
                            f"extension ({p1618:.2f}) — Fischer's canonical "
                            "mean-reversion target."
                        ),
                        target_price=float(b["price"]),
                        stop_loss=float(cl * 1.02),
                        instrument="stock",
                        theory_anchor={"level": "161.8% ext", "i": i},
                    ))
            else:
                if prev > p1618 >= cl:
                    sigs.append(_Signal(
                        action="BUY",
                        ts=ts, price=float(cl), confidence=0.55,
                        reasoning=(
                            f"Close ({cl:.2f}) tagged the 161.8% Fibonacci "
                            f"extension ({p1618:.2f}) — Fischer mean-"
                            "reversion bounce target."
                        ),
                        target_price=float(b["price"]),
                        stop_loss=float(cl * 0.98),
                        instrument="stock",
                        theory_anchor={"level": "161.8% ext", "i": i},
                    ))
    if len(sigs) > 25:
        sigs = sigs[-25:]
    ann.signals = promote_all(sigs, market_context, enabled=promote_options)

    # Plain-English "where are we now in the retracement progress?"
    spot = bar_close(bars[-1])
    progress_pct = 0.0
    if up_move:
        # Up move: 100% sits at the high (b), 0% at the low (a).
        # Retracement progress = how far back toward the low we have come.
        progress_pct = max(0.0, min(1.0, (b["price"] - spot) / height))
    else:
        progress_pct = max(0.0, min(1.0, (spot - b["price"]) / height))
    nearest = min(level_prices, key=lambda lv: abs(spot - lv["price"]))
    direction_label = "up-swing" if up_move else "down-swing"
    key_now = (
        f"Spot {spot:.2f} has retraced {progress_pct*100:.1f}% of the "
        f"{direction_label} from {a['price']:.2f} → {b['price']:.2f}. "
        f"Nearest level: {nearest['pct']*100:.1f}% "
        f"({'ext' if nearest['kind']=='extension' else 'ret'}) at "
        f"{nearest['price']:.2f}."
    )
    ann.primer = {
        "what_it_measures": (
            "Fibonacci retracements measure how deeply a counter-trend "
            "pullback has eaten into a prior swing. The ratios "
            "(23.6%, 38.2%, 50%, 61.8%, 78.6%) come from the golden ratio "
            "φ — the same mathematics describing natural growth, "
            "spirals, and population dynamics. Frost & Prechter codified "
            "the grid for Elliott-wave practitioners; Fischer expanded it "
            "to extensions (127.2%, 161.8%, 261.8%) for projecting "
            "post-breakout targets."
        ),
        "how_to_read": (
            "Treat 38.2% / 50% / 61.8% as the three retracement 'value' "
            "zones — bulls defend the up-swing on first touch, bears do "
            "the same on a down-swing. 78.6% is the 'last-stand' line: "
            "a clean break past it usually invalidates the trend. "
            "Extensions (127.2% and 161.8%) are the canonical profit "
            "targets after a successful retracement-and-resume."
        ),
        "key_levels_now": key_now,
    }
    return ann


__all__ = ["analyze", "RETRACEMENT_LEVELS", "EXTENSION_LEVELS"]
