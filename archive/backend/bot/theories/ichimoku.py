"""Ichimoku Kinko Hyo (一目均衡表) cloud.

Per Goichi Hosoda's 1969 original specification (and the modern
TradingView / MT5 default-parameter sets):

  * Tenkan-sen  (転換線, Conversion Line):
        (highest_high_9 + lowest_low_9) / 2
  * Kijun-sen   (基準線, Base Line):
        (highest_high_26 + lowest_low_26) / 2
  * Senkou Span A (先行スパン A, Leading Span A):
        (Tenkan + Kijun) / 2, plotted 26 periods AHEAD.
  * Senkou Span B (先行スパン B, Leading Span B):
        (highest_high_52 + lowest_low_52) / 2, plotted 26 periods AHEAD.
  * Chikou Span (遅行スパン, Lagging Span):
        close, plotted 26 periods BACK.
  * Cloud (Kumo, 雲):
        area between Senkou A and Senkou B.
        Green when A > B (bullish), red when B > A (bearish).

References:

  * Goichi Hosoda (細田悟一), "一目均衡表" ("Ichimoku Kinko Hyo"),
    1969 first edition. The canonical 9/26/52/26 period spec.
  * Manesh Patel, "Trading with Ichimoku Clouds: The Essential Guide to
    Ichimoku Kinko Hyo Technical Analysis" (Wiley, 2010).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .schema import (
    Line, TheoryAnnotation, Zone,
    bar_close, bar_high, bar_low, bar_ts,
)


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _hl_mid(bars: List[Dict[str, Any]], end_idx: int,
             window: int) -> Optional[float]:
    if end_idx + 1 < window:
        return None
    lo = end_idx + 1 - window
    sub = bars[lo:end_idx + 1]
    if not sub:
        return None
    hh = max(bar_high(b) for b in sub)
    ll = min(bar_low(b) for b in sub if bar_low(b) > 0)
    return (hh + ll) / 2.0


def _shift_ts(bars: List[Dict[str, Any]], from_idx: int, shift: int) -> str:
    """ISO timestamp ``shift`` bars away from ``from_idx`` (positive =
    future, negative = past). Extrapolates past the end of the series."""
    target = from_idx + shift
    if 0 <= target < len(bars):
        return bar_ts(bars[target])
    if not bars:
        return ""
    # Estimate inter-bar spacing from the trailing 50 bars.
    tail = bars[-min(50, len(bars)):]
    deltas = []
    for i in range(1, len(tail)):
        a = _parse_ts(bar_ts(tail[i - 1]))
        b = _parse_ts(bar_ts(tail[i]))
        if a and b:
            deltas.append((b - a).total_seconds())
    if not deltas:
        return bar_ts(bars[-1] if shift > 0 else bars[0])
    deltas.sort()
    med = deltas[len(deltas) // 2]
    anchor_ts = _parse_ts(bar_ts(bars[max(0, min(len(bars) - 1, from_idx))]))
    if anchor_ts is None:
        return bar_ts(bars[-1])
    out = anchor_ts + timedelta(seconds=med * shift)
    return out.isoformat()


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    tenkan_p = int(params.get("tenkan", 9))
    kijun_p = int(params.get("kijun", 26))
    senkou_b_p = int(params.get("senkou_b", 52))
    displacement = int(params.get("displacement", 26))
    params.update({
        "tenkan": tenkan_p,
        "kijun": kijun_p,
        "senkou_b": senkou_b_p,
        "displacement": displacement,
    })

    ann = TheoryAnnotation(
        theory="ichimoku",
        params=params,
        citation=(
            "Hosoda, '一目均衡表' (1969); "
            "Patel, 'Trading with Ichimoku Clouds' (Wiley 2010)."
        ),
    )
    if len(bars) < max(tenkan_p, kijun_p, senkou_b_p):
        ann.notes.append("Not enough bars for the Ichimoku window sizes.")
        return ann

    tenkan_pts: List[Dict[str, Any]] = []
    kijun_pts: List[Dict[str, Any]] = []
    # Span A/B carry a ``src_i`` field so we can zip them by source bar
    # index when painting the Kumo (different windows mean the lists
    # start at different ``i`` — pairing by list index would mis-align
    # the cloud).
    span_a_pts: List[Dict[str, Any]] = []
    span_b_pts: List[Dict[str, Any]] = []
    chikou_pts: List[Dict[str, Any]] = []

    for i in range(len(bars)):
        t = _hl_mid(bars, i, tenkan_p)
        k = _hl_mid(bars, i, kijun_p)
        b = _hl_mid(bars, i, senkou_b_p)
        if t is not None:
            tenkan_pts.append({"ts": bar_ts(bars[i]), "price": float(t)})
        if k is not None:
            kijun_pts.append({"ts": bar_ts(bars[i]), "price": float(k)})
        # Senkou A/B are plotted +displacement.
        if t is not None and k is not None:
            a = (t + k) / 2.0
            ts_shift = _shift_ts(bars, i, displacement)
            span_a_pts.append({"ts": ts_shift, "price": float(a), "src_i": i})
        if b is not None:
            ts_shift = _shift_ts(bars, i, displacement)
            span_b_pts.append({"ts": ts_shift, "price": float(b), "src_i": i})
        # Chikou = close shifted -displacement.
        ts_shift = _shift_ts(bars, i, -displacement)
        chikou_pts.append({"ts": ts_shift, "price": float(bar_close(bars[i]))})

    # MITS Phase 10.1 — emit each line as a ``series`` (one Line per
    # curve, 5 total) instead of N-1 trendline segments per curve. Each
    # of Tenkan/Kijun/SpanA/SpanB/Chikou was producing ~250 trendlines
    # on a 1y window — now produces 1 Line each.
    def _series(points, color, label, style="solid", width=2):
        out: List[Line] = []
        clean = [{"ts": p["ts"], "price": p["price"]}
                 for p in points if p.get("ts")]
        if not clean:
            return out
        out.append(Line(
            kind="series",
            start=clean[0],
            end=clean[-1],
            color=color, width=width, style=style,
            label=label,
            points=clean,
        ))
        return out

    ann.lines.extend(_series(tenkan_pts, "#1f6feb", "Tenkan-sen (9)"))
    ann.lines.extend(_series(kijun_pts, "#d63a3a", "Kijun-sen (26)"))
    ann.lines.extend(_series(span_a_pts, "#36c26b", "Senkou Span A", "solid", 1))
    ann.lines.extend(_series(span_b_pts, "#ff5a5f", "Senkou Span B", "solid", 1))
    ann.lines.extend(_series(chikou_pts, "#9aa4b2", "Chikou Span", "dotted", 1))

    # Cloud (Kumo) — pair Span A / Span B BY SOURCE BAR INDEX, not list
    # index (Span A becomes available at i = tenkan_p + kijun_p − 1
    # whereas Span B needs senkou_b_p bars; their list-indices would
    # otherwise reference different historical bars and produce wrong
    # cloud colouring).
    span_b_by_src = {p["src_i"]: p for p in span_b_pts}
    paired = []
    for ap in span_a_pts:
        bp = span_b_by_src.get(ap["src_i"])
        if bp is None:
            continue
        paired.append((ap, bp))
    for i in range(1, len(paired)):
        (a0, b0) = paired[i - 1]
        (a1, b1) = paired[i]
        y_top = max(a1["price"], b1["price"])
        y_bot = min(a1["price"], b1["price"])
        bullish = a1["price"] >= b1["price"]
        ann.zones.append(Zone(
            x1=a0["ts"], y1=float(y_top),
            x2=a1["ts"], y2=float(y_bot),
            color=("#36c26b" if bullish else "#ff5a5f"),
            opacity=0.15,
        ))

    ann.confidence = 0.85
    ann.notes.append(
        "Trade with the trend OUT of the cloud. Cloud below price = uptrend; cloud above = downtrend."
    )

    spot = bar_close(bars[-1])
    # Cloud-relative position uses the latest paired Span A/B (still
    # displaced +26 into the future — we re-pair by source bar index).
    bias = "—"
    if paired:
        a_now, b_now = paired[-1]
        cloud_top = max(a_now["price"], b_now["price"])
        cloud_bot = min(a_now["price"], b_now["price"])
        if spot > cloud_top:
            bias = f"Spot {spot:.2f} sits ABOVE the cloud ({cloud_top:.2f}) — bullish regime."
        elif spot < cloud_bot:
            bias = f"Spot {spot:.2f} sits BELOW the cloud ({cloud_bot:.2f}) — bearish regime."
        else:
            bias = (
                f"Spot {spot:.2f} is INSIDE the cloud "
                f"({cloud_bot:.2f}–{cloud_top:.2f}) — no-trend; stand aside."
            )
    ann.primer = {
        "what_it_measures": (
            "Hosoda's Ichimoku ('one-glance equilibrium') is a five-line "
            "trend-and-momentum framework that's been default on every "
            "Japanese brokerage chart since 1969. Tenkan = 9-bar mid-"
            "range (fast); Kijun = 26-bar mid-range (slow); Senkou A/B "
            "draw a 26-bar-forward 'cloud' (Kumo) defining future "
            "support/resistance; Chikou = current close plotted 26 bars "
            "back as a sanity check on the trend's clarity."
        ),
        "how_to_read": (
            "Above the cloud = uptrend; below = downtrend; inside = "
            "no-trend (stand aside). Green cloud (Senkou A > B) is a "
            "bullish forecast; red cloud is bearish. Tenkan/Kijun cross "
            "= short-term trigger; the cross above/below the cloud is "
            "the high-quality signal. Chikou Span unobstructed by past "
            "price = confirmation the trend is 'clean'."
        ),
        "key_levels_now": bias,
    }
    return ann


__all__ = ["analyze"]
