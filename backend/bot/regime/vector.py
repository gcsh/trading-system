"""MITS Phase 15.A — RegimeVector consolidation.

Single normalized view across the six regime surfaces already present in
the codebase:

  * ``trend``             — from ``MarketRegime.trend`` (detect_regime)
  * ``volatility_state``  — from ``MarketRegime.volatility``
  * ``iv_rank``           — from snapshot / features (0-100 scaled)
  * ``iv_regime``         — from ``classify_ticker`` (IVRegimeReport)
  * ``intraday_regime``   — from ``IntradayRegimeClassifier._cache``
  * ``gamma_state``       — from dealer-positioning features
  * ``macro_regime``      — from the most recent SPY composite_macro_regime
                            observation

Each dimension carries its own ``freshness_seconds``, ``source`` tag and
``health`` flag. The composite ``health`` rolls those up via thresholds
from ``TUNABLES.regime_vector_*_age_sec`` so downstream consumers can
gate on a single label.

Builder is best-effort: a missing or stale dim degrades that one dim's
health (yellow / red) and the composite, never raises.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import desc, select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.market_observation import MarketObservation

logger = logging.getLogger(__name__)


_GREEN = "green"
_YELLOW = "yellow"
_RED = "red"


# Per-dimension freshness thresholds (seconds). Different sources have
# different natural cadences: GEX recomputes per cycle, IV regime caches
# hourly, intraday classifier ticks every minute, composite_macro_regime
# only emits on regime crossings (potentially weeks apart).
_DIM_AGE_OVERRIDES: Dict[str, Tuple[int, int]] = {
    # source → (red_age_sec, yellow_age_sec)
    "macro":    (14 * 86400, 7 * 86400),    # 14d red / 7d yellow — event-only source
    "intraday": (300, 60),                  # 5min red / 1min yellow — tick cadence
    # All other sources fall through to TUNABLES.regime_vector_red_age_sec / yellow_age_sec
}


def _resolve_thresholds(source: str) -> Tuple[int, int]:
    """Return (red_age_sec, yellow_age_sec) for a dim's source.
    Falls back to global TUNABLES when no override exists."""
    override = _DIM_AGE_OVERRIDES.get(source)
    if override is not None:
        return override
    return (
        TUNABLES.regime_vector_red_age_sec,
        TUNABLES.regime_vector_yellow_age_sec,
    )


def _apply_freshness_health(dim: "RegimeDimension") -> "RegimeDimension":
    """Elevate dim.health based on freshness vs. its source's thresholds.
    Red beats yellow beats existing label; never downgrades."""
    if dim.freshness_seconds is None:
        return dim
    red_age, yellow_age = _resolve_thresholds(dim.source)
    if dim.freshness_seconds > red_age:
        dim.health = _RED
    elif dim.freshness_seconds > yellow_age and dim.health != _RED:
        dim.health = _YELLOW
    return dim


@dataclass
class RegimeDimension:
    value: Any
    freshness_seconds: Optional[float]
    source: str
    health: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "freshness_seconds": self.freshness_seconds,
            "source": self.source,
            "health": self.health,
        }


@dataclass
class RegimeVector:
    ticker: str
    as_of: datetime
    trend: RegimeDimension
    volatility_state: RegimeDimension
    iv_rank: RegimeDimension
    iv_regime: RegimeDimension
    intraday_regime: RegimeDimension
    gamma_state: RegimeDimension
    macro_regime: RegimeDimension
    health: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "as_of": self.as_of.isoformat(),
            "trend": self.trend.to_dict(),
            "volatility_state": self.volatility_state.to_dict(),
            "iv_rank": self.iv_rank.to_dict(),
            "iv_regime": self.iv_regime.to_dict(),
            "intraday_regime": self.intraday_regime.to_dict(),
            "gamma_state": self.gamma_state.to_dict(),
            "macro_regime": self.macro_regime.to_dict(),
            "health": self.health,
        }

    def summary_text(self) -> str:
        iv_rank = self.iv_rank.value
        iv_rank_s = (
            f"{iv_rank:.0f}" if isinstance(iv_rank, (int, float))
            else str(iv_rank)
        )
        gs = self.gamma_state.value if isinstance(self.gamma_state.value, dict) else {}
        gamma_label = gs.get("regime", "unknown") if gs else self.gamma_state.value
        wall = gs.get("dominant_wall", "?") if gs else "?"
        pin = gs.get("pinning_probability")
        pin_s = f"{float(pin):.2f}" if isinstance(pin, (int, float)) else "n/a"
        return (
            f"trend={self.trend.value} | vol={self.volatility_state.value} | "
            f"iv_rank={iv_rank_s} | iv_regime={self.iv_regime.value} | "
            f"intraday={self.intraday_regime.value} | "
            f"gamma={gamma_label} (wall={wall}, pin={pin_s}) | "
            f"macro={self.macro_regime.value} | health={self.health}"
        )


def _dim_health_from_value(value: Any, unknown_tokens=("unknown", None, "")) -> str:
    if value in unknown_tokens:
        return _YELLOW
    return _GREEN


def _iv_regime_freshness(ticker: str) -> Optional[float]:
    """Read the IV regime cache's monotonic timestamp without forcing a
    re-classify. Returns seconds since cache write, or None when the
    ticker hasn't been classified in this process yet."""
    try:
        from backend.bot.iv_regime import _CACHE
        hit = _CACHE.get(ticker.upper())
        if hit is None:
            return None
        return max(0.0, time.monotonic() - float(hit[0]))
    except Exception:
        return None


