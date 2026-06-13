"""MITS Phase 15.B — HistoricalAnalogRetrieval as a first-class primitive.

Lifts the analog-pull path that used to live inside ``SimulatorAgent``
into a standalone module so the same primitive can be re-used by other
agents (deep composer, brain context, autopsy) without invoking the full
simulator pipeline.

Contract preserved verbatim from ``simulator._collect_analog_returns``
(and its helpers ``_outcomes_for_hits`` + ``_dte_to_horizon``):

  * Query text format::

        f"ticker={TICKER} regime={regime|unknown} "
        f"vol={vol|normal} pattern={pattern|na}"

  * Two-pass fallback: same-ticker preferred → any-ticker top-up when
    the same-ticker pass returns fewer than 10 outcomes. De-dupes by
    observation id, preserves same-ticker-first ordering, truncates to
    ``k``.

  * Outcome join: snapshot key + ticker filter → observation rows →
    matching ``MarketOutcome.horizon`` rows. Returns ``return_pct * 100``
    so callers always see PERCENT units.

  * Namespace: ``regime_snapshot_v2`` (populated by the Phase 11.K
    daily fingerprint job).
"""
from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from backend.bot.regime.vector import RegimeVector


_VALID_HORIZONS = ("5min", "30min", "60min", "1d", "5d", "20d")


@dataclass
class AnalogHit:
    observation_id: int
    ticker: str
    timestamp: datetime
    distance: float                         # 1.0 - cosine; smaller = closer
    cosine: float
    regime_label: Optional[str]
    pattern_set: List[str]                  # patterns on the same (ticker, ts)
    realized_return_pct: float              # PERCENT units
    horizon: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "ticker": self.ticker,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "distance": self.distance,
            "cosine": self.cosine,
            "regime_label": self.regime_label,
            "pattern_set": list(self.pattern_set),
            "realized_return_pct": self.realized_return_pct,
            "horizon": self.horizon,
        }


@dataclass
class AnalogCluster:
    query_state: Dict[str, Any]
    analogs: List[AnalogHit]
    outcome_distribution: Dict[str, float]
    cohort_size: int
    sector_fallback_used: bool
    freshness_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query_state": dict(self.query_state),
            "analogs": [a.to_dict() for a in self.analogs],
            "outcome_distribution": dict(self.outcome_distribution),
            "cohort_size": self.cohort_size,
            "sector_fallback_used": self.sector_fallback_used,
            "freshness_seconds": self.freshness_seconds,
        }


