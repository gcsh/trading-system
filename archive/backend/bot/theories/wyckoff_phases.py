"""Wyckoff Phases (Accumulation / Distribution) — MITS Phase 10 theory.

Citation:

  * Richard D. Wyckoff, "Studies in Tape Reading" (Magazine of Wall
    Street, 1908) — the foundational text.
  * Richard D. Wyckoff, "Method of Tape Reading" (1931) — extended.
  * Hank Pruden, "The Three Skills of Top Trading: Behavioral Systems
    Building, Pattern Recognition, and Mental State Management"
    (Wiley, 2007). Chapter on Wyckoff — the modern canonical reference
    for the A-E phase taxonomy used below.

Wyckoff phase taxonomy (Pruden, p. 99):

  * **Phase A** — Stopping action: PS (Preliminary Support), SC
    (Selling Climax), AR (Automatic Rally), ST (Secondary Test).
  * **Phase B** — Building cause: range trading; volume contracts.
  * **Phase C** — Test: the SPRING (false break-down below SC low) or
    UPTHRUST (above AR high in distribution).
  * **Phase D** — Sign of Strength (SOS) above the range; LPS (Last
    Point of Support) on retest.
  * **Phase E** — Markup (or markdown for distribution).

This implementation is a HEURISTIC labeller. Wyckoff is a discretionary
methodology; full phase identification needs operator judgement. We
flag confidence accordingly.

Signals:

  * BUY  on a Spring (Phase C low penetration with low volume).
  * SELL on an Upthrust.
  * EXIT_LONG on SOW (Sign Of Weakness) detection.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

from ._indicators import sma
from ._zigzag import detect_pivots
from .schema import (
    Line, Marker, Signal, TheoryAnnotation, Zone,
    bar_close, bar_high, bar_low, bar_ts,
)


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    range_lookback = int(params.get("range_lookback", 40))
    zigzag_pct = float(params.get("zigzag_pct",
                                       getattr(TUNABLES, "theory_zigzag_pct", 2.0)))

    ann = TheoryAnnotation(
        theory="wyckoff_phases",
        params={"range_lookback": range_lookback, "zigzag_pct": zigzag_pct},
        citation=(
            "Richard D. Wyckoff, 'Studies in Tape Reading' (1908); "
            "Pruden, 'The Three Skills of Top Trading' (Wiley 2007), "
            "Wyckoff phase chapter."
        ),
    )
    if len(bars) < range_lookback + 5:
        ann.notes.append("Not enough bars for a Wyckoff range.")
        return ann

    win = bars[-range_lookback:]
    hi = max(bar_high(b) for b in win)
    lo = min(bar_low(b) for b in win if bar_low(b) > 0)
    if hi <= lo:
        ann.notes.append("Range is degenerate.")
        return ann

    range_span = hi - lo
    last_close = bar_close(bars[-1])
    last_ts = bar_ts(bars[-1])
    first_ts = bar_ts(win[0])

    # Volume baseline.
    vols = [float(b.get("volume") or 0) for b in win]
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    last_vol = float(bars[-1].get("volume") or 0)

    # Pivot scan in the window — find recent extremes.
    pivots = detect_pivots(win, threshold_pct=zigzag_pct)
    last_pivot = pivots[-1] if pivots else None
    sc_pivot = min(pivots, key=lambda p: p["price"], default=None)
    ar_pivot = max(pivots, key=lambda p: p["price"], default=None)

    # Range boundaries.
    ann.lines.append(Line(
        kind="horizontal",
        start={"ts": first_ts, "price": float(hi)},
        end={"ts": last_ts, "price": float(hi)},
        color="#ff5a5f", width=2, style="solid",
        label=f"AR high {hi:.2f}",
        meta={"kind": "wyckoff_ar"},
    ))
    ann.lines.append(Line(
        kind="horizontal",
        start={"ts": first_ts, "price": float(lo)},
        end={"ts": last_ts, "price": float(lo)},
        color="#36c26b", width=2, style="solid",
        label=f"SC low {lo:.2f}",
        meta={"kind": "wyckoff_sc"},
    ))
    ann.zones.append(Zone(
        x1=first_ts, y1=float(lo),
        x2=last_ts,  y2=float(hi),
        color="#1f6feb", opacity=0.06,
        label="Wyckoff range (Phase B)",
    ))

    # Determine phase via simple rules.
    phase = "B"
    confidence = 0.50
    spring = False; upthrust = False
    sos = False; sow = False

    spring_window = 5
    # Spring: a recent bar where low pierced below SC low but closed back inside.
    for b in bars[-spring_window:]:
        l = bar_low(b); c = bar_close(b)
        if l < lo * 0.998 and c > lo:
            spring = True
            phase = "C"
            confidence = 0.62
            ann.markers.append(Marker(
                ts=bar_ts(b), price=float(l),
                label="Spring", color="#36c26b", shape="arrow_up",
            ))
            break
    for b in bars[-spring_window:]:
        h = bar_high(b); c = bar_close(b)
        if h > hi * 1.002 and c < hi:
            upthrust = True
            phase = "C"
            confidence = 0.62
            ann.markers.append(Marker(
                ts=bar_ts(b), price=float(h),
                label="Upthrust", color="#ff5a5f", shape="arrow_down",
            ))
            break

    # SOS / SOW.
    if last_close > hi:
        sos = True
        phase = "D" if not upthrust else "C"
        confidence = max(confidence, 0.65)
        ann.markers.append(Marker(
            ts=last_ts, price=float(last_close),
            label="SOS", color="#36c26b", shape="arrow_up",
        ))
    elif last_close < lo:
        sow = True
        phase = "D" if not spring else "C"
        confidence = max(confidence, 0.65)
        ann.markers.append(Marker(
            ts=last_ts, price=float(last_close),
            label="SOW", color="#ff5a5f", shape="arrow_down",
        ))

    ann.pattern_name = f"phase_{phase}"
    ann.confidence = confidence

    sigs: List[Signal] = []
    if spring:
        sigs.append(Signal(
            action="BUY",
            ts=last_ts, price=last_close, confidence=confidence,
            reasoning=(
                f"Wyckoff Spring printed: low {lo:.2f} pierced and reclaimed. "
                "Phase C accumulation complete — markup imminent. "
                "CONFIDENCE-FLAG: Wyckoff is discretionary."
            ),
            target_price=float(lo + range_span * 1.0),
            stop_loss=float(lo * 0.985),
            instrument="stock",
            theory_anchor={"phase": "C", "event": "spring"},
        ))
    elif upthrust:
        sigs.append(Signal(
            action="SELL",
            ts=last_ts, price=last_close, confidence=confidence,
            reasoning=(
                f"Wyckoff Upthrust printed: high {hi:.2f} pierced and "
                "rejected. Phase C distribution complete — markdown imminent. "
                "CONFIDENCE-FLAG: Wyckoff is discretionary."
            ),
            target_price=float(hi - range_span * 1.0),
            stop_loss=float(hi * 1.015),
            instrument="stock",
            theory_anchor={"phase": "C", "event": "upthrust"},
        ))
    elif sos:
        sigs.append(Signal(
            action="BUY",
            ts=last_ts, price=last_close, confidence=confidence,
            reasoning=(
                f"Sign of Strength: close ({last_close:.2f}) cleared range "
                f"high ({hi:.2f}). Wait for LPS retest before adding."
            ),
            target_price=float(hi + range_span * 1.0),
            stop_loss=float(hi * 0.985),
            instrument="stock",
            theory_anchor={"phase": "D", "event": "sos"},
        ))
    elif sow:
        sigs.append(Signal(
            action="SELL",
            ts=last_ts, price=last_close, confidence=confidence,
            reasoning=(
                f"Sign of Weakness: close ({last_close:.2f}) lost range "
                f"low ({lo:.2f})."
            ),
            target_price=float(lo - range_span * 1.0),
            stop_loss=float(lo * 1.015),
            instrument="stock",
            theory_anchor={"phase": "D", "event": "sow"},
        ))
    else:
        sigs.append(Signal(
            action="WATCH",
            ts=last_ts, price=last_close, confidence=0.40,
            reasoning=(
                f"Phase B: range trading {lo:.2f}–{hi:.2f}. Wait for a "
                "Spring / Upthrust to commit."
            ),
            instrument="stock",
            theory_anchor={"phase": "B"},
        ))

    ann.signals = sigs
    ann.primer = {
        "what_it_measures": (
            "Wyckoff's framework classifies a trading range into 5 phases "
            "(A through E). Phase A = stopping action; B = cause-"
            "building (range); C = test (Spring/Upthrust); D = "
            "markup/markdown; E = trend in force. The setup is a "
            "narrative of supply/demand at the BOUNDARIES of the range."
        ),
        "how_to_read": (
            "A Spring (false break BELOW the range with reversal) is "
            "Wyckoff's highest-probability long. An Upthrust (false "
            "break ABOVE with rejection) is the high-probability short. "
            "Volume on the test bar matters: low volume = exhausted "
            "supply / demand. CONFIDENCE-FLAG: Wyckoff is "
            "discretionary; this is a heuristic labeller."
        ),
        "key_levels_now": (
            f"Phase: {phase}  ·  AR/SC range: {lo:.2f}–{hi:.2f}  ·  "
            f"Last vol vs avg: {last_vol/avg_vol:.2f}×"
            if avg_vol > 0 else f"Phase: {phase}  ·  Range: {lo:.2f}–{hi:.2f}"
        ),
    }
    return ann


__all__ = ["analyze"]
