"""MITS Phase 18-FU Stream A — Decision Funnel + Throughput Diagnostic.

A pure-read measurement layer over the ``decision_provenance`` ledger,
the ``policy_rule_evaluations`` per-rule ledger, and closed ``Trade``
rows. Answers the operator's central throughput question:

  "5,726 cycles in 14 days → 7 submissions → 1 closed P&L row.
   Where, exactly, did everything else die?"

The module exposes four structured surfaces packaged into one
``FunnelReport``:

  1. **10-stage funnel** — for each stage, ``(n_passed, n_dropped,
     pass_rate, top_3_drop_reasons)``. Stage 7-8 honestly parse
     ``policy_result.blocking_factors`` rather than the headline
     ``event_status`` so concurrent vetoes surface, not just the
     winner.
  2. **Confidence histogram** — bin counts for ALL decisions, the
     subset where Brain produced a non-HOLD signal, and the subset
     that submitted. Tells the operator where in the confidence
     distribution submissions concentrate.
  3. **Cooldown audit** — n_cooldown_hits + n_lost_opportunities
     (subset of cooldown windows where a high-confidence setup
     was active and got squelched).
  4. **Counterfactual histogram** — over the last 1000 HOLD
     decisions, "what would the headline blocker have been if
     ``signal_hold`` had not fired?" Tally of new_headline_blocker
     by rule name.

The compute is read-only. NO threshold changes are ever applied.
Every recommendation surfaced via ``top_surgical_change_candidate``
is ADVISORY — the operator decides whether to act on it.

Honesty caveats baked into every report (surfaced via ``notes``):

  * Stage 2 (analysis_candidate) is sparsely populated: the engine
    only writes ``strategy_matrix_json`` on the EOD analysis path
    today (lazy build in /analysis + /strategy/matrix). ~54% of
    provenance rows lack it. The stage's ``note`` carries this
    caveat verbatim so the operator never reads a misleading rate.
  * Cooldown audit cannot reconstruct intra-cooldown opportunities
    with perfect fidelity — we approximate "would have produced a
    high-confidence setup" by checking the same ticker's provenance
    rows within the cooldown window for ``composite_quality_score >=
    cooldown_lost_opportunity_composite_threshold``. The ``notes``
    carries this caveat.
  * Counterfactual histogram uses
    ``backend.bot.learning.counterfactual.policy_counterfactual``;
    rows where signal_hold was NOT the original headline blocker
    (or where ``policy_result_json`` is missing) are skipped — the
    aggregate ``n_decisions_analyzed`` carries the true denominator.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from backend.bot.learning.counterfactual import policy_counterfactual
from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance
from backend.models.policy_rule_evaluation import PolicyRuleEvaluation
from backend.models.trade import Trade


logger = logging.getLogger(__name__)


# ── Public knobs (config-driven; no magic numbers in the math) ─────────

# Bin edges for the confidence histogram. 11 edges → 10 bins of width
# 0.1 from 0.0 to 1.0. Used uniformly for the three series (all /
# non-hold / submitted) so the cockpit overlays cleanly.
CONFIDENCE_BIN_EDGES: Tuple[float, ...] = tuple(
    round(x / 10.0, 1) for x in range(0, 11)
)

# Number of HOLD decisions to sample for the counterfactual histogram.
# 1000 is enough to bracket the top-3 candidate new blockers with a
# tight CI; cheap to compute (just JSON reads, no aggregator re-run).
COUNTERFACTUAL_SAMPLE_SIZE: int = 1000

# Cooldown TTL — mirrors ``Engine._brain_cooldown_seconds`` (10 min).
# Lifted here as a constant so the funnel logic doesn't import the
# Engine class. Confirmed at engine.py:148.
DEFAULT_BRAIN_COOLDOWN_SECONDS: float = 600.0

# Composite-quality threshold above which a cooldown firing is flagged
# as a "lost opportunity". 60 = mid of the bin range; below the EOD
# top-grade band (≥70) but well above the historical median (47.27).
COOLDOWN_LOST_OPPORTUNITY_COMPOSITE_THRESHOLD: float = 60.0

# Surgical-change advisory thresholds. All ADVISORY only — never
# auto-applied. See ``_derive_surgical_change_candidate``.
SURGICAL_DOMINANT_BLOCKER_MIN: int = 500
SURGICAL_LOW_CONFIDENCE_MIN: int = 200
SURGICAL_CONSENSUS_ABSTAIN_MIN: int = 200


# ── Dataclasses (round-trippable; consumer reads to_dict) ──────────────


@dataclass
class FunnelStage:
    """One stage of the 10-stage decision funnel.

    ``n_decisions`` is the cohort that entered the stage (= n_passed
    of the prior stage; equal to the window total for stage 1).
    ``n_passed`` is the subset that progressed; ``n_dropped`` is
    ``n_decisions - n_passed``. ``pass_rate`` is
    ``n_passed / n_decisions`` rounded to 4 decimal places (None
    when n_decisions == 0). ``top_3_drop_reasons`` is the most-common
    rule names that caused this stage's drops, tallied from
    ``policy_result.blocking_factors`` — concurrent vetoes count
    separately so the operator sees the real picture.
    """
    name: str
    n_decisions: int
    n_passed: int
    n_dropped: int
    pass_rate: Optional[float]
    top_3_drop_reasons: List[Dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "n_decisions": int(self.n_decisions),
            "n_passed": int(self.n_passed),
            "n_dropped": int(self.n_dropped),
            "pass_rate": (
                None if self.pass_rate is None
                else round(float(self.pass_rate), 4)
            ),
            "top_3_drop_reasons": list(self.top_3_drop_reasons),
            "note": self.note,
        }


@dataclass
class ConfidenceHistogram:
    """Per-bin counts of decisions by consensus confidence.

    ``bin_edges`` has length n+1 where n is the count length. The
    three count series share the same bin edges so the consumer
    can overlay them as three lines on one axis.
    """
    bin_edges: List[float]
    all_evals: List[int]
    non_hold: List[int]
    submitted: List[int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bin_edges": list(self.bin_edges),
            "all_evals": list(self.all_evals),
            "non_hold": list(self.non_hold),
            "submitted": list(self.submitted),
        }


@dataclass
class CooldownAudit:
    """Counts of brain-cooldown firings + the subset where a
    high-confidence setup was active within the cooldown window.

    ``affected_tickers`` is the de-duplicated list of tickers that
    had at least one lost-opportunity hit, capped at 50 so the
    payload stays bounded.
    """
    n_cooldown_hits: int
    n_lost_opportunities: int
    affected_tickers: List[str]
    avg_cooldown_seconds: float
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_cooldown_hits": int(self.n_cooldown_hits),
            "n_lost_opportunities": int(self.n_lost_opportunities),
            "affected_tickers": list(self.affected_tickers),
            "avg_cooldown_seconds": round(
                float(self.avg_cooldown_seconds), 1,
            ),
            "note": self.note,
        }


@dataclass
class CounterfactualHistogram:
    """Counterfactual: "if signal_hold had not fired, what WOULD have
    been the headline blocker?" Tallied over the last
    ``COUNTERFACTUAL_SAMPLE_SIZE`` HOLD-flagged decisions where
    signal_hold was indeed the original headline blocker.

    ``n_decisions_analyzed`` is the actual count we ran the
    counterfactual on (may be lower than the sample size when
    rows lack ``policy_result_json``).
    ``eligible_after_override`` is the count where REMOVING
    signal_hold cleared all hard blockers — pure throughput gain.
    """
    rule_overridden: str
    n_decisions_analyzed: int
    new_headline_blocker_counts: Dict[str, int]
    eligible_after_override: int
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_overridden": self.rule_overridden,
            "n_decisions_analyzed": int(self.n_decisions_analyzed),
            "new_headline_blocker_counts": dict(
                self.new_headline_blocker_counts
            ),
            "eligible_after_override": int(self.eligible_after_override),
            "note": self.note,
        }


@dataclass
class FunnelReport:
    """Complete funnel diagnostic for one window.

    Always carries ``window_days`` so the consumer can label the
    panel. ``computed_at`` is an ISO timestamp for cache-busting.
    ``top_surgical_change_candidate`` is the operator-facing
    recommendation derived from the counterfactual histogram — see
    ``_derive_surgical_change_candidate`` for the decision tree.
    """
    window_days: int
    window_start: str
    window_end: str
    watchlist_size: Optional[int]
    stages: List[FunnelStage]
    confidence_histograms: ConfidenceHistogram
    cooldown_audit: CooldownAudit
    counterfactual: CounterfactualHistogram
    top_surgical_change_candidate: Dict[str, Any]
    composite_quality_mean: Optional[float]
    composite_quality_median: Optional[float]
    computed_at: str
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_days": int(self.window_days),
            "window_start": self.window_start,
            "window_end": self.window_end,
            "watchlist_size": self.watchlist_size,
            "stages": [s.to_dict() for s in self.stages],
            "confidence_histograms": self.confidence_histograms.to_dict(),
            "cooldown_audit": self.cooldown_audit.to_dict(),
            "counterfactual": self.counterfactual.to_dict(),
            "top_surgical_change_candidate": dict(
                self.top_surgical_change_candidate
            ),
            "composite_quality_mean": self.composite_quality_mean,
            "composite_quality_median": self.composite_quality_median,
            "computed_at": self.computed_at,
            "notes": list(self.notes),
        }


# ── JSON decode helpers ───────────────────────────────────────────────


def _decode(blob: Optional[str]) -> Optional[Any]:
    if not blob:
        return None
    try:
        return json.loads(blob)
    except (TypeError, ValueError):
        return None


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _confidence_bin_index(confidence: float) -> int:
    """Map a confidence in [0, 1] to its bin index in
    [0, len(bin_edges)-2]. Confidences ≥ 1.0 fall in the last bin.
    """
    if confidence >= 1.0:
        return len(CONFIDENCE_BIN_EDGES) - 2
    if confidence < 0.0:
        return 0
    return int(confidence * 10)


def _blocking_rules(policy_result: Any) -> List[str]:
    """Pull every BlockingFactor's ``rule`` field from a persisted
    policy_result dict. Returns rule names in order, including
    soft ones — caller filters by severity if needed.
    """
    if not isinstance(policy_result, dict):
        return []
    bfs = policy_result.get("blocking_factors") or []
    if not isinstance(bfs, list):
        return []
    return [
        str(b.get("rule") or "")
        for b in bfs
        if isinstance(b, dict) and b.get("rule")
    ]


def _hard_blocking_rules(policy_result: Any) -> List[str]:
    """Like ``_blocking_rules`` but filters to ``severity == 'hard'``
    so soft penalties (0% sizing) don't pollute the funnel drop tally.
    """
    if not isinstance(policy_result, dict):
        return []
    bfs = policy_result.get("blocking_factors") or []
    if not isinstance(bfs, list):
        return []
    return [
        str(b.get("rule") or "")
        for b in bfs
        if isinstance(b, dict)
        and b.get("rule")
        and str(b.get("severity") or "") == "hard"
    ]


def _has_non_hold_vote(agent_outputs: Any) -> bool:
    """True if at least one persisted AgentOutput has a stance other
    than ``hold`` / ``abstain``."""
    if not isinstance(agent_outputs, list):
        return False
    for o in agent_outputs:
        if not isinstance(o, dict):
            continue
        stance = str(o.get("stance") or "").lower()
        if stance in {"buy", "sell"}:
            return True
    return False


# ── Snapshot loader (one DB round-trip per compute) ────────────────────


@dataclass
class _ProvSnapshot:
    """Detached projection of the prov columns we read. Materialized
    inside the session so the records survive session close without
    DetachedInstance surprises."""
    id: int
    trade_id: Optional[int]
    event_status: str
    ticker: str
    decision_timestamp: datetime
    strategy_matrix: Optional[Dict[str, Any]]
    agent_outputs: List[Dict[str, Any]]
    consensus: Optional[Dict[str, Any]]
    policy_result: Optional[Dict[str, Any]]
    simulator_verdict: Optional[Dict[str, Any]]
    correlation_cap: Optional[Dict[str, Any]]
    composite_quality: Optional[float]


def _load_prov_snapshots(
    *, window_start: datetime, window_end: datetime,
) -> List[_ProvSnapshot]:
    """Fetch every provenance row in the window with a single query.

    Returns DETACHED snapshots — the columns we care about are
    materialized into a plain dataclass so the funnel compute can
    iterate them outside the session.
    """
    snaps: List[_ProvSnapshot] = []
    with session_scope() as s:
        rows = s.execute(
            select(DecisionProvenance)
            .where(
                DecisionProvenance.decision_timestamp >= window_start,
            )
            .where(
                DecisionProvenance.decision_timestamp < window_end,
            )
            .order_by(DecisionProvenance.decision_timestamp.asc())
        ).scalars().all()
        for row in rows:
            sm = _decode(row.strategy_matrix_json)
            ag = _decode(row.agent_outputs_json) or []
            if not isinstance(ag, list):
                ag = []
            cons = _decode(row.consensus_json)
            pol = _decode(row.policy_result_json)
            sim = _decode(row.simulator_verdict_json)
            corr = _decode(row.correlation_cap_json)
            dqs = _decode(row.decision_quality_score_json)
            comp = None
            if isinstance(dqs, dict):
                comp = _safe_float(dqs.get("composite"))
            snaps.append(_ProvSnapshot(
                id=int(row.id),
                trade_id=(
                    int(row.trade_id) if row.trade_id is not None else None
                ),
                event_status=str(row.event_status or ""),
                ticker=str(row.ticker or ""),
                decision_timestamp=row.decision_timestamp,
                strategy_matrix=sm if isinstance(sm, dict) else None,
                agent_outputs=ag,
                consensus=cons if isinstance(cons, dict) else None,
                policy_result=pol if isinstance(pol, dict) else None,
                simulator_verdict=(
                    sim if isinstance(sim, dict) else None
                ),
                correlation_cap=(
                    corr if isinstance(corr, dict) else None
                ),
                composite_quality=comp,
            ))
    return snaps


def _load_closed_trade_count(
    *, window_start: datetime, window_end: datetime,
    submitted_trade_ids: List[int],
) -> int:
    """Count trades that have ``status='closed'`` AND ``pnl IS NOT NULL``
    and whose id is in the submitted set. We anchor by the prov.trade_id
    list rather than the trade.created_at window so we honestly count
    decisions-in-window that REACHED closure (regardless of when the
    close fired)."""
    if not submitted_trade_ids:
        return 0
    with session_scope() as s:
        rows = s.execute(
            select(Trade.id, Trade.status, Trade.pnl)
            .where(Trade.id.in_(submitted_trade_ids))
        ).all()
    n = 0
    for _id, status, pnl in rows:
        st = str(status or "").lower()
        if st == "closed" and pnl is not None:
            n += 1
    return n


# ── Stage compute (the 10-stage funnel) ────────────────────────────────


def _stage_passes(
    snaps: List[_ProvSnapshot],
) -> Tuple[List[FunnelStage], List[int]]:
    """Walk each provenance snapshot through the 10-stage funnel and
    return the list of FunnelStage rollups plus the list of trade_ids
    that REACHED the ``submitted`` stage (used downstream by the
    closed-with-pnl count).
    """
    # Pre-compute each snap's per-stage pass flags. Stage 1 is "the
    # cohort that entered the window" — every snap qualifies. Each
    # later stage filters down. We also track the drop-reason rule
    # tally per stage by reading hard_blocking_rules off snaps that
    # FAIL the gate but ARE present in the prior cohort.
    n_total = len(snaps)

    # Stage 1 — watchlist_evaluated.
    pass_1 = [True] * n_total

    # Stage 2 — analysis_candidate: strategy_matrix_json non-null AND
    # at least one candidate. Sparse coverage today (only EOD-injected
    # rows). Stage carries a caveat note.
    pass_2: List[bool] = []
    for snap in snaps:
        sm = snap.strategy_matrix
        if not isinstance(sm, dict):
            pass_2.append(False)
            continue
        cands = sm.get("candidates")
        if not isinstance(cands, list) or len(cands) == 0:
            # Some matrix shapes use "ranked" instead of "candidates";
            # check both so we don't undercount.
            ranked = sm.get("ranked") or []
            if isinstance(ranked, list) and len(ranked) > 0:
                pass_2.append(True)
            else:
                pass_2.append(False)
        else:
            pass_2.append(True)

    # Stage 3 — brain_non_hold: consensus.recommendation != 'abstain' OR
    # any AgentOutput.stance in {buy, sell}. The OR is deliberate: a
    # row with non-empty agent_outputs but blocked-before-consensus
    # (consensus_json missing) should still count as "Brain produced
    # signals" iff at least one agent took an actionable stance.
    pass_3: List[bool] = []
    for snap in snaps:
        cons = snap.consensus or {}
        rec = str(cons.get("recommendation") or "").lower()
        if rec and rec != "abstain":
            pass_3.append(True)
            continue
        if _has_non_hold_vote(snap.agent_outputs):
            pass_3.append(True)
            continue
        pass_3.append(False)

    # Stage 4 — policy_eligible: policy_result.eligible == True.
    pass_4: List[bool] = []
    for snap in snaps:
        pol = snap.policy_result or {}
        pass_4.append(bool(pol.get("eligible")))

    # Stage 5 — consensus_quorum_met: consensus.quorum_met == True OR
    # consensus.consensus_strength == 'quorum_met' / similar. Use a
    # tolerant check — older provenance rows may carry slightly
    # different shapes. When ``quorum_met`` is absent we fall back to
    # the rule of "consensus.stance is buy/sell/hold AND quorum_required
    # ≤ contributing_count".
    pass_5: List[bool] = []
    for snap in snaps:
        cons = snap.consensus or {}
        if "quorum_met" in cons:
            pass_5.append(bool(cons.get("quorum_met")))
            continue
        qreq = cons.get("quorum_required") or 0
        ccount = cons.get("contributing_count") or cons.get("n_contributing")
        try:
            qreq_i = int(qreq or 0)
            ccount_i = int(ccount or 0)
            pass_5.append(ccount_i >= qreq_i and qreq_i > 0)
        except (TypeError, ValueError):
            pass_5.append(False)

    # Stage 6 — consensus_non_abstain: consensus.recommendation !=
    # 'abstain' AND non-empty.
    pass_6: List[bool] = []
    for snap in snaps:
        cons = snap.consensus or {}
        rec = str(cons.get("recommendation") or "").lower()
        pass_6.append(bool(rec) and rec != "abstain")

    # Stage 7 — risk_passed: no 'risk_manager_rejected' / 'kill_switch_active'
    # in the persisted blocking_factors (HARD only — soft penalties
    # don't block).
    pass_7: List[bool] = []
    for snap in snaps:
        rules = set(_hard_blocking_rules(snap.policy_result))
        if "risk_manager_rejected" in rules or "kill_switch_active" in rules:
            pass_7.append(False)
        else:
            pass_7.append(True)

    # Stage 8 — simulator_passed: no 'simulator_veto' in blocking_factors
    # AND no 'correlation_cap_block' (treat both as portfolio-level
    # risk gates the simulator stage owns).
    pass_8: List[bool] = []
    for snap in snaps:
        rules = set(_hard_blocking_rules(snap.policy_result))
        if "simulator_veto" in rules or "correlation_cap_block" in rules:
            pass_8.append(False)
        else:
            pass_8.append(True)

    # Stage 9 — submitted: event_status == 'submitted'.
    pass_9: List[bool] = []
    for snap in snaps:
        pass_9.append(str(snap.event_status or "").lower() == "submitted")

    # Stage 10 — closed_with_pnl: we count downstream after the
    # submitted set is known. Initialize as False; the report builder
    # patches the count after the trade lookup.
    submitted_trade_ids: List[int] = []
    for i, snap in enumerate(snaps):
        if pass_9[i] and snap.trade_id is not None:
            submitted_trade_ids.append(int(snap.trade_id))

    # Build the FunnelStage rollups. For stages 2-9 the
    # ``top_3_drop_reasons`` tally walks the hard_blocking_rules of
    # snaps that ENTERED the stage but FAILED it. Stage 1 has no
    # drop reasons by construction (nothing was filtered before it).
    def _stage(
        name: str,
        prev_pass: List[bool],
        this_pass: List[bool],
        *,
        tally_drops: bool = True,
        custom_drop_filter=None,
    ) -> FunnelStage:
        n_decisions = sum(1 for p in prev_pass if p)
        n_passed = 0
        drop_tally: Counter = Counter()
        for i, snap in enumerate(snaps):
            if not prev_pass[i]:
                continue
            if this_pass[i]:
                n_passed += 1
            elif tally_drops:
                if custom_drop_filter is not None:
                    for rule in custom_drop_filter(snap):
                        drop_tally[rule] += 1
                else:
                    for rule in _hard_blocking_rules(snap.policy_result):
                        drop_tally[rule] += 1
        n_dropped = n_decisions - n_passed
        pass_rate = (
            (n_passed / n_decisions) if n_decisions > 0 else None
        )
        top_3 = [
            {"rule": r, "n": n}
            for r, n in drop_tally.most_common(3)
        ]
        return FunnelStage(
            name=name,
            n_decisions=n_decisions,
            n_passed=n_passed,
            n_dropped=n_dropped,
            pass_rate=pass_rate,
            top_3_drop_reasons=top_3,
        )

    stage_1 = _stage(
        "watchlist_evaluated", pass_1, pass_1, tally_drops=False,
    )
    stage_2 = _stage(
        "analysis_candidate", pass_1, pass_2,
        # Stage 2 drops are "no strategy_matrix" — not a rule veto.
        tally_drops=False,
    )
    stage_2.note = (
        "strategy_matrix_json is populated only on the EOD analysis "
        "path today; rate is a lower bound on real analysis breadth"
    )

    stage_3 = _stage(
        "brain_non_hold", pass_2, pass_3,
        custom_drop_filter=lambda s: ["signal_hold"]
        if str(s.event_status or "").lower() == "hold"
        else _hard_blocking_rules(s.policy_result),
    )

    stage_4 = _stage("policy_eligible", pass_3, pass_4)
    stage_5 = _stage("consensus_quorum_met", pass_4, pass_5)
    stage_6 = _stage("consensus_non_abstain", pass_5, pass_6)
    stage_7 = _stage("risk_passed", pass_6, pass_7)
    stage_8 = _stage("simulator_passed", pass_7, pass_8)
    stage_9 = _stage("submitted", pass_8, pass_9)
    # Stage 10 — closed_with_pnl. Built without a per-snap drop tally
    # since "didn't close yet" is not a rule veto.
    stage_10 = FunnelStage(
        name="closed_with_pnl",
        n_decisions=stage_9.n_passed,
        n_passed=0,  # patched below by the report builder
        n_dropped=stage_9.n_passed,
        pass_rate=None,
        top_3_drop_reasons=[],
        note=(
            "drops at this stage are 'trade open / partially filled / "
            "missing pnl' — not a rule veto"
        ),
    )

    return (
        [
            stage_1, stage_2, stage_3, stage_4, stage_5,
            stage_6, stage_7, stage_8, stage_9, stage_10,
        ],
        submitted_trade_ids,
    )


# ── Confidence histogram ──────────────────────────────────────────────


def _build_confidence_histogram(
    snaps: List[_ProvSnapshot],
) -> ConfidenceHistogram:
    """Per-bin counts for ALL evals, non_hold subset, and submitted
    subset. Reads ``consensus.confidence`` when present; falls back
    to ``consensus.confidence_pct / 100`` when the bot stored the
    percentage form."""
    n_bins = len(CONFIDENCE_BIN_EDGES) - 1
    all_evals = [0] * n_bins
    non_hold = [0] * n_bins
    submitted = [0] * n_bins
    for snap in snaps:
        cons = snap.consensus or {}
        conf = _safe_float(cons.get("confidence"))
        if conf is None:
            pct = _safe_float(cons.get("confidence_pct"))
            if pct is not None:
                conf = pct / 100.0
        if conf is None:
            continue
        # Clamp to [0, 1] before binning.
        if conf < 0.0:
            conf = 0.0
        if conf > 1.0:
            conf = 1.0
        idx = _confidence_bin_index(conf)
        all_evals[idx] += 1
        if _has_non_hold_vote(snap.agent_outputs):
            non_hold[idx] += 1
        if str(snap.event_status or "").lower() == "submitted":
            submitted[idx] += 1
    return ConfidenceHistogram(
        bin_edges=list(CONFIDENCE_BIN_EDGES),
        all_evals=all_evals,
        non_hold=non_hold,
        submitted=submitted,
    )


# ── Cooldown audit ─────────────────────────────────────────────────────


def _build_cooldown_audit(
    *,
    snaps: List[_ProvSnapshot],
    window_start: datetime,
    window_end: datetime,
    cooldown_seconds: float = DEFAULT_BRAIN_COOLDOWN_SECONDS,
    composite_threshold: float = (
        COOLDOWN_LOST_OPPORTUNITY_COMPOSITE_THRESHOLD
    ),
) -> CooldownAudit:
    """Audit brain_cooldown firings from policy_rule_evaluations + the
    subset where a high-confidence setup was active inside the cooldown
    window.

    Lost-opportunity logic: for each cooldown firing on ticker T at
    time t0, look at ALL provenance rows for the same ticker with
    decision_timestamp in [t0, t0 + cooldown_seconds]. If any of those
    rows has ``composite_quality >= composite_threshold`` or contains
    at least one non-hold agent vote with confidence >= 0.65, count
    the cooldown as a lost opportunity.
    """
    # 1) Pull cooldown firings from policy_rule_evaluations.
    firings: List[Tuple[str, datetime]] = []
    with session_scope() as s:
        rows = s.execute(
            select(
                PolicyRuleEvaluation.ticker,
                PolicyRuleEvaluation.evaluated_at,
            )
            .where(PolicyRuleEvaluation.rule_name == "brain_cooldown")
            .where(PolicyRuleEvaluation.blocked.is_(True))
            .where(PolicyRuleEvaluation.evaluated_at >= window_start)
            .where(PolicyRuleEvaluation.evaluated_at < window_end)
        ).all()
        for ticker, evaluated_at in rows:
            firings.append((str(ticker or "").upper(), evaluated_at))
    n_hits = len(firings)

    if n_hits == 0:
        return CooldownAudit(
            n_cooldown_hits=0,
            n_lost_opportunities=0,
            affected_tickers=[],
            avg_cooldown_seconds=float(cooldown_seconds),
            note="no brain_cooldown firings in window",
        )

    # 2) Group snaps by ticker for fast cooldown-window lookup.
    snaps_by_ticker: Dict[str, List[_ProvSnapshot]] = {}
    for snap in snaps:
        if not snap.ticker:
            continue
        snaps_by_ticker.setdefault(snap.ticker.upper(), []).append(snap)

    # 3) For each firing, check the cooldown window for a
    #    high-confidence setup.
    lost_tickers: Counter = Counter()
    n_lost = 0
    for ticker, t0 in firings:
        t1 = t0 + timedelta(seconds=float(cooldown_seconds))
        cohort = snaps_by_ticker.get(ticker, [])
        had_setup = False
        for snap in cohort:
            if snap.decision_timestamp is None:
                continue
            if snap.decision_timestamp <= t0:
                continue
            if snap.decision_timestamp > t1:
                continue
            # Two parallel checks: composite_quality_score OR
            # high-confidence non-hold vote.
            if (
                snap.composite_quality is not None
                and snap.composite_quality >= composite_threshold
            ):
                had_setup = True
                break
            for o in snap.agent_outputs:
                if not isinstance(o, dict):
                    continue
                stance = str(o.get("stance") or "").lower()
                if stance not in {"buy", "sell"}:
                    continue
                conf_raw = _safe_float(o.get("confidence"))
                if conf_raw is None:
                    continue
                # AgentOutput.confidence is persisted 0..100.
                conf = (
                    conf_raw / 100.0 if conf_raw > 1.0
                    else conf_raw
                )
                if conf >= 0.65:
                    had_setup = True
                    break
            if had_setup:
                break
        if had_setup:
            n_lost += 1
            lost_tickers[ticker] += 1

    affected = [t for t, _ in lost_tickers.most_common(50)]
    note = (
        "lost-opportunity detection is approximate: relies on a "
        "follow-up provenance row landing inside the cooldown window "
        "for the same ticker; a setup that never produced a prov row "
        "due to the cooldown itself is invisible"
    )
    return CooldownAudit(
        n_cooldown_hits=n_hits,
        n_lost_opportunities=n_lost,
        affected_tickers=affected,
        avg_cooldown_seconds=float(cooldown_seconds),
        note=note,
    )


# ── Counterfactual histogram ──────────────────────────────────────────


def _build_counterfactual_histogram(
    snaps: List[_ProvSnapshot],
    *,
    rule_overridden: str = "signal_hold",
    sample_size: int = COUNTERFACTUAL_SAMPLE_SIZE,
) -> CounterfactualHistogram:
    """For the most-recent N HOLD decisions, run policy_counterfactual
    against ``rule_overridden`` and tally the new_headline_blocker.

    "HOLD decision" = event_status == 'hold' OR
    signal_hold present in hard_blocking_rules. We pick the LATEST
    rows first (sort by decision_timestamp desc) since the operator
    cares about recency.
    """
    # Build the candidate list — provenance rows where signal_hold is
    # the original headline OR rows where event_status='hold' (the
    # signal_hold rule's BlockingFactor sets legacy_status='hold').
    candidates: List[_ProvSnapshot] = []
    for snap in snaps:
        rules = _hard_blocking_rules(snap.policy_result)
        if rule_overridden in rules:
            candidates.append(snap)
            continue
        if str(snap.event_status or "").lower() == "hold":
            # Some early-pipeline HOLDs may not carry a full policy
            # result (e.g. blocked before policy evaluation finished).
            # Skip those — we only counterfactual rows that actually
            # had the named rule fire as a hard blocker.
            if rule_overridden in rules:
                candidates.append(snap)
    # Sort newest-first then truncate.
    candidates.sort(
        key=lambda s: s.decision_timestamp or datetime.min, reverse=True,
    )
    candidates = candidates[:sample_size]

    new_blocker_counts: Counter = Counter()
    eligible_after = 0
    n_analyzed = 0
    for snap in candidates:
        cf = policy_counterfactual(snap.id, rule_overridden)
        if cf is None:
            continue
        n_analyzed += 1
        if cf.eligible_with_override:
            eligible_after += 1
            new_blocker_counts["__eligible__"] += 1
            continue
        key = cf.new_headline_blocker or "__no_new_headline__"
        new_blocker_counts[key] += 1

    note = (
        "counterfactual semantics mirror DecisionPolicy.evaluate — "
        "removing the named rule's BlockingFactor reveals the next "
        "concurrent veto in registration order"
    )
    return CounterfactualHistogram(
        rule_overridden=rule_overridden,
        n_decisions_analyzed=n_analyzed,
        new_headline_blocker_counts=dict(new_blocker_counts),
        eligible_after_override=eligible_after,
        note=note,
    )


# ── Surgical change recommendation ────────────────────────────────────


def _derive_surgical_change_candidate(
    *,
    counterfactual: CounterfactualHistogram,
    cooldown_audit: CooldownAudit,
) -> Dict[str, Any]:
    """Decision tree over the counterfactual histogram.

    Branch 1: ``eligible_after_override`` >= SURGICAL_DOMINANT_BLOCKER_MIN
    → ``signal_hold`` is the dominant single blocker.
    Branch 2: ``new_headline_blocker_counts['low_confidence']`` is the
    top blocker with count > SURGICAL_LOW_CONFIDENCE_MIN → confidence
    distribution is the bottleneck.
    Branch 3: ``consensus_abstain`` is top with count >
    SURGICAL_CONSENSUS_ABSTAIN_MIN → quorum / silence is the bottleneck.
    Else: nothing dominates; recommend "no single surgical change"
    + a read-only investigation pointer.

    The recommendation is ADVISORY ONLY — payload carries
    ``auto_apply: False`` and a ``investigation`` field describing the
    read-only follow-up the operator should run.
    """
    counts = dict(counterfactual.new_headline_blocker_counts)
    # Exclude the "__eligible__" sentinel from the top-N tally — it's
    # the "no new blocker, would have traded" bucket.
    real = {k: v for k, v in counts.items() if not k.startswith("__")}
    eligible = int(counterfactual.eligible_after_override)

    if eligible >= SURGICAL_DOMINANT_BLOCKER_MIN:
        return {
            "candidate": "fix_brain_hold_bias",
            "rationale": (
                f"removing signal_hold would have left "
                f"{eligible} decisions eligible cleanly (≥ "
                f"{SURGICAL_DOMINANT_BLOCKER_MIN}); the Brain's HOLD "
                "default is the throughput bottleneck"
            ),
            "evidence": {
                "eligible_after_override": eligible,
                "threshold": SURGICAL_DOMINANT_BLOCKER_MIN,
            },
            "auto_apply": False,
            "investigation": (
                "audit the AI Brain confidence + HOLD-trigger logic; "
                "compare HOLD rate vs base-rate expectation for the "
                "trial window; do NOT change min_confidence until the "
                "root cause is understood"
            ),
            "severity": "high",
        }

    if real:
        sorted_real = sorted(
            real.items(), key=lambda kv: kv[1], reverse=True,
        )
        top_rule, top_count = sorted_real[0]
        if (
            top_rule == "low_confidence"
            and top_count > SURGICAL_LOW_CONFIDENCE_MIN
        ):
            return {
                "candidate": "investigate_confidence_distribution",
                "rationale": (
                    f"{top_count} decisions would still be blocked by "
                    "low_confidence after removing signal_hold; the "
                    "min_confidence threshold sits adjacent to where "
                    "the confidence mass lives"
                ),
                "evidence": {
                    "new_headline_blocker_counts": dict(sorted_real[:5]),
                    "threshold": SURGICAL_LOW_CONFIDENCE_MIN,
                },
                "auto_apply": False,
                "investigation": (
                    "read /learning/funnel confidence_histograms; "
                    "compare the all_evals series mass to the 0.40 / "
                    "0.50 / 0.60 bin edges; do NOT change "
                    "min_confidence until the distribution shape is "
                    "understood"
                ),
                "severity": "medium",
            }
        if (
            top_rule == "consensus_abstain"
            and top_count > SURGICAL_CONSENSUS_ABSTAIN_MIN
        ):
            return {
                "candidate": "investigate_quorum_and_silence",
                "rationale": (
                    f"{top_count} decisions would still be blocked by "
                    "consensus_abstain after removing signal_hold; "
                    "either quorum is unreachable or too many agents "
                    "are silent"
                ),
                "evidence": {
                    "new_headline_blocker_counts": dict(sorted_real[:5]),
                    "threshold": SURGICAL_CONSENSUS_ABSTAIN_MIN,
                },
                "auto_apply": False,
                "investigation": (
                    "audit /learning/attribution/agents for "
                    "insufficient_sample_size flags; check agent "
                    "silence rate via reasoning_type=insufficient_signal "
                    "tally; do NOT change quorum_required until silent "
                    "agents are diagnosed"
                ),
                "severity": "medium",
            }
        return {
            "candidate": "no_single_dominant_blocker",
            "rationale": (
                "no single rule passes the surgical-change threshold "
                "after removing signal_hold; throughput is diffuse"
            ),
            "evidence": {
                "new_headline_blocker_counts": dict(sorted_real[:5]),
            },
            "auto_apply": False,
            "investigation": (
                "review the per-stage drop_reasons across the full "
                "funnel; surgical change is premature until one rule "
                "dominates"
            ),
            "severity": "low",
        }

    return {
        "candidate": "insufficient_counterfactual_signal",
        "rationale": (
            "counterfactual sample is empty or signal_hold did not "
            "fire as headline; cannot recommend a surgical change"
        ),
        "evidence": {
            "n_decisions_analyzed": int(counterfactual.n_decisions_analyzed),
        },
        "auto_apply": False,
        "investigation": (
            "verify decision_provenance carries policy_result_json on "
            "HOLD rows; check if signal_hold is firing earlier than "
            "the persistence point"
        ),
        "severity": "info",
    }


# ── Public entry point ─────────────────────────────────────────────────


def _watchlist_size() -> Optional[int]:
    """Best-effort count of active watchlist tickers. Returns None if
    the model isn't reachable — never raises."""
    try:
        from backend.models.watchlist import WatchlistItem
        with session_scope() as s:
            n = s.execute(
                select(WatchlistItem)
            ).scalars().all()
            return len(n)
    except Exception:
        logger.debug("watchlist_size lookup failed", exc_info=True)
        return None