def _build_trend_vol(snapshot: Dict[str, Any]) -> tuple[RegimeDimension, RegimeDimension]:
    from backend.bot.regime import detect_regime
    mr = detect_regime(snapshot)
    trend_val = mr.trend or "unknown"
    vol_val = mr.volatility or "normal"
    trend = RegimeDimension(
        value=trend_val, freshness_seconds=0.0, source="regime",
        health=_GREEN if trend_val != "unknown" else _YELLOW,
    )
    vol = RegimeDimension(
        value=vol_val, freshness_seconds=0.0, source="regime",
        health=_GREEN,
    )
    return trend, vol


def _build_iv_rank(ticker: str, snapshot: Dict[str, Any]) -> RegimeDimension:
    features = snapshot.get("features") or {}
    raw = snapshot.get("iv_rank")
    if raw is None:
        raw = features.get("iv_rank")
    try:
        value: Any = float(raw) if raw is not None else None
        if value != value:  # NaN
            value = None
    except (TypeError, ValueError):
        value = None
    return RegimeDimension(
        value=value,
        freshness_seconds=_iv_regime_freshness(ticker),
        source="iv_regime_cache" if value is not None else "iv_regime",
        health=_GREEN if value is not None else _YELLOW,
    )


def _build_iv_regime(ticker: str) -> RegimeDimension:
    try:
        from backend.bot.iv_regime import classify_ticker
        report = classify_ticker(ticker)
        value = report.regime or "unknown"
        return RegimeDimension(
            value=value,
            freshness_seconds=_iv_regime_freshness(ticker),
            source="iv_regime",
            health=_dim_health_from_value(value),
        )
    except Exception as exc:
        logger.debug("regime_vector: iv_regime fetch failed for %s: %s", ticker, exc)
        return RegimeDimension(
            value="unknown", freshness_seconds=None,
            source="iv_regime", health=_YELLOW,
        )


def _build_intraday(intraday_classifier: Optional[Any]) -> RegimeDimension:
    if intraday_classifier is None or not hasattr(intraday_classifier, "_cache"):
        return RegimeDimension(
            value="unknown", freshness_seconds=None,
            source="intraday", health=_YELLOW,
        )
    cached = intraday_classifier._cache
    cache_at = float(getattr(intraday_classifier, "_cache_at", 0.0) or 0.0)
    if cached is None or cache_at <= 0.0:
        return RegimeDimension(
            value="unknown", freshness_seconds=None,
            source="intraday", health=_YELLOW,
        )
    freshness = max(0.0, time.time() - cache_at)
    value = getattr(cached, "state", None) or "unknown"
    return RegimeDimension(
        value=value, freshness_seconds=freshness,
        source="intraday", health=_dim_health_from_value(value),
    )


