"""Anchored VWAP — MITS Phase 10 theory module.

Citation:

  * Brian Shannon, "Maximum Trading Gains with Anchored VWAP"
    (Self-published, 2022). Shannon popularised the technique of
    anchoring VWAP to a specific bar (earnings, FOMC, swing pivot)
    rather than the trading-day open.
  * Paul L. Kaufman, "Trading Systems and Methods" (Wiley, 6th ed.,
    2020) — Chapter on Volume-Weighted Average Price.

    AVWAP(anchor) = Σ(price · volume) / Σ(volume),  cumulative
    from the anchor bar forward, where ``price`` is the typical
    price (H + L + C) / 3.

Anchors used here (auto):

  * The most recent **swing pivot** detected by ZigZag.
  * The most recent **major gap** (|gap| > 2% on open).
  * The first bar (window-start VWAP) — for context.

Signals:

  * BUY  when price reclaims an AVWAP from below.
  * SELL when price loses an AVWAP from above.
  * The most recent (rightmost) AVWAP is treated as the most
    actionable anchor.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

from ._zigzag import detect_pivots
from .schema import (
    Line, Marker, Signal, TheoryAnnotation,
    bar_close, bar_high, bar_low, bar_ts,
)


def _avwap(bars: List[Dict[str, Any]], anchor_idx: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    cum_pv = 0.0
    cum_v = 0.0
    for i, b in enumerate(bars):
        if i < anchor_idx:
            out.append(None); continue
        h = bar_high(b); l = bar_low(b); c = bar_close(b)
        tp = (h + l + c) / 3.0
        v = float(b.get("volume") or 0.0)
        cum_pv += tp * v
        cum_v += v
        if cum_v <= 0:
            out.append(None)
        else:
            out.append(cum_pv / cum_v)
    return out


def _find_major_gap(bars: List[Dict[str, Any]], min_gap_pct: float) -> Optional[int]:
    for i in range(len(bars) - 1, 0, -1):
        prev_close = bar_close(bars[i - 1])
        open_now = float(bars[i].get("open") or 0.0)
        if prev_close <= 0 or open_now <= 0:
            continue
        gap = (open_now - prev_close) / prev_close
        if abs(gap) >= min_gap_pct:
            return i
    return None


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    zigzag_pct = float(params.get("zigzag_pct",
                                       getattr(TUNABLES, "theory_zigzag_pct", 3.0)))
    min_gap_pct = float(params.get("min_gap_pct", 0.02))
    lookback = int(params.get("lookback", 250))

    ann = TheoryAnnotation(
        theory="avwap",
        params={"zigzag_pct": zigzag_pct, "min_gap_pct": min_gap_pct,
                "lookback": lookback},
        citation=(
            "Shannon, 'Maximum Trading Gains with Anchored VWAP' (2022); "
            "Kaufman, 'Trading Systems and Methods' (Wiley 2020) — VWAP chapter."
        ),
    )
    if len(bars) < 10:
        ann.notes.append("Not enough bars for an Anchored VWAP.")
        return ann

    anchors: List[Dict[str, Any]] = []

    # 1) Window-start.
    anchors.append({"i": 0, "label": "Window start", "color": "#9aa4b2"})

    # 2) Most-recent significant pivot.
    pivots = detect_pivots(bars[-lookback:], threshold_pct=zigzag_pct)
    if pivots:
        last_pivot = pivots[-1]
        global_i = (len(bars) - lookback) + last_pivot["i"] if len(bars) > lookback else last_pivot["i"]
        anchors.append({
            "i": max(0, global_i),
            "label": f"Pivot {last_pivot['type']}",
            "color": "#ffd166",
        })

    # 3) Major gap anchor (earnings-like).
    gap_idx = _find_major_gap(bars[-lookback:], min_gap_pct=min_gap_pct)
    if gap_idx is not None:
        global_g = (len(bars) - lookback) + gap_idx if len(bars) > lookback else gap_idx
        anchors.append({
            "i": global_g, "label": "Gap anchor",
            "color": "#36c26b",
        })

    # MITS Phase 10.1 — one ``series`` Line per anchor (typically 3).
    avwaps: List[List[Optional[float]]] = []
    for a in anchors:
        series = _avwap(bars, a["i"])
        avwaps.append(series)
        points = [
            {"ts": bar_ts(bars[i]), "price": float(v)}
            for i, v in enumerate(series) if v is not None
        ]
        if points:
            ann.lines.append(Line(
                kind="series",
                start=points[0],
                end=points[-1],
                color=a["color"], width=1, style="solid",
                label=a["label"],
                meta={"kind": "avwap", "anchor_ts": bar_ts(bars[a["i"]])},
                points=points,
            ))
        # Anchor marker.
        ann.markers.append(Marker(
            ts=bar_ts(bars[a["i"]]),
            price=float(bar_close(bars[a["i"]])),
            label=a["label"],
            color=a["color"],
            shape="circle",
        ))

    # MITS-P10.2 — walk every bar; emit a BUY each time price RECLAIMS
    # the most-recent AVWAP from below, SELL each time it LOSES the
    # AVWAP from above (Brian Shannon's "institutional cost-basis flip").
    # We use the rightmost (most-recent) anchor only — older anchors
    # become noise once a fresher earnings/gap anchor exists.
    from .signal_promote import promote_all
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})
    sigs: List[Signal] = []
    if avwaps:
        # Most-recent anchor = last entry in anchors[].
        series = avwaps[-1]
        label = anchors[-1]["label"]
        for i in range(1, len(bars)):
            if i >= len(series):
                continue
            avwap_now = series[i]
            avwap_prev = series[i - 1]
            if avwap_now is None or avwap_prev is None:
                continue
            cl = bar_close(bars[i])
            cl_prev = bar_close(bars[i - 1])
            ts = bar_ts(bars[i])
            if cl_prev < avwap_prev and cl > avwap_now:
                sigs.append(Signal(
                    action="BUY",
                    ts=ts, price=float(cl), confidence=0.65,
                    reasoning=(
                        f"Close ({cl:.2f}) reclaimed the {label} Anchored "
                        f"VWAP ({avwap_now:.2f}) from below — institutional "
                        "cost-basis flip is bullish (Brian Shannon)."
                    ),
                    stop_loss=float(avwap_now),
                    instrument="stock",
                    theory_anchor={"avwap": label, "i": i},
                ))
            elif cl_prev > avwap_prev and cl < avwap_now:
                sigs.append(Signal(
                    action="SELL",
                    ts=ts, price=float(cl), confidence=0.65,
                    reasoning=(
                        f"Close ({cl:.2f}) lost the {label} Anchored "
                        f"VWAP ({avwap_now:.2f}) — institutional cost-"
                        "basis flip is bearish."
                    ),
                    stop_loss=float(avwap_now),
                    instrument="stock",
                    theory_anchor={"avwap": label, "i": i},
                ))

    if len(sigs) > 25:
        sigs = sigs[-25:]
    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.82
    last_close = bar_close(bars[-1])
    primer_levels = []
    for a, s in zip(anchors, avwaps):
        v = s[-1] if s else None
        if v is not None:
            primer_levels.append(f"{a['label']}: {v:.2f}")
    ann.primer = {
        "what_it_measures": (
            "Anchored VWAP is the volume-weighted average price computed "
            "from a chosen anchor bar (earnings, gap, swing pivot) "
            "forward. Unlike daily VWAP, it does NOT reset — so it traces "
            "the institutional cost basis of every share traded since "
            "that anchor. Brian Shannon popularised the technique."
        ),
        "how_to_read": (
            "Above an AVWAP = buyers from the anchor are in profit and "
            "will defend it on retests. Below = trapped sellers; the line "
            "becomes resistance. A reclaim is the highest-quality long "
            "trigger Shannon teaches. Use the GAP anchor as the prime "
            "post-earnings level."
        ),
        "key_levels_now": "  ·  ".join(primer_levels) if primer_levels else "—",
    }
    return ann


__all__ = ["analyze"]
