"""Volume Profile (Value Area / POC) — MITS Phase 10 theory module.

Citation:

  * J. Peter Steidlmayer & Steven Hawkins, "Steidlmayer on Markets:
    Trading with Market Profile" (Wiley, 1989, 2nd ed. 2003). Original
    publication of the Market Profile / Volume Profile framework.
  * James Dalton, "Mind Over Markets" (Probus, 1990) — popularised
    Value Area, Point of Control (POC), and HVN / LVN terminology used
    by every futures pit and modern broker UI.

    POC          = price level with the most traded volume in window.
    Value Area   = central 70% of volume around POC (Steidlmayer's
                   "fair value range"; one standard deviation in a
                   normal-distribution-like profile).
    VAH / VAL    = upper / lower edges of the value area.
    HVN / LVN    = High / Low Volume Nodes — local maxima / minima in
                   the volume-at-price histogram.

Signals:

  * BUY  when price tests VAL or a low-side HVN and shows bullish
    rejection (close > prior bar's close).
  * SELL when price tests VAH or a high-side HVN with bearish rejection.
  * WATCH when price sits AT the POC — Dalton's "rotation" regime.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from .schema import (
    Line, Signal, TheoryAnnotation, Zone,
    bar_close, bar_high, bar_low, bar_ts,
)


def _build_profile(
    bars: List[Dict[str, Any]], bins: int,
) -> Dict[str, Any]:
    """Bin each bar's volume linearly across its high-low range.

    Returns ``{prices: [...], volumes: [...], step: float}``.
    """
    hi = max(bar_high(b) for b in bars)
    lo = min(bar_low(b) for b in bars if bar_low(b) > 0)
    if hi <= lo or bins <= 0:
        return {"prices": [], "volumes": [], "step": 0.0}
    step = (hi - lo) / bins
    if step <= 0:
        return {"prices": [], "volumes": [], "step": 0.0}
    buckets = defaultdict(float)
    for b in bars:
        h = bar_high(b); l = bar_low(b)
        v = float(b.get("volume") or 0.0)
        if h <= l or v <= 0: continue
        # Spread volume linearly across the bar's price range.
        span = h - l
        n = max(1, int(span / step))
        per = v / n
        for i in range(n):
            p = l + step * i + step / 2.0
            idx = int((p - lo) / step)
            buckets[idx] += per
    prices = [lo + step * (i + 0.5) for i in range(bins)]
    volumes = [buckets.get(i, 0.0) for i in range(bins)]
    return {"prices": prices, "volumes": volumes, "step": step,
             "lo": lo, "hi": hi}


def _value_area(profile: Dict[str, Any], pct: float = 0.70) -> Dict[str, Any]:
    """Expand from the POC outward until ``pct`` of volume is enclosed."""
    prices = profile["prices"]
    volumes = profile["volumes"]
    if not prices or not volumes:
        return {}
    total_vol = sum(volumes)
    if total_vol <= 0:
        return {}
    poc_idx = max(range(len(volumes)), key=lambda i: volumes[i])
    target = total_vol * pct
    accum = volumes[poc_idx]
    lo_i = poc_idx; hi_i = poc_idx
    while accum < target and (lo_i > 0 or hi_i < len(volumes) - 1):
        up_vol = volumes[hi_i + 1] if hi_i + 1 < len(volumes) else 0
        dn_vol = volumes[lo_i - 1] if lo_i > 0 else 0
        if up_vol >= dn_vol and hi_i + 1 < len(volumes):
            hi_i += 1; accum += up_vol
        elif lo_i > 0:
            lo_i -= 1; accum += dn_vol
        else:
            break
    return {
        "poc_idx": poc_idx,
        "poc_price": prices[poc_idx],
        "vah": prices[hi_i],
        "val": prices[lo_i],
        "enclosed_pct": accum / total_vol if total_vol > 0 else 0.0,
    }


def _find_nodes(volumes: List[float], prices: List[float],
                  k: int = 3) -> Dict[str, List[Dict[str, Any]]]:
    """Local maxima (HVN) and minima (LVN) in the volume histogram."""
    hvn = []; lvn = []
    for i in range(1, len(volumes) - 1):
        if volumes[i] > volumes[i - 1] and volumes[i] > volumes[i + 1]:
            hvn.append({"price": prices[i], "volume": volumes[i]})
        elif volumes[i] < volumes[i - 1] and volumes[i] < volumes[i + 1]:
            lvn.append({"price": prices[i], "volume": volumes[i]})
    hvn.sort(key=lambda x: -x["volume"])
    lvn.sort(key=lambda x: x["volume"])
    return {"hvn": hvn[:k], "lvn": lvn[:k]}


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    bins = int(params.get("bins", 50))
    va_pct = float(params.get("value_area_pct", 0.70))
    lookback = int(params.get("lookback", 100))

    ann = TheoryAnnotation(
        theory="volume_profile",
        params={"bins": bins, "value_area_pct": va_pct, "lookback": lookback},
        citation=(
            "Steidlmayer & Hawkins, 'Steidlmayer on Markets' (Wiley 1989, "
            "2nd ed. 2003); Dalton, 'Mind Over Markets' (Probus 1990)."
        ),
    )
    if len(bars) < 20:
        ann.notes.append("Not enough bars for a volume profile.")
        return ann

    win = bars[-lookback:] if len(bars) > lookback else bars[:]
    profile = _build_profile(win, bins)
    if not profile["prices"]:
        ann.notes.append("Could not build a volume profile.")
        return ann
    va = _value_area(profile, pct=va_pct)
    if not va:
        ann.notes.append("Could not compute the value area.")
        return ann
    nodes = _find_nodes(profile["volumes"], profile["prices"], k=3)

    first_ts = bar_ts(win[0])
    last_ts = bar_ts(win[-1])

    # POC.
    ann.lines.append(Line(
        kind="horizontal",
        start={"ts": first_ts, "price": float(va["poc_price"])},
        end={"ts": last_ts, "price": float(va["poc_price"])},
        color="#ffd166", width=2, style="solid",
        label=f"POC {va['poc_price']:.2f}",
        meta={"kind": "poc", "price": va["poc_price"]},
    ))
    # VAH / VAL.
    ann.lines.append(Line(
        kind="horizontal",
        start={"ts": first_ts, "price": float(va["vah"])},
        end={"ts": last_ts, "price": float(va["vah"])},
        color="#36c26b", width=1, style="dashed",
        label=f"VAH {va['vah']:.2f}",
        meta={"kind": "vah", "price": va["vah"]},
    ))
    ann.lines.append(Line(
        kind="horizontal",
        start={"ts": first_ts, "price": float(va["val"])},
        end={"ts": last_ts, "price": float(va["val"])},
        color="#ff5a5f", width=1, style="dashed",
        label=f"VAL {va['val']:.2f}",
        meta={"kind": "val", "price": va["val"]},
    ))
    # Value area zone.
    ann.zones.append(Zone(
        x1=first_ts, y1=float(va["val"]),
        x2=last_ts,  y2=float(va["vah"]),
        color="#36c26b", opacity=0.10,
        label="Value Area 70%",
    ))
    # HVN / LVN.
    for node in nodes["hvn"]:
        ann.lines.append(Line(
            kind="horizontal",
            start={"ts": first_ts, "price": float(node["price"])},
            end={"ts": last_ts, "price": float(node["price"])},
            color="#9aa4b2", width=1, style="dotted",
            label=f"HVN {node['price']:.2f}",
            meta={"kind": "hvn", "price": node["price"]},
        ))
    for node in nodes["lvn"]:
        ann.lines.append(Line(
            kind="horizontal",
            start={"ts": first_ts, "price": float(node["price"])},
            end={"ts": last_ts, "price": float(node["price"])},
            color="#5b6985", width=1, style="dotted",
            label=f"LVN {node['price']:.2f}",
            meta={"kind": "lvn", "price": node["price"]},
        ))

    # MITS-P10.2 — walk every bar; emit on each VAL/VAH test.
    from .signal_promote import promote_all
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})
    sigs: List[Signal] = []
    step = profile["step"]
    last_close = bar_close(bars[-1])
    tol = max(step * 1.5, 0.005 * last_close)
    for i in range(1, len(bars)):
        cl = bar_close(bars[i])
        prev = bar_close(bars[i - 1])
        ts = bar_ts(bars[i])
        if abs(cl - va["val"]) <= tol and cl >= prev and prev < va["val"]:
            sigs.append(Signal(
                action="BUY",
                ts=ts, price=float(cl), confidence=0.65,
                reasoning=(
                    f"Spot ({cl:.2f}) tested the Value Area Low "
                    f"({va['val']:.2f}) and closed higher — Dalton's "
                    "value-buyer rejection."
                ),
                target_price=float(va["poc_price"]),
                stop_loss=float(va["val"] - step * 2),
                instrument="stock",
                theory_anchor={"level": "VAL", "i": i},
            ))
        elif abs(cl - va["vah"]) <= tol and cl <= prev and prev > va["vah"]:
            sigs.append(Signal(
                action="SELL",
                ts=ts, price=float(cl), confidence=0.65,
                reasoning=(
                    f"Spot ({cl:.2f}) tested the Value Area High "
                    f"({va['vah']:.2f}) and closed lower — Dalton's "
                    "value-seller rejection."
                ),
                target_price=float(va["poc_price"]),
                stop_loss=float(va["vah"] + step * 2),
                instrument="stock",
                theory_anchor={"level": "VAH", "i": i},
            ))

    if len(sigs) > 25:
        sigs = sigs[-25:]
    ann.signals = promote_all(sigs, market_context, enabled=promote_options)
    ann.confidence = 0.80
    ann.primer = {
        "what_it_measures": (
            "Volume Profile bins traded volume by PRICE (not time). The "
            "Point of Control is the single most-traded price in the "
            "window; the Value Area is the central 70% of volume around "
            "POC. Steidlmayer's framework treats POC as 'fair value' and "
            "the Value Area as the band auction participants accept."
        ),
        "how_to_read": (
            "Inside Value = balance; fade extremes back to POC. Outside "
            "Value = imbalance; trend day candidate. Value Area High and "
            "Low are high-quality acceptance/rejection levels. HVN = "
            "magnet, LVN = price moves through fast. Dalton's framework "
            "is the foundation of every modern auction-theory trader."
        ),
        "key_levels_now": (
            f"POC {va['poc_price']:.2f}  ·  VAH {va['vah']:.2f}  ·  "
            f"VAL {va['val']:.2f}  ·  enclosed "
            f"{va['enclosed_pct']*100:.0f}%"
        ),
    }
    return ann


__all__ = ["analyze"]
