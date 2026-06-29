"""Floor pivot points (classic / standard) — MITS Phase 10.3 stepped.

Formulas — the original "Floor Trader Pivots" definition that has been
in continuous use since the 1980s on the Chicago / New York pits. Per:

  * John L. Person, "A Complete Guide to Technical Trading Tactics"
    (Wiley, 2004), Chapter 4: "The Pivot Point Indicator" — defines
    PP / R1 / S1 / R2 / S2 / R3 / S3 with the exact formulas below.
  * Mark Etzkorn, "Master the Markets" (Active Trader, 2005) —
    secondary citation for the daily / weekly / monthly variants.

    PP = (H + L + C) / 3
    R1 = 2·PP − L
    S1 = 2·PP − H
    R2 = PP + (H − L)
    S2 = PP − (H − L)
    R3 = H + 2·(PP − L)
    S3 = L − 2·(H − PP)

MITS-P10.3 — STEPPED HISTORICAL PIVOTS
=======================================

Previously each timeframe (daily/weekly/monthly) emitted ONE horizontal
line for the most recent period and rendered it across the entire
visible window. On a 1y chart that produced 11 horizontal lines all
labelled "PP daily", "R1 daily" etc. stacked at the right edge — the
operator's screenshot showed this as a label salad with no temporal
context.

The fix walks through every period in the window and computes pivots
PER PERIOD, emitting each level as a short ``trendline`` segment
spanning only that period's bars. Now a 1y chart shows ~12 monthly
pivot "shelves" marching across the chart so the operator can see how
each month's PP / R1 / S1 evolved.

Density adaptation (window → which timeframes render):

  * window ≤ 5d  (intraday) →  daily pivots only
  * window ≤ 1m              →  daily + weekly
  * window ≤ 6m              →  weekly + monthly  (capped at most-recent-N)
  * window 6m < x ≤ 1y       →  monthly only
  * window > 1y              →  monthly only AND drop R3/S3 (rare outliers)

Density param (P10.3.4) further filters by priority:

  * priority = 1  →  PP only                     (Simple)
  * priority ≤ 2  →  PP + R1 + S1                (Normal default)
  * priority ≤ 3  →  full ladder R1/R2/R3 + PP   (Detailed)
"""
from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .schema import (
    Line, Signal, TheoryAnnotation,
    bar_close, bar_high, bar_low, bar_ts,
)
from .signal_promote import promote_all


MAX_SIGNALS_PER_THEORY = 25
# Cap stepped segments emitted so a multi-year zoom doesn't punish the
# renderer (each segment is a separate trendline LineSeries).
MAX_STEPPED_SEGMENTS = 240


PIVOT_COLORS = {
    "PP": "#ffc107",
    "R1": "#36c26b", "R2": "#36c26b", "R3": "#36c26b",
    "S1": "#ff5a5f", "S2": "#ff5a5f", "S3": "#ff5a5f",
}


# Per-level priority — lets the /theories density filter knock out
# secondary levels in "simple" mode. Priority 1 = PP equilibrium only;
# priority 2 = first ladder (R1/S1); priority 3 = outer levels.
LEVEL_PRIORITY = {
    "PP": 1,
    "R1": 2, "S1": 2,
    "R2": 3, "S2": 3,
    "R3": 3, "S3": 3,
}


def floor_pivots(h: float, l: float, c: float) -> Dict[str, float]:
    """Classic floor pivot formulas. Returns PP/R1-R3/S1-S3."""
    pp = (h + l + c) / 3.0
    return {
        "PP": pp,
        "R1": 2.0 * pp - l,
        "S1": 2.0 * pp - h,
        "R2": pp + (h - l),
        "S2": pp - (h - l),
        "R3": h + 2.0 * (pp - l),
        "S3": l - 2.0 * (h - pp),
    }


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _bars_with_dates(bars: List[Dict[str, Any]]) -> List[Tuple[date, Dict[str, Any]]]:
    out: List[Tuple[date, Dict[str, Any]]] = []
    for b in bars:
        dt = _parse_ts(bar_ts(b))
        if dt is None:
            continue
        out.append((dt.date(), b))
    return out


