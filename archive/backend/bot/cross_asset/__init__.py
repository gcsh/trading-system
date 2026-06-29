"""Stage-4 cross-asset intelligence — markets are interconnected.

Pulls the small set of cross-asset benchmarks that condition every other
signal, computes a regime per axis, and produces a unified ``CrossAssetState``
that strategies + meta-AI consume:

  • Equities:    SPY trend, QQQ trend, IWM trend, breadth proxy
  • Volatility:  VIX level + regime + 5d change
  • Yields:      10Y (^TNX) level + slope vs 2Y
  • Dollar:      DXY level + 5d change
  • Commodities: gold, oil — risk-on/off + inflation pulse
  • Crypto:      BTC trend (risk-asset proxy)
  • Sectors:     XLK / XLF / XLE / XLU rotation

The output drives:
  • False-breakout filtering — equity rally with rising VIX is suspect
  • Hedge suggestions — long-vol when state flips risk-off
  • Regime overlays in the analytics layer
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── data model ─────────────────────────────────────────────────────────────


@dataclass
class AssetState:
    ticker: str
    last: float = 0.0
    change_pct_1d: float = 0.0
    change_pct_5d: float = 0.0
    trend: str = "unknown"               # bullish | bearish | choppy | unknown
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CrossAssetState:
    equities: str = "unknown"            # risk_on | risk_off | mixed
    yields: str = "unknown"              # rising | falling | stable
    dollar: str = "unknown"              # rising | falling | stable
    volatility: str = "unknown"          # compressed | elevated | spiking
    commodities: str = "unknown"         # inflationary | disinflationary | mixed
    crypto: str = "unknown"
    breadth: str = "unknown"             # broad | narrow | unknown
    regime_label: str = "unknown"        # combined headline
    confidence: float = 0.0              # 0 → 1; based on how many feeds gave data
    fetched_at: str = ""
    assets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── fetchers ──────────────────────────────────────────────────────────────


def _fetch_asset(ticker: str) -> AssetState:
    """Pull 6 days of daily history and compute 1d + 5d change + a coarse trend."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="10d")
        if hist is None or hist.empty:
            return AssetState(ticker=ticker,
                                notes=["no history available"])
        closes = hist["Close"].tolist()
        last = float(closes[-1])
        c1 = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 and closes[-2] else 0.0
        c5 = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 and closes[-6] else 0.0
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else last
        # Trend: above 5-day MA + positive 5-day change → bullish
        if c5 > 1.0 and last > ma5:
            trend = "bullish"
        elif c5 < -1.0 and last < ma5:
            trend = "bearish"
        else:
            trend = "choppy"
        return AssetState(ticker=ticker, last=round(last, 4),
                            change_pct_1d=round(c1, 3),
                            change_pct_5d=round(c5, 3), trend=trend)
    except Exception:
        logger.debug("cross-asset fetch failed for %s", ticker, exc_info=True)
        return AssetState(ticker=ticker, notes=["fetch failed"])


# ── universe + cache ───────────────────────────────────────────────────────


_CROSS_ASSET_TICKERS = [
    "SPY", "QQQ", "IWM",          # equities
    "^VIX",                       # volatility
    "^TNX", "^FVX",               # yields (10Y, 5Y)
    "DX=F",                       # dollar futures (DXY proxy)
    "GLD", "USO",                 # commodities
    "BTC-USD",                    # crypto
    "XLK", "XLF", "XLE", "XLU",   # sectors
]

_CACHE: Dict[str, Tuple[float, CrossAssetState]] = {}
_TTL_SECONDS = 300.0


def clear_cache() -> None:
    """Test helper — invalidate the cached state."""
    _CACHE.clear()


def _label_from_axes(equities: str, vix: str, yields: str, dollar: str) -> str:
    """Combine per-axis labels into one headline regime tag."""
    if equities == "risk_on" and vix == "compressed":
        return "risk_on_compressed_vol"
    if equities == "risk_off" and vix in ("elevated", "spiking"):
        return "risk_off_high_vol"
    if equities == "risk_on" and vix in ("elevated", "spiking"):
        return "rally_with_fear"
    if yields == "rising" and dollar == "rising":
        return "tighten_pressure"
    if yields == "falling" and dollar == "falling":
        return "ease_pulse"
    return "mixed"


# ── public entry ──────────────────────────────────────────────────────────