def _build_gamma(snapshot: Dict[str, Any]) -> RegimeDimension:
    features = snapshot.get("features") or {}
    regime = features.get("dealer_regime") or snapshot.get("dealer_regime") or "unknown"
    wall = features.get("dominant_wall") or "neutral"
    pin = features.get("pinning_probability")
    value = {
        "regime": regime,
        "dominant_wall": wall,
        "pinning_probability": pin,
    }
    health = _GREEN if regime != "unknown" else _YELLOW
    return RegimeDimension(
        value=value, freshness_seconds=0.0,
        source="gex", health=health,
    )


def _build_macro() -> RegimeDimension:
    try:
        with session_scope() as s:
            row = s.execute(
                select(MarketObservation)
                .where(MarketObservation.pattern == "composite_macro_regime")
                .where(MarketObservation.ticker == "SPY")
                .order_by(desc(MarketObservation.timestamp))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return RegimeDimension(
                    value="unknown", freshness_seconds=None,
                    source="macro", health=_YELLOW,
                )
            obs = row.to_dict()
            feats = obs.get("features") or {}
            value = feats.get("regime") or "unknown"
            ts = row.timestamp
            freshness = (
                max(0.0, (datetime.utcnow() - ts).total_seconds())
                if ts else None
            )
            return RegimeDimension(
                value=value, freshness_seconds=freshness,
                source="macro", health=_dim_health_from_value(value),
            )
    except Exception as exc:
        logger.debug("regime_vector: macro fetch failed: %s", exc)
        return RegimeDimension(
            value="unknown", freshness_seconds=None,
            source="macro", health=_YELLOW,
        )


def _aggregate_health(dims: Dict[str, RegimeDimension]) -> str:
    red_count = 0
    any_yellow = False
    any_red = False

    for d in dims.values():
        if d.health == _RED:
            red_count += 1
            any_red = True
        elif d.health == _YELLOW:
            any_yellow = True

    if red_count >= 2 or any_red:
        return _RED
    if any_yellow:
        return _YELLOW
    return _GREEN


def build_regime_vector(
    *,
    ticker: str,
    snapshot: Dict[str, Any],
    intraday_classifier: Optional[Any] = None,
) -> RegimeVector:
    """Assemble the RegimeVector from the live snapshot + cached regime
    classifiers + the latest macro observation. Never raises — every
    sub-fetch degrades to a yellow dim on failure."""
    snapshot = snapshot or {}
    ticker_u = (ticker or "").upper()
    trend, vol = _build_trend_vol(snapshot)
    iv_rank = _build_iv_rank(ticker_u, snapshot)
    iv_regime = _build_iv_regime(ticker_u)
    intraday = _build_intraday(intraday_classifier)
    gamma = _build_gamma(snapshot)
    macro = _build_macro()

    dims = {
        "trend": trend, "volatility_state": vol, "iv_rank": iv_rank,
        "iv_regime": iv_regime, "intraday_regime": intraday,
        "gamma_state": gamma, "macro_regime": macro,
    }
    for d in dims.values():
        _apply_freshness_health(d)
    health = _aggregate_health(dims)

    return RegimeVector(
        ticker=ticker_u,
        as_of=datetime.utcnow(),
        trend=trend,
        volatility_state=vol,
        iv_rank=iv_rank,
        iv_regime=iv_regime,
        intraday_regime=intraday,
        gamma_state=gamma,
        macro_regime=macro,
        health=health,
    )


__all__ = [
    "RegimeDimension",
    "RegimeVector",
    "build_regime_vector",
]
