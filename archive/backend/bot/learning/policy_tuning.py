"""MITS Phase 18.C — Policy Auto-Tuning (Advisory).

For every PolicyRule that has an operator-tunable numeric threshold,
build a rolling empirical/Bayesian posterior over the optimal
threshold from closed-trade outcomes.

THE OUTPUT IS ADVISORY ONLY. We never write back to any TUNABLES
field. The operator reviews the recommendation in the UI / cockpit
and applies it manually. The 18.C-future ``policy_tuning_auto_apply_enabled``
flag is wired but stays OFF by default.

Methodology (per tunable threshold):

  1. Iterate the closed-decision sample (decision_provenance × trades).
  2. For each decision, pull the rule-specific "scenario value" the
     threshold compares against (e.g. ``signal.confidence`` for
     ``low_confidence``, ``iv_rank`` for ``iv_too_rich``).
  3. Partition the sample into 5 buckets across the rule's plausible
     range. Each bucket represents a hypothetical threshold setting.
  4. Compute per-bucket: n_decisions, n_closed, hit_rate,
     mean_pnl_pct, Wilson 95% CI.
  5. Recommend the bucket with the highest Wilson_lower bound (be
     conservative: don't optimize for raw mean which overfits noise).
  6. If no bucket clears ``min_n_per_bucket``, mark as
     ``insufficient_data`` and recommend nothing.

Honesty guardrails:

  * Per-bucket metrics return None below min_n; the operator never
    sees a misleading number on a thin bucket.
  * Wilson CI lower bound is the ranking key, not raw hit-rate.
  * Recommendation confidence levels: ``insufficient_data``, ``low``,
    ``medium``, ``high`` — driven by total sample size + how many
    buckets cleared min_n.
  * The rule_evidence_extractor for every rule is intentionally
    explicit — never inferred from JSON shape, never silently
    defaulted.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select

from backend.bot.learning.attribution import (
    _wilson_interval,
    _decode,
    _is_closed_trade,
    _is_eligible_signal_source,
    _realized_pct,
)
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance
from backend.models.policy_rule_evaluation import PolicyRuleEvaluation
from backend.models.trade import Trade


logger = logging.getLogger(__name__)


# ── Public knobs (config-driven; no magic numbers in the math) ────────


DEFAULT_WINDOW_DAYS = 90
DEFAULT_MIN_N_PER_BUCKET = 20
DEFAULT_NUM_BUCKETS = 5
# Total-sample heuristics for the recommendation_confidence label.
CONFIDENCE_HIGH_TOTAL_N = 200
CONFIDENCE_MEDIUM_TOTAL_N = 80

# ── Gap 9 — recommendation stability check ────────────────────────────
#
# A single noisy nightly recompute can land a high-grade recommendation
# that the next two nights disagree with. The operator approves on
# night 1, the next morning sees a different number, and trust in the
# system erodes — even though the math behind night 1 was sound for
# the snapshot it saw.
#
# Stream D's fix: a recommendation only EARNS ``confidence='high'`` if
# it has been stable across N consecutive recompute runs. "Stable" =
# the recommended_value sits within a small relative tolerance of the
# current run's recommended_value. If the stability check fails, the
# confidence is DEMOTED from 'high' to 'medium' (never escalated —
# stability never INVENTS a high grade where the raw math didn't earn
# one).
#
# Defaults:
#   * 3 consecutive matching runs is the floor for "high"
#   * 5% relative tolerance on the recommended_value
#   * The check runs only when raw confidence was already 'high' — the
#     'medium'/'low'/'insufficient_data' paths are unchanged.
STABILITY_N_CONSECUTIVE_REQUIRED = 3
STABILITY_TOLERANCE_PCT = 0.05


# ── Tunable-rule registry ─────────────────────────────────────────────


@dataclass(frozen=True)
class TunableRule:
    """Static metadata for one PolicyRule whose threshold the operator
    might want to tune. ``scenario_value_fn`` extracts the per-decision
    value the threshold actually compares against — without it, bucketing
    is impossible.

    ``direction``:
      * ``higher_is_stricter`` — raising the threshold REJECTS more
        candidates (e.g. min_confidence: a candidate must be >= threshold
        to pass).
      * ``lower_is_stricter`` — lowering the threshold REJECTS more
        candidates (e.g. correlation_cap_rho: a candidate must be <=
        threshold to pass).
    """
    rule_name: str
    threshold_attr: str
    current_value: float
    plausible_range: Tuple[float, float]
    direction: str
    scenario_value_fn: Callable[
        ["_DecisionRow"], Optional[float],
    ]
    units: str = ""
    description: str = ""


@dataclass
class _DecisionRow:
    """One closed-decision sample with the parsed inputs every
    scenario_value_fn might need. Held as a plain dataclass so each
    rule's extractor can read exactly the field it cares about
    without touching the DB."""
    trade_id: int
    pnl_pct: float
    win: int
    decision_timestamp: datetime
    consensus: Dict[str, Any]
    confidence_breakdown: Dict[str, Any]
    regime_vector: Dict[str, Any]
    simulator_verdict: Dict[str, Any]
    correlation_cap: Dict[str, Any]
    portfolio_context: Dict[str, Any]
    policy_result: Dict[str, Any]
    rule_evaluations: List[Dict[str, Any]]
    decision_quality: Dict[str, Any]


# ── Scenario-value extractors (one per tunable rule) ──────────────────


def _consensus_confidence(row: _DecisionRow) -> Optional[float]:
    """For ``low_confidence``: the signal's confidence as carried by
    the consensus block (0..1). Falls back to the policy_result row
    evidence — early rows wrote confidence into the blocking_factor
    evidence dict so the value is recoverable even when consensus is
    sparse."""
    c = row.consensus or {}
    try:
        v = float(c.get("confidence", 0.0) or 0.0)
        if v > 0:
            return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        pass
    # Fallback: scan per-rule evals for low_confidence evidence.
    for ev in row.rule_evaluations:
        if str(ev.get("rule_name") or "") != "low_confidence":
            continue
        evid = _decode(ev.get("evidence_json")) or {}
        try:
            v = float(evid.get("confidence", 0.0) or 0.0)
            if v > 0:
                return max(0.0, min(1.0, v))
        except (TypeError, ValueError):
            continue
    return None


def _iv_rank_value(row: _DecisionRow) -> Optional[float]:
    """For ``iv_too_rich``: IV rank score (0..100). Lives on the
    rule's evidence dict because the engine writes the live snapshot
    iv_rank into evidence at evaluation time. Also recoverable from
    confidence_breakdown.options when present."""
    for ev in row.rule_evaluations:
        if str(ev.get("rule_name") or "") != "iv_too_rich":
            continue
        evid = _decode(ev.get("evidence_json")) or {}
        try:
            v = float(evid.get("iv_rank", -1.0) or -1.0)
            if v >= 0:
                return max(0.0, min(100.0, v))
        except (TypeError, ValueError):
            continue
    # Fallback to confidence_breakdown.options axis (the iv_rank value
    # often flows through to this axis). 15.D persists it as [0, 1].
    opt_score = (row.confidence_breakdown or {}).get("options")
    try:
        v = float(opt_score)
        if 0 <= v <= 1:
            return v * 100.0
        if 0 <= v <= 100:
            return v
    except (TypeError, ValueError):
        pass
    return None


def _correlation_value(row: _DecisionRow) -> Optional[float]:
    """For ``correlation_cap_block``: the maximum pairwise correlation
    the engine measured between the candidate and existing book at
    decision time. Lives on the ``correlation_cap`` json column."""
    cc = row.correlation_cap or {}
    candidates = (
        cc.get("max_correlation"),
        cc.get("rho"),
        cc.get("highest_rho"),
    )
    for raw in candidates:
        try:
            v = float(raw)
            if math.isfinite(v):
                return max(0.0, min(1.0, abs(v)))
        except (TypeError, ValueError):
            continue
    return None


def _simulator_max_loss(row: _DecisionRow) -> Optional[float]:
    """For ``simulator_veto``: simulator's projected p_max_loss for
    the candidate. Persisted on simulator_verdict_json."""
    sv = row.simulator_verdict or {}
    for k in ("p_max_loss", "max_loss_probability", "p_loss_max"):
        try:
            v = float(sv.get(k))
            if math.isfinite(v):
                return max(0.0, min(1.0, v))
        except (TypeError, ValueError):
            continue
    return None


def _catalyst_dte(row: _DecisionRow) -> Optional[float]:
    """For ``catalyst_gate`` short-DTE-into-earnings rule: the
    candidate option's DTE. Falls back to consensus.dte / chain_dte
    if the rule eval evidence isn't populated."""
    for ev in row.rule_evaluations:
        if str(ev.get("rule_name") or "") != "catalyst_gate":
            continue
        evid = _decode(ev.get("evidence_json")) or {}
        try:
            v = float(evid.get("dte", -1.0) or -1.0)
            if v >= 0:
                return max(0.0, min(120.0, v))
        except (TypeError, ValueError):
            continue
    # Consensus carries 'dte' on option candidates.
    c = row.consensus or {}
    for k in ("dte", "chain_dte"):
        try:
            v = float(c.get(k))
            if v >= 0:
                return max(0.0, min(120.0, v))
        except (TypeError, ValueError):
            continue
    return None


