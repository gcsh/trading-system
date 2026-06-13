"""Market Regime Detection Layer.

Reads a market-data snapshot (the same flat dict strategies consume) and labels
the current environment along several axes — trend, volatility, liquidity,
momentum, dealer-gamma and risk-on/off — plus the strategy families that tend to
work in that environment. Pure and deterministic: no network, no side effects, so
it's cheap to call every cycle and trivially testable.

All thresholds are config-driven (``TUNABLES.regime_*``) — no magic numbers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from backend.config import TUNABLES

# Strategy families that historically suit each environment (names match
# all_strategies.py keys so the adaptive selector / ranker can use them).
_TREND_STRATS = ["macd_momentum", "trend_pullback", "opening_range_breakout"]
_RANGE_STRATS = ["rsi_mean_reversion", "vwap_reversion", "gap_fill", "iron_condor"]
_VOL_STRATS = ["news_catalyst_momentum", "zero_dte_scalp"]
_INCOME_STRATS = ["covered_call_wheel", "cash_secured_put", "iron_condor"]


@dataclass
class MarketRegime:
    trend: str = "unknown"          # bullish | bearish | choppy | unknown
    volatility: str = "normal"      # high | normal | low
    liquidity: str = "unknown"      # thin | normal | deep | unknown
    momentum: str = "neutral"       # expanding | contracting | neutral
    gamma: str = "unknown"          # long_gamma | short_gamma | unknown
    risk: str = "neutral"           # risk_on | risk_off | neutral
    confidence: float = 0.0         # 0-1, how strongly the inputs agree
    preferred_strategies: List[str] = field(default_factory=list)
    label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _num(snapshot: Dict[str, Any], key: str, default: float) -> float:
    v = snapshot.get(key)
    try:
        f = float(v)
        return default if f != f else f   # NaN-safe
    except (TypeError, ValueError):
        return default


def _trend(snapshot: Dict[str, Any]) -> str:
    # Prefer an explicit market/SPY trend label if the snapshot carries one.
    for key in ("market_trend", "spy_trend"):
        val = str(snapshot.get(key) or "").lower()
        if "up" in val or "bull" in val:
            return "bullish"
        if "down" in val or "bear" in val:
            return "bearish"
        if "chop" in val or "side" in val or "range" in val:
            return "choppy"
    # Else derive from price vs moving averages.
    price = _num(snapshot, "price", 0.0)
    ma50 = _num(snapshot, "ma50", 0.0)
    ma200 = _num(snapshot, "ma200", 0.0)
    if price > 0 and ma50 > 0 and ma200 > 0:
        if price > ma50 > ma200:
            return "bullish"
        if price < ma50 < ma200:
            return "bearish"
        return "choppy"
    return "unknown"


def _volatility(snapshot: Dict[str, Any]) -> str:
    vix = _num(snapshot, "vix", TUNABLES.vix_fallback)
    iv_rank = _num(snapshot, "iv_rank", -1.0)
    if vix >= TUNABLES.regime_vix_high or iv_rank >= 70:
        return "high"
    if vix <= TUNABLES.regime_vix_low and (iv_rank < 0 or iv_rank <= 30):
        return "low"
    return "normal"


def _momentum(snapshot: Dict[str, Any]) -> str:
    adx = _num(snapshot, "adx", -1.0)
    if adx < 0:
        return "neutral"
    if adx >= TUNABLES.regime_adx_trend:
        return "expanding"
    if adx <= 15:
        return "contracting"
    return "neutral"


def _liquidity(snapshot: Dict[str, Any]) -> str:
    vol = _num(snapshot, "volume", 0.0)
    avg = _num(snapshot, "avg_volume", 0.0)
    if vol <= 0 or avg <= 0:
        return "unknown"
    ratio = vol / avg
    if ratio < TUNABLES.regime_thin_vol_ratio:
        return "thin"
    if ratio > 1.5:
        return "deep"
    return "normal"


def _preferred(trend: str, volatility: str, momentum: str, gamma: str) -> List[str]:
    picks: List[str] = []
    if trend in ("bullish", "bearish") and momentum != "contracting":
        picks += _TREND_STRATS
    if trend == "choppy" or momentum == "contracting":
        picks += _RANGE_STRATS
    if volatility == "high":
        picks += _VOL_STRATS
    if volatility == "low":
        picks += _INCOME_STRATS
    # Dealer gamma tilts the preference: short gamma amplifies moves (favor
    # trend/breakout); long gamma dampens them (favor mean-reversion/income).
    if gamma == "short_gamma":
        picks = _TREND_STRATS + _VOL_STRATS + picks
    elif gamma == "long_gamma":
        picks = _RANGE_STRATS + _INCOME_STRATS + picks
    # De-dupe, preserve order.
    seen, out = set(), []
    for p in picks:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out or _RANGE_STRATS


def detect_regime(snapshot: Dict[str, Any]) -> MarketRegime:
    """Classify the current market environment from a snapshot. Never raises."""
    snapshot = snapshot or {}
    trend = _trend(snapshot)
    volatility = _volatility(snapshot)
    momentum = _momentum(snapshot)
    liquidity = _liquidity(snapshot)
    gamma = str(snapshot.get("dealer_regime") or "unknown").lower()
    if gamma not in ("long_gamma", "short_gamma"):
        gamma = "unknown"

    if trend == "bullish" and volatility != "high":
        risk = "risk_on"
    elif trend == "bearish" or volatility == "high":
        risk = "risk_off"
    else:
        risk = "neutral"

    # Confidence = share of axes that resolved to a confident (non-unknown/neutral)
    # value — a rough measure of how clear the picture is.
    axes = [trend, volatility, liquidity, momentum, gamma]
    resolved = sum(1 for a in axes if a not in ("unknown", "neutral", "normal"))
    confidence = round(min(1.0, 0.4 + resolved / len(axes) * 0.6), 2)

    label = f"{trend} · {volatility}-vol · {momentum} momentum"
    if gamma != "unknown":
        label += f" · {gamma.replace('_', ' ')}"

    return MarketRegime(
        trend=trend, volatility=volatility, liquidity=liquidity, momentum=momentum,
        gamma=gamma, risk=risk, confidence=confidence,
        preferred_strategies=_preferred(trend, volatility, momentum, gamma), label=label,
    )