def _pctile(sorted_vals: List[float], q: float) -> float:
    """Linear-interp percentile. Copy of ``simulator._pctile`` so the
    primitive doesn't pull in the simulator module."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = pos - lo
    return float(sorted_vals[lo]) * (1 - frac) + float(sorted_vals[hi]) * frac


def _build_query_text(*, ticker: str, regime_label: Optional[str],
                      vol_label: Optional[str], pattern: Optional[str]) -> str:
    """The exact format the existing simulator analog path uses. Tests
    monkeypatch ``embed`` on this contract."""
    return (
        f"ticker={ticker.upper()} regime={regime_label or 'unknown'} "
        f"vol={vol_label or 'normal'} pattern={pattern or 'na'}"
    )


def _resolve_labels(regime_vector: "RegimeVector") -> tuple[Optional[str], Optional[str]]:
    """Pull (trend_label, vol_label) off a RegimeVector. Dimension values
    are already plain strings in the live RegimeVector, but defensively
    coerce to None when missing so the query string falls back to the
    'unknown' / 'normal' defaults."""
    trend = getattr(regime_vector.trend, "value", None)
    vol = getattr(regime_vector.volatility_state, "value", None)
    return (trend if trend else None, vol if vol else None)


def _outcomes_for_hits(hits, *, ticker: Optional[str],
                       horizon: str) -> List["AnalogHit"]:
    """Resolve hit metadata → AnalogHit rows for the chosen horizon.

    Mirrors the join logic in ``simulator._outcomes_for_hits`` (one
    obs-pull per hit, filter by ticker when requested, then load
    matching MarketOutcome rows) but returns the full ``AnalogHit``
    payload — observation_id, timestamp, regime_label, pattern_set,
    cosine + distance — so the cluster can be inspected / explained
    upstream."""
    try:
        from sqlalchemy import select
        from backend.db import session_scope
        from backend.models.market_observation import MarketObservation
        from backend.models.market_outcome import MarketOutcome
    except Exception:
        return []
    out: List[AnalogHit] = []
    try:
        with session_scope() as s:
            for hit in hits:
                meta = hit.metadata or {}
                snap_date = meta.get("date")
                if not snap_date:
                    continue
                q = (select(MarketObservation)
                     .where(MarketObservation.timestamp.is_not(None))
                     .order_by(MarketObservation.timestamp.desc())
                     .limit(20))
                if ticker:
                    q = q.where(MarketObservation.ticker == ticker)
                obs_rows = s.execute(q).scalars().all()
                if not obs_rows:
                    continue
                obs_ids = [r.id for r in obs_rows]
                obs_by_id = {r.id: r for r in obs_rows}
                outcomes = s.execute(
                    select(MarketOutcome)
                    .where(MarketOutcome.observation_id.in_(obs_ids))
                    .where(MarketOutcome.horizon == horizon)
                ).scalars().all()
                cosine = float(getattr(hit, "cosine", 0.0) or 0.0)
                distance = max(0.0, 1.0 - cosine)
                regime_label = meta.get("regime")
                for oc in outcomes:
                    if oc.return_pct is None:
                        continue
                    obs = obs_by_id.get(oc.observation_id)
                    if obs is None:
                        continue
                    out.append(AnalogHit(
                        observation_id=int(oc.observation_id),
                        ticker=obs.ticker,
                        timestamp=obs.timestamp,
                        distance=distance,
                        cosine=cosine,
                        regime_label=regime_label,
                        pattern_set=[obs.pattern] if obs.pattern else [],
                        realized_return_pct=float(oc.return_pct) * 100.0,
                        horizon=horizon,
                    ))
    except Exception:
        return out
    return out


def _empty_cluster(query_state: Dict[str, Any]) -> AnalogCluster:
    return AnalogCluster(
        query_state=query_state,
        analogs=[],
        outcome_distribution={},
        cohort_size=0,
        sector_fallback_used=False,
        freshness_seconds=0.0,
    )


def _build_distribution(returns: List[float]) -> Dict[str, float]:
    if not returns:
        return {}
    srt = sorted(returns)
    return {
        "mean": statistics.mean(returns),
        "std": statistics.pstdev(returns) if len(returns) > 1 else 0.0,
        "min": min(returns),
        "max": max(returns),
        "p10": _pctile(srt, 0.10),
        "p25": _pctile(srt, 0.25),
        "p50": _pctile(srt, 0.50),
        "p75": _pctile(srt, 0.75),
        "p90": _pctile(srt, 0.90),
    }


def retrieve_analogs(
    *,
    ticker: str,
    regime_vector: "RegimeVector",
    pattern: str,
    horizon: str = "1d",
    k: int = 50,
    sector_fallback: bool = True,
) -> AnalogCluster:
    """K-NN over ``regime_snapshot_v2`` → realized forward returns →
    AnalogCluster summary.

    Two-pass: same-ticker first, then any-ticker top-up when the
    same-ticker pass returns < 10 outcomes (``sector_fallback=True``).
    Returns an empty cluster (cohort_size=0) when the vector store is
    unavailable, the embed fails, or both passes return zero matches.
    """
    if horizon not in _VALID_HORIZONS:
        raise ValueError(
            f"horizon must be one of {_VALID_HORIZONS}, got {horizon!r}")

    ticker_u = (ticker or "").upper()
    regime_label, vol_label = _resolve_labels(regime_vector)
    query_state: Dict[str, Any] = {
        "ticker": ticker_u,
        "regime": regime_label or "unknown",
        "vol_state": vol_label or "normal",
        "pattern": pattern or "na",
        "horizon": horizon,
        "k": int(k),
    }

    from backend.bot.ai.vector_store import embed, similarity_search

    query_text = _build_query_text(
        ticker=ticker_u, regime_label=regime_label,
        vol_label=vol_label, pattern=pattern,
    )
    vec = embed(query_text)
    if not vec:
        return _empty_cluster(query_state)

    hits = similarity_search("regime_snapshot_v2", vec, k=int(k))
    if not hits:
        return _empty_cluster(query_state)

    same_ticker = _outcomes_for_hits(hits, ticker=ticker_u, horizon=horizon)
    sector_fallback_used = False
    if sector_fallback and len(same_ticker) < 10:
        any_ticker = _outcomes_for_hits(hits, ticker=None, horizon=horizon)
        # Use the any-ticker pass only when it actually adds new observations.
        if any_ticker:
            sector_fallback_used = True
    else:
        any_ticker = []

    seen: set[int] = set()
    merged: List[AnalogHit] = []
    for a in same_ticker + any_ticker:
        if a.observation_id in seen:
            continue
        seen.add(a.observation_id)
        merged.append(a)
        if len(merged) >= int(k):
            break

    if not merged:
        return _empty_cluster(query_state)

    distribution = _build_distribution(
        [a.realized_return_pct for a in merged])

    return AnalogCluster(
        query_state=query_state,
        analogs=merged,
        outcome_distribution=distribution,
        cohort_size=len(merged),
        sector_fallback_used=sector_fallback_used,
        freshness_seconds=0.0,
    )


__all__ = [
    "AnalogHit",
    "AnalogCluster",
    "retrieve_analogs",
]
