"""Stage-20b — Heuristic Chairman.

The Chairman is the panel's reconciliation layer. It runs AFTER the
5-agent council has voted and the Stage-20a contract is enforced. Its
job is **lossless compression + reconciliation**: surface the same
information the council produced, organized so a human (or downstream
consumer) can act on it.

Critical constraint (from the master contract): the Chairman MUST NOT
invent new signals, infer drivers the council didn't cite, or generate
free-form text beyond what an agent already wrote. Every output
sentence is a quote or concatenation of agent inputs. The Chairman is
allowed to:

  • reweight votes by dynamic per-agent weights (already done in aggregate)
  • reconcile evidence categories via Jaccard overlap
  • surface dissent explicitly (primary_dissenter + dissent_weight + share)
  • compute independent_signal_count (count of unique source_categories)
  • compute overlap_coefficient (mean pairwise Jaccard on category sets)
  • derive position_size_modifier from overlap + dissent
  • produce a decision ∈ {EXECUTE, SIZE_DOWN, MONITOR, ABSTAIN}
  • summarize by concatenating drivers/invalidators — no new prose

What the Chairman MAY NOT do:

  • invent new drivers
  • write narrative that doesn't quote an agent
  • change individual vote stances or confidences
  • re-classify a contributing vote as dissenting (or vice versa)

This module is heuristic + deterministic. Stage-21 adds a Claude
chairman behind an eval gate; the Claude path must obey the same
constraints (it's prompted with the rule list).

This module refuses to operate on legacy votes — production agents
emit structured payloads. Test fixtures using positional AgentVote
construction continue to work in ``aggregate()`` but will receive an
empty Chairman report.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from backend.bot.agents.contract import (
    KeyDriver,
    REASONING_CONTRIBUTING,
    REASONING_DISSENTING,
    REASONING_INSUFFICIENT_SIGNAL,
    SOURCE_CATEGORIES,
)


# ── decisions ───────────────────────────────────────────────────────────


DECISION_EXECUTE = "EXECUTE"
DECISION_SIZE_DOWN = "SIZE_DOWN"
DECISION_MONITOR = "MONITOR"
DECISION_ABSTAIN = "ABSTAIN"

DECISIONS = (DECISION_EXECUTE, DECISION_SIZE_DOWN,
                  DECISION_MONITOR, DECISION_ABSTAIN)


# ── evidence-correlation banding ────────────────────────────────────────


CORRELATION_INDEPENDENT = "independent"
CORRELATION_MIXED = "mixed"
CORRELATION_CORRELATED = "correlated"

# Display bands — overlap_coefficient is mean pairwise Jaccard in [0, 1].
# Tight bands so the label flips meaningfully as the council changes.
_OVERLAP_INDEPENDENT_CEILING = 0.30
_OVERLAP_CORRELATED_FLOOR = 0.60


def _evidence_correlation_label(overlap: float) -> str:
    if overlap < _OVERLAP_INDEPENDENT_CEILING:
        return CORRELATION_INDEPENDENT
    if overlap >= _OVERLAP_CORRELATED_FLOOR:
        return CORRELATION_CORRELATED
    return CORRELATION_MIXED


# ── dataclasses ─────────────────────────────────────────────────────────


@dataclass
class DissentSurface:
    """Explicit shape for council disagreement.

    - ``dissenters``: every agent whose stance opposed the consensus
      (excludes silent agents).
    - ``primary_dissenter``: the dissenter with the highest
      ``confidence * weight`` (the loudest opposing voice). ``None``
      when there are no dissenters.
    - ``dissent_weight``: sum of ``confidence * weight`` over
      dissenters.
    - ``dissent_share``: dissent_weight / (supporter_weight + dissent_weight).
      In [0, 1]. 0 = unanimous; 1 = unanimous opposite (shouldn't
      happen — that becomes the consensus). 0.50 is a panel split.
    """

    dissenters: List[str] = field(default_factory=list)
    primary_dissenter: Optional[str] = None
    dissent_weight: float = 0.0
    dissent_share: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChairmanReport:
    """Lossless reconciliation of one council vote.

    Every text field is either empty or a concatenation of agent-emitted
    strings. The Chairman never writes prose from scratch.
    """

    decision: str = DECISION_ABSTAIN
    decision_reason: str = ""              # short tag, e.g. "quorum_failure"
    conviction: float = 0.0                # weighted supporter confidence, [0, 1]

    # Disagreement
    dissent: DissentSurface = field(default_factory=DissentSurface)
    disagreement_axes: List[Dict[str, Any]] = field(default_factory=list)

    # Evidence reconciliation
    independent_signal_count: int = 0      # unique source_categories cited
    overlap_coefficient: float = 0.0       # mean pairwise Jaccard on categories
    evidence_correlation: str = CORRELATION_INDEPENDENT
    sources_cited: List[str] = field(default_factory=list)

    # Lossless summaries — concatenated agent inputs.
    bull_case: str = ""
    bear_case: str = ""
    critical_risk: str = ""
    why_now: str = ""

    # Sizing
    position_size_modifier: float = 1.0    # multiply consensus size_multiplier

    # MITS Phase 16.B — Chairman Decision Memo extras. All four fields
    # are lossless projections of existing council inputs (no new prose).
    # ``kill_condition``: highest-weight supporter invalidator, verbatim.
    # ``structured_why``: top-5 supporter driver descriptions, verbatim.
    # ``main_risk``: primary dissenter's first driver, verbatim
    # (or critical_risk fallback).
    # ``confidence_pct``: integer percent presentation of ``conviction``.
    kill_condition: Optional[str] = None
    structured_why: List[str] = field(default_factory=list)
    main_risk: Optional[str] = None
    confidence_pct: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


# ── pure helpers ────────────────────────────────────────────────────────


def _vote_categories(vote: Any) -> Set[str]:
    """Set of source_categories cited by a vote's key_drivers."""
    cats: Set[str] = set()
    for kd in getattr(vote, "key_drivers", []) or []:
        if isinstance(kd, KeyDriver):
            cats.add(kd.source_category)
        elif isinstance(kd, dict):
            c = kd.get("source_category")
            if c:
                cats.add(c)
    return cats