def fetch_state(*, force: bool = False) -> CrossAssetState:
    """Build the combined CrossAssetState. Cached for 5 min."""
    if not force and "default" in _CACHE:
        ts, state = _CACHE["default"]
        if (time.monotonic() - ts) < _TTL_SECONDS:
            return state

    assets: Dict[str, Dict[str, Any]] = {}
    notes: List[str] = []
    successes = 0
    for tk in _CROSS_ASSET_TICKERS:
        asset = _fetch_asset(tk)
        assets[tk] = asset.to_dict()
        if asset.last > 0:
            successes += 1

    # Equities axis
    spy_trend = assets.get("SPY", {}).get("trend") or "unknown"
    qqq_trend = assets.get("QQQ", {}).get("trend") or "unknown"
    iwm_trend = assets.get("IWM", {}).get("trend") or "unknown"
    bull_axes = sum(1 for t in (spy_trend, qqq_trend, iwm_trend) if t == "bullish")
    bear_axes = sum(1 for t in (spy_trend, qqq_trend, iwm_trend) if t == "bearish")
    equities = "risk_on" if bull_axes >= 2 else ("risk_off" if bear_axes >= 2 else "mixed")
    breadth = "broad" if (bull_axes == 3 or bear_axes == 3) else ("narrow" if bull_axes == 1 or bear_axes == 1 else "unknown")

    # Volatility axis
    vix_last = assets.get("^VIX", {}).get("last") or 0.0
    vix_5d = assets.get("^VIX", {}).get("change_pct_5d") or 0.0
    if vix_last > 25 or vix_5d > 20:
        vol = "spiking"
    elif vix_last > 18:
        vol = "elevated"
    elif vix_last > 0:
        vol = "compressed"
    else:
        vol = "unknown"

    # Yields axis
    tnx_5d = assets.get("^TNX", {}).get("change_pct_5d") or 0.0
    if tnx_5d > 2.0:
        yields = "rising"
    elif tnx_5d < -2.0:
        yields = "falling"
    elif assets.get("^TNX", {}).get("last", 0) > 0:
        yields = "stable"
    else:
        yields = "unknown"

    # Dollar axis
    dxy_5d = assets.get("DX=F", {}).get("change_pct_5d") or 0.0
    if dxy_5d > 1.0:
        dollar = "rising"
    elif dxy_5d < -1.0:
        dollar = "falling"
    elif assets.get("DX=F", {}).get("last", 0) > 0:
        dollar = "stable"
    else:
        dollar = "unknown"

    # Commodities axis
    gld_5d = assets.get("GLD", {}).get("change_pct_5d") or 0.0
    uso_5d = assets.get("USO", {}).get("change_pct_5d") or 0.0
    if gld_5d > 1 and uso_5d > 1:
        commodities = "inflationary"
    elif gld_5d < -1 and uso_5d < -1:
        commodities = "disinflationary"
    else:
        commodities = "mixed"

    # Crypto axis
    btc_trend = assets.get("BTC-USD", {}).get("trend") or "unknown"

    confidence = successes / max(1, len(_CROSS_ASSET_TICKERS))
    state = CrossAssetState(
        equities=equities, yields=yields, dollar=dollar, volatility=vol,
        commodities=commodities, crypto=btc_trend, breadth=breadth,
        regime_label=_label_from_axes(equities, vol, yields, dollar),
        confidence=round(confidence, 3),
        fetched_at=datetime.utcnow().isoformat(),
        assets=assets, notes=notes,
    )
    _CACHE["default"] = (time.monotonic(), state)
    return state


# ── regime alignment + hedge suggestion ──────────────────────────────────


def alignment_for(*, ticker_regime_trend: str, state: Optional[CrossAssetState] = None
                    ) -> Dict[str, Any]:
    """Does a per-ticker regime align with the cross-asset state?
    Returns ``aligned`` (bool) + ``aligned_axes`` (list) + ``conflicts`` (list)."""
    if state is None:
        state = fetch_state()
    aligned: List[str] = []
    conflicts: List[str] = []

    ticker_long_bias = ticker_regime_trend == "bullish"
    ticker_short_bias = ticker_regime_trend == "bearish"

    if ticker_long_bias:
        if state.equities == "risk_on": aligned.append("SPY/QQQ/IWM bullish")
        elif state.equities == "risk_off": conflicts.append("indices bearish")
        if state.volatility in ("compressed", "elevated"): aligned.append(f"vol {state.volatility}")
        elif state.volatility == "spiking": conflicts.append("vol spiking")
        if state.yields == "falling": aligned.append("yields falling — easing tailwind")
        elif state.yields == "rising" and state.dollar == "rising":
            conflicts.append("tightening pressure (yields + dollar both up)")
    elif ticker_short_bias:
        if state.equities == "risk_off": aligned.append("indices bearish")
        elif state.equities == "risk_on": conflicts.append("indices bullish — fighting tape")
        if state.volatility in ("elevated", "spiking"): aligned.append(f"vol {state.volatility}")

    return {
        "regime_label": state.regime_label,
        "aligned": len(aligned) >= 2 and not conflicts,
        "aligned_axes": aligned, "conflicts": conflicts,
        "confidence": state.confidence,
    }


def hedge_suggestion(*, state: Optional[CrossAssetState] = None,
                       net_beta: float = 1.0) -> Dict[str, Any]:
    """Suggest a hedge sizing based on cross-asset state + portfolio beta.
    Heuristic — Stage 6 portfolio optimizer will replace this with real
    CVaR/Kelly math but the recommendation surface is the same."""
    if state is None:
        state = fetch_state()
    if state.equities == "risk_off" or state.volatility == "spiking":
        size = min(0.5, max(0.1, 0.10 * net_beta * 2))
        reason = (f"cross-asset {state.regime_label}: long VIX or SPY puts at "
                   f"~{size*100:.0f}% of equity exposure")
        instruments = ["VXX", "SH", "SPY puts (ATM 30d)"]
    elif state.regime_label == "tighten_pressure":
        size = 0.10
        reason = "yields + dollar both rising — small SQQQ or TLT puts hedge"
        instruments = ["SQQQ", "TLT puts"]
    else:
        size = 0.0
        reason = "risk-on / mixed — no systemic hedge required"
        instruments = []
    return {
        "size_fraction": size,
        "reason": reason,
        "instruments": instruments,
        "regime_label": state.regime_label,
    }
