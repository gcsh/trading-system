"""MITS Phase 16.A — declarative decision policy engine.

The engine's per-ticker run_cycle gate stack is rewritten as a list of
``PolicyRule`` evaluators. Each rule returns ``None`` (pass) or a
:class:`BlockingFactor` (block / soft penalty). The aggregate
:class:`DecisionPolicy` walks them in registration order and emits a
:class:`DecisionPolicyResult` that the engine consumes.

Why declarative: the legacy procedural block intermixed control-flow
``continue`` statements with side-effecting status writes. Hidden gates
(every ``try/except`` that silently swallowed a failure) never produced
an event status, so the "Why didn't I trade?" surface could not explain
them. Each rule is now a named, observable function whose every
evaluation is persisted to ``policy_rule_evaluations`` — explicit veto
budget telemetry, no more silent gates.
"""
from backend.bot.decision.policy import (
    BlockingFactor,
    DecisionPolicy,
    DecisionPolicyResult,
    PolicyContext,
    PolicyRule,
)

__all__ = [
    "BlockingFactor",
    "DecisionPolicy",
    "DecisionPolicyResult",
    "PolicyContext",
    "PolicyRule",
]
