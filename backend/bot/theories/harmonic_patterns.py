"""Harmonic Patterns (Gartley / Butterfly / Bat / Crab / Cypher) —
MITS Phase 10 theory module.

Citation:

  * H. M. Gartley, "Profits in the Stock Market" (Lambert-Gann, 1935)
    — first published the "222" pattern, the seed of the harmonic
    family.
  * Larry Pesavento, "Fibonacci Ratios with Pattern Recognition" (Trader's
    Press, 1997) — formalised the Fibonacci-ratio constraints used by
    every modern harmonic toolkit.
  * Scott M. Carney, "Harmonic Trading, Volumes I & II" (FT Press,
    2010) — modern canonical reference. Defines Bat, Crab, Cypher.

XABCD-pattern ratio constraints (allowable ±5%):

    Gartley:    AB = 0.618 XA, BC ∈ [0.382, 0.886] AB, CD ∈ [1.272, 1.618] BC, AD = 0.786 XA
    Butterfly:  AB = 0.786 XA, BC ∈ [0.382, 0.886] AB, CD ∈ [1.618, 2.618] BC, AD = 1.27 XA
    Bat:        AB ∈ [0.382, 0.50] XA, BC ∈ [0.382, 0.886] AB, CD ∈ [1.618, 2.618] BC, AD = 0.886 XA
    Crab:       AB ∈ [0.382, 0.618] XA, BC ∈ [0.382, 0.886] AB, CD ∈ [2.618, 3.618] BC, AD = 1.618 XA
    Cypher:     AB ∈ [0.382, 0.618] XA, BC ∈ [1.13, 1.414] AB,  CD ∈ [1.272, 1.414] XC, AD = 0.786 XC

Signal at point D = the PRZ (Potential Reversal Zone). BUY for bullish
variants (X high, D low), SELL for bearish (X low, D high).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from backend.config import TUNABLES

from ._zigzag import detect_pivots
from .schema import (
    Line, Marker, Signal, TheoryAnnotation, Zone,
    bar_close, bar_ts,
)


PATTERNS: Dict[str, Dict[str, Any]] = {
    "gartley":   {"AB_XA": (0.618, 0.618), "BC_AB": (0.382, 0.886),
                   "CD_BC": (1.272, 1.618), "AD_XA": (0.786, 0.786)},
    "butterfly": {"AB_XA": (0.786, 0.786), "BC_AB": (0.382, 0.886),
                   "CD_BC": (1.618, 2.618), "AD_XA": (1.27, 1.27)},
    "bat":       {"AB_XA": (0.382, 0.50),  "BC_AB": (0.382, 0.886),
                   "CD_BC": (1.618, 2.618), "AD_XA": (0.886, 0.886)},
    "crab":      {"AB_XA": (0.382, 0.618), "BC_AB": (0.382, 0.886),
                   "CD_BC": (2.618, 3.618), "AD_XA": (1.618, 1.618)},
}


def _within(value: float, lo: float, hi: float, tol: float = 0.05) -> bool:
    margin = max(0.0, (hi - lo) / 2.0) + max(lo, hi) * tol
    return (lo - margin) <= value <= (hi + margin)


def _evaluate(X, A, B, C, D, spec) -> Tuple[float, Dict[str, float]]:
    """Return ``(score, ratios)`` for a XABCD candidate vs a spec."""
    XA = abs(A["price"] - X["price"])
    AB = abs(B["price"] - A["price"])
    BC = abs(C["price"] - B["price"])
    CD = abs(D["price"] - C["price"])
    AD = abs(D["price"] - A["price"])
    if XA <= 0 or AB <= 0 or BC <= 0 or CD <= 0:
        return 0.0, {}
    r_ab_xa = AB / XA
    r_bc_ab = BC / AB
    r_cd_bc = CD / BC
    r_ad_xa = AD / XA
    score = 0
    if _within(r_ab_xa, *spec["AB_XA"]): score += 1
    if _within(r_bc_ab, *spec["BC_AB"]): score += 1
    if _within(r_cd_bc, *spec["CD_BC"]): score += 1
    if _within(r_ad_xa, *spec["AD_XA"]): score += 1
    return score / 4.0, {"AB/XA": r_ab_xa, "BC/AB": r_bc_ab,
                          "CD/BC": r_cd_bc, "AD/XA": r_ad_xa}


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    zigzag_pct = float(params.get("zigzag_pct",
                                       getattr(TUNABLES, "theory_zigzag_pct", 3.0)))
    min_score = float(params.get("min_score", 0.75))

    ann = TheoryAnnotation(
        theory="harmonic_patterns",
        params={"zigzag_pct": zigzag_pct, "min_score": min_score,
                "patterns": list(PATTERNS.keys())},
        citation=(
            "Gartley, 'Profits in the Stock Market' (Lambert-Gann 1935); "
            "Pesavento, 'Fibonacci Ratios with Pattern Recognition' (1997); "
            "Carney, 'Harmonic Trading' Vols I & II (FT Press 2010)."
        ),
    )
    if len(bars) < 30:
        ann.notes.append("Not enough bars for harmonic search.")
        return ann

    pivots = detect_pivots(bars, threshold_pct=zigzag_pct)
    if len(pivots) < 5:
        ann.notes.append("Need at least 5 pivots for XABCD scanning.")
        return ann

    best: Optional[Dict[str, Any]] = None
    # Walk the last K=8 pivots' last-5 windows.
    K = min(12, len(pivots))
    candidates = pivots[-K:]
    for i in range(0, len(candidates) - 4):
        X, A, B, C, D = candidates[i:i + 5]
        # Ensure alternating types: harmonic patterns require XABCD with
        # alternating directions.
        types = [p["type"] for p in (X, A, B, C, D)]
        if len(set(types[0::2])) != 1 or len(set(types[1::2])) != 1:
            continue
        # Bullish XABCD = X high, A low, B high, C low, D low (D is the BUY).
        bullish = (X["type"] == "high" and D["type"] == "low")
        bearish = (X["type"] == "low"  and D["type"] == "high")
        if not (bullish or bearish):
            continue
        for name, spec in PATTERNS.items():
            score, ratios = _evaluate(X, A, B, C, D, spec)
            if score < min_score:
                continue
            if best is None or score > best["score"]:
                best = {
                    "name": name, "score": score, "ratios": ratios,
                    "X": X, "A": A, "B": B, "C": C, "D": D,
                    "side": "bullish" if bullish else "bearish",
                }

    if best is None:
        ann.notes.append("No XABCD pattern met the minimum score.")
        return ann

    X, A, B, C, D = best["X"], best["A"], best["B"], best["C"], best["D"]
    ann.pattern_name = f"{best['name']}_{best['side']}"
    ann.confidence = best["score"]

    # Draw the XABCD skeleton.
    legs = [("XA", X, A, "#9aa4b2"), ("AB", A, B, "#1f6feb"),
            ("BC", B, C, "#36c26b"), ("CD", C, D, "#ff5a5f")]
    for label, a, b, color in legs:
        ann.lines.append(Line(
            kind="trendline",
            start={"ts": a["ts"], "price": float(a["price"])},
            end={"ts": b["ts"], "price": float(b["price"])},
            color=color, width=2, style="solid",
            label=label,
            meta={"kind": "harmonic_leg", "leg": label},
        ))

    # Mark the PRZ at D ±2% as a zone.
    prz_low = D["price"] * 0.98
    prz_high = D["price"] * 1.02
    ann.zones.append(Zone(
        x1=D["ts"], y1=float(prz_low),
        x2=bar_ts(bars[-1]), y2=float(prz_high),
        color=("#36c26b" if best["side"] == "bullish" else "#ff5a5f"),
        opacity=0.15,
        label=f"PRZ — {best['name']} {best['side']}",
    ))

    for label, p in (("X", X), ("A", A), ("B", B), ("C", C), ("D", D)):
        ann.markers.append(Marker(
            ts=p["ts"], price=float(p["price"]),
            label=label, color="#ffd166", shape="circle",
        ))

    sigs: List[Signal] = []
    last_ts = bar_ts(bars[-1])
    last_close = bar_close(bars[-1])
    if best["side"] == "bullish":
        target_pct = 0.382 if best["name"] == "gartley" else 0.50
        sigs.append(Signal(
            action="BUY",
            ts=last_ts, price=float(D["price"]), confidence=best["score"],
            reasoning=(
                f"{best['name'].title()} bullish XABCD complete at "
                f"D = {D['price']:.2f}. Ratios: " +
                ", ".join(f"{k} {v:.3f}" for k, v in best["ratios"].items()) +
                f". Carney target: {target_pct*100:.1f}% retrace of CD."
            ),
            target_price=float(D["price"] + abs(C["price"] - D["price"]) * target_pct),
            stop_loss=float(D["price"] * 0.98),
            instrument="stock",
            theory_anchor={"pattern": best["name"], "side": "bullish",
                            "score": best["score"]},
        ))
    else:
        target_pct = 0.382 if best["name"] == "gartley" else 0.50
        sigs.append(Signal(
            action="SELL",
            ts=last_ts, price=float(D["price"]), confidence=best["score"],
            reasoning=(
                f"{best['name'].title()} bearish XABCD complete at "
                f"D = {D['price']:.2f}. Ratios: " +
                ", ".join(f"{k} {v:.3f}" for k, v in best["ratios"].items()) +
                f". Carney target: {target_pct*100:.1f}% retrace of CD."
            ),
            target_price=float(D["price"] - abs(C["price"] - D["price"]) * target_pct),
            stop_loss=float(D["price"] * 1.02),
            instrument="stock",
            theory_anchor={"pattern": best["name"], "side": "bearish",
                            "score": best["score"]},
        ))

    ann.signals = sigs
    ann.primer = {
        "what_it_measures": (
            "Harmonic patterns are XABCD price structures whose four "
            "legs honour a SPECIFIC Fibonacci ratio set per pattern. "
            "Gartley wrote the seed in 1935; Pesavento and Carney "
            "formalised the modern Gartley / Butterfly / Bat / Crab / "
            "Cypher templates. Point D is the Potential Reversal Zone "
            "(PRZ) — the trigger."
        ),
        "how_to_read": (
            "Wait for the structure to complete (price arrives at D). "
            "Take the trade with stop just past D and target the 38.2% "
            "/ 50% retrace of CD. Higher pattern-match score = higher "
            "expected win-rate (Carney's audit: ~70% at score ≥ 0.85). "
            "DO NOT pre-empt — the pattern is unconfirmed until D prints."
        ),
        "key_levels_now": (
            f"{best['name'].title()} {best['side']} "
            f"(score {best['score']*100:.0f}%)  ·  D = {D['price']:.2f}  ·  "
            f"PRZ {prz_low:.2f}–{prz_high:.2f}"
        ),
    }
    return ann


__all__ = ["analyze", "PATTERNS"]
