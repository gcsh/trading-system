"""MITS Phase 18.B — Counterfactual Replayer.

If you can't ask "what if?" against the audit trail, the audit trail
is decoration. Given a closed Trade with a ``decision_provenance``
row, this module re-runs a decision with ONE input changed and
reports how the verdict / sizing / outcome would have differed.

Three supported variations (all read-only over the provenance ledger):

  1. **Sizing counterfactual** — given a closed Trade with
     ``sizing_chain_json`` (Phase 17.C), recompute realized P&L if
     ``final_qty`` had been scaled by factor F. Linear scaling only —
     no slippage / market impact modeling. The point isn't "what
     would the FILL have been" (that needs a quote replay); the
     point is "we left 50% upside on the table by sizing at half
     base on this setup type".

  2. **Policy counterfactual** — given a provenance row whose
     ``policy_result_json`` lists at least one hard blocker, ask
     "what if THIS rule had not blocked?" The remaining
     ``blocking_factors`` carry the surviving headline blocker (if
     any). Answers "did this one rule actually stop the trade, or
     were there 3 other rules waiting behind it?"

  3. **Consensus counterfactual** — given a provenance row with
     ``agent_outputs_json`` populated, flip one agent's vote
     (stance + confidence) and re-run ``aggregate()`` with the same
     quorum + thresholds used at decision time. The aggregation
     primitive is the same one Phase 16.B's
     ``replay_consensus_from_provenance`` already uses, so the
     replay-invariant (drift = 0.0) is preserved by construction.

Honesty rules:

  * Sizing CF: ``note`` carries
    ``"linear scaling — slippage + market impact not modeled; for
    paper trading + small size only"``. The operator sees the
    caveat next to the curve.
  * Policy CF: only valid for rows where the named rule actually
    fired as a BLOCKER on the original decision. If the named rule
    didn't block, the helper returns ``None`` and the result note
    carries ``"rule_did_not_block_original_decision"``.
  * Consensus CF: only valid when ``agent_outputs_json`` is
    non-empty + decodes to a list. Re-running aggregation MUST be
    deterministic — the unit tests assert call twice → same result.
  * All three: if ``prov_id`` is missing or the row is non-decision
    (e.g. ``event_status = market_closed``, no agent_outputs), the
    helper returns ``None`` and the surrounding ``compute_all``
    bundles a note explaining why.

Time-shifted counterfactuals ("what if this had run an hour later")
are NOT supported in 18.B — they require bar fetches + intraday
chain replays, which is a Phase 19 problem. The 3 supported
variations cover the operator's three concrete questions:
"sized wrong? blocked spuriously? wrong consensus?"
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from backend.bot.agents import (
    STANCE_ABSTAIN, STANCE_BUY, STANCE_HOLD, STANCE_SELL,
    aggregate,
)
from backend.bot.decision.replay import _rebuild_vote
from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance
from backend.models.trade import Trade


logger = logging.getLogger(__name__)


# ── Public knobs (config-driven; no magic numbers in the math) ─────────


# MITS Phase 18-FU Stream D (Gap 12) — code version stamp on every
# counterfactual result. Cache rows persist ``result_json`` blobs; when
# the math in this module changes (e.g. a bug fix in
# ``policy_counterfactual``), old cached rows are STALE even though the
# (prov_id, variation_kind, variation_key) cache key still matches.
# The version stamp lets cache CONSUMERS detect the mismatch and decide
# whether to recompute or serve-with-warning.
#
# Cache-version semantics (the more honest reading of Gap 12 — the
# Stream D task brief explicitly asked us to confirm or revise this):
#   * Consensus CF is computed against ``agent_outputs_json`` snapshot
#     from decision_provenance — which is FROZEN at decision time. So
#     a later flip of 18.D's apply flag does NOT make consensus CF
#     stale. The cache is still mathematically valid against the
#     snapshot it was computed from. Original Gap 12's "weight change
#     invalidates cache" framing was incorrect.
#   * What DOES make cached results stale is a code change in THIS
#     module (counterfactual.py). A bug fix in
#     ``_persisted_blocking_factors`` or ``aggregate`` changes the
#     output shape; old cache rows are still served by the bare
#     (prov_id, kind, key) cache key. The version stamp gives the
#     reader a signal to recompute.
#
# Bump this constant whenever you ship a meaningful behavior change to
# the math in this module. Format: SemVer-ish, no enforced parse rule.
COUNTERFACTUAL_CODE_VERSION = "v1.0.0"


DEFAULT_SIZING_FACTORS: Tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)

SIZING_NOTE = (
    "linear scaling — slippage + market impact not modeled; "
    "for paper trading + small size only"
)
POLICY_NOTE_RULE_DID_NOT_BLOCK = "rule_did_not_block_original_decision"
POLICY_NOTE_RULE_OVERRIDE_CLEARED = "no_other_blockers_eligible_after_override"
POLICY_NOTE_CONCURRENT_BLOCKERS = "concurrent_blockers_remain_after_override"
CONSENSUS_NOTE_FLIPPED = "vote_overridden_consensus_re_aggregated"
CONSENSUS_NOTE_NO_OUTPUTS = "agent_outputs_missing_or_empty"
CF_NOTE_MISSING_PROV = "decision_provenance_not_found"
CF_NOTE_NOT_DECISION_ROW = "row_is_not_decision_bearing"
CF_NOTE_TRADE_NOT_CLOSED = "linked_trade_not_closed_or_pnl_missing"
CF_NOTE_NO_SIZING_CHAIN = "linked_trade_missing_sizing_chain_json"

# Stances the consensus aggregator accepts. Mirrors
# ``backend.bot.agents.STANCES`` — re-exported here so the route
# layer can validate inbound payloads without importing agents/.
ALLOWED_STANCES: Tuple[str, ...] = (
    STANCE_BUY, STANCE_SELL, STANCE_ABSTAIN, STANCE_HOLD,
)


# ── Dataclasses (round-trippable) ─────────────────────────────────────


@dataclass
class SizingCounterfactual:
    """Linear-scaling P&L curve over a set of size multipliers.

    ``original_factor`` is the factor that produced the realized
    trade — by definition 1.0 since the sizing pipeline already
    landed on the chain's ``final_qty``. ``pnl_curve`` is the list
    of ``(factor, projected_pnl)`` pairs at each requested factor.
    """
    factors: List[float]
    original_pnl: float
    original_factor: float
    pnl_curve: List[Tuple[float, float]]
    note: str = SIZING_NOTE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "factors": list(self.factors),
            "original_pnl": float(self.original_pnl),
            "original_factor": float(self.original_factor),
            "pnl_curve": [
                [float(f), float(p)] for f, p in self.pnl_curve
            ],
            "note": self.note,
            # Gap 12 — embed code version so consumers can detect stale
            # cache rows even when the cache key still matches.
            "code_version": COUNTERFACTUAL_CODE_VERSION,
        }


@dataclass
class PolicyCounterfactual:
    """Re-evaluated policy verdict after overriding ONE blocker.

    The override does NOT re-run any rule evaluator — that would
    require rebuilding a live ``PolicyContext`` with snapshot +
    analytics + risk_manager etc. (which we don't persist). Instead
    we read the persisted ``policy_result.blocking_factors`` list
    and ask "if this rule's BlockingFactor is removed, what would
    the headline blocker be?" The semantics match
    ``DecisionPolicy.evaluate`` exactly because the policy evaluator
    itself runs every hard rule + picks the first hard
    BlockingFactor as headline.
    """
    rule_overridden: str
    original_headline_blocker: Optional[str]
    new_headline_blocker: Optional[str]
    eligible_with_override: bool
    other_blockers_still_firing: List[str]
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_overridden": self.rule_overridden,
            "original_headline_blocker": self.original_headline_blocker,
            "new_headline_blocker": self.new_headline_blocker,
            "eligible_with_override": bool(self.eligible_with_override),
            "other_blockers_still_firing": list(
                self.other_blockers_still_firing
            ),
            "note": self.note,
            # Gap 12 — code version stamp.
            "code_version": COUNTERFACTUAL_CODE_VERSION,
        }


@dataclass
class ConsensusCounterfactual:
    """Re-aggregated consensus with one agent's vote replaced.

    ``original_consensus`` + ``new_consensus`` carry the
    ``{stance, confidence, recommendation, size_multiplier}`` quad
    so the UI can compare directly. ``flipped_recommendation`` is
    True when the engine would have taken a different action (e.g.
    abstain → execute).
    """
    agent_flipped: str
    original_stance: str
    new_stance: str
    new_confidence: int
    original_consensus: Dict[str, Any]
    new_consensus: Dict[str, Any]
    flipped_recommendation: bool
    note: str = CONSENSUS_NOTE_FLIPPED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_flipped": self.agent_flipped,
            "original_stance": self.original_stance,
            "new_stance": self.new_stance,
            "new_confidence": int(self.new_confidence),
            "original_consensus": dict(self.original_consensus),
            "new_consensus": dict(self.new_consensus),
            "flipped_recommendation": bool(self.flipped_recommendation),
            "note": self.note,
            # Gap 12 — code version stamp.
            "code_version": COUNTERFACTUAL_CODE_VERSION,
        }


@dataclass
class CounterfactualResult:
    """Bundle of all three variations for one provenance id.

    Any field may be ``None`` — e.g. sizing CF on an open trade,
    policy CF when nothing blocked, consensus CF on a row without
    agent_outputs. The ``notes`` list explains every absent slot so
    the cockpit can render "policy CF n/a — original decision was
    eligible" instead of just a blank panel.
    """
    provenance_id: int
    sizing: Optional[SizingCounterfactual] = None
    policy: Optional[PolicyCounterfactual] = None
    consensus: Optional[ConsensusCounterfactual] = None
    computed_at: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provenance_id": int(self.provenance_id),
            "sizing": self.sizing.to_dict() if self.sizing else None,
            "policy": self.policy.to_dict() if self.policy else None,
            "consensus": (
                self.consensus.to_dict() if self.consensus else None
            ),
            "computed_at": self.computed_at,
            "notes": list(self.notes),
            # Gap 12 — top-level code version stamp on the bundle.
            "code_version": COUNTERFACTUAL_CODE_VERSION,
        }


# ── JSON decode helpers ───────────────────────────────────────────────


def _decode(blob: Optional[str]) -> Optional[Any]:
    if not blob:
        return None
    try:
        return json.loads(blob)
    except (TypeError, ValueError):
        return None


def _load_prov_row(prov_id: int) -> Optional[_ProvSnapshot]:
    """Single-row fetch; returns None when missing. Returns a
    detached snapshot — the caller can read fields after the session
    has closed without hitting DetachedInstanceError."""
    return _fetch_prov_snapshot(prov_id)


@dataclass
class _ProvSnapshot:
    """Detached snapshot of the fields we read off a DecisionProvenance
    row. Materialized inside the session so the dataclass survives
    session close without DetachedInstanceError surprises."""
    id: int
    trade_id: Optional[int]
    event_status: str
    ticker: str
    policy_result_json: Optional[str]
    agent_outputs_json: Optional[str]
    consensus_json: Optional[str]


@dataclass
class _TradeSnapshot:
    """Detached snapshot of the fields the sizing CF reads."""
    id: int
    pnl: Optional[float]
    status: str
    sizing_chain_json: Optional[str]


def _snapshot_prov(row: DecisionProvenance) -> _ProvSnapshot:
    """Materialize the columns we touch before the session closes.
    Cheap (small strings + one int); kills DetachedInstance bugs."""
    return _ProvSnapshot(
        id=int(row.id),
        trade_id=(int(row.trade_id) if row.trade_id is not None else None),
        event_status=str(row.event_status or ""),
        ticker=str(row.ticker or ""),
        policy_result_json=row.policy_result_json,
        agent_outputs_json=row.agent_outputs_json,
        consensus_json=row.consensus_json,
    )


def _snapshot_trade(trade: Trade) -> _TradeSnapshot:
    return _TradeSnapshot(
        id=int(trade.id),
        pnl=(float(trade.pnl) if trade.pnl is not None else None),
        status=str(trade.status or ""),
        sizing_chain_json=trade.sizing_chain_json,
    )


def _fetch_prov_snapshot(prov_id: int) -> Optional[_ProvSnapshot]:
    with session_scope() as s:
        row = s.get(DecisionProvenance, int(prov_id))
        if row is None:
            return None
        return _snapshot_prov(row)


def _fetch_trade_for_prov(prov_id: int) -> Tuple[
    Optional[_ProvSnapshot], Optional[_TradeSnapshot],
]:
    """Joint fetch — same session for the prov row + its linked trade.
    Returns (prov_snapshot, trade_snapshot); trade is None when
    ``trade_id`` is null."""
    with session_scope() as s:
        row = s.get(DecisionProvenance, int(prov_id))
        if row is None:
            return None, None
        prov_snap = _snapshot_prov(row)
        trade_snap: Optional[_TradeSnapshot] = None
        if row.trade_id is not None:
            trade = s.execute(
                select(Trade).where(Trade.id == int(row.trade_id))
            ).scalar_one_or_none()
            if trade is not None:
                trade_snap = _snapshot_trade(trade)
        return prov_snap, trade_snap


# ── Variation 1 — Sizing counterfactual ───────────────────────────────


def sizing_counterfactual(
    prov_id: int,
    factors: Optional[List[float]] = None,
) -> Optional[SizingCounterfactual]:
    """Linear P&L curve under size scaling.

    Returns ``None`` when:
      * ``prov_id`` doesn't exist OR has no linked Trade
      * The Trade isn't closed (status != 'closed' or pnl is NULL)
      * ``Trade.sizing_chain_json`` is missing / malformed

    The curve uses ``cf_pnl = trade.pnl * factor`` for each factor.
    No slippage / impact modeling — see ``SIZING_NOTE`` for the
    explicit caveat surfaced to the operator.
    """
    f_list: List[float] = (
        [float(f) for f in factors] if factors
        else list(DEFAULT_SIZING_FACTORS)
    )
    row, trade = _fetch_trade_for_prov(prov_id)
    if row is None or trade is None:
        return None
    # Honor the same closure rule the 18.A learning layer uses: status
    # == 'closed' + pnl populated. 'closed_by_reset' is a synthetic
    # cleanup status — counterfactuals over those rows would mislead.
    status = (trade.status or "").lower()
    if status != "closed" or trade.pnl is None:
        return None
    chain = _decode(trade.sizing_chain_json)
    if not isinstance(chain, dict):
        return None
    if "final_qty" not in chain and "rounded_final" not in chain:
        return None
    original_pnl = float(trade.pnl or 0.0)
    pnl_curve: List[Tuple[float, float]] = []
    for f in f_list:
        pnl_curve.append((float(f), round(original_pnl * float(f), 4)))
    return SizingCounterfactual(
        factors=f_list,
        original_pnl=round(original_pnl, 4),
        original_factor=1.0,
        pnl_curve=pnl_curve,
    )


# ── Variation 2 — Policy counterfactual ───────────────────────────────


def _persisted_blocking_factors(
    policy_result: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Pull the ``blocking_factors`` list out of the persisted
    policy_result dict. Returns an empty list when missing /
    malformed — robust to early-deploy rows that may have written a
    partial shape."""
    if not isinstance(policy_result, dict):
        return []
    bfs = policy_result.get("blocking_factors") or []
    if not isinstance(bfs, list):
        return []
    return [b for b in bfs if isinstance(b, dict)]


def _headline_from_blockers(
    blockers: List[Dict[str, Any]],
) -> Optional[str]:
    """Mirror of ``DecisionPolicyResult.headline_blocker`` over a
    persisted list — returns the ``rule`` of the first HARD blocker
    in registration order, or None when the list is empty / soft-only.
    """
    for b in blockers:
        if str(b.get("severity") or "") == "hard":
            return str(b.get("rule") or "") or None
    return None


def policy_counterfactual(
    prov_id: int, rule_name: str,
) -> Optional[PolicyCounterfactual]:
    """Re-evaluate the persisted policy result with one rule's hard
    veto removed.

    Returns ``None`` when:
      * ``prov_id`` doesn't exist
      * ``policy_result_json`` is missing or carries no
        ``blocking_factors``
      * ``rule_name`` did not actually appear as a HARD BlockingFactor
        on the original decision (this is the
        ``rule_did_not_block_original_decision`` honesty case — the
        operator gets ``None`` with the explanatory note in the result
        bundle, not a fake "we would have traded" verdict)

    The override does NOT re-execute rule evaluators. It removes the
    named rule's BlockingFactor entry from the persisted list and asks
    "what would the headline blocker be now?" Identical semantics to
    ``DecisionPolicy.evaluate`` since the evaluator collects every
    concurrent veto and picks the first hard one as headline.
    """
    row = _load_prov_row(prov_id)
    if row is None:
        return None
    policy_result = _decode(row.policy_result_json) or {}
    blockers = _persisted_blocking_factors(policy_result)
    if not blockers:
        return None
    original_headline = _headline_from_blockers(blockers)
    # The override only makes sense when ``rule_name`` is a HARD
    # blocker on the persisted result.
    target_name = str(rule_name or "").strip()
    if not target_name:
        return None
    target_hard_hits = [
        b for b in blockers
        if str(b.get("rule") or "") == target_name
        and str(b.get("severity") or "") == "hard"
    ]
    if not target_hard_hits:
        return None
    surviving = [
        b for b in blockers
        if not (
            str(b.get("rule") or "") == target_name
            and str(b.get("severity") or "") == "hard"
        )
    ]
    new_headline = _headline_from_blockers(surviving)
    other_hard = [
        str(b.get("rule") or "") for b in surviving
        if str(b.get("severity") or "") == "hard"
    ]
    eligible = new_headline is None
    note = (
        POLICY_NOTE_RULE_OVERRIDE_CLEARED if eligible
        else POLICY_NOTE_CONCURRENT_BLOCKERS
    )
    return PolicyCounterfactual(
        rule_overridden=target_name,
        original_headline_blocker=original_headline,
        new_headline_blocker=new_headline,
        eligible_with_override=eligible,
        other_blockers_still_firing=other_hard,
        note=note,
    )


# ── Variation 3 — Consensus counterfactual ────────────────────────────


def _consensus_summary(c: Any) -> Dict[str, Any]:
    """Project a Consensus dataclass (or persisted dict) into the
    compact comparison shape used by the cockpit panel. Robust to
    both the live dataclass + the json-decoded form."""
    if c is None:
        return {}
    if hasattr(c, "stance"):
        return {
            "stance": str(getattr(c, "stance", "") or ""),
            "confidence": round(
                float(getattr(c, "confidence", 0.0) or 0.0), 4,
            ),
            "recommendation": str(
                getattr(c, "recommendation", "") or "",
            ),
            "size_multiplier": round(
                float(getattr(c, "size_multiplier", 0.0) or 0.0), 4,
            ),
            "recommendation_reason": str(
                getattr(c, "recommendation_reason", "") or "",
            ),
        }
    if isinstance(c, dict):
        return {
            "stance": str(c.get("stance") or ""),
            "confidence": round(float(c.get("confidence") or 0.0), 4),
            "recommendation": str(c.get("recommendation") or ""),
            "size_multiplier": round(
                float(c.get("size_multiplier") or 0.0), 4,
            ),
            "recommendation_reason": str(
                c.get("recommendation_reason") or "",
            ),
        }
    return {}


def _override_agent_output(
    output: Dict[str, Any], *, new_stance: str, new_confidence: int,
) -> Dict[str, Any]:
    """Return a NEW dict mirroring ``output`` with the stance and
    confidence overridden. Reasoning_type is promoted to
    ``contributing`` when the override moves an
    ``insufficient_signal`` (silent) vote into an active stance —
    otherwise the rebuild contract would reject the new shape.
    A flipped confidence below the contribution floor stays
    legacy-typed so the contract doesn't reject the vote at
    rebuild time.
    """
    overridden = dict(output)
    overridden["stance"] = new_stance
    # AgentOutput.confidence is persisted as int 0..100 — keep parity.
    overridden["confidence"] = int(new_confidence)
    rt = str(overridden.get("reasoning_type") or "")
    if new_stance == STANCE_ABSTAIN:
        # Move the override into the silent state so the rebuild
        # invariant (insufficient_signal ⇒ no drivers, abstain) holds.
        overridden["reasoning_type"] = "insufficient_signal"
        overridden["supporting_factors"] = []
        overridden["concerns"] = []
        overridden["source_categories"] = []
    elif rt == "insufficient_signal":
        # Was silent, now active — promote to contributing.
        overridden["reasoning_type"] = "contributing"
    return overridden


def consensus_counterfactual(
    prov_id: int, *, agent: str, new_stance: str, new_confidence: int,
) -> Optional[ConsensusCounterfactual]:
    """Replay consensus with ONE agent's vote replaced.

    Returns ``None`` when:
      * ``prov_id`` doesn't exist
      * ``agent_outputs_json`` is missing / empty / not a list
      * No persisted agent output matches ``agent`` (case-sensitive
        match on the ``"agent"`` field of each output dict)
      * ``new_stance`` is not one of ``ALLOWED_STANCES``

    Deterministic by construction: re-uses
    ``backend.bot.decision.replay._rebuild_vote`` + ``aggregate`` with
    the same ``quorum_required`` the consensus was originally
    computed with — the only difference is the one swapped vote.
    """
    if new_stance not in ALLOWED_STANCES:
        return None
    row = _load_prov_row(prov_id)
    if row is None:
        return None
    outputs = _decode(row.agent_outputs_json) or []
    if not isinstance(outputs, list) or not outputs:
        return None
    persisted_consensus = _decode(row.consensus_json) or {}
    persisted_stance = str(persisted_consensus.get("stance") or "")
    quorum_required = int(
        persisted_consensus.get("quorum_required") or 0
    )

    target_name = str(agent or "")
    if not target_name:
        return None
    original_output: Optional[Dict[str, Any]] = None
    overridden_outputs: List[Dict[str, Any]] = []
    flipped = False
    for o in outputs:
        if not isinstance(o, dict):
            continue
        if str(o.get("agent") or "") == target_name and not flipped:
            original_output = dict(o)
            overridden_outputs.append(_override_agent_output(
                o, new_stance=new_stance, new_confidence=new_confidence,
            ))
            flipped = True
        else:
            overridden_outputs.append(dict(o))
    if not flipped or original_output is None:
        return None

    # The new vote's reasoning_type drives which consensus_stance the
    # rebuilder should align drivers to. For non-abstain overrides we
    # treat the override stance as the alignment direction so the
    # rebuilt drivers stay on the side the operator asked for.
    override_alignment = (
        new_stance if new_stance in (STANCE_BUY, STANCE_SELL)
        else persisted_stance
    )

    rebuilt_votes = []
    for o in overridden_outputs:
        if str(o.get("agent") or "") == target_name:
            v = _rebuild_vote(o, consensus_stance=override_alignment)
        else:
            v = _rebuild_vote(o, consensus_stance=persisted_stance)
        if v is not None:
            rebuilt_votes.append(v)

    new_consensus = aggregate(
        rebuilt_votes, quorum_min=quorum_required or None,
    )
    original_summary = _consensus_summary(persisted_consensus)
    new_summary = _consensus_summary(new_consensus)
    flipped_rec = (
        original_summary.get("recommendation")
        != new_summary.get("recommendation")
    )
    return ConsensusCounterfactual(
        agent_flipped=target_name,
        original_stance=str(original_output.get("stance") or ""),
        new_stance=new_stance,
        new_confidence=int(new_confidence),
        original_consensus=original_summary,
        new_consensus=new_summary,
        flipped_recommendation=flipped_rec,
    )


# ── Bundle helper ─────────────────────────────────────────────────────


def _default_consensus_target(
    outputs: List[Dict[str, Any]],
) -> Optional[Tuple[str, str, int]]:
    """Pick a sensible default agent + override for the all-in-one
    bundle. Strategy: find the first SILENT or ABSTAIN agent and
    flip it to a BUY at confidence 70. If no silent agent exists,
    flip the first contributing agent to the OPPOSITE side. Returns
    (agent_name, new_stance, new_confidence)."""
    silent = [
        o for o in outputs
        if isinstance(o, dict)
        and str(o.get("reasoning_type") or "") == "insufficient_signal"
    ]
    abstain = [
        o for o in outputs
        if isinstance(o, dict)
        and str(o.get("stance") or "").lower() == STANCE_ABSTAIN
    ]
    pick: Optional[Dict[str, Any]] = None
    if silent:
        pick = silent[0]
    elif abstain:
        pick = abstain[0]
    if pick is not None:
        agent = str(pick.get("agent") or "")
        if agent:
            return (agent, STANCE_BUY, 70)
    # Fallback: flip the first contributing buy → sell (or vice versa)
    for o in outputs:
        if not isinstance(o, dict):
            continue
        s = str(o.get("stance") or "").lower()
        agent = str(o.get("agent") or "")
        if not agent:
            continue
        if s == STANCE_BUY:
            return (agent, STANCE_SELL, 70)
        if s == STANCE_SELL:
            return (agent, STANCE_BUY, 70)
    return None


def _default_policy_target(
    policy_result: Dict[str, Any],
) -> Optional[str]:
    """Pick the headline hard-blocker name as the default policy CF
    override target — that's the "if only this rule had let me through"
    operator question."""
    blockers = _persisted_blocking_factors(policy_result)
    return _headline_from_blockers(blockers)


def compute_all_counterfactuals(prov_id: int) -> CounterfactualResult:
    """Compute all three variations using sensible defaults.

    This is the helper the cockpit's What-if panel calls. It will
    populate any variation it can and leave the rest as ``None`` with
    a note explaining the gap (e.g. ``"row_is_not_decision_bearing"``
    when ``policy_result_json`` is empty + no agent_outputs).
    """
    notes: List[str] = []
    row, trade = _fetch_trade_for_prov(prov_id)
    if row is None:
        return CounterfactualResult(
            provenance_id=int(prov_id),
            computed_at=datetime.utcnow().isoformat(),
            notes=[CF_NOTE_MISSING_PROV],
        )

    sizing: Optional[SizingCounterfactual] = None
    if trade is None or (trade.pnl is None) or (
        (trade.status or "").lower() != "closed"
    ):
        notes.append(CF_NOTE_TRADE_NOT_CLOSED)
    else:
        chain = _decode(trade.sizing_chain_json)
        if not isinstance(chain, dict):
            notes.append(CF_NOTE_NO_SIZING_CHAIN)
        else:
            sizing = sizing_counterfactual(prov_id)

    policy: Optional[PolicyCounterfactual] = None
    policy_result = _decode(row.policy_result_json) or {}
    default_policy_rule = _default_policy_target(policy_result)
    if default_policy_rule:
        policy = policy_counterfactual(prov_id, default_policy_rule)

    consensus: Optional[ConsensusCounterfactual] = None
    outputs = _decode(row.agent_outputs_json) or []
    if not isinstance(outputs, list) or not outputs:
        notes.append(CONSENSUS_NOTE_NO_OUTPUTS)
    else:
        default = _default_consensus_target(outputs)
        if default is not None:
            agent_name, stance, conf = default
            consensus = consensus_counterfactual(
                prov_id, agent=agent_name,
                new_stance=stance, new_confidence=conf,
            )

    if sizing is None and policy is None and consensus is None:
        if CF_NOTE_TRADE_NOT_CLOSED not in notes:
            notes.append(CF_NOTE_NOT_DECISION_ROW)

    return CounterfactualResult(
        provenance_id=int(prov_id),
        sizing=sizing,
        policy=policy,
        consensus=consensus,
        computed_at=datetime.utcnow().isoformat(),
        notes=notes,
    )


# ── Gap 12 — cache version helpers ─────────────────────────────────────


def get_code_version() -> str:
    """Public accessor for the current ``COUNTERFACTUAL_CODE_VERSION``.

    Consumers (cache readers, observability endpoints) import this
    rather than reaching for the constant directly so future refactors
    that move the version to a config knob don't break callers.
    """
    return str(COUNTERFACTUAL_CODE_VERSION)


def cache_version_status(
    cached_payload: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Inspect a cached counterfactual result and report whether it
    matches the current ``COUNTERFACTUAL_CODE_VERSION``.

    Returns a dict:
      * ``cached_code_version`` — the version stored on the cache row
        (None when the row pre-dates Gap 12's version stamp).
      * ``current_code_version`` — the live module constant.
      * ``cache_version_mismatch`` — True when the cache row was
        produced by a different module version OR when the row carries
        no version stamp at all (legacy cache).

    Designed for routes to call when they pull from the
    ``counterfactual_replays`` cache so the response can flag stale
    payloads to the cockpit without forcing a recompute. Cockpit can
    then decide: serve-with-warning or fall through to live recompute.
    """
    current = get_code_version()
    if not isinstance(cached_payload, dict):
        return {
            "cached_code_version": None,
            "current_code_version": current,
            "cache_version_mismatch": True,
        }
    cached_v = cached_payload.get("code_version")
    cached_str = str(cached_v) if cached_v is not None else None
    mismatch = (cached_str is None) or (cached_str != current)
    return {
        "cached_code_version": cached_str,
        "current_code_version": current,
        "cache_version_mismatch": bool(mismatch),
    }
