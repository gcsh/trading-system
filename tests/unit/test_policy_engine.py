"""MITS Phase 16.A — DecisionPolicy primitives.

Covers the registration / evaluation / soft-penalty semantics that the
engine relies on. Rule-specific behavior lives in
``test_policy_rules.py``; this file pins down the orchestration.
"""
from __future__ import annotations

import pytest

from backend.bot.decision.policy import (
    BlockingFactor,
    DecisionPolicy,
    PolicyContext,
    PolicyRule,
)


def _ctx() -> PolicyContext:
    """Minimal context — rule evaluators ignore everything they don't
    look at; engine fields default to None."""
    return PolicyContext(
        ticker="AAPL", signal=None, event={}, data={},
        analytics_cfg={}, ai_config={}, config={},
        kill_active=False, portfolio_risk_dict=None,
        eod_bias_map={}, brain_cooldown={}, use_brain=False,
        cycle_id="cycle-1",
    )


def _block(rule: str, severity: str = "hard", penalty: float = 0.0):
    return lambda ctx: BlockingFactor(
        category="market", rule=rule, severity=severity,
        reason=f"{rule}-blocked", evidence={"rule": rule},
        sizing_penalty_pct=penalty, legacy_status=rule,
    )


def _pass(_ctx):
    return None


def test_duplicate_rule_name_rejected():
    p = DecisionPolicy()
    p.register(PolicyRule("a", "market", "hard", _pass))
    with pytest.raises(ValueError):
        p.register(PolicyRule("a", "market", "hard", _pass))


def test_invalid_category_rejected():
    with pytest.raises(ValueError):
        PolicyRule("x", "bogus", "hard", _pass)


def test_invalid_severity_rejected():
    with pytest.raises(ValueError):
        PolicyRule("x", "market", "wibble", _pass)


def test_evaluate_all_pass_returns_eligible():
    p = DecisionPolicy()
    p.register(PolicyRule("a", "market", "hard", _pass))
    p.register(PolicyRule("b", "strategy", "soft", _pass))
    r = p.evaluate(_ctx())
    assert r.eligible is True
    assert r.blocking_factors == []
    assert r.soft_penalties_total_pct == 0.0
    assert len(r.rule_evaluations) == 2


def test_single_hard_block_makes_ineligible():
    p = DecisionPolicy()
    p.register(PolicyRule("a", "market", "hard", _block("a")))
    p.register(PolicyRule("b", "strategy", "hard", _pass))
    r = p.evaluate(_ctx())
    assert r.eligible is False
    assert [b.rule for b in r.blocking_factors] == ["a"]
    assert r.headline_blocker().rule == "a"


def test_multiple_hard_blocks_all_collected_headline_is_first():
    p = DecisionPolicy()
    p.register(PolicyRule("a", "market", "hard", _block("a")))
    p.register(PolicyRule("b", "strategy", "hard", _block("b")))
    p.register(PolicyRule("c", "risk", "hard", _pass))
    r = p.evaluate(_ctx())
    assert r.eligible is False
    # Both hard rules fired, but headline blocker is the first
    # registered.
    assert [b.rule for b in r.blocking_factors] == ["a", "b"]
    assert r.headline_blocker().rule == "a"


def test_soft_rules_skipped_when_a_hard_blocks():
    p = DecisionPolicy()
    p.register(PolicyRule("hard_block", "market", "hard", _block("hard_block")))
    p.register(PolicyRule("soft_a", "strategy", "soft",
                                _block("soft_a", "soft", 5.0)))
    r = p.evaluate(_ctx())
    assert r.eligible is False
    assert [b.rule for b in r.blocking_factors] == ["hard_block"]
    assert r.soft_penalties_total_pct == 0.0


def test_soft_penalties_accumulate_when_eligible():
    p = DecisionPolicy()
    p.register(PolicyRule("a", "market", "soft",
                                _block("a", "soft", 3.0)))
    p.register(PolicyRule("b", "strategy", "soft",
                                _block("b", "soft", 2.5)))
    r = p.evaluate(_ctx())
    assert r.eligible is True
    assert pytest.approx(r.soft_penalties_total_pct) == 5.5
    assert [b.rule for b in r.blocking_factors] == ["a", "b"]


def test_disabled_rule_is_skipped():
    p = DecisionPolicy()
    p.register(PolicyRule("a", "market", "hard", _block("a"), enabled=False))
    p.register(PolicyRule("b", "strategy", "hard", _pass))
    r = p.evaluate(_ctx())
    assert r.eligible is True
    assert r.blocking_factors == []
    # all_rules() still includes the disabled one.
    assert {r.name for r in p.all_rules()} == {"a", "b"}
    assert {r.name for r in p.enabled_rules()} == {"b"}


def test_deferred_rule_is_skipped_from_evaluate_but_visible():
    p = DecisionPolicy()
    p.register(PolicyRule("a", "market", "hard", _block("a"), deferred=True))
    p.register(PolicyRule("b", "strategy", "hard", _pass))
    r = p.evaluate(_ctx())
    assert r.eligible is True
    assert {ev["rule"] for ev in r.rule_evaluations} == {"b"}
    # but ``all_rules`` includes the deferred one so /policy/rules shows it
    assert {r.name for r in p.all_rules()} == {"a", "b"}


def test_blocking_factor_to_dict_round_trip():
    bf = BlockingFactor(
        category="strategy", rule="x", severity="hard",
        reason="why", evidence={"a": 1}, sizing_penalty_pct=2.5,
        legacy_status="x_status", override_event_reason=False,
    )
    d = bf.to_dict()
    assert d == {
        "category": "strategy", "rule": "x", "severity": "hard",
        "reason": "why", "evidence": {"a": 1},
        "sizing_penalty_pct": 2.5, "legacy_status": "x_status",
        "override_event_reason": False,
    }


def test_result_to_dict_shape():
    p = DecisionPolicy()
    p.register(PolicyRule("a", "market", "soft",
                                _block("a", "soft", 4.0)))
    r = p.evaluate(_ctx())
    d = r.to_dict()
    assert d["eligible"] is True
    assert d["soft_penalties_total_pct"] == 4.0
    assert len(d["blocking_factors"]) == 1
    assert d["rule_evaluations"][0]["rule"] == "a"
    assert "evaluated_at" in d
