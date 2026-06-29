"""Elliott Wave — MITS Phase 10 theory module (confidence-flag).

Citation:

  * Ralph N. Elliott, "The Wave Principle" (Investment Counsel, 1938) —
    the original publication. Elliott observed that crowd psychology
    moves markets in a recognisable 5-3 wave pattern.
  * A. J. Frost & Robert R. Prechter Jr., "Elliott Wave Principle: Key
    to Market Behavior" (10th ed., New Classics Library, 2005). The
    modern canonical reference. Codifies the Fibonacci wave-ratio
    relationships used below.

5-wave impulse rules (must hold):

  * Wave 2 NEVER retraces more than 100% of Wave 1.
  * Wave 3 is NEVER the shortest of waves 1, 3, 5.
  * Wave 4 does NOT overlap Wave 1's price territory (with rare
    diagonals — the typical impulse rule).

Typical Fibonacci ratios (probabilistic, not rules):

  * Wave 2 = 50% / 61.8% / 78.6% retrace of Wave 1.
  * Wave 3 = 161.8% extension of Wave 1 (commonest).
  * Wave 4 = 23.6% / 38.2% retrace of Wave 3.
  * Wave 5 ≈ Wave 1 OR 61.8% of (Wave 1 + Wave 3).

This implementation is a HEURISTIC labelling — Elliott counting is
notoriously subjective. The confidence flag reflects this: a perfect
5-pivot match with all rule-checks passing returns ~0.65; otherwise
the theory returns a low-confidence "best guess" and flags it.

Signals:

  * BUY  when wave 4 looks complete (5th wave-up expected).
  * SELL when wave 5 looks complete (correction expected).
  * WATCH otherwise — the count is ambiguous.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

from ._zigzag import detect_pivots
from .schema import (
    Line, Marker, Signal, TheoryAnnotation,
    bar_close, bar_ts,
)


def _classify(pivots: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Take the last 6 pivots and try to label them 0-1-2-3-4-5."""
    if len(pivots) < 6:
        return None
    p = pivots[-6:]
    # Pivot 0 should be opposite-type to wave1's end (pivot 1).
    types = [x["type"] for x in p]
    # An "up" impulse: low-high-low-high-low-high.
    up_pattern = ["low", "high", "low", "high", "low", "high"]
    dn_pattern = ["high", "low", "high", "low", "high", "low"]
    if types == up_pattern:
        direction = "up"
    elif types == dn_pattern:
        direction = "down"
    else:
        return None
    w0, w1, w2, w3, w4, w5 = p
    wave1 = abs(w1["price"] - w0["price"])
    wave2 = abs(w2["price"] - w1["price"])
    wave3 = abs(w3["price"] - w2["price"])
    wave4 = abs(w4["price"] - w3["price"])
    wave5 = abs(w5["price"] - w4["price"])
    if wave1 == 0 or wave3 == 0:
        return None

    rules_pass = []
    rules_pass.append(wave2 < wave1)               # rule 1
    rules_pass.append(not (wave3 < wave1 and wave3 < wave5))  # rule 2
    # rule 3 (no overlap): w4 stays past w1.
    if direction == "up":
        rules_pass.append(w4["price"] >= w1["price"])
    else:
        rules_pass.append(w4["price"] <= w1["price"])

    score = sum(1 for r in rules_pass if r) / 3.0
    return {
        "direction": direction,
        "pivots": p,
        "wave1": wave1, "wave2": wave2, "wave3": wave3,
        "wave4": wave4, "wave5": wave5,
        "score": score,
        "rules_pass": rules_pass,
        "ratios": {
            "w2/w1": wave2 / wave1 if wave1 else None,
            "w3/w1": wave3 / wave1 if wave1 else None,
            "w4/w3": wave4 / wave3 if wave3 else None,
            "w5/w1": wave5 / wave1 if wave1 else None,
        },
    }


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    zigzag_pct = float(params.get("zigzag_pct",
                                       getattr(TUNABLES, "theory_zigzag_pct", 3.0)))

    ann = TheoryAnnotation(
        theory="elliott_wave",
        params={"zigzag_pct": zigzag_pct},
        citation=(
            "Elliott, 'The Wave Principle' (Investment Counsel 1938); "
            "Frost & Prechter, 'Elliott Wave Principle' (New Classics "
            "Library, 10th ed., 2005)."
        ),
    )
    if len(bars) < 30:
        ann.notes.append("Not enough bars for an Elliott count.")
        return ann

    pivots = detect_pivots(bars, threshold_pct=zigzag_pct)
    if len(pivots) < 6:
        ann.notes.append("Not enough pivots for a 5-wave count.")
        return ann

    count = _classify(pivots)
    if count is None:
        ann.notes.append("Pivot sequence doesn't match a 5-wave impulse.")
        ann.confidence = 0.30
        return ann

    p = count["pivots"]
    labels = ["0", "1", "2", "3", "4", "5"]
    colors = ["#9aa4b2", "#36c26b", "#ff5a5f", "#1f6feb", "#ff9f1c", "#ffd166"]
    for i in range(1, 6):
        ann.lines.append(Line(
            kind="trendline",
            start={"ts": p[i - 1]["ts"], "price": float(p[i - 1]["price"])},
            end={"ts": p[i]["ts"], "price": float(p[i]["price"])},
            color=colors[i], width=2, style="solid",
            label=f"Wave {labels[i]}",
            meta={"kind": "elliott_wave", "wave": labels[i]},
        ))
    for i, q in enumerate(p):
        ann.markers.append(Marker(
            ts=q["ts"], price=float(q["price"]),
            label=labels[i], color=colors[i], shape="circle",
        ))

    ann.pattern_name = f"5wave_{count['direction']}"
    ann.confidence = max(0.30, count["score"] * 0.65)
    if count["score"] < 1.0:
        ann.notes.append(
            "Confidence flag: not all 3 Elliott rules pass. "
            "Counts are heuristic — DO NOT trade off this alone."
        )

    sigs: List[Signal] = []
    last_ts = bar_ts(bars[-1])
    last_close = bar_close(bars[-1])
    if count["direction"] == "up":
        # If price is at/near pivot 5 (the last high), expect ABC correction.
        sigs.append(Signal(
            action="SELL",
            ts=last_ts, price=last_close, confidence=ann.confidence,
            reasoning=(
                f"5-wave UP impulse appears complete at "
                f"{p[5]['price']:.2f}. Frost & Prechter expect an ABC "
                f"corrective wave to retrace 38.2–61.8% of waves 1+3+5. "
                "CONFIDENCE-FLAG: Elliott counts are subjective."
            ),
            target_price=float(p[5]["price"] - (p[5]["price"] - p[0]["price"]) * 0.382),
            stop_loss=float(p[5]["price"] * 1.02),
            instrument="stock",
            theory_anchor={"wave": "5_complete", "direction": "up"},
        ))
    else:
        sigs.append(Signal(
            action="BUY",
            ts=last_ts, price=last_close, confidence=ann.confidence,
            reasoning=(
                f"5-wave DOWN impulse appears complete at "
                f"{p[5]['price']:.2f}. Frost & Prechter expect an ABC "
                f"corrective UP-wave to retrace 38.2–61.8%. "
                "CONFIDENCE-FLAG: Elliott counts are subjective."
            ),
            target_price=float(p[5]["price"] + (p[0]["price"] - p[5]["price"]) * 0.382),
            stop_loss=float(p[5]["price"] * 0.98),
            instrument="stock",
            theory_anchor={"wave": "5_complete", "direction": "down"},
        ))

    ann.signals = sigs
    ratios_str = "  ·  ".join(
        f"{k}: {(v or 0):.2f}" for k, v in count["ratios"].items() if v is not None
    )
    ann.primer = {
        "what_it_measures": (
            "Elliott's Wave Principle holds that crowd psychology moves "
            "markets in 5-wave impulses (3 with-trend + 2 corrective) "
            "followed by 3-wave (ABC) corrections. Frost & Prechter "
            "codified the modern rule set and Fibonacci ratio cousins."
        ),
        "how_to_read": (
            "5-wave UP complete = expect an ABC down correction. 5-down "
            "complete = expect an ABC up correction. Wave 3 is usually "
            "the extended one (1.618× wave 1). CONFIDENCE FLAG: counting "
            "is SUBJECTIVE — Elliott himself revised his counts in print. "
            "Treat this theory as a framing tool, not a trade trigger on "
            "its own."
        ),
        "key_levels_now": (
            f"Last wave-5 pivot: {p[5]['price']:.2f}  ·  Score "
            f"{count['score']*100:.0f}% rules pass  ·  Ratios: {ratios_str}"
        ),
    }
    return ann


__all__ = ["analyze"]