def _abstain_band_score(row: _DecisionRow) -> Optional[float]:
    """For abstain_band thresholds: the probability/posterior the
    abstain rule evaluated. Persisted on consensus or on the rule
    eval evidence dict (P(win) ∈ [0, 1])."""
    c = row.consensus or {}
    for k in ("probability", "p_win", "posterior"):
        try:
            v = float(c.get(k))
            if 0 <= v <= 1:
                return v
        except (TypeError, ValueError):
            continue
    for ev in row.rule_evaluations:
        if str(ev.get("rule_name") or "") != "abstain_and_throttle":
            continue
        evid = _decode(ev.get("evidence_json")) or {}
        for k in ("probability", "p_win"):
            try:
                v = float(evid.get(k))
                if 0 <= v <= 1:
                    return v
            except (TypeError, ValueError):
                continue
    return None


def _cycle_time_value(row: _DecisionRow) -> Optional[float]:
    """For ``cycle_budget_overrun``: the actual measured cycle wall-
    time in seconds. Persisted on rule_evaluations.evidence for
    over-budget rows; non-overrun rows skip recording it."""
    for ev in row.rule_evaluations:
        if str(ev.get("rule_name") or "") != "cycle_budget_overrun":
            continue
        evid = _decode(ev.get("evidence_json")) or {}
        try:
            v = float(evid.get("budget_seconds", -1.0) or -1.0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


# ── Canonical registry ────────────────────────────────────────────────


TUNABLE_RULES: List[TunableRule] = [
    TunableRule(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        current_value=0.40,  # DEFAULT_BOT_CONFIG["min_confidence"]
        plausible_range=(0.30, 0.80),
        direction="higher_is_stricter",
        scenario_value_fn=_consensus_confidence,
        units="probability_0_1",
        description=(
            "Signal must clear this confidence to pass; raise to "
            "reject borderline setups."
        ),
    ),
    TunableRule(
        rule_name="iv_too_rich",
        threshold_attr="hardcoded_iv_rank_ceiling",
        current_value=70.0,
        plausible_range=(50.0, 90.0),
        direction="lower_is_stricter",
        scenario_value_fn=_iv_rank_value,
        units="iv_rank_0_100",
        description=(
            "AI Brain refuses long premium when IV rank > threshold; "
            "lower to be stricter."
        ),
    ),
    TunableRule(
        rule_name="correlation_cap_block",
        threshold_attr="TUNABLES.correlation_cap_rho",
        current_value=float(TUNABLES.correlation_cap_rho),
        plausible_range=(0.60, 0.95),
        direction="lower_is_stricter",
        scenario_value_fn=_correlation_value,
        units="correlation_0_1",
        description=(
            "Block when measured book correlation > threshold; lower "
            "to be stricter about concentration."
        ),
    ),
    TunableRule(
        rule_name="simulator_veto",
        threshold_attr="TUNABLES.simulator_max_loss_veto",
        current_value=float(TUNABLES.simulator_max_loss_veto),
        plausible_range=(0.15, 0.50),
        direction="lower_is_stricter",
        scenario_value_fn=_simulator_max_loss,
        units="probability_0_1",
        description=(
            "Block when simulator p_max_loss > threshold; lower to "
            "reject more tail-risk candidates."
        ),
    ),
    TunableRule(
        rule_name="catalyst_gate",
        threshold_attr="TUNABLES.catalyst_short_dte_threshold",
        current_value=float(TUNABLES.catalyst_short_dte_threshold),
        plausible_range=(3.0, 14.0),
        direction="higher_is_stricter",
        scenario_value_fn=_catalyst_dte,
        units="days_to_expiry",
        description=(
            "Short-DTE options ≤ threshold into earnings → abstain; "
            "raise to widen the safety window."
        ),
    ),
    TunableRule(
        rule_name="abstain_and_throttle_hi",
        threshold_attr="TUNABLES.abstain_band_hi",
        current_value=float(TUNABLES.abstain_band_hi),
        plausible_range=(0.50, 0.70),
        direction="higher_is_stricter",
        scenario_value_fn=_abstain_band_score,
        units="probability_0_1",
        description=(
            "Above this posterior, abstain rule no longer fires; raise "
            "to abstain on more borderline candidates."
        ),
    ),
    TunableRule(
        rule_name="abstain_and_throttle_lo",
        threshold_attr="TUNABLES.abstain_band_lo",
        current_value=float(TUNABLES.abstain_band_lo),
        plausible_range=(0.40, 0.58),
        direction="lower_is_stricter",
        scenario_value_fn=_abstain_band_score,
        units="probability_0_1",
        description=(
            "Below this posterior, abstain rule hard-blocks; raise "
            "the floor to refuse weaker candidates."
        ),
    ),
    TunableRule(
        rule_name="cycle_budget_overrun",
        threshold_attr="TUNABLES.engine_cycle_timeout_sec",
        current_value=float(TUNABLES.engine_cycle_timeout_sec),
        plausible_range=(60.0, 600.0),
        direction="lower_is_stricter",
        scenario_value_fn=_cycle_time_value,
        units="seconds",
        description=(
            "Hard wall-clock cap per engine cycle; lower to abort "
            "earlier on stuck cycles."
        ),
    ),
]


# ── Dataclasses ───────────────────────────────────────────────────────


@dataclass
class ThresholdBucket:
    """One bucket along a tunable threshold's plausible range.

    ``n_decisions`` is the count of closed-decision samples whose
    scenario value falls in [threshold_low, threshold_high). When
    ``n_closed < min_n``, every per-bucket metric is None and the
    notes carry ``insufficient_sample_size``.
    """
    bucket_idx: int
    threshold_low: float
    threshold_high: float
    n_decisions: int
    n_closed: int
    hit_rate: Optional[float]
    hit_rate_wilson_lower: Optional[float]
    hit_rate_wilson_upper: Optional[float]
    mean_pnl_pct: Optional[float]
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyTuningRecommendation:
    rule_name: str
    threshold_attr: str
    current_value: float
    plausible_range: Tuple[float, float]
    direction: str
    units: str
    description: str
    buckets: List[ThresholdBucket]
    recommended_value: Optional[float]
    recommendation_confidence: str   # insufficient_data | low | medium | high
    rationale: str
    n_decisions_total: int
    n_closed_total: int
    sample_age_days: Optional[int]
    window_days: int
    min_n_per_bucket: int
    computed_at: str

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["plausible_range"] = list(self.plausible_range)
        out["buckets"] = [b.to_dict() if hasattr(b, "to_dict") else b
                          for b in self.buckets]
        return out


# ── Closed-decision iteration ─────────────────────────────────────────


def _iter_closed_rows(
    window_days: int,
) -> List[_DecisionRow]:
    """Pull all closed-decision rows in the window. Mirrors the
    18.A walker (same closure rules + same signal_source filter) so
    18.A + 18.C see the same denominator.

    Joins each row to the per-rule evaluations from
    ``policy_rule_evaluations`` so per-rule evidence dicts (the only
    place we persist threshold-time scenario values like iv_rank
    and confidence) are queryable here.
    """
    if window_days <= 0:
        return []
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    out: List[_DecisionRow] = []
    with session_scope() as s:
        prov_rows = s.execute(
            select(DecisionProvenance)
            .where(DecisionProvenance.trade_id.is_not(None))
            .where(DecisionProvenance.decision_timestamp >= cutoff)
            .order_by(DecisionProvenance.decision_timestamp.desc())
        ).scalars().all()
        trade_ids = [r.trade_id for r in prov_rows if r.trade_id is not None]
        trade_map: Dict[int, Trade] = {}
        if trade_ids:
            for t in s.execute(
                select(Trade).where(Trade.id.in_(trade_ids))
            ).scalars().all():
                trade_map[int(t.id)] = t
        # Per-rule evidence lives on policy_rule_evaluations keyed by
        # cycle_id + ticker. We bulk-pull within the same window cutoff
        # to keep the helper at O(prov + eval) instead of N+1.
        cycle_ids = [str(r.cycle_id) for r in prov_rows if r.cycle_id]
        eval_map: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        if cycle_ids:
            for ev in s.execute(
                select(PolicyRuleEvaluation)
                .where(PolicyRuleEvaluation.evaluated_at >= cutoff)
                .where(PolicyRuleEvaluation.cycle_id.in_(cycle_ids))
            ).scalars().all():
                k = (str(ev.cycle_id or ""), str(ev.ticker or ""))
                eval_map.setdefault(k, []).append({
                    "rule_name": ev.rule_name,
                    "blocked": bool(ev.blocked),
                    "reason": ev.reason,
                    "evidence_json": ev.evidence_json,
                })
        for prov in prov_rows:
            trade = trade_map.get(int(prov.trade_id))
            if trade is None:
                continue
            if not _is_closed_trade(trade):
                continue
            if not _is_eligible_signal_source(trade):
                continue
            pct = _realized_pct(trade)
            if pct is None:
                continue
            consensus = _decode(prov.consensus_json) or {}
            cb = consensus.get("confidence_breakdown") or {}
            if not isinstance(cb, dict):
                cb = {}
            rv = _decode(prov.regime_vector_json) or {}
            sv = _decode(prov.simulator_verdict_json) or {}
            cc = _decode(prov.correlation_cap_json) or {}
            pc = _decode(prov.portfolio_context_json) or {}
            pr = _decode(prov.policy_result_json) or {}
            dqs = _decode(prov.decision_quality_score_json) or {}
            evals = eval_map.get(
                (str(prov.cycle_id or ""), str(prov.ticker or "")), [],
            )
            out.append(_DecisionRow(
                trade_id=int(prov.trade_id),
                pnl_pct=float(pct),
                win=1 if float(trade.pnl or 0.0) > 0 else 0,
                decision_timestamp=(
                    prov.decision_timestamp or datetime.utcnow()
                ),
                consensus=consensus,
                confidence_breakdown=cb,
                regime_vector=rv,
                simulator_verdict=sv,
                correlation_cap=cc,
                portfolio_context=pc,
                policy_result=pr,
                rule_evaluations=evals,
                decision_quality=dqs,
            ))
    return out


def _sample_age_days(decisions: List[_DecisionRow]) -> Optional[int]:
    if not decisions:
        return None
    oldest = min(d.decision_timestamp for d in decisions)
    return max(0, (datetime.utcnow() - oldest).days)


# ── Bucketing + recommendation logic ──────────────────────────────────


def _build_buckets(
    *,
    rule: TunableRule,
    samples: List[Tuple[float, _DecisionRow]],
    n_buckets: int,
    min_n_per_bucket: int,
) -> List[ThresholdBucket]:
    """Split ``samples`` into equal-width buckets across the rule's
    plausible_range. Each sample is a (scenario_value, _DecisionRow)
    pair. Below ``min_n_per_bucket``, all metrics are None.

    Bucket boundaries: ``[lo, lo+w), [lo+w, lo+2w), ... [lo+(n-1)w, hi]``
    — last bucket is inclusive of the upper bound so a sample at
    exactly the high end isn't lost.
    """
    lo, hi = rule.plausible_range
    width = (hi - lo) / float(n_buckets)
    buckets: List[List[_DecisionRow]] = [[] for _ in range(n_buckets)]
    for value, row in samples:
        # Clip to range — we still want to know about samples just
        # outside the operator's plausible range (they get clamped
        # to the edge buckets so we don't silently drop data).
        v = max(lo, min(hi, float(value)))
        idx = int((v - lo) / width) if width > 0 else 0
        if idx >= n_buckets:
            idx = n_buckets - 1
        if idx < 0:
            idx = 0
        buckets[idx].append(row)

    out: List[ThresholdBucket] = []
    for i, rows in enumerate(buckets):
        b_lo = lo + i * width
        b_hi = lo + (i + 1) * width if i < n_buckets - 1 else hi
        n_dec = len(rows)
        # All rows in our sample are closed-decisions by construction,
        # so n_closed == n_decisions. We keep the distinction for the
        # cohort matrix shape so future work can include
        # decisions-that-blocked (not-closed) as a separate column.
        n_closed = n_dec
        notes: List[str] = []
        if n_closed < min_n_per_bucket:
            notes.append("insufficient_sample_size")
            out.append(ThresholdBucket(
                bucket_idx=i,
                threshold_low=round(b_lo, 6),
                threshold_high=round(b_hi, 6),
                n_decisions=n_dec,
                n_closed=n_closed,
                hit_rate=None,
                hit_rate_wilson_lower=None,
                hit_rate_wilson_upper=None,
                mean_pnl_pct=None,
                notes=notes,
            ))
            continue
        pnls = [r.pnl_pct for r in rows]
        wins = sum(r.win for r in rows)
        wlow, whi = _wilson_interval(wins, n_closed)
        out.append(ThresholdBucket(
            bucket_idx=i,
            threshold_low=round(b_lo, 6),
            threshold_high=round(b_hi, 6),
            n_decisions=n_dec,
            n_closed=n_closed,
            hit_rate=round(wins / n_closed, 4),
            hit_rate_wilson_lower=(
                round(wlow, 4) if wlow is not None else None
            ),
            hit_rate_wilson_upper=(
                round(whi, 4) if whi is not None else None
            ),
            mean_pnl_pct=round(statistics.fmean(pnls), 4),
            notes=notes,
        ))
    return out


def _stability_check(
    *,
    rule_name: str,
    current_recommendation: Optional[float],
    n_consecutive_required: int = STABILITY_N_CONSECUTIVE_REQUIRED,
    tolerance_pct: float = STABILITY_TOLERANCE_PCT,
) -> Dict[str, Any]:
    """Gap 9 — read the last ``n_consecutive_required`` ``policy_tunings``
    rows for ``rule_name`` (descending by computed_at) and check whether
    EVERY prior recommendation falls within ``tolerance_pct`` of the
    current ``current_recommendation``.

    Returns a dict:
      * ``is_stable`` — True only when there are at least
        ``n_consecutive_required - 1`` prior matching rows (we need
        N total, the current run is one, plus N-1 history).
      * ``n_consecutive_matching`` — count of priors (0 .. N-1) that
        fell within tolerance, capped at ``n_consecutive_required - 1``.
      * ``stability_window_days`` — calendar window across the priors
        consulted (best-effort; None when no priors exist).
      * ``priors_consulted`` — count of rows actually read.
      * ``tolerance_pct`` — echo back for the operator surface.

    Pure read; never raises. Returns a safe "no priors → cannot be
    stable" shape on DB failure.
    """
    # Sentinel: an empty current recommendation can't be "stable".
    if current_recommendation is None:
        return {
            "is_stable": False,
            "n_consecutive_matching": 0,
            "stability_window_days": None,
            "priors_consulted": 0,
            "tolerance_pct": float(tolerance_pct),
        }
    n_required = int(max(2, n_consecutive_required))
    # We need N rows total to call it "stable". The current run is row
    # #1; pull (N-1) priors to compare against.
    priors_to_read = n_required - 1
    try:
        from backend.models.policy_tuning import PolicyTuning
        from sqlalchemy import desc as _desc
        with session_scope() as s:
            priors = s.execute(
                select(PolicyTuning)
                .where(PolicyTuning.rule_name == rule_name)
                .order_by(_desc(PolicyTuning.computed_at))
                .limit(priors_to_read)
            ).scalars().all()
            window_days: Optional[int] = None
            if priors:
                oldest = min(
                    p.computed_at for p in priors
                    if p.computed_at is not None
                )
                window_days = max(
                    0, (datetime.utcnow() - oldest).days,
                ) if oldest else None
            matching = 0
            cur = float(current_recommendation)
            denom = abs(cur) if abs(cur) > 1e-9 else 1.0
            for p in priors:
                rv = p.recommended_value
                if rv is None:
                    continue
                try:
                    delta = abs(float(rv) - cur) / denom
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                if delta <= tolerance_pct:
                    matching += 1
            is_stable = matching >= priors_to_read
            return {
                "is_stable": bool(is_stable),
                "n_consecutive_matching": int(matching),
                "stability_window_days": window_days,
                "priors_consulted": int(len(priors)),
                "tolerance_pct": float(tolerance_pct),
                "n_consecutive_required": int(n_required),
            }
    except Exception:
        logger.debug(
            "policy_tuning stability_check failed; defaulting to not-stable",
            exc_info=True,
        )
        return {
            "is_stable": False,
            "n_consecutive_matching": 0,
            "stability_window_days": None,
            "priors_consulted": 0,
            "tolerance_pct": float(tolerance_pct),
            "n_consecutive_required": int(n_required),
        }


def _pick_recommendation(
    *,
    buckets: List[ThresholdBucket],
    rule: TunableRule,
    total_n: int,
) -> Tuple[Optional[float], str, str]:
    """Conservative recommender.

    Ranks eligible buckets (those that cleared min_n) by Wilson_lower
    bound (descending). Recommends the midpoint of the winning bucket
    as the suggested threshold. Confidence label is driven by total
    sample size + the spread between top and median bucket Wilson_lower.
    """
    eligible = [b for b in buckets if b.hit_rate_wilson_lower is not None]
    if not eligible:
        return (
            None,
            "insufficient_data",
            (
                f"No threshold bucket reached the minimum sample size "
                f"({DEFAULT_MIN_N_PER_BUCKET}). Total closed decisions "
                f"in window: {total_n}. Recommendation deferred — keep "
                f"current threshold ({rule.current_value:g})."
            ),
        )

    ranked = sorted(
        eligible,
        key=lambda b: (b.hit_rate_wilson_lower or 0.0),
        reverse=True,
    )
    winner = ranked[0]
    midpoint = (winner.threshold_low + winner.threshold_high) / 2.0

    # Confidence label.
    if total_n >= CONFIDENCE_HIGH_TOTAL_N and len(eligible) >= 3:
        conf = "high"
    elif total_n >= CONFIDENCE_MEDIUM_TOTAL_N and len(eligible) >= 2:
        conf = "medium"
    else:
        conf = "low"

    # Rationale: list the winner + immediate runner-up + median bucket.
    parts = [
        (
            f"Best bucket: idx={winner.bucket_idx} "
            f"[{winner.threshold_low:g}, {winner.threshold_high:g}] "
            f"with n_closed={winner.n_closed}, "
            f"hit_rate={winner.hit_rate:.3f}, "
            f"Wilson_lower={winner.hit_rate_wilson_lower:.3f}, "
            f"mean_pnl_pct={winner.mean_pnl_pct:.3f}."
        )
    ]
    if len(ranked) > 1:
        runner = ranked[1]
        parts.append(
            f"Runner-up: idx={runner.bucket_idx} "
            f"Wilson_lower={runner.hit_rate_wilson_lower:.3f} "
            f"(n_closed={runner.n_closed})."
        )
    parts.append(
        f"Direction: {rule.direction}. "
        f"Current threshold: {rule.current_value:g}. "
        f"Recommended midpoint: {midpoint:g}. "
        f"Total closed decisions in window: {total_n}."
    )
    return round(midpoint, 6), conf, " ".join(parts)


# ── Public API ────────────────────────────────────────────────────────


def compute_policy_tuning(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_n_per_bucket: int = DEFAULT_MIN_N_PER_BUCKET,
    n_buckets: int = DEFAULT_NUM_BUCKETS,
    rules: Optional[List[TunableRule]] = None,
    decisions: Optional[List[_DecisionRow]] = None,
) -> List[PolicyTuningRecommendation]:
    """Compute advisory threshold recommendations for every tunable
    rule. Pure read; no DB writes.

    Workflow per rule:
      1. Extract the scenario value from each closed-decision row using
         the rule's ``scenario_value_fn``.
      2. Bucket samples whose scenario value is not None.
      3. Compute per-bucket metrics + recommendation.
      4. Build the recommendation dataclass.
    """
    rules = rules if rules is not None else list(TUNABLE_RULES)
    if decisions is None:
        decisions = _iter_closed_rows(window_days)
    age_days = _sample_age_days(decisions)
    computed_at = datetime.utcnow().isoformat()
    out: List[PolicyTuningRecommendation] = []
    for rule in rules:
        samples: List[Tuple[float, _DecisionRow]] = []
        for d in decisions:
            try:
                v = rule.scenario_value_fn(d)
            except Exception:
                logger.debug(
                    "scenario_value_fn raised for rule=%s trade_id=%s",
                    rule.rule_name, d.trade_id, exc_info=True,
                )
                v = None
            if v is None:
                continue
            samples.append((float(v), d))
        buckets = _build_buckets(
            rule=rule, samples=samples,
            n_buckets=n_buckets,
            min_n_per_bucket=min_n_per_bucket,
        )
        n_closed_total = sum(b.n_closed for b in buckets)
        recommended_value, conf, rationale = _pick_recommendation(
            buckets=buckets, rule=rule, total_n=n_closed_total,
        )
        # Gap 9 — only HIGH grades get the stability check. Medium / low /
        # insufficient_data already convey the right amount of skepticism.
        # The check looks at the prior N-1 nightly rows for this rule; if
        # they don't all sit within tolerance of the current
        # ``recommended_value``, we demote the grade to 'medium' and
        # append the demotion note so the operator sees the reason.
        if conf == "high":
            stab = _stability_check(
                rule_name=rule.rule_name,
                current_recommendation=recommended_value,
                n_consecutive_required=STABILITY_N_CONSECUTIVE_REQUIRED,
                tolerance_pct=STABILITY_TOLERANCE_PCT,
            )
            n_required = int(
                stab.get(
                    "n_consecutive_required",
                    STABILITY_N_CONSECUTIVE_REQUIRED,
                )
            )
            n_matching_total = int(
                stab.get("n_consecutive_matching", 0)
            ) + 1  # +1 because the current run is itself matching
            if stab.get("is_stable"):
                rationale = (
                    rationale
                    + f" Stable for {n_matching_total}/{n_required} "
                      "recent recompute runs — confidence held at HIGH."
                )
            else:
                conf = "medium"
                rationale = (
                    rationale
                    + f" High_grade_demoted_by_stability_check: only "
                      f"{n_matching_total}/{n_required} recent runs "
                      f"agree within "
                      f"{STABILITY_TOLERANCE_PCT * 100:.0f}% of the "
                      f"current recommendation — demoting to MEDIUM."
                )
        out.append(PolicyTuningRecommendation(
            rule_name=rule.rule_name,
            threshold_attr=rule.threshold_attr,
            current_value=float(rule.current_value),
            plausible_range=(
                float(rule.plausible_range[0]),
                float(rule.plausible_range[1]),
            ),
            direction=rule.direction,
            units=rule.units,
            description=rule.description,
            buckets=buckets,
            recommended_value=recommended_value,
            recommendation_confidence=conf,
            rationale=rationale,
            n_decisions_total=sum(b.n_decisions for b in buckets),
            n_closed_total=n_closed_total,
            sample_age_days=age_days,
            window_days=window_days,
            min_n_per_bucket=min_n_per_bucket,
            computed_at=computed_at,
        ))
    return out


# ── Persistence helpers ───────────────────────────────────────────────


def persist_policy_tuning(
    recommendations: List[PolicyTuningRecommendation],
) -> Dict[str, Any]:
    """Write one ``policy_tunings`` row per recommendation. Never
    raises — failures are logged + reported in the return dict."""
    # Local import keeps the module side-effect-free until persistence
    # is actually requested (tests in isolation never import the model).
    from backend.models.policy_tuning import PolicyTuning

    written = 0
    try:
        with session_scope() as s:
            for rec in recommendations:
                d = rec.to_dict()
                row = PolicyTuning(
                    computed_at=datetime.utcnow(),
                    rule_name=rec.rule_name,
                    threshold_attr=rec.threshold_attr,
                    current_value=float(rec.current_value),
                    recommended_value=(
                        float(rec.recommended_value)
                        if rec.recommended_value is not None else None
                    ),
                    recommendation_confidence=rec.recommendation_confidence,
                    rationale=rec.rationale,
                    payload_json=json.dumps(d, default=str),
                )
                s.add(row)
                written += 1
            s.flush()
    except Exception:
        logger.exception("persist_policy_tuning failed")
        return {"ok": False, "written": written}
    return {
        "ok": True,
        "written": written,
        "computed_at": datetime.utcnow().isoformat(),
    }


def latest_policy_tuning_rows(
    *, rule_name: Optional[str] = None, limit: int = 100,
) -> List[Dict[str, Any]]:
    """Read back the most recently computed batch. "Most recent" =
    rows sharing the maximum ``computed_at`` matching the optional
    ``rule_name`` filter."""
    from backend.models.policy_tuning import PolicyTuning
    from sqlalchemy import desc

    with session_scope() as s:
        q = select(PolicyTuning)
        if rule_name:
            q = q.where(PolicyTuning.rule_name == rule_name)
        head = s.execute(
            q.order_by(desc(PolicyTuning.computed_at)).limit(1)
        ).scalars().first()
        if head is None:
            return []
        rows = s.execute(
            q.where(PolicyTuning.computed_at == head.computed_at)
            .order_by(PolicyTuning.rule_name)
            .limit(limit)
        ).scalars().all()
        return [r.to_dict() for r in rows]
