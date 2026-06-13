"""MITS Phase 17.E — engine close-path integration.

Strategy:
  * Synthesize a closed-Trade row populated from a real
    ExitPolicyResult (TP hit) — confirm exit_policy_result_json
    round-trips through the ORM, the cockpit Trade.to_dict surfaces it,
    and ExitRuleEvaluation rows are written.
  * Synthesize a HOLD cycle — confirm should_close=False, every rule
    recorded as fired=False, and no Trade row would be written.

Why not run a full engine cycle? The exit_manager path lives inside
``_close_option_via_exit_manager`` which requires a live executor,
chain quotes, and the position-MTM stack — too many moving parts to
wire deterministically in CI. The plumbing-level integration here is
the appropriate granularity for the 17.E back-compat invariant:
the rich result must persist + the legacy decision string must match.
"""
from __future__ import annotations

import json

import pytest

from backend.bot.decision.exit_rules import build_default_policy
from backend.bot.options.exit_manager import (
    _build_exit_context,
    decide_exit,
    decide_exit_with_policy,
    persist_exit_evaluations,
)
from backend.db import session_scope
from backend.models.exit_rule_evaluation import ExitRuleEvaluation
from backend.models.trade import Trade


pytestmark = [pytest.mark.integration]


def test_close_path_persists_exit_policy_result_json(temp_db):
    """Take-profit hit → decide_exit returns 'close-equivalent',
    Trade row carries exit_policy_result_json, ExitRuleEvaluation rows
    are persisted, cockpit Trade.to_dict surfaces the rich dict."""
    # Build a TP-hit ExitContext: entry $5, current $9, peak $15,
    # dte 21. This trips the trailing_stop rule.
    decision, result = decide_exit_with_policy(
        entry_premium_per_share=5.0,
        current_premium_per_share=9.0,
        peak_premium_per_share=15.0,
        dte=21,
        entry_iv=0.30,
        current_iv=0.30,
        position_id=42,
        ticker="AAPL",
    )
    assert decision.should_exit is True
    assert result.should_close is True
    assert result.chosen is not None
    assert result.legacy_action == "close"

    # Persist the per-rule ledger.
    persist_exit_evaluations(
        result=result, position_id=42, ticker="AAPL",
    )
    with session_scope() as session:
        rows = session.query(ExitRuleEvaluation).filter(
            ExitRuleEvaluation.position_id == 42,
        ).all()
        assert len(rows) == 3, "every cataloged rule must record a row"
        fired_names = {r.rule_name for r in rows if r.fired}
        # trailing_stop is the trigger here.
        assert "trailing_stop" in fired_names

    # Synthesize a closing Trade row carrying exit_policy_result_json.
    serialized = json.dumps(result.to_dict())
    with session_scope() as session:
        trade = Trade(
            ticker="AAPL", action="CLOSE_OPTION", quantity=1,
            price=9.0, strategy="exit_manager", signal_source="live_engine",
            confidence=1.0, reason=decision.reason, paper=1, pnl=400.0,
            status="closed", instrument="option", option_type="call",
            strike=200.0, expiration="2026-07-19", contracts=1,
            exit_policy_result_json=serialized,
        )
        session.add(trade)
        session.flush()
        trade_id = int(trade.id)

    # Round-trip through Trade.to_dict (the cockpit consumes this).
    with session_scope() as session:
        row = session.query(Trade).filter(Trade.id == trade_id).first()
        assert row is not None
        d = row.to_dict()
        epr = d["exit_policy_result"]
        assert epr is not None
        assert epr["should_close"] is True
        assert epr["legacy_action"] == "close"
        assert epr["chosen"]["rule_name"] == "trailing_stop"
        rule_names = {r["rule_name"] for r in epr["rule_evaluations"]}
        assert rule_names == {
            "dte_cliff", "catastrophe_stop", "trailing_stop",
        }


def test_hold_cycle_records_every_rule_not_fired(temp_db):
    """No trigger → decide_exit returns hold; rule ledger shows
    every cataloged rule with fired=False."""
    decision, result = decide_exit_with_policy(
        entry_premium_per_share=5.0,
        current_premium_per_share=5.05,
        peak_premium_per_share=5.05,
        dte=21,
        entry_iv=0.30,
        current_iv=0.30,
        position_id=99,
        ticker="MSFT",
    )
    assert decision.should_exit is False
    assert result.should_close is False
    assert result.legacy_action == "hold"
    assert result.chosen is None
    assert result.triggers == []

    persist_exit_evaluations(
        result=result, position_id=99, ticker="MSFT",
    )
    with session_scope() as session:
        rows = session.query(ExitRuleEvaluation).filter(
            ExitRuleEvaluation.position_id == 99,
        ).all()
        assert len(rows) == 3
        assert all(not r.fired for r in rows)
        assert all(r.ticker == "MSFT" for r in rows)


def test_decide_exit_back_compat_close_string(temp_db):
    """The legacy decide_exit() callers consume ExitDecision.should_exit
    + ExitDecision.reason verbatim. Confirm the pre-refactor strings
    round-trip 1:1 for the 3 trigger families + the 3 hold flavours."""
    # 1. dte_cliff
    d = decide_exit(
        entry_premium_per_share=5.0, current_premium_per_share=5.25,
        peak_premium_per_share=5.25, dte=2,
        entry_iv=0.30, current_iv=0.30,
    )
    assert d.should_exit is True
    assert d.reason.startswith("DTE 2 ≤ 3 (theta cliff)")

    # 2. catastrophe_stop
    d = decide_exit(
        entry_premium_per_share=5.0, current_premium_per_share=2.5,
        peak_premium_per_share=5.0, dte=21,
        entry_iv=0.30, current_iv=0.30,
    )
    assert d.should_exit is True
    assert d.reason.startswith("catastrophe stop:")

    # 3. trailing_stop
    d = decide_exit(
        entry_premium_per_share=5.0, current_premium_per_share=9.0,
        peak_premium_per_share=15.0, dte=21,
        entry_iv=0.30, current_iv=0.30,
    )
    assert d.should_exit is True
    assert d.reason.startswith("trail hit:")

    # 4. early-phase hold
    d = decide_exit(
        entry_premium_per_share=5.0, current_premium_per_share=5.05,
        peak_premium_per_share=5.05, dte=21,
        entry_iv=0.30, current_iv=0.30,
    )
    assert d.should_exit is False
    assert "early phase" in d.reason

    # 5. monitoring hold
    d = decide_exit(
        entry_premium_per_share=5.0, current_premium_per_share=6.0,
        peak_premium_per_share=6.0, dte=21,
        entry_iv=0.30, current_iv=0.30,
    )
    assert d.should_exit is False
    assert d.reason.startswith("monitoring:")

    # 6. holding-at-loss hold (catastrophe not yet tripped)
    d = decide_exit(
        entry_premium_per_share=5.0, current_premium_per_share=4.5,
        peak_premium_per_share=5.0, dte=21,
        entry_iv=0.30, current_iv=0.30,
    )
    assert d.should_exit is False
    assert d.reason.startswith("holding:")
