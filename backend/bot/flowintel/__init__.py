"""Institutional Flow Intelligence Layer.

Reads the GEX result + the live options-flow alerts and surfaces the things a
dealer-aware trader actually wants to know:

  * how strongly is dealer hedging biased right now (long vs short gamma)
  * how close are we to the call / put wall (the *pinning* zone)
  * a pinning-probability estimate combining proximity + gamma sign
  * is institutional flow leaning bullish or bearish, how aggressive, repeated?
  * is a >$1M dark-pool print backing it?

Pure functions — they accept the data they need (a GEXResult dict and a list of
flow alert dicts) so they're trivially testable and loop-safe. ``analyze(ticker)``
is the convenience wrapper that pulls both, used by the API endpoint.

A real vanna/charm computation needs per-strike greeks across an IV surface,
which we don't have today. Marked as a follow-up; everything below is reliable
on the data we already collect.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DealerPositioning:
    regime: str = "unknown"          # long_gamma | short_gamma | unknown
    net_gex: float = 0.0
    flip_distance_pct: Optional[float] = None     # +above flip / -below
    call_wall_distance_pct: Optional[float] = None
    put_wall_distance_pct: Optional[float] = None
    pinning_probability: float = 0.0   # 0-1, higher = price likely to pin
    hedging_pressure: str = "normal"   # high | normal | low
    dominant_wall: str = "neutral"     # call | put | neutral
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FlowProfile:
    bullish_sweeps: int = 0
    bearish_sweeps: int = 0
    premarket_bullish_sweeps: int = 0
    total_premium: float = 0.0
    avg_urgency: float = 0.0
    sweep_aggressiveness: float = 0.0      # 0-1, urgency × normalised premium
    darkpool_confirms: bool = False
    repeat_orders: int = 0                 # same strike/expiry hit ≥ 2 times
    direction: str = "neutral"             # bullish | bearish | mixed | neutral

    def to_dict(self) -> dict:
        return asdict(self)


def _pct_distance(price: float, level: Optional[float]) -> Optional[float]:
    if not price or level in (None, 0):
        return None
    return round((price - float(level)) / price * 100, 2)


def dealer_positioning(gex: Dict[str, Any]) -> DealerPositioning:
    """Distill a GEXResult-shaped dict into dealer-positioning intelligence."""
    if not gex or not gex.get("ok"):
        return DealerPositioning(regime=(gex or {}).get("dealer_regime") or "unknown",
                                  notes=["GEX unavailable"])
    spot = float(gex.get("spot_price") or 0)
    regime = gex.get("dealer_regime") or "unknown"
    flip_dist = _pct_distance(spot, gex.get("gamma_flip"))
    call_dist = _pct_distance(spot, gex.get("call_wall"))
    put_dist = _pct_distance(spot, gex.get("put_wall"))

    # Dominant wall: whichever the price is closer to (in % terms).
    dominant = "neutral"
    if call_dist is not None and put_dist is not None:
        if abs(call_dist) < abs(put_dist):
            dominant = "call"
        elif abs(put_dist) < abs(call_dist):
            dominant = "put"

    # Pinning probability: high when we're within ±1% of a wall AND dealers are
    # long gamma (long gamma = mean-reverting / pinning behaviour).
    nearest = min((abs(d) for d in (call_dist, put_dist) if d is not None), default=None)
    pin = 0.0
    if nearest is not None:
        proximity = max(0.0, 1.0 - min(nearest, 5.0) / 5.0)   # 1.0 right at wall, 0 at ±5%
        if regime == "long_gamma":
            pin = round(min(1.0, proximity * 0.9), 2)
        elif regime == "short_gamma":
            pin = round(max(0.0, proximity * 0.25), 2)   # short gamma rejects pins
        else:
            pin = round(proximity * 0.5, 2)

    # Hedging pressure: large negative net GEX in short-gamma is most explosive;
    # very large positive in long-gamma means strong dampening — both count as
    # "high" pressure from the perspective of dealer activity.
    net = float(gex.get("net_gex_total") or 0)
    abs_net = abs(net)
    if abs_net > 5e9:
        pressure = "high"
    elif abs_net > 1e9:
        pressure = "normal"
    else:
        pressure = "low"

    notes: List[str] = []
    if regime == "short_gamma":
        notes.append("short-gamma regime — dealers AMPLIFY moves (trend / volatility)")
    elif regime == "long_gamma":
        notes.append("long-gamma regime — dealers DAMPEN moves (mean-reversion / pinning)")
    if nearest is not None and nearest <= 1.0:
        notes.append(f"price within ±1% of the {dominant} wall — high pin risk")
    if gex.get("opex_day"):
        notes.append("OPEX day — dealer re-hedging especially active into expiry")

    return DealerPositioning(
        regime=regime, net_gex=net, flip_distance_pct=flip_dist,
        call_wall_distance_pct=call_dist, put_wall_distance_pct=put_dist,
        pinning_probability=pin, hedging_pressure=pressure,
        dominant_wall=dominant, notes=notes,
    )


def flow_profile(alerts: List[Dict[str, Any]]) -> FlowProfile:
    """Profile a list of FlowAlert-shaped dicts: aggression, dedup, direction."""
    if not alerts:
        return FlowProfile()
    bullish = [a for a in alerts if a.get("sentiment") == "bullish" and a.get("trade_type") == "sweep"]
    bearish = [a for a in alerts if a.get("sentiment") == "bearish" and a.get("trade_type") == "sweep"]
    pre_bull = [a for a in bullish if a.get("session") == "pre_market"]
    premium = sum(float(a.get("premium") or 0) for a in alerts)
    avg_urg = sum(float(a.get("urgency_score") or 0) for a in alerts) / len(alerts)
    # Aggressiveness: average urgency, plus a small bump if premium is heavy.
    premium_norm = min(1.0, premium / 5_000_000.0)
    aggressiveness = round(min(1.0, 0.65 * avg_urg + 0.35 * premium_norm), 3)

    seen: Dict[tuple, int] = {}
    for a in alerts:
        key = ((a.get("ticker") or "").upper(), a.get("strike"),
               a.get("expiry"), a.get("option_type"))
        seen[key] = seen.get(key, 0) + 1
    repeats = sum(1 for c in seen.values() if c >= 2)

    nb, nbear = len(bullish), len(bearish)
    if nb > nbear * 1.5:
        direction = "bullish"
    elif nbear > nb * 1.5:
        direction = "bearish"
    elif nb > 0 or nbear > 0:
        direction = "mixed"
    else:
        direction = "neutral"

    return FlowProfile(
        bullish_sweeps=nb, bearish_sweeps=nbear, premarket_bullish_sweeps=len(pre_bull),
        total_premium=round(premium, 2), avg_urgency=round(avg_urg, 3),
        sweep_aggressiveness=aggressiveness,
        darkpool_confirms=any(a.get("trade_type") == "darkpool" for a in alerts),
        repeat_orders=repeats, direction=direction,
    )


def analyze(ticker: str) -> Dict[str, Any]:
    """Convenience: fetch the live GEX + flow snapshots and build both views."""
    ticker = ticker.upper()
    out: Dict[str, Any] = {"ticker": ticker}
    try:
        from backend.bot.signals.gex import gex as gex_fn

        gex_data = gex_fn(ticker).to_dict()
    except Exception:
        gex_data = {}
    try:
        from backend.bot.signals.flow import flow_for

        alerts = [a.to_dict() for a in flow_for(ticker)]
    except Exception:
        alerts = []
    out["dealer_positioning"] = dealer_positioning(gex_data).to_dict()
    out["flow_profile"] = flow_profile(alerts).to_dict()
    return out