def _composite_quality_stats(
    snaps: List[_ProvSnapshot],
) -> Tuple[Optional[float], Optional[float]]:
    """Mean + median composite quality across snaps with the score
    populated. Returns (None, None) when no snap carries the score."""
    vals = [
        snap.composite_quality for snap in snaps
        if snap.composite_quality is not None
    ]
    if not vals:
        return None, None
    import statistics as _st
    return (
        round(_st.fmean(vals), 4),
        round(_st.median(vals), 4),
    )


def compute_funnel_report(
    *,
    window_days: int = 14,
    counterfactual_rule: str = "signal_hold",
    counterfactual_sample_size: int = COUNTERFACTUAL_SAMPLE_SIZE,
    cooldown_seconds: float = DEFAULT_BRAIN_COOLDOWN_SECONDS,
    window_end: Optional[datetime] = None,
) -> FunnelReport:
    """Compute the funnel report over the trailing ``window_days``.

    ``window_end`` defaults to UTC now; supply explicitly for daily
    snapshot semantics (e.g. window_days=1, window_end=midnight ET so
    the row keyed on yesterday's date contains yesterday's funnel).
    """
    win_end = window_end or datetime.utcnow()
    win_start = win_end - timedelta(days=int(window_days))

    snaps = _load_prov_snapshots(
        window_start=win_start, window_end=win_end,
    )
    stages, submitted_trade_ids = _stage_passes(snaps)

    # Patch stage 10's n_passed with the real closed_with_pnl count.
    n_closed = _load_closed_trade_count(
        window_start=win_start, window_end=win_end,
        submitted_trade_ids=submitted_trade_ids,
    )
    stage_10 = stages[-1]
    stage_10.n_passed = int(n_closed)
    stage_10.n_dropped = max(0, stage_10.n_decisions - n_closed)
    stage_10.pass_rate = (
        (n_closed / stage_10.n_decisions)
        if stage_10.n_decisions > 0 else None
    )

    confidence_hist = _build_confidence_histogram(snaps)
    cooldown_audit = _build_cooldown_audit(
        snaps=snaps,
        window_start=win_start,
        window_end=win_end,
        cooldown_seconds=cooldown_seconds,
    )
    counterfactual = _build_counterfactual_histogram(
        snaps,
        rule_overridden=counterfactual_rule,
        sample_size=counterfactual_sample_size,
    )
    surgical = _derive_surgical_change_candidate(
        counterfactual=counterfactual,
        cooldown_audit=cooldown_audit,
    )
    comp_mean, comp_median = _composite_quality_stats(snaps)

    notes: List[str] = []
    # Surface n_evaluations honestly.
    if stages[0].n_decisions == 0:
        notes.append("empty_window_no_decision_provenance_rows")
    # Stage 2 caveat — already in stage.note, but also a top-level note
    # so the cockpit sees it at the report root.
    n_with_matrix = stages[1].n_passed
    if (
        stages[0].n_decisions > 0
        and n_with_matrix < stages[0].n_decisions
    ):
        ratio = n_with_matrix / max(1, stages[0].n_decisions)
        notes.append(
            f"strategy_matrix_coverage_partial={ratio:.2f}"
        )

    return FunnelReport(
        window_days=int(window_days),
        window_start=win_start.isoformat(),
        window_end=win_end.isoformat(),
        watchlist_size=_watchlist_size(),
        stages=stages,
        confidence_histograms=confidence_hist,
        cooldown_audit=cooldown_audit,
        counterfactual=counterfactual,
        top_surgical_change_candidate=surgical,
        composite_quality_mean=comp_mean,
        composite_quality_median=comp_median,
        computed_at=datetime.utcnow().isoformat(),
        notes=notes,
    )


