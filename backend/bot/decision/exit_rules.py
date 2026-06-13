"""Registered rule library for the declarative exit policy.

Every exit trigger the legacy procedural ``decide_exit`` block enforced
is implemented here as one ``rule_*`` function. Each function reads
:class:`ExitContext`, performs the same check the legacy code inlined,
and returns an :class:`ExitTrigger` or ``None``.

The canonical 3-rule set comes from reading the actual ``decide_exit``
body in ``backend/bot/options/exit_manager.py``:

    1. ``dte_cliff``        — DTE <= cliff + gain > 0 → bank profit.
    2. ``catastrophe_stop`` — gain <= -hard_stop (DTE-adjusted).
    3. ``trailing_stop``    — monitor mode + gain below floor.

Reason strings round-trip 1:1 with the legacy emitter so persisted
Trade.reason + UI labels are byte-identical pre/post refactor.

Future rules (iv_crush_advisory, spread_blown, must_exit_by_eod) can
slot into ``_register_all`` without changing the contract — the policy
runs them all and the ledger records every evaluation.
"""
from __future__ import annotations

from typing import Optional

from backend.bot.decision.exit_policy import (
    ExitContext,
    ExitPolicy,
    ExitRule,
    ExitTrigger,
)


# ── 1. dte_cliff ───────────────────────────────────────────────────────

def rule_dte_cliff(ctx: ExitContext) -> Optional[ExitTrigger]:
    """Theta cliff: at very low DTE, any profit must be banked.

    Mirrors the legacy guard ``if dte <= dte_cliff and gain_pct > 0:``.
    The reason string is reproduced byte-for-byte so persisted
    Trade.reason rows + UI strings round-trip unchanged.
    """
    if not (ctx.dte <= ctx.dte_cliff and ctx.gain_pct > 0):
        return None
    return ExitTrigger(
        rule_name="dte_cliff",
        severity="hard",
        legacy_action="close",
        reason=(
            f"DTE {ctx.dte} ≤ {ctx.dte_cliff} (theta cliff) — "
            f"banking +{ctx.gain_pct:.1f}% before decay"
        ),
        evidence={
            "dte": ctx.dte,
            "dte_cliff": ctx.dte_cliff,
            "gain_pct": round(ctx.gain_pct, 4),
        },
    )


# ── 2. catastrophe_stop ────────────────────────────────────────────────

def rule_catastrophe_stop(ctx: ExitContext) -> Optional[ExitTrigger]:
    """DTE-adjusted hard floor on losses.

    Mirrors ``if gain_pct <= -hard_stop:``. The hard_stop value was
    already DTE-adjusted by the policy builder; we just consume it
    here so the rule body stays declarative.
    """
    if not (ctx.gain_pct <= -ctx.hard_stop_pct):
        return None
    return ExitTrigger(
        rule_name="catastrophe_stop",
        severity="hard",
        legacy_action="close",
        reason=(
            f"catastrophe stop: {ctx.gain_pct:.1f}% ≤ "
            f"-{ctx.hard_stop_pct:.0f}% (DTE {ctx.dte})"
        ),
        evidence={
            "gain_pct": round(ctx.gain_pct, 4),
            "hard_stop_pct": round(ctx.hard_stop_pct, 4),
            "dte": ctx.dte,
        },
    )


# ── 3. trailing_stop ───────────────────────────────────────────────────

def rule_trailing_stop(ctx: ExitContext) -> Optional[ExitTrigger]:
    """Monitor-mode trailing stop — no upper ceiling on winners.

    Mirrors ``if monitor_active and trailing_floor is not None and
    gain_pct < trailing_floor:``. The trailing floor was computed once
    by the policy builder so this rule does not re-derive it.

    The ``(IV crush)`` suffix on the reason string is preserved 1:1
    with the legacy emitter — without it the persisted Trade.reason
    would differ pre/post refactor.
    """
    if not ctx.monitor_active:
        return None
    if ctx.trailing_floor_pct is None:
        return None
    if not (ctx.gain_pct < ctx.trailing_floor_pct):
        return None
    crush_note = " (IV crush)" if ctx.iv_crush_detected else ""
    return ExitTrigger(
        rule_name="trailing_stop",
        severity="hard",
        legacy_action="close",
        reason=(
            f"trail hit: peak +{ctx.peak_gain_pct:.1f}% → now "
            f"+{ctx.gain_pct:.1f}%, gave back "
            f"{ctx.drawdown_from_peak_pct:.1f}%{crush_note}"
        ),
        evidence={
            "peak_gain_pct": round(ctx.peak_gain_pct, 4),
            "gain_pct": round(ctx.gain_pct, 4),
            "drawdown_from_peak_pct": round(ctx.drawdown_from_peak_pct, 4),
            "trailing_floor_pct": round(ctx.trailing_floor_pct, 4),
            "iv_crush_detected": bool(ctx.iv_crush_detected),
        },
    )


def _register_all(policy: ExitPolicy) -> None:
    """Register every exit rule in the order ``ExitPolicy.evaluate``
    must walk them.

    Order matters: it determines which hard rule becomes the headline
    ``chosen`` trigger when several fire on the same cycle. The chosen
    ordering matches the legacy procedural cascade so that pre-refactor
    and post-refactor see the SAME headline:

        1. dte_cliff        — banked profit beats theta bleed
        2. catastrophe_stop — hard floor on losses
        3. trailing_stop    — monitor-mode trail (only active above +15%)

    Concurrency cases that flip pre/post:
        * dte_cliff + catastrophe_stop CANNOT both fire (cliff requires
          gain > 0, catastrophe requires gain <= -hard_stop).
        * dte_cliff + trailing_stop is possible at low DTE on a profitable
          giveback. Legacy returned dte_cliff first; we preserve that.
        * catastrophe_stop + trailing_stop is possible (large drawdown
          past both floors). Legacy returned catastrophe first; we
          preserve that.
    """
    policy.register(ExitRule(
        name="dte_cliff",
        severity="hard",
        evaluator=rule_dte_cliff,
    ))
    policy.register(ExitRule(
        name="catastrophe_stop",
        severity="hard",
        evaluator=rule_catastrophe_stop,
    ))
    policy.register(ExitRule(
        name="trailing_stop",
        severity="hard",
        evaluator=rule_trailing_stop,
    ))


def build_default_policy() -> ExitPolicy:
    """Convenience: an ExitPolicy with the canonical 3-rule set."""
    p = ExitPolicy()
    _register_all(p)
    return p