def _jaccard(a: Set[str], b: Set[str]) -> float:
    """Jaccard similarity over two sets. Returns 0.0 when both empty."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _mean_pairwise_jaccard(category_sets: List[Set[str]]) -> float:
    """Mean Jaccard over every unordered pair. Returns 0 for < 2 sets."""
    n = len(category_sets)
    if n < 2:
        return 0.0
    total = 0.0
    pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += _jaccard(category_sets[i], category_sets[j])
            pairs += 1
    return total / pairs if pairs else 0.0


def _is_structured(vote: Any) -> bool:
    """A vote is Chairman-eligible if it carries a structured reasoning
    type (not legacy)."""
    rt = getattr(vote, "reasoning_type", None)
    return rt in (REASONING_CONTRIBUTING, REASONING_DISSENTING,
                       REASONING_INSUFFICIENT_SIGNAL)


# ── chairman_review ────────────────────────────────────────────────────


def chairman_review(
    *,
    votes: Sequence[Any],
    consensus_stance: str,
    abstain_stance: str,
    quorum_met: bool,
    quorum_count: int,
    quorum_required: int,
) -> ChairmanReport:
    """Lossless reconciliation of a council vote.

    Pure function — no side effects, no external state, deterministic
    given the same inputs.

    Returns an empty ``ChairmanReport`` (decision=ABSTAIN, all fields
    default) when the panel has no structured votes — the Chairman
    refuses to operate on a legacy-only panel.
    """
    structured = [v for v in votes if _is_structured(v)]
    if not structured:
        # Refuses to operate on a legacy-only panel.
        return ChairmanReport(
            decision=DECISION_ABSTAIN,
            decision_reason="no_structured_votes",
        )

    # Quorum is the first gate — even with great conviction, the
    # Chairman refuses to recommend EXECUTE when the council is
    # structurally under-informed.
    if not quorum_met:
        return ChairmanReport(
            decision=DECISION_ABSTAIN,
            decision_reason="insufficient_council_quorum",
            sources_cited=[],
        )

    # Partition votes by reasoning type.
    contributing = [v for v in structured
                          if v.reasoning_type == REASONING_CONTRIBUTING]
    dissenting = [v for v in structured
                       if v.reasoning_type == REASONING_DISSENTING]
    silent = [v for v in structured
                  if v.reasoning_type == REASONING_INSUFFICIENT_SIGNAL]

    # Special case: consensus itself is abstain (majority-abstain path
    # in aggregate). There's no "winning side" to dissent from. Active
    # voters surface in disagreement_axes so the user can see who
    # wanted what, but no DissentSurface is computed (it would falsely
    # report dissent_share=1.0 against a non-decision).
    if consensus_stance == abstain_stance:
        all_voters = [v for v in contributing + dissenting
                            if v.stance != abstain_stance]
        ax = []
        for v in all_voters:
            ax.append({
                "agent": v.agent,
                "stance": v.stance,
                "confidence": round(float(v.confidence), 3),
                "reasoning": getattr(v, "reasoning", ""),
                "categories": sorted(_vote_categories(v)),
            })
        sources = sorted({c for v in structured
                                for c in _vote_categories(v)})
        return ChairmanReport(
            decision=DECISION_ABSTAIN,
            decision_reason="consensus_abstain",
            dissent=DissentSurface(),    # empty — no side to dissent from
            disagreement_axes=ax,
            sources_cited=sources,
            independent_signal_count=len(sources),
            overlap_coefficient=0.0,
            evidence_correlation=CORRELATION_INDEPENDENT,
        )

    # Among contributing votes, split by stance alignment with consensus.
    supporters: List[Any] = []
    counters: List[Any] = []
    for v in contributing + dissenting:
        if v.stance == abstain_stance:
            continue                     # contributing-but-abstain doesn't choose a side
        if v.stance == consensus_stance:
            supporters.append(v)
        else:
            counters.append(v)
    # If the panel was unanimous abstain (all contributing votes were
    # abstain stance), there's nothing to choose.
    if not supporters and not counters:
        return ChairmanReport(
            decision=DECISION_ABSTAIN,
            decision_reason="all_contributing_abstain",
            sources_cited=sorted({c for v in structured
                                       for c in _vote_categories(v)}),
        )

    def _vw(v: Any) -> float:
        return float(getattr(v, "confidence", 0.0)) * float(
            getattr(v, "weight", 1.0))

    supporter_weight = sum(_vw(v) for v in supporters)
    counter_weight = sum(_vw(v) for v in counters)
    total_decisive = supporter_weight + counter_weight

    # Dissent surface (lossless: just lists + scalars from the inputs).
    primary_dissenter = None
    if counters:
        primary_dissenter = max(counters, key=_vw).agent
    dissent_share = (counter_weight / total_decisive
                          if total_decisive > 0 else 0.0)
    dissent = DissentSurface(
        dissenters=[v.agent for v in counters],
        primary_dissenter=primary_dissenter,
        dissent_weight=round(counter_weight, 3),
        dissent_share=round(dissent_share, 3),
    )

    # Disagreement axes — quote each dissenter's reasoning verbatim.
    disagreement_axes = []
    for v in counters:
        disagreement_axes.append({
            "agent": v.agent,
            "stance": v.stance,
            "confidence": round(float(v.confidence), 3),
            "reasoning": getattr(v, "reasoning", ""),
            "categories": sorted(_vote_categories(v)),
        })

    # Category reconciliation. Combine contributing + dissenting (NOT
    # silent — they have no drivers by contract).
    cat_sets_decisive = [_vote_categories(v)
                              for v in supporters + counters
                              if _vote_categories(v)]
    overlap = _mean_pairwise_jaccard(cat_sets_decisive)
    unique_cats = sorted({c for s in cat_sets_decisive for c in s})
    independent_signal_count = len(unique_cats)
    correlation = _evidence_correlation_label(overlap)

    # Conviction = weighted-mean confidence over supporters.
    supporter_confs = [float(v.confidence) for v in supporters]
    supporter_weights = [float(v.weight) for v in supporters]
    if supporter_confs:
        w_sum = sum(supporter_weights) or 1.0
        conviction = sum(c * w for c, w in
                              zip(supporter_confs, supporter_weights)) / w_sum
    else:
        conviction = 0.0

    # Lossless summaries — bull_case = concatenation of supporters'
    # driver descriptions. bear_case = concatenation of counters'
    # driver descriptions. critical_risk = the highest-weight HIGH
    # risk_level invalidator on the supporter side (verbatim).
    def _all_driver_strings(vs: Sequence[Any]) -> List[str]:
        out: List[str] = []
        for v in vs:
            for kd in getattr(v, "key_drivers", []) or []:
                desc = (kd.description
                              if isinstance(kd, KeyDriver) else kd.get("description"))
                if desc:
                    out.append(f"{v.agent}: {desc}")
        return out

    bull_case = " | ".join(_all_driver_strings(supporters))
    bear_case = " | ".join(_all_driver_strings(counters))

    # critical_risk = highest-weight HIGH risk-level vote's first
    # invalidator (a string the agent already wrote).
    high_risk_votes = [v for v in supporters + counters
                            if getattr(v, "risk_level", "") == "HIGH"]
    critical_risk = ""
    if high_risk_votes:
        top = max(high_risk_votes, key=_vw)
        inv = getattr(top, "invalidators", []) or []
        critical_risk = f"{top.agent}: {inv[0]}" if inv else ""

    # why_now = list of time_sensitive driver descriptions (verbatim).
    why_now_parts: List[str] = []
    for v in supporters + counters:
        for kd in getattr(v, "key_drivers", []) or []:
            ts = (kd.time_sensitive if isinstance(kd, KeyDriver)
                        else kd.get("time_sensitive"))
            if ts:
                desc = (kd.description
                              if isinstance(kd, KeyDriver) else kd.get("description"))
                if desc:
                    why_now_parts.append(f"{v.agent}: {desc}")
    why_now = " | ".join(why_now_parts)

    # Position size modifier:
    #   start at 1.0
    #   subtract 0.5 * overlap (high overlap = correlated agents → less
    #     independent confirmation; cut size)
    #   subtract 1.0 * dissent_share (dissent erodes conviction)
    #   floor at 0.0
    size_mod = 1.0 - 0.5 * overlap - 1.0 * dissent_share
    size_mod = max(0.0, min(1.0, size_mod))

    # Decision logic — staged thresholds.
    if conviction < 0.45 or len(supporters) == 0:
        decision = DECISION_ABSTAIN
        reason = "low_conviction"
    elif dissent_share >= 0.50:
        decision = DECISION_ABSTAIN
        reason = "panel_split"
    elif (correlation == CORRELATION_CORRELATED
              and independent_signal_count <= 2):
        decision = DECISION_MONITOR
        reason = "correlated_evidence_thin"
    elif (dissent_share >= 0.30 or
              correlation == CORRELATION_CORRELATED or
              len(counters) >= 2):
        decision = DECISION_SIZE_DOWN
        reason = "dissent_or_overlap"
    else:
        decision = DECISION_EXECUTE
        reason = ""

    # MITS Phase 16.B — Chairman Decision Memo extras. Every value is a
    # verbatim projection of a council-emitted string; the Chairman
    # invents nothing. Computed AFTER the decision logic so the memo
    # always reflects the same partition (supporters / counters) the
    # decision used.

    # kill_condition: highest-weight supporter invalidator. Falls back
    # to critical_risk (which itself is verbatim from an agent string)
    # so the memo always has at least the dominant HIGH-risk
    # invalidator when one exists.
    kill_condition: Optional[str] = None
    supporter_invs: List[Tuple[float, str, str]] = []
    for v in supporters:
        for inv in (getattr(v, "invalidators", []) or []):
            if inv:
                supporter_invs.append((_vw(v), v.agent, inv))
    if supporter_invs:
        supporter_invs.sort(key=lambda x: -x[0])
        kill_condition = f"{supporter_invs[0][1]}: {supporter_invs[0][2]}"
    elif critical_risk:
        kill_condition = critical_risk

    # structured_why: top-5 supporter driver descriptions (by vote
    # weight), each prefixed with the emitting agent. Each entry is the
    # FIRST key_driver of that supporter — chairman doesn't aggregate
    # multiple drivers per agent into the memo, that's already the role
    # of bull_case.
    structured_why: List[str] = []
    for v in sorted(supporters, key=lambda x: -_vw(x))[:5]:
        kds = getattr(v, "key_drivers", []) or []
        if not kds:
            continue
        first = kds[0]
        desc = (first.description if isinstance(first, KeyDriver)
                else first.get("description"))
        if desc:
            structured_why.append(f"{v.agent}: {desc}")

    # main_risk: primary dissenter's first key_driver, verbatim. Falls
    # back to critical_risk when there are no dissenters but a HIGH-risk
    # supporter posted an invalidator.
    main_risk: Optional[str] = None
    if primary_dissenter:
        pd_vote = next(
            (v for v in counters if v.agent == primary_dissenter), None,
        )
        if pd_vote is not None:
            kds = getattr(pd_vote, "key_drivers", []) or []
            if kds:
                first = kds[0]
                desc = (first.description if isinstance(first, KeyDriver)
                        else first.get("description"))
                if desc:
                    main_risk = f"{pd_vote.agent}: {desc}"
    if main_risk is None and critical_risk:
        main_risk = critical_risk

    confidence_pct = int(round(float(conviction) * 100))

    return ChairmanReport(
        decision=decision,
        decision_reason=reason,
        conviction=round(conviction, 3),
        dissent=dissent,
        disagreement_axes=disagreement_axes,
        independent_signal_count=independent_signal_count,
        overlap_coefficient=round(overlap, 3),
        evidence_correlation=correlation,
        sources_cited=unique_cats,
        bull_case=bull_case,
        bear_case=bear_case,
        critical_risk=critical_risk,
        why_now=why_now,
        position_size_modifier=round(size_mod, 3),
        kill_condition=kill_condition,
        structured_why=structured_why,
        main_risk=main_risk,
        confidence_pct=confidence_pct,
    )