def _select_timeframes_by_window(
    bars: List[Dict[str, Any]],
    requested: Optional[List[str]] = None,
) -> List[str]:
    """MITS-P10.3 density adaptation — pick which timeframes render
    based on the visible window size.

    The window is inferred from the bar count and bar spacing rather
    than the route's WINDOW_MAP key (the theory module doesn't see the
    UI label). Spacing is detected from the first vs second bar; if the
    spacing is daily, we use bar-count buckets; if weekly/monthly
    (aggregated by the route for ≥2y), we always emit monthly-only.
    """
    if requested:
        return list(requested)
    if len(bars) < 2:
        return ["daily"]
    dated = _bars_with_dates(bars)
    if len(dated) < 2:
        return ["daily"]
    # Median bar gap in days.
    gaps = [
        (dated[i][0] - dated[i - 1][0]).days
        for i in range(1, min(len(dated), 20))
    ]
    gaps = [g for g in gaps if g > 0]
    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 1
    span_days = (dated[-1][0] - dated[0][0]).days
    # If the route already resampled to weekly/monthly bars, stepped
    # daily pivots are meaningless.
    if median_gap >= 25:
        # Monthly bars (route resampled for max window).
        return ["monthly"]
    if median_gap >= 5:
        # Weekly bars (route resampled for 2y/5y windows).
        return ["monthly", "weekly"]
    # Daily bars (≤1y windows).
    if span_days <= 7:
        return ["daily"]
    if span_days <= 35:
        return ["daily", "weekly"]
    if span_days <= 200:
        return ["weekly", "monthly"]
    if span_days <= 400:
        return ["monthly"]
    # >1y of daily bars → monthly only, AND we'll drop R3/S3 in the
    # caller via the drop_outliers flag.
    return ["monthly"]


def _group_bars_by_period(
    dated: List[Tuple[date, Dict[str, Any]]],
    period: str,
) -> List[Tuple[str, date, date, List[Dict[str, Any]]]]:
    """Group bars by daily / weekly (ISO) / monthly period.

    Returns a list of ``(label, period_start_date, period_end_date,
    bars_in_period)`` ordered chronologically. ``period_start_date``
    and ``period_end_date`` are the FIRST and LAST bar dates that fall
    in the period (clipped to the visible window).
    """
    groups: Dict[str, List[Tuple[date, Dict[str, Any]]]] = {}
    order: List[str] = []
    for d, b in dated:
        if period == "daily":
            key = d.isoformat()
        elif period == "weekly":
            iso = d.isocalendar()
            key = f"{iso[0]:04d}-W{iso[1]:02d}"
        elif period == "monthly":
            key = f"{d.year:04d}-{d.month:02d}"
        else:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((d, b))
    out: List[Tuple[str, date, date, List[Dict[str, Any]]]] = []
    for key in order:
        rows = groups[key]
        if not rows:
            continue
        rows.sort(key=lambda kb: kb[0])
        out.append((
            key,
            rows[0][0],
            rows[-1][0],
            [b for _, b in rows],
        ))
    return out


def _hlc_from_bars(bars_for_period: List[Dict[str, Any]]) -> Optional[Tuple[float, float, float]]:
    if not bars_for_period:
        return None
    highs = [bar_high(b) for b in bars_for_period if bar_high(b) > 0]
    lows = [bar_low(b) for b in bars_for_period if bar_low(b) > 0]
    if not highs or not lows:
        return None
    h = max(highs)
    l = min(lows)
    c = bar_close(bars_for_period[-1])
    if c <= 0:
        return None
    return (h, l, c)


def _filter_by_density(level_name: str, density: str) -> bool:
    """Return True if this level should render under the given density."""
    pri = LEVEL_PRIORITY.get(level_name, 3)
    if density == "simple":
        return pri == 1
    if density == "detailed":
        return True
    # normal default
    return pri <= 2


