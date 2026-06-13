"""MITS Phase 16.B — deterministic consensus replay.

Given a ``decision_provenance.id``, rebuild the votes from
``agent_outputs_json`` and re-run ``aggregate()`` with the same quorum
+ thresholds used at decision time. The point is to prove that the
persisted shape is replayable end-to-end — if ``replayed.stance`` !=
``persisted.stance`` or the confidences drift beyond a tight tolerance,
the projection is leaking information and the bug must be fixed before
we lean on provenance for offline audit / hyperparameter search.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from backend.bot.agents import (
    AgentVote,
    STANCE_ABSTAIN,
    STANCE_BUY,
    STANCE_HOLD,
    STANCE_SELL,
    aggregate,
)
from backend.bot.agents.contract import (
    DIRECTION_ABSTAIN,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    KeyDriver,
    REASONING_CONTRIBUTING,
    REASONING_DISSENTING,
    REASONING_INSUFFICIENT_SIGNAL,
    REASONING_LEGACY,
    SOURCE_CATEGORIES,
)
from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance


_STANCE_WHITELIST = {STANCE_BUY, STANCE_SELL, STANCE_ABSTAIN, STANCE_HOLD}
_STRUCTURED_RT = {
    REASONING_CONTRIBUTING, REASONING_DISSENTING,
    REASONING_INSUFFICIENT_SIGNAL,
}


def _direction_from_consensus(stance: str) -> str:
    if stance == STANCE_BUY:
        return DIRECTION_LONG
    if stance == STANCE_SELL:
        return DIRECTION_SHORT
    return DIRECTION_ABSTAIN


def _rebuild_vote(
    output: Dict[str, Any], *, consensus_stance: str,
) -> Optional[AgentVote]:
    """Project one ``agent_outputs_json`` entry back into an ``AgentVote``.

    Drivers are reconstructed from ``supporting_factors`` (direction
    aligned with consensus) + ``concerns`` (opposite direction). Each
    bucket gets a single ``KeyDriver`` per entry — the projection is
    lossless w.r.t. the council's vote contract: stance + confidence +
    weight + reasoning_type all come from the persisted dict.
    """
    stance = output.get("stance")
    rt = output.get("reasoning_type")
    if stance not in _STANCE_WHITELIST:
        return None
    # Insufficient_signal votes must have no key_drivers + abstain stance
    # (Stage-20a invariant). Honor that during rebuild.
    if rt == REASONING_INSUFFICIENT_SIGNAL:
        return AgentVote(
            agent=str(output.get("agent") or ""),
            role=str(output.get("role") or ""),
            stance=STANCE_ABSTAIN,
            confidence=0.0,
            weight=float(output.get("weight", 1.0) or 1.0),
            reasoning=str(output.get("reasoning") or ""),
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=str(output.get("risk_level") or "UNKNOWN"),
            expected_edge=float(output.get("expected_edge_bps", 0.0) or 0.0),
            invalidators=(
                [output["invalidation"]] if output.get("invalidation") else []
            ),
            key_drivers=[],
        )

    # Direction map: drivers that supported the consensus take the same
    # direction tag the original vote used; concerns get the opposite.
    aligned = _direction_from_consensus(consensus_stance)
    opposite = (
        DIRECTION_SHORT if aligned == DIRECTION_LONG
        else DIRECTION_LONG if aligned == DIRECTION_SHORT
        else DIRECTION_ABSTAIN
    )

    categories: List[str] = list(output.get("source_categories") or [])
    fallback_cat = (
        categories[0] if categories
        else SOURCE_CATEGORIES[0]  # macro_liquidity — neutral generic
    )

    drivers: List[KeyDriver] = []
    for i, desc in enumerate(output.get("supporting_factors") or []):
        if not desc:
            continue
        cat = categories[i] if i < len(categories) else fallback_cat
        drivers.append(KeyDriver(
            description=desc, source_category=cat,
            direction=aligned if aligned != DIRECTION_ABSTAIN else DIRECTION_LONG,
            weight=0.5, time_sensitive=False,
        ))
    for i, desc in enumerate(output.get("concerns") or []):
        if not desc:
            continue
        cat = categories[i] if i < len(categories) else fallback_cat
        drivers.append(KeyDriver(
            description=desc, source_category=cat,
            direction=opposite if opposite != DIRECTION_ABSTAIN else DIRECTION_SHORT,
            weight=0.5, time_sensitive=False,
        ))

    # Reasoning type must match the persisted projection. If the vote
    # has no drivers + a non-insufficient_signal reasoning_type, demote
    # to legacy so the contract doesn't reject the rebuild.
    final_rt = rt if rt in _STRUCTURED_RT else REASONING_LEGACY
    if final_rt in {REASONING_CONTRIBUTING, REASONING_DISSENTING} and not drivers:
        final_rt = REASONING_LEGACY

    confidence_raw = output.get("confidence", 0)
    try:
        confidence_f = float(confidence_raw) / 100.0
    except (TypeError, ValueError):
        confidence_f = 0.0

    return AgentVote(
        agent=str(output.get("agent") or ""),
        role=str(output.get("role") or ""),
        stance=stance,
        confidence=max(0.0, min(1.0, confidence_f)),
        weight=float(output.get("weight", 1.0) or 1.0),
        reasoning=str(output.get("reasoning") or ""),
        reasoning_type=final_rt,
        risk_level=str(output.get("risk_level") or "UNKNOWN"),
        expected_edge=float(output.get("expected_edge_bps", 0.0) or 0.0),
        invalidators=(
            [output["invalidation"]] if output.get("invalidation") else []
        ),
        key_drivers=drivers,
    )


def replay_consensus_from_provenance(prov_id: int) -> Dict[str, Any]:
    """Load a ``DecisionProvenance`` row, rebuild ``AgentVote`` objects
    from ``agent_outputs_json``, re-run ``aggregate()`` with identical
    quorum + threshold params, and report the diff.

    Returns
    -------
    {
      "persisted": {"stance": ..., "confidence": ...},
      "replayed":  {"stance": ..., "confidence": ...},
      "match":     bool,
      "drift":     {"stance_drift": bool, "confidence_drift": float},
    }
    """
    with session_scope() as session:
        row = session.get(DecisionProvenance, prov_id)
        if row is None:
            raise ValueError(f"DecisionProvenance id={prov_id} not found")
        outputs_blob = row.agent_outputs_json
        consensus_blob = row.consensus_json

    persisted = json.loads(consensus_blob) if consensus_blob else {}
    outputs = json.loads(outputs_blob) if outputs_blob else []
    if not isinstance(outputs, list):
        outputs = []

    persisted_stance = str(persisted.get("stance") or "")
    persisted_conf = float(persisted.get("confidence") or 0.0)
    quorum_required = int(persisted.get("quorum_required") or 0)

    votes: List[AgentVote] = []
    for out in outputs:
        if not isinstance(out, dict):
            continue
        v = _rebuild_vote(out, consensus_stance=persisted_stance)
        if v is not None:
            votes.append(v)

    replayed = aggregate(votes, quorum_min=quorum_required or None)
    replayed_stance = replayed.stance
    replayed_conf = float(replayed.confidence)

    confidence_drift = abs(persisted_conf - replayed_conf)
    stance_drift = persisted_stance != replayed_stance
    match = (not stance_drift) and confidence_drift < 0.01

    return {
        "persisted": {
            "stance": persisted_stance,
            "confidence": persisted_conf,
        },
        "replayed": {
            "stance": replayed_stance,
            "confidence": replayed_conf,
        },
        "match": match,
        "drift": {
            "stance_drift": stance_drift,
            "confidence_drift": round(confidence_drift, 4),
        },
    }