# ── Persistence ────────────────────────────────────────────────────────


def persist_funnel_report(
    report: FunnelReport,
    *,
    target_date: Optional[Any] = None,
) -> Dict[str, Any]:
    """Write one row to ``decision_funnel_daily``.

    ``target_date`` defaults to the date portion of ``report.window_end``
    (typically "yesterday" for the nightly job). When a row already
    exists for that date the existing row is UPDATED in place — the
    column has a unique index so this is the only safe path.

    Returns ``{ok, row_id, date, computed_at, n_evaluations,
    n_submitted, n_closed_with_pnl}`` — a compact ack the scheduler
    log can render without re-decoding the payload.
    """
    from backend.models.decision_funnel_daily import DecisionFunnelDaily
    from datetime import date as _date

    # Resolve the target date.
    if target_date is None:
        # Parse window_end ISO timestamp.
        try:
            we = datetime.fromisoformat(report.window_end)
        except (TypeError, ValueError):
            we = datetime.utcnow()
        target_date = we.date()
    elif isinstance(target_date, datetime):
        target_date = target_date.date()
    elif isinstance(target_date, str):
        try:
            target_date = _date.fromisoformat(target_date)
        except ValueError:
            target_date = datetime.utcnow().date()

    stages = report.stages
    payload = report.to_dict()
    payload_json = json.dumps(payload, default=str)
    conf_json = json.dumps(
        report.confidence_histograms.to_dict(), default=str,
    )
    # Compose top-3 blockers across all stages — a flat "what bit me
    # most overall?" tally for the daily row.
    top_tally: Counter = Counter()
    for stage in stages:
        for entry in stage.top_3_drop_reasons:
            rule = entry.get("rule")
            n = int(entry.get("n") or 0)
            if rule and n:
                top_tally[str(rule)] += n
    top_3_blockers_json = json.dumps(
        [
            {"rule": r, "n": n}
            for r, n in top_tally.most_common(3)
        ],
        default=str,
    )
    notes_csv = ",".join(report.notes) if report.notes else None

    n_evals = stages[0].n_decisions if stages else 0
    n_analysis_candidate = stages[1].n_passed if len(stages) > 1 else 0
    n_brain_non_hold = stages[2].n_passed if len(stages) > 2 else 0
    n_policy_eligible = stages[3].n_passed if len(stages) > 3 else 0
    n_quorum = stages[4].n_passed if len(stages) > 4 else 0
    n_non_abstain = stages[5].n_passed if len(stages) > 5 else 0
    n_risk = stages[6].n_passed if len(stages) > 6 else 0
    n_sim = stages[7].n_passed if len(stages) > 7 else 0
    n_submitted = stages[8].n_passed if len(stages) > 8 else 0
    n_closed = stages[9].n_passed if len(stages) > 9 else 0

    with session_scope() as s:
        existing = s.execute(
            select(DecisionFunnelDaily).where(
                DecisionFunnelDaily.date == target_date,
            )
        ).scalar_one_or_none()
        if existing is None:
            row = DecisionFunnelDaily(
                date=target_date,
                computed_at=datetime.utcnow(),
                watchlist_size=report.watchlist_size,
                n_evaluations=int(n_evals),
                n_analysis_candidate=int(n_analysis_candidate),
                n_brain_non_hold=int(n_brain_non_hold),
                n_policy_eligible=int(n_policy_eligible),
                n_consensus_quorum_met=int(n_quorum),
                n_consensus_non_abstain=int(n_non_abstain),
                n_risk_passed=int(n_risk),
                n_simulator_passed=int(n_sim),
                n_submitted=int(n_submitted),
                # n_filled is best-effort identical to n_submitted on
                # the paper bot; future broker integration can diverge.
                n_filled=int(n_submitted),
                n_closed_with_pnl=int(n_closed),
                n_cooldown_hits=int(report.cooldown_audit.n_cooldown_hits),
                n_cooldown_lost_opportunities=int(
                    report.cooldown_audit.n_lost_opportunities
                ),
                composite_quality_mean=report.composite_quality_mean,
                confidence_histogram_json=conf_json,
                top_3_blockers_json=top_3_blockers_json,
                payload_json=payload_json,
                notes=notes_csv,
            )
            s.add(row)
            s.flush()
            row_id = int(row.id)
        else:
            existing.computed_at = datetime.utcnow()
            existing.watchlist_size = report.watchlist_size
            existing.n_evaluations = int(n_evals)
            existing.n_analysis_candidate = int(n_analysis_candidate)
            existing.n_brain_non_hold = int(n_brain_non_hold)
            existing.n_policy_eligible = int(n_policy_eligible)
            existing.n_consensus_quorum_met = int(n_quorum)
            existing.n_consensus_non_abstain = int(n_non_abstain)
            existing.n_risk_passed = int(n_risk)
            existing.n_simulator_passed = int(n_sim)
            existing.n_submitted = int(n_submitted)
            existing.n_filled = int(n_submitted)
            existing.n_closed_with_pnl = int(n_closed)
            existing.n_cooldown_hits = int(
                report.cooldown_audit.n_cooldown_hits
            )
            existing.n_cooldown_lost_opportunities = int(
                report.cooldown_audit.n_lost_opportunities
            )
            existing.composite_quality_mean = report.composite_quality_mean
            existing.confidence_histogram_json = conf_json
            existing.top_3_blockers_json = top_3_blockers_json
            existing.payload_json = payload_json
            existing.notes = notes_csv
            row_id = int(existing.id)

    return {
        "ok": True,
        "row_id": row_id,
        "date": target_date.isoformat(),
        "computed_at": datetime.utcnow().isoformat(),
        "n_evaluations": int(n_evals),
        "n_submitted": int(n_submitted),
        "n_closed_with_pnl": int(n_closed),
    }