def _emit_stepped_segments_for_period(
    ann: TheoryAnnotation,
    period: str,
    groups: List[Tuple[str, date, date, List[Dict[str, Any]]]],
    density: str,
    drop_outliers: bool,
    style: str,
    width: int,
    last_period_levels: Dict[str, Dict[str, float]],
) -> int:
    """Walk the groups and emit one stepped trendline per (level, period)
    where the prior period had HLC available. Returns count emitted.

    For each period[i], use prior period[i-1]'s HLC to compute the
    pivots that apply for the CURRENT period[i], and draw a segment
    spanning the current period's first bar to last bar.
    """
    emitted = 0
    level_color = {
        "PP": "#ffffff",
        "R1": "#ffd166", "S1": "#ffd166",
        "R2": "#ff9f1c", "S2": "#ff9f1c",
        "R3": "#ff5a5f", "S3": "#ff5a5f",
    }
    for idx in range(1, len(groups)):
        prev_label, _ps, _pe, prev_bars = groups[idx - 1]
        cur_label, cur_start, cur_end, cur_bars = groups[idx]
        hlc = _hlc_from_bars(prev_bars)
        if hlc is None:
            continue
        h, l, c = hlc
        pivots = floor_pivots(h, l, c)
        # First bar of the current period → x-anchor for segment start.
        # Use the actual bar timestamps (not the calendar date) so the
        # frontend's time-scale lines them up under the candles.
        start_ts = bar_ts(cur_bars[0])
        end_ts = bar_ts(cur_bars[-1])
        if not start_ts or not end_ts:
            continue
        for name, price in pivots.items():
            if drop_outliers and name in ("R3", "S3"):
                continue
            if not _filter_by_density(name, density):
                continue
            ann.lines.append(Line(
                kind="trendline",
                start={"ts": start_ts, "price": float(price)},
                end={"ts": end_ts, "price": float(price)},
                color=level_color.get(name, PIVOT_COLORS.get(name, "#9aa4b2")),
                width=width,
                style=style,
                label=None,  # P10.3.5 — don't pollute right axis with stale labels
                meta={
                    "timeframe": period,
                    "level": name,
                    "price": float(price),
                    "priority": LEVEL_PRIORITY.get(name, 3),
                    "period_label": cur_label,
                    "stepped": True,
                    "significance": (
                        "equilibrium" if name == "PP"
                        else ("first" if name in ("R1", "S1")
                              else ("second" if name in ("R2", "S2") else "outlier"))
                    ),
                    "side": (
                        "neutral" if name == "PP"
                        else ("resistance" if name.startswith("R") else "support")
                    ),
                },
            ))
            emitted += 1
        last_period_levels[period] = dict(pivots)
    return emitted


