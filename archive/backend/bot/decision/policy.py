"""Declarative decision policy primitives.

A :class:`DecisionPolicy` owns an ordered list of :class:`PolicyRule`
instances. Each rule's evaluator examines an immutable
:class:`PolicyContext` and either returns ``None`` (pass) or a
:class:`BlockingFactor` describing why the trade is blocked or soft-
sized.

Evaluation order:

1. All hard rules run first, in registration order. Each is recorded
   in ``rule_evaluations``. If any hard rule blocks, the result is
   ``eligible=False`` — but every hard rule still runs so the
   "Why didn't I trade?" surface can show every concurrent veto.
2. All soft rules run only when no hard rule blocked. Their
   ``sizing_penalty_pct`` values accumulate into
   ``soft_penalties_total_pct``.

The headline blocker is the first hard rule (by registration order)
that returned a BlockingFactor — its ``legacy_status`` becomes
``event["status"]``, preserving the 1:1 contract with the existing UI
and decision_log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


# Six categories the operator monitors on the veto-budget panel.
VALID_CATEGORIES = {
    "market", "strategy", "risk", "execution", "portfolio", "data_quality",
}
VALID_SEVERITIES = {"hard", "soft"}


@dataclass
class BlockingFactor:
    """One reason a candidate trade did not (or barely) make it through.

    ``severity == "hard"`` short-circuits eligibility. ``severity ==
    "soft"`` keeps the trade eligible but accumulates
    ``sizing_penalty_pct`` into the policy result.

    ``legacy_status`` is the original ``event["status"]`` string the
    engine used to emit. Every UI consumer (Mission Control,
    gate_diagnostics, decision_log analytics) reads this string. The
    refactor MUST round-trip it 1:1.

    ``override_event_reason`` controls whether the engine overwrites
    ``event["reason"]`` with this BlockingFactor's reason. Some legacy
    gates (signal_hold, low_confidence, risk_manager_rejected) never
    overwrote the reason — they left signal.reason in place. Setting
    ``override_event_reason=False`` preserves that contract.
    """

    category: str
    rule: str
    severity: str
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    sizing_penalty_pct: float = 0.0
    legacy_status: str = ""
    override_event_reason: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "rule": self.rule,
            "severity": self.severity,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "sizing_penalty_pct": self.sizing_penalty_pct,
            "legacy_status": self.legacy_status,
            "override_event_reason": self.override_event_reason,
        }


@dataclass
class DecisionPolicyResult:
    """Aggregate verdict from one policy evaluation."""

    eligible: bool
    blocking_factors: List[BlockingFactor]
    soft_penalties_total_pct: float
    evaluated_at: datetime
    rule_evaluations: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "eligible": self.eligible,
            "blocking_factors": [b.to_dict() for b in self.blocking_factors],
            "soft_penalties_total_pct": self.soft_penalties_total_pct,
            "evaluated_at": self.evaluated_at.isoformat(),
            "rule_evaluations": list(self.rule_evaluations),
        }

    def headline_blocker(self) -> Optional[BlockingFactor]:
        """Return the first hard BlockingFactor in registration order
        (the one that becomes ``event["status"]``), or ``None`` when the
        candidate is eligible."""
        for b in self.blocking_factors:
            if b.severity == "hard":
                return b
        return None


@dataclass
class PolicyContext:
    """Bundle of everything a rule evaluator may need.

    Built once per ticker per cycle so individual rules don't re-compute
    or re-import. Rules MUST treat fields as read-only — the engine
    holds the only mutating reference to ``event``.

    Some fields are populated lazily by rules earlier in the pipeline
    (e.g. ``rule_analytics_build_failed`` writes ``analytics_result``).
    Downstream rules read those via the ``event`` dict (the canonical
    snapshot the engine already mutates).
    """

    ticker: str
    signal: Any
    event: Dict[str, Any]
    data: Dict[str, Any]
    analytics_cfg: Dict[str, Any]
    ai_config: Dict[str, Any]
    config: Dict[str, Any]
    kill_active: bool
    portfolio_risk_dict: Optional[Dict[str, Any]]
    eod_bias_map: Dict[str, Any]
    brain_cooldown: Dict[str, float]
    use_brain: bool
    cycle_id: str
    # Engine-owned scratch space: the analytics_result + consensus_obj
    # the engine builds inline have to flow back so the persistence + UI
    # path can read them. Rules write into ``scratch`` to share results;
    # nothing else mutates it.
    scratch: Dict[str, Any] = field(default_factory=dict)
    # Held-position sets, threaded so rules can check for pyramiding.
    held_tickers: set = field(default_factory=set)
    held_option_keys: set = field(default_factory=set)
    # The engine's risk manager + account snapshot, needed by risk +
    # execution rules.
    risk_manager: Any = None
    account: Any = None
    # The engine's analytics + meta + executor — risk rules touch them.
    analytics_engine: Any = None
    meta_engine: Any = None
    intraday_classifier: Any = None
    executor: Any = None


@dataclass
class PolicyRule:
    """A single declarative evaluator.

    ``name`` matches the ``rule`` field on the BlockingFactor it
    produces (and the row key in ``policy_rule_evaluations``). The
    veto-budget endpoint groups by this name.

    ``deferred`` marks rules that the policy's main ``evaluate()`` pass
    must skip — the engine invokes their evaluators inline at the
    correct point in the cycle (e.g. ``dust_order`` runs only AFTER
    eod sizing applies its multiplier). The rule still appears in
    ``/policy/rules`` and aggregates into ``/policy/veto-budget``.
    """

    name: str
    category: str
    severity: str
    evaluator: Callable[[PolicyContext], Optional[BlockingFactor]]
    enabled: bool = True
    deferred: bool = False

    def __post_init__(self) -> None:
        if self.category not in VALID_CATEGORIES:
            raise ValueError(
                f"PolicyRule {self.name}: invalid category "
                f"{self.category!r} (allowed: {sorted(VALID_CATEGORIES)})"
            )
        if self.severity not in VALID_SEVERITIES:
            raise ValueError(
                f"PolicyRule {self.name}: invalid severity "
                f"{self.severity!r} (allowed: {sorted(VALID_SEVERITIES)})"
            )


class DecisionPolicy:
    """Ordered registry + evaluator.

    Registration order matters — it determines which hard rule becomes
    the headline blocker when several fire simultaneously.
    """

    def __init__(self) -> None:
        self._rules: List[PolicyRule] = []
        self._names: set = set()

    def register(self, rule: PolicyRule) -> None:
        if rule.name in self._names:
            raise ValueError(f"duplicate PolicyRule name: {rule.name}")
        self._names.add(rule.name)
        self._rules.append(rule)

    def enabled_rules(self) -> List[PolicyRule]:
        return [r for r in self._rules if r.enabled]

    def all_rules(self) -> List[PolicyRule]:
        return list(self._rules)

    def evaluate(self, ctx: PolicyContext) -> DecisionPolicyResult:
        """Run every enabled rule against ``ctx``.

        Two passes:
        - Hard rules run first; every result is collected. Eligibility
          fails as soon as any hard rule blocks, but later hard rules
          still run so the caller sees every concurrent veto.
        - Soft rules run only if every hard rule passed. They accumulate
          ``sizing_penalty_pct`` into the total.

        18-FU Gap 1 — BEFORE running the rules, inject any
        operator-approved policy_tunings overrides into
        ``ctx.scratch['applied_thresholds']``. Tunable rules consult
        the dict via ``policy_apply.resolve_threshold`` and prefer the
        override over the TUNABLE default. The injection is a no-op
        when ``TUNABLES.policy_tuning_auto_apply_enabled`` is OFF;
        scratch keys are still written (as empty dict / list) so rule
        code can read them unconditionally.
        """
        # Lazy import: avoid circular import at module load (policy_apply
        # imports PolicyContext for its type hint).
        try:
            from backend.bot.learning.policy_apply import (
                apply_to_tunable_context,
            )
            apply_to_tunable_context(ctx)
        except Exception:
            # Fail-open: a broken apply path must NEVER block the
            # engine. Worst case, every rule reads the TUNABLE
            # default (same as auto-apply OFF).
            ctx.scratch.setdefault("applied_thresholds", {})
            ctx.scratch.setdefault("applied_threshold_ids", [])

        blocking: List[BlockingFactor] = []
        evaluations: List[Dict[str, Any]] = []
        eligible = True
        soft_total = 0.0

        # First pass: hard rules.
        for rule in self._rules:
            if not rule.enabled or rule.deferred or rule.severity != "hard":
                continue
            bf = rule.evaluator(ctx)
            blocked = bf is not None
            evaluations.append({
                "rule": rule.name,
                "category": rule.category,
                "severity": rule.severity,
                "blocked": blocked,
                "reason": bf.reason if bf else "",
            })
            if bf is not None:
                blocking.append(bf)
                eligible = False

        # Second pass: soft rules only run when eligible.
        if eligible:
            for rule in self._rules:
                if (not rule.enabled or rule.deferred
                        or rule.severity != "soft"):
                    continue
                bf = rule.evaluator(ctx)
                blocked = bf is not None
                evaluations.append({
                    "rule": rule.name,
                    "category": rule.category,
                    "severity": rule.severity,
                    "blocked": blocked,
                    "reason": bf.reason if bf else "",
                })
                if bf is not None:
                    blocking.append(bf)
                    soft_total += float(bf.sizing_penalty_pct or 0.0)

        return DecisionPolicyResult(
            eligible=eligible,
            blocking_factors=blocking,
            soft_penalties_total_pct=round(soft_total, 4),
            evaluated_at=datetime.utcnow(),
            rule_evaluations=evaluations,
        )
