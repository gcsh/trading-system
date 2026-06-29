"""MITS Phase 16.B — formalized AgentInput / AgentOutput schemas.

This module ADDS a typed envelope around the loose context dict the
council reads + a standardized output projection of ``AgentVote``. It
is fully additive: the existing agents continue to accept the legacy
dict, the existing ``AgentVote`` shape is unchanged. ``AgentInput``
exposes ``legacy_context()`` so a caller can hand the same envelope to
both the new typed path and the old dict-consuming agents without
shape drift.

Why this exists: 16.B introduces deterministic replay (see
``backend.bot.decision.replay``). The replayer rebuilds ``AgentVote``
objects from JSON; the only way that round-trip is lossless is if the
agents emit a stable, named projection. ``AgentOutput`` is that
projection — supporting_factors / concerns are split by comparing each
``KeyDriver.direction`` against the consensus direction string, so a
``supports_long`` driver on a long consensus lands in
``supporting_factors`` and the same driver on a short consensus lands
in ``concerns``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from backend.bot.agents.contract import (
    DIRECTION_ABSTAIN,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    KeyDriver,
)

if TYPE_CHECKING:
    from backend.bot.agents import AgentVote


# Maps the loose ``consensus_direction`` strings ("long" | "short" |
# "neutral" | "abstain") to the KeyDriver direction constants. Used by
# ``agent_output_from_vote`` to classify each driver as supporting or
# opposing the consensus.
_CONSENSUS_TO_DRIVER_DIRECTION = {
    "long": DIRECTION_LONG,
    "short": DIRECTION_SHORT,
    "neutral": DIRECTION_ABSTAIN,
    "abstain": DIRECTION_ABSTAIN,
}


@dataclass(frozen=True)
class AgentInput:
    """Single structured input every council agent reads.

    Replaces the loose context dict for the new typed path. Existing
    agents continue accepting the dict; ``legacy_context()`` renders
    this envelope into the same loose shape so a caller can feed both
    paths from one source of truth.
    """

    ticker: str
    action: str
    proposed_direction: str
    snapshot: Dict[str, Any]
    regime_vector: Optional[Dict[str, Any]]
    strategy_matrix: Optional[Dict[str, Any]]
    thesis: Dict[str, Any]
    historical_analogs: Optional[Dict[str, Any]]
    risk_context: Dict[str, Any]
    portfolio_state: Dict[str, Any]
    market_internals: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "action": self.action,
            "proposed_direction": self.proposed_direction,
            "snapshot": dict(self.snapshot or {}),
            "regime_vector": dict(self.regime_vector or {}) or None,
            "strategy_matrix": (
                dict(self.strategy_matrix) if self.strategy_matrix else None
            ),
            "thesis": dict(self.thesis or {}),
            "historical_analogs": (
                dict(self.historical_analogs)
                if self.historical_analogs else None
            ),
            "risk_context": dict(self.risk_context or {}),
            "portfolio_state": dict(self.portfolio_state or {}),
            "market_internals": (
                dict(self.market_internals)
                if self.market_internals else None
            ),
        }

    def legacy_context(self) -> Dict[str, Any]:
        """Render as the loose context dict today's agents read.

        Mirrors the keys ``run_consensus`` / ``rule_consensus_exception``
        already pass: ``analytics``, ``snapshot``, ``portfolio_risk``,
        ``regime_vector``, ``strategy_matrix``, ``knowledge_evidence``
        (the analog cluster surfaces through this key), plus the macro /
        breadth / earnings / insider / short-pressure / cot bag carried
        in ``risk_context``.
        """
        features = (self.snapshot or {}).get("features") or {}
        regime_trend = None
        if self.regime_vector:
            trend = (self.regime_vector or {}).get("trend") or {}
            regime_trend = (
                trend.get("value") if isinstance(trend, dict) else trend
            )
        risk = self.risk_context or {}
        ctx: Dict[str, Any] = {
            "ticker": self.ticker,
            "action": self.action,
            "snapshot": dict(self.snapshot or {}),
            "analytics": {
                "regime": regime_trend,
                "features": features,
            },
            "features": features,
            "portfolio_risk": dict(self.portfolio_state or {}),
            "regime_vector": (
                dict(self.regime_vector) if self.regime_vector else None
            ),
            "strategy_matrix": (
                dict(self.strategy_matrix) if self.strategy_matrix else None
            ),
            "knowledge_evidence": (
                dict(self.historical_analogs)
                if self.historical_analogs else None
            ),
            "market_internals_obj": self.market_internals,
            "thesis": dict(self.thesis or {}),
            "macro": risk.get("macro"),
            "breadth": risk.get("breadth"),
            "cot_snapshot": risk.get("cot"),
            "earnings_intel": risk.get("earnings_intel"),
            "insider_activity": risk.get("insider"),
            "short_pressure": risk.get("short_pressure"),
            "cross_asset": risk.get("cross_asset"),
        }
        return ctx


@dataclass
class AgentOutput:
    """Standardized output every agent emits — lossless projection from
    an existing ``AgentVote``.

    The split between ``supporting_factors`` and ``concerns`` is the
    16.B addition: each ``KeyDriver`` is bucketed by whether its
    ``direction`` aligns with the consensus direction handed in.
    Abstain-direction drivers fall into neither bucket — they are
    inherently non-directional and inflating either side would bias
    replay.
    """

    agent: str
    role: str
    stance: str
    confidence: int
    weight: float
    reasoning: str
    reasoning_type: str
    supporting_factors: List[str] = field(default_factory=list)
    concerns: List[str] = field(default_factory=list)
    invalidation: Optional[str] = None
    source_categories: List[str] = field(default_factory=list)
    expected_edge_bps: float = 0.0
    risk_level: str = "UNKNOWN"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def make_agent_input(context: Dict[str, Any]) -> AgentInput:
    """Adapter from the loose context dict to ``AgentInput``.

    Called once per cycle by ``run_consensus``. Reads only fields the
    consensus path already populates — no new lookups, no I/O.
    """
    action = str(context.get("action") or "")
    proposed_direction = _direction_from_action(action)

    analytics = context.get("analytics") or {}
    snapshot = dict(context.get("snapshot") or {})
    # Surface analytics.features under snapshot.features so legacy_context
    # can recover it without re-reading analytics.
    if "features" not in snapshot:
        feats = analytics.get("features") or context.get("features") or {}
        if feats:
            snapshot["features"] = feats

    thesis = {
        "signal_reason": context.get("signal_reason"),
        "strategy": context.get("strategy"),
        "confidence": context.get("confidence"),
        "invalidation": (context.get("thesis") or {}).get("invalidation"),
    }

    risk_context: Dict[str, Any] = {
        "macro": context.get("macro"),
        "breadth": context.get("breadth"),
        "cot": context.get("cot_snapshot"),
        "earnings_intel": context.get("earnings_intel"),
        "insider": context.get("insider_activity"),
        "short_pressure": context.get("short_pressure"),
        "cross_asset": context.get("cross_asset"),
    }

    portfolio_state = dict(context.get("portfolio_risk") or {})
    # 14.B / 14.E correlation matrix + cohort data live under ``portfolio``
    # alongside the legacy ``portfolio_risk``. Surface both so replay can
    # reproduce the agents' view.
    portfolio_extra = context.get("portfolio") or {}
    if portfolio_extra:
        portfolio_state.setdefault("portfolio_extra", portfolio_extra)

    market_internals_obj = context.get("market_internals_obj")
    market_internals_dict: Optional[Dict[str, Any]] = None
    if market_internals_obj is not None:
        to_dict = getattr(market_internals_obj, "to_dict", None)
        if callable(to_dict):
            market_internals_dict = to_dict()
        elif isinstance(market_internals_obj, dict):
            market_internals_dict = dict(market_internals_obj)

    return AgentInput(
        ticker=str(context.get("ticker") or ""),
        action=action,
        proposed_direction=proposed_direction,
        snapshot=snapshot,
        regime_vector=context.get("regime_vector"),
        strategy_matrix=context.get("strategy_matrix"),
        thesis=thesis,
        historical_analogs=(
            context.get("knowledge_evidence")
            or context.get("analog_cluster")
        ),
        risk_context=risk_context,
        portfolio_state=portfolio_state,
        market_internals=market_internals_dict,
    )


def agent_output_from_vote(
    vote: "AgentVote", *, consensus_direction: str,
) -> AgentOutput:
    """Project an ``AgentVote`` into the standardized ``AgentOutput``.

    ``consensus_direction`` is the council's chosen direction string —
    one of "long", "short", "neutral", "abstain". Each ``KeyDriver``
    on the vote is classified:

      driver.direction == matching driver constant for consensus_direction
        ⇒ supporting_factors
      driver.direction == opposite driver constant
        ⇒ concerns

    Abstain-direction drivers are excluded from both buckets. When the
    consensus is itself neutral / abstain, every directional driver
    drops into ``concerns`` — there is no "supports the abstain"
    direction in the contract.
    """
    aligned = _CONSENSUS_TO_DRIVER_DIRECTION.get(
        (consensus_direction or "").lower(), DIRECTION_ABSTAIN,
    )
    supporting: List[str] = []
    concerns: List[str] = []
    categories: List[str] = []
    for kd in (getattr(vote, "key_drivers", []) or []):
        if isinstance(kd, KeyDriver):
            desc = kd.description
            direction = kd.direction
            category = kd.source_category
        else:
            desc = kd.get("description")
            direction = kd.get("direction")
            category = kd.get("source_category")
        if category and category not in categories:
            categories.append(category)
        if not desc:
            continue
        if aligned in (DIRECTION_LONG, DIRECTION_SHORT):
            if direction == aligned:
                supporting.append(desc)
            elif direction in (DIRECTION_LONG, DIRECTION_SHORT):
                concerns.append(desc)
        else:
            # Consensus is abstain/neutral — any directional driver is a
            # concern relative to inaction.
            if direction in (DIRECTION_LONG, DIRECTION_SHORT):
                concerns.append(desc)

    invalidators = getattr(vote, "invalidators", None) or []
    invalidation = invalidators[0] if invalidators else None

    confidence_raw = float(getattr(vote, "confidence", 0.0) or 0.0)
    confidence_int = int(round(confidence_raw * 100))

    return AgentOutput(
        agent=str(getattr(vote, "agent", "") or ""),
        role=str(getattr(vote, "role", "") or ""),
        stance=str(getattr(vote, "stance", "") or ""),
        confidence=confidence_int,
        weight=float(getattr(vote, "weight", 1.0) or 0.0),
        reasoning=str(getattr(vote, "reasoning", "") or ""),
        reasoning_type=str(getattr(vote, "reasoning_type", "") or ""),
        supporting_factors=supporting,
        concerns=concerns,
        invalidation=invalidation,
        source_categories=categories,
        expected_edge_bps=float(getattr(vote, "expected_edge", 0.0) or 0.0),
        risk_level=str(getattr(vote, "risk_level", "UNKNOWN") or "UNKNOWN"),
    )


def _direction_from_action(action: Optional[str]) -> str:
    if not action:
        return "neutral"
    a = action.upper()
    if "PUT" in a or a.startswith("SELL"):
        return "short"
    if a.startswith("BUY"):
        return "long"
    return "neutral"
