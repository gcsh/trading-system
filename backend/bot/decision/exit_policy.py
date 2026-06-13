"""MITS Phase 17.E — declarative exit policy primitives.

Mirrors 16.A's :class:`DecisionPolicy` pattern for option-exit triggers.
Where the old procedural ``decide_exit()`` returned the *first* trigger
that matched (hiding any concurrent ones), :class:`ExitPolicy` evaluates
EVERY registered rule against the live :class:`ExitContext` and surfaces
every trigger that fired — answering Phase 17's 5th observability
question:

    "Why this exact exit?"

The canonical rule list comes from reading the actual ``decide_exit``
body in ``backend/bot/options/exit_manager.py``. It is NOT invented —
each rule here corresponds 1:1 to a code path the legacy function
already exercised:

    1. ``dte_cliff``        — DTE <= cliff + gain_pct > 0 → bank profit.
    2. ``catastrophe_stop`` — gain_pct <= -hard_stop (DTE-adjusted).
    3. ``trailing_stop``    — monitor mode active + gain below trail floor.

A position that touches NONE of these is a HOLD (``should_close=False``).
Back-compat layer: :class:`ExitPolicyResult.legacy_action` is the string
the original ``decide_exit()`` callers consume — ``"close"`` on fire,
``"hold"`` on the no-trigger fall-through. The rich result is delivered
via the new ``decide_exit_with_policy()`` helper alongside.

Invariant (mirrors 16.A): "no trigger fired but ``should_close=True``"
is a registration-order violation — every close MUST be backed by at
least one ExitTrigger.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


# Severities the engine + UI both understand. ``hard`` triggers force a
# close; ``soft`` is advisory (logged + surfaced, but does not flip
# ``should_close`` on its own — reserved for future trailing-advice
# rules that just nudge sizing or warn the operator).
VALID_EXIT_SEVERITIES = {"hard", "soft"}


@dataclass
class ExitContext:
    """Immutable snapshot of an open position's exit-relevant state.

    Built once per cycle per position by ``exit_manager.decide_exit``
    (back-compat path) or ``decide_exit_with_policy`` (the new rich-
    result path). Rules read these fields; they MUST NOT mutate.

    Mirrors the spec's required shape — ``position`` is the live row
    dict (paper_executor.positions() output), ``mark_price`` is the
    per-share mark the manager is judging against. The IV + spread +
    minutes_to_close fields are reserved for future rules; the 3
    cataloged rules use only ``mark_price`` (encoded via gain/peak
    derived numbers), ``iv_now``/``iv_at_entry``, and the dte/cliff
    extras.
    """

    # The actual per-share derived numbers the legacy ``decide_exit``
    # consumed. We carry them explicitly (rather than re-deriving inside
    # each rule) so rule bodies stay declarative + side-effect-free.
    entry_premium_per_share: float
    current_premium_per_share: float
    peak_premium_per_share: Optional[float]
    dte: int
    entry_iv: Optional[float]
    current_iv: Optional[float]

    # Hard-stop + monitor-floor + trailing-distance state computed once
    # by the policy before rules run, so each rule sees consistent
    # numbers (avoids 3 rules each re-computing the floor).
    gain_pct: float
    peak_gain_pct: float
    drawdown_from_peak_pct: float
    hard_stop_pct: float
    monitor_floor_pct: float
    monitor_active: bool
    trailing_floor_pct: Optional[float]
    iv_crush_detected: bool
    dte_cliff: int

    # Optional caller-supplied identifiers — populated on the engine-
    # driven path so persisted ExitRuleEvaluation rows can be joined by
    # ticker + position_id. None on pure-function callers.
    position_id: Optional[int] = None
    ticker: Optional[str] = None

    # Extension point for rules added later (e.g. spread blown, IV
    # spike, must_exit_by_eod). The cataloged 3-rule set does not use
    # this — it's here so the contract matches the spec's shape and
    # future rules don't require a context schema change.
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExitTrigger:
    """One reason the position should close.

    ``rule_name`` is the stable string the veto-budget + ExitRuleEvaluation
    ledger key on. ``severity`` is ``hard`` for the 3 cataloged rules
    (force-close) — reserved ``soft`` slot exists for future advisory
    rules.

    ``legacy_action`` is the back-compat bridge: the string the original
    procedural ``decide_exit`` callers expected. All 3 cataloged rules
    map to ``"close"`` (the old function returned an ExitDecision with
    ``should_exit=True``); the no-trigger fall-through maps to
    ``"hold"``. Future ``reduce_50`` semantics are reserved.

    ``reason`` matches the human-readable string the legacy code emits
    1:1 so persisted Trade.reason + UI strings round-trip unchanged.
    """

    rule_name: str
    severity: str
    legacy_action: str
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    triggered_at: str = ""

    def __post_init__(self) -> None:
        if self.severity not in VALID_EXIT_SEVERITIES:
            raise ValueError(
                f"ExitTrigger {self.rule_name}: invalid severity "
                f"{self.severity!r} (allowed: "
                f"{sorted(VALID_EXIT_SEVERITIES)})"
            )
        if not self.triggered_at:
            self.triggered_at = datetime.utcnow().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "severity": self.severity,
            "legacy_action": self.legacy_action,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "triggered_at": self.triggered_at,
        }


@dataclass
class ExitRuleEvaluation:
    """One row per rule per cycle. Mirrors policy_rule_evaluations.

    ``fired`` is the boolean form of "did this rule produce a trigger?".
    The DB ledger persists every evaluation so the
    ``/exit/veto-budget`` panel can report rolling fire-rate per rule
    across the operator's chosen window.
    """

    rule_name: str
    severity: str
    fired: bool
    legacy_action: Optional[str]
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "severity": self.severity,
            "fired": bool(self.fired),
            "legacy_action": self.legacy_action,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


@dataclass
class ExitPolicyResult:
    """Aggregate verdict from a single :meth:`ExitPolicy.evaluate` pass.

    ``rule_evaluations`` carries one row per registered rule (whether it
    fired or not) so the cockpit can render the full exit ledger for
    the cycle. ``triggers`` is the subset that actually fired.
    ``chosen`` is the headline trigger (first hard in registration
    order; falls back to first soft if no hard fired); it controls
    the legacy back-compat string the engine + paper_executor consume.

    Invariant: ``should_close`` is True iff at least one hard trigger
    fired. ``legacy_action`` is the matched legacy string for back-compat
    callers (``"close"`` when any hard fires, otherwise ``"hold"``).
    """

    rule_evaluations: List[ExitRuleEvaluation]
    triggers: List[ExitTrigger]
    chosen: Optional[ExitTrigger]
    should_close: bool
    legacy_action: str
    evaluated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_evaluations": [r.to_dict() for r in self.rule_evaluations],
            "triggers": [t.to_dict() for t in self.triggers],
            "chosen": self.chosen.to_dict() if self.chosen else None,
            "should_close": bool(self.should_close),
            "legacy_action": self.legacy_action,
            "evaluated_at": self.evaluated_at,
        }


@dataclass
class ExitRule:
    """A single declarative exit evaluator.

    ``name`` matches ``ExitTrigger.rule_name`` and the
    ``exit_rule_evaluations.rule_name`` column. Registration order
    matters: when multiple hard rules fire on the same cycle, the first
    registered hard rule becomes the headline ``chosen`` trigger (and
    its ``legacy_action`` flows back to the engine).
    """

    name: str
    severity: str
    evaluator: Callable[[ExitContext], Optional[ExitTrigger]]
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.severity not in VALID_EXIT_SEVERITIES:
            raise ValueError(
                f"ExitRule {self.name}: invalid severity "
                f"{self.severity!r} (allowed: "
                f"{sorted(VALID_EXIT_SEVERITIES)})"
            )


class ExitPolicy:
    """Ordered registry + evaluator for exit triggers.

    Mirrors :class:`backend.bot.decision.policy.DecisionPolicy` for
    entries. The single behavioural difference: there is no "soft
    sizing-penalty" two-pass — exits are binary (close or hold). Soft
    rules still record their evaluation row + appear in the trigger
    list when they fire, but they never set ``should_close``.
    """

    def __init__(self) -> None:
        self._rules: List[ExitRule] = []
        self._names: set = set()

    def register(self, rule: ExitRule) -> None:
        if rule.name in self._names:
            raise ValueError(f"duplicate ExitRule name: {rule.name}")
        self._names.add(rule.name)
        self._rules.append(rule)

    def enabled_rules(self) -> List[ExitRule]:
        return [r for r in self._rules if r.enabled]

    def all_rules(self) -> List[ExitRule]:
        return list(self._rules)

    def evaluate(self, ctx: ExitContext) -> ExitPolicyResult:
        """Run every enabled rule against ``ctx`` — never short-circuit.

        Every registered rule (regardless of severity) is evaluated in
        registration order. Each result becomes a row in
        ``rule_evaluations``. Triggers (fired rules) flow into
        ``triggers``; the headline ``chosen`` is the first ``hard``
        trigger (or the first ``soft`` trigger when no hard fired).

        ``should_close`` is True iff at least one hard trigger fired —
        matching the contract the engine + paper_executor consumed
        from the legacy ``decide_exit.should_exit``.
        """
        evaluations: List[ExitRuleEvaluation] = []
        triggers: List[ExitTrigger] = []

        for rule in self._rules:
            if not rule.enabled:
                # Even disabled rules deserve a row so the UI can show
                # "rule registered but currently off" rather than the
                # operator wondering if it disappeared. The fire flag
                # is False and the reason carries the disabled marker.
                evaluations.append(ExitRuleEvaluation(
                    rule_name=rule.name,
                    severity=rule.severity,
                    fired=False,
                    legacy_action=None,
                    reason="rule_disabled",
                    evidence={},
                ))
                continue
            trig = rule.evaluator(ctx)
            fired = trig is not None
            evaluations.append(ExitRuleEvaluation(
                rule_name=rule.name,
                severity=rule.severity,
                fired=fired,
                legacy_action=trig.legacy_action if trig else None,
                reason=trig.reason if trig else "",
                evidence=dict(trig.evidence) if trig else {},
            ))
            if trig is not None:
                triggers.append(trig)

        # Pick the headline trigger. First hard wins; otherwise first
        # soft. None when no rule fired.
        chosen: Optional[ExitTrigger] = None
        for t in triggers:
            if t.severity == "hard":
                chosen = t
                break
        if chosen is None and triggers:
            for t in triggers:
                if t.severity == "soft":
                    chosen = t
                    break

        # should_close is hard-only by design. A soft trigger may
        # appear in ``triggers`` + ``chosen`` (when no hard fired) but
        # never forces the close path — back-compat with the legacy
        # function that only returned should_exit=True on hard cases.
        any_hard = any(t.severity == "hard" for t in triggers)
        legacy_action = "close" if any_hard else "hold"

        return ExitPolicyResult(
            rule_evaluations=evaluations,
            triggers=triggers,
            chosen=chosen,
            should_close=any_hard,
            legacy_action=legacy_action,
            evaluated_at=datetime.utcnow().isoformat(),
        )