def latest_funnel_row() -> Optional[Dict[str, Any]]:
    """Return the most recently persisted ``decision_funnel_daily`` row
    as a dict, or None when the table is empty. Used by the cockpit
    funnel_snapshot panel for an O(1) read."""
    from backend.models.decision_funnel_daily import DecisionFunnelDaily
    with session_scope() as s:
        row = s.execute(
            select(DecisionFunnelDaily)
            .order_by(DecisionFunnelDaily.date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return row.to_dict()


def funnel_history(*, days: int = 30) -> List[Dict[str, Any]]:
    """Return up to ``days`` of decision_funnel_daily rows, newest
    first. Each row is the to_dict projection — payload_json stays
    string-encoded for compact transit; the consumer decodes when it
    needs the full FunnelReport."""
    from backend.models.decision_funnel_daily import DecisionFunnelDaily
    with session_scope() as s:
        rows = s.execute(
            select(DecisionFunnelDaily)
            .order_by(DecisionFunnelDaily.date.desc())
            .limit(int(days))
        ).scalars().all()
        return [r.to_dict() for r in rows]


# ── Anomaly detection ─────────────────────────────────────────────────


def is_anomalous_drop(
    current_row: Dict[str, Any],
    history_rows: List[Dict[str, Any]],
    *,
    drop_threshold: float = 0.5,
) -> Dict[str, Any]:
    """Compare ``current_row`` against the median of ``history_rows``
    (the 7-day window excluding the current row). Returns a dict
    {anomalous: bool, anomalous_stages: list, deltas: dict}.

    A stage is flagged as anomalous when ``current / median <
    (1 - drop_threshold)``. Default 0.5 → fires when current is
    less than 50% of the 7d median. Ignored when the median is < 5
    (too small to be reliable).
    """
    if not history_rows:
        return {"anomalous": False, "anomalous_stages": [], "deltas": {}}
    stage_keys = [
        "n_evaluations", "n_analysis_candidate", "n_brain_non_hold",
        "n_policy_eligible", "n_consensus_quorum_met",
        "n_consensus_non_abstain", "n_risk_passed",
        "n_simulator_passed", "n_submitted",
    ]
    import statistics as _st
    anomalous_stages: List[str] = []
    deltas: Dict[str, Dict[str, Any]] = {}
    for key in stage_keys:
        vals = [
            int(r.get(key) or 0) for r in history_rows
            if r.get(key) is not None
        ]
        if len(vals) < 3:
            continue
        med = _st.median(vals)
        cur = int(current_row.get(key) or 0)
        if med < 5:
            continue
        ratio = cur / med if med else None
        deltas[key] = {
            "current": cur,
            "median_7d": float(med),
            "ratio": (
                round(float(ratio), 4) if ratio is not None else None
            ),
        }
        if ratio is not None and ratio < (1.0 - drop_threshold):
            anomalous_stages.append(key)
    return {
        "anomalous": bool(anomalous_stages),
        "anomalous_stages": anomalous_stages,
        "deltas": deltas,
    }


__all__ = [
    "CONFIDENCE_BIN_EDGES",
    "COUNTERFACTUAL_SAMPLE_SIZE",
    "DEFAULT_BRAIN_COOLDOWN_SECONDS",
    "COOLDOWN_LOST_OPPORTUNITY_COMPOSITE_THRESHOLD",
    "SURGICAL_DOMINANT_BLOCKER_MIN",
    "SURGICAL_LOW_CONFIDENCE_MIN",
    "SURGICAL_CONSENSUS_ABSTAIN_MIN",
    "FunnelStage",
    "ConfidenceHistogram",
    "CooldownAudit",
    "CounterfactualHistogram",
    "FunnelReport",
    "compute_funnel_report",
    "persist_funnel_report",
    "latest_funnel_row",
    "funnel_history",
    "is_anomalous_drop",
]