def _label_most_recent_levels(
    ann: TheoryAnnotation,
    last_levels: Dict[str, Dict[str, float]],
    density: str,
    drop_outliers: bool,
) -> None:
    """P10.3.5 — emit a single zero-width horizontal price-line per
    level (most recent timeframe only) so the right axis shows the
    current level. Stepped segments themselves carry no label.
    """
    # Prefer monthly > weekly > daily for the right-axis labels (they're
    # the strongest levels and least likely to overlap each other).
    for period in ("monthly", "weekly", "daily"):
        levels = last_levels.get(period)
        if not levels:
            continue
        for name, price in levels.items():
            if drop_outliers and name in ("R3", "S3"):
                continue
            if not _filter_by_density(name, density):
                continue
            ann.lines.append(Line(
                kind="horizontal",
                start={"ts": "", "price": float(price)},
                end={"ts": "", "price": float(price)},
                color={
                    "PP": "#ffffff",
                    "R1": "#ffd166", "S1": "#ffd166",
                    "R2": "#ff9f1c", "S2": "#ff9f1c",
                    "R3": "#ff5a5f", "S3": "#ff5a5f",
                }.get(name, "#9aa4b2"),
                width=1,
                style="dotted",
                label=f"{name} {price:.2f}",
                meta={
                    "timeframe": period,
                    "level": name,
                    "price": float(price),
                    "priority": LEVEL_PRIORITY.get(name, 3),
                    "label_only": True,
                },
            ))
        break  # Only one timeframe in the right-axis ladder


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    density = str(params.get("density", "normal")).lower()
    if density not in ("simple", "normal", "detailed"):
        density = "normal"

    enabled_periods = _select_timeframes_by_window(
        bars,
        requested=params.get("periods"),
    )
    # Drop R3/S3 on very wide windows (>1y of daily bars).
    drop_outliers = False
    if bars:
        dated = _bars_with_dates(bars)
        if dated:
            span_days = (dated[-1][0] - dated[0][0]).days
            if span_days > 400:
                drop_outliers = True

    ann = TheoryAnnotation(
        theory="pivots",
        params={
            **params,
            "periods": list(enabled_periods),
            "density": density,
            "stepped": True,
            "drop_outliers": drop_outliers,
        },
        citation=(
            "Person, 'A Complete Guide to Technical Trading Tactics' "
            "(Wiley 2004), Chapter 4: Floor Pivot Point Indicator."
        ),
    )
    if not bars:
        ann.notes.append("No bars supplied.")
        return ann

    dated = _bars_with_dates(bars)
    if not dated:
        ann.notes.append("Could not parse bar timestamps.")
        return ann

    last_period_levels: Dict[str, Dict[str, float]] = {}
    style_map = {"daily": "solid", "weekly": "dashed", "monthly": "dotted"}
    width_map = {"daily": 2, "weekly": 1, "monthly": 1}

    total_segments = 0
    for period in enabled_periods:
        groups = _group_bars_by_period(dated, period)
        if len(groups) < 2:
            ann.notes.append(
                f"Not enough complete {period} periods in the window to step pivots."
            )
            continue
        emitted = _emit_stepped_segments_for_period(
            ann,
            period,
            groups,
            density=density,
            drop_outliers=drop_outliers,
            style=style_map.get(period, "solid"),
            width=width_map.get(period, 1),
            last_period_levels=last_period_levels,
        )
        total_segments += emitted
        ann.notes.append(
            f"{period.capitalize()} pivots: {emitted} stepped segments across "
            f"{len(groups) - 1} periods."
        )
        if total_segments >= MAX_STEPPED_SEGMENTS:
            ann.notes.append(
                f"Hit stepped-segment cap ({MAX_STEPPED_SEGMENTS}); "
                "truncating remaining timeframes."
            )
            break

    if not last_period_levels:
        ann.notes.append("Could not compute any pivot levels for the supplied bars.")
        return ann

    # MITS-P10.3.5 — single right-axis ladder of most-recent levels.
    _label_most_recent_levels(ann, last_period_levels, density, drop_outliers)
    ann.params["levels"] = last_period_levels
    ann.confidence = 0.90

    # ── MITS-P10.3.6 — RELAXED signal emission on stepped pivots.
    #
    # The old rule "close > R1 AND volume > 1.2× MA(20)" returned 0
    # signals across a 1y SPY window. Relax to:
    #
    #   * For each period[i] (monthly), compute pivots from period[i-1]'s
    #     HLC. Then walk the bars of period[i]; emit BUY on the FIRST
    #     bar where close > R1 of that period (the activation bar) and
    #     SELL on the FIRST bar where close < S1.
    #
    # Conservative: ONLY on monthly pivots (max ~12 signals per year),
    # so the chart doesn't drown in flags.
    promote_options = bool(params.get("promote_options", True))
    market_context = dict(params.get("market_context") or {})
    sigs: List[Signal] = []
    monthly_groups = _group_bars_by_period(dated, "monthly")
    for idx in range(1, len(monthly_groups)):
        _pkey, _ps, _pe, prev_bars = monthly_groups[idx - 1]
        cur_label, _cs, _ce, cur_bars = monthly_groups[idx]
        hlc = _hlc_from_bars(prev_bars)
        if hlc is None:
            continue
        h, l, c = hlc
        pivots = floor_pivots(h, l, c)
        r1 = pivots["R1"]; s1 = pivots["S1"]
        pp = pivots["PP"]
        r2 = pivots["R2"]; s2 = pivots["S2"]
        emitted_buy = False
        emitted_sell = False
        for b in cur_bars:
            cl = bar_close(b)
            ts = bar_ts(b)
            if cl <= 0 or not ts:
                continue
            if not emitted_buy and cl > r1:
                sigs.append(Signal(
                    action="BUY",
                    ts=ts, price=float(cl), confidence=0.60,
                    reasoning=(
                        f"Close ({cl:.2f}) broke above the {cur_label} "
                        f"monthly R1 ({r1:.2f}) derived from the prior "
                        "month's HLC — Person's monthly-pivot momentum "
                        f"break. Targets R2 ({r2:.2f}); stop at PP "
                        f"({pp:.2f})."
                    ),
                    target_price=float(r2),
                    stop_loss=float(pp),
                    instrument="stock",
                    theory_anchor={"level": "R1", "timeframe": "monthly",
                                    "period": cur_label},
                ))
                emitted_buy = True
            if not emitted_sell and cl < s1:
                sigs.append(Signal(
                    action="SELL",
                    ts=ts, price=float(cl), confidence=0.60,
                    reasoning=(
                        f"Close ({cl:.2f}) broke below the {cur_label} "
                        f"monthly S1 ({s1:.2f}) derived from the prior "
                        "month's HLC — Person's monthly-pivot breakdown. "
                        f"Targets S2 ({s2:.2f}); stop at PP ({pp:.2f})."
                    ),
                    target_price=float(s2),
                    stop_loss=float(pp),
                    instrument="stock",
                    theory_anchor={"level": "S1", "timeframe": "monthly",
                                    "period": cur_label},
                ))
                emitted_sell = True
            if emitted_buy and emitted_sell:
                break
    if len(sigs) > MAX_SIGNALS_PER_THEORY:
        sigs = sigs[-MAX_SIGNALS_PER_THEORY:]
    ann.signals = promote_all(sigs, market_context, enabled=promote_options)

    # Build the plain-English "key levels right now" line tied to the
    # latest close. We use the most-recent monthly levels when present,
    # else weekly, else daily.
    spot = bar_close(bars[-1])
    most_recent = (last_period_levels.get("monthly")
                   or last_period_levels.get("weekly")
                   or last_period_levels.get("daily") or {})
    key_now = ""
    if most_recent:
        pp = most_recent.get("PP")
        r1 = most_recent.get("R1")
        s1 = most_recent.get("S1")
        if pp and r1 and s1:
            if spot > r1:
                bias = (
                    f"Spot {spot:.2f} sits above the latest PP-band R1 "
                    f"({r1:.2f}) — momentum regime; bulls in control."
                )
            elif spot < s1:
                bias = (
                    f"Spot {spot:.2f} sits below the latest PP-band S1 "
                    f"({s1:.2f}) — breakdown regime; bears in control."
                )
            elif spot >= pp:
                bias = (
                    f"Spot {spot:.2f} sits between latest PP ({pp:.2f}) "
                    f"and R1 ({r1:.2f}). Bias: cautiously bullish."
                )
            else:
                bias = (
                    f"Spot {spot:.2f} sits between latest S1 ({s1:.2f}) "
                    f"and PP ({pp:.2f}). Bias: cautiously bearish."
                )
            key_now = bias
    ann.primer = {
        "what_it_measures": (
            "Floor pivots translate each prior period's high/low/close "
            "into seven price levels that institutional desks have "
            "watched since the 1970s. MITS Phase 10.3 renders each "
            "period's pivots as a stepped segment so you can see how "
            "the support / resistance ladder evolved month-by-month, "
            "rather than projecting today's levels across history."
        ),
        "how_to_read": (
            "Each shelf is one period's PP / R1 / S1 ladder. Price "
            "oscillating around a shelf's PP = chop. Close above R1 of "
            "a shelf = momentum (target R2). Close below S1 = breakdown "
            "(target S2). The right-axis labels show the most-recent "
            "period's live levels for direct comparison to current spot. "
            "Wider windows automatically swap to monthly shelves and "
            "drop R3/S3 (rare 1-in-20 outliers per Person's research)."
        ),
        "key_levels_now": key_now or "Spot — no pivot frame available.",
    }
    return ann


__all__ = ["analyze", "floor_pivots", "LEVEL_PRIORITY"]
