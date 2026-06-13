"""MITS Phase 16.A — one test per registered PolicyRule.

Each test synthesizes a minimal :class:`PolicyContext` that triggers
the rule and asserts the produced :class:`BlockingFactor`'s
``legacy_status`` matches the engine's original event["status"]
string.

Where a rule depends on real backend modules (analytics, council,
risk), we monkey-patch the smallest surface needed to keep tests
fast + hermetic.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.bot.decision import rules as rmod
from backend.bot.decision.policy import PolicyContext
from backend.bot.strategies.base import Action, Signal


def _signal(action=Action.BUY_STOCK, confidence=0.9, reason="test",
                strategy="t_strat", **meta):
    return Signal(
        action=action, ticker="AAPL", confidence=confidence,
        reason=reason, strategy=strategy, metadata=meta,
    )


def _ctx(signal=None, **overrides):
    ctx = PolicyContext(
        ticker="AAPL",
        signal=signal or _signal(),
        event={
            "ticker": "AAPL",
            "reason": (signal or _signal()).reason,
            "action": (signal or _signal()).action.value,
            "confidence": (signal or _signal()).confidence,
            "timestamp": "2026-06-11T15:00:00",
        },
        data={"price": 100.0, "iv_rank": 30.0},
        analytics_cfg={"enabled": True},
        ai_config={},
        config={"force_run_when_closed": True, "min_confidence": 0.6},
        kill_active=False,
        portfolio_risk_dict=None,
        eod_bias_map={},
        brain_cooldown={},
        use_brain=False,
        cycle_id="cycle-1",
    )
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


# ── 1. market_closed ───────────────────────────────────────────────────

def test_rule_market_closed_fires_when_closed(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.calendar.is_us_market_open", lambda: False,
    )
    ctx = _ctx()
    ctx.config = {}  # force_run_when_closed off
    bf = rmod.rule_market_closed(ctx)
    assert bf is not None
    assert bf.legacy_status == "market_closed"
    assert bf.category == "market"


def test_rule_market_closed_skipped_when_force_run_when_closed(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.calendar.is_us_market_open", lambda: False,
    )
    ctx = _ctx()
    ctx.config = {"force_run_when_closed": True}
    assert rmod.rule_market_closed(ctx) is None


# ── 2. kill_switch_active ───────────────────────────────────────────────

def test_rule_kill_switch_fires_on_buy():
    ctx = _ctx(kill_active=True)
    bf = rmod.rule_kill_switch_active(ctx)
    assert bf is not None
    assert bf.legacy_status == "kill_switch"


def test_rule_kill_switch_skipped_on_sell():
    ctx = _ctx(signal=_signal(action=Action.SELL_STOCK), kill_active=True)
    assert rmod.rule_kill_switch_active(ctx) is None


# ── 3. options_disabled ─────────────────────────────────────────────────

def test_rule_options_disabled_blocks_buy_call():
    ctx = _ctx(signal=_signal(action=Action.BUY_CALL))
    ctx.config["options_disabled"] = True
    bf = rmod.rule_options_disabled(ctx)
    assert bf is not None
    assert bf.legacy_status == "options_disabled"


def test_rule_options_disabled_skips_stock():
    ctx = _ctx()
    ctx.config["options_disabled"] = True
    assert rmod.rule_options_disabled(ctx) is None


# ── 4. abstain_and_throttle ─────────────────────────────────────────────

def test_rule_abstain_fires_when_decision_marks_abstain(monkeypatch):
    fake_dec = SimpleNamespace(
        abstain=True, reasons=["cohort floor"],
        size_multiplier=1.0, monitor_only=True,
        triggered_rules=["cohort_floor"],
        to_dict=lambda: {"abstain": True},
    )
    monkeypatch.setattr(rmod, "logger", MagicMock())
    monkeypatch.setattr(
        "backend.bot.abstain.abstain_and_throttle",
        lambda **kw: fake_dec,
    )
    monkeypatch.setattr(
        "backend.bot.cohort_matrix.cohort_win_rate",
        lambda *a, **kw: (None, 0),
    )
    bf = rmod.rule_abstain_and_throttle(_ctx())
    assert bf is not None
    assert bf.legacy_status == "abstain"


# ── 5. event_risk_window ────────────────────────────────────────────────

def test_rule_event_risk_window_blocks(monkeypatch):
    perm = SimpleNamespace(can_trade=False, reason="earnings tomorrow",
                              next_window="2026-06-14")
    monkeypatch.setattr(
        "backend.bot.event_risk.can_trade", lambda _t: perm,
    )
    bf = rmod.rule_event_risk_window(_ctx())
    assert bf is not None
    assert bf.legacy_status == "event_hold"


def test_rule_event_risk_window_skipped_on_sell(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.event_risk.can_trade",
        lambda _t: SimpleNamespace(can_trade=False, reason="x",
                                          next_window=None),
    )
    ctx = _ctx(signal=_signal(action=Action.SELL_STOCK))
    assert rmod.rule_event_risk_window(ctx) is None


# ── 6. catalyst_gate ────────────────────────────────────────────────────

def test_rule_catalyst_gate_blocks(monkeypatch):
    fake_gate = SimpleNamespace(
        passes=False, reason="earnings in 1d",
        conviction_multiplier=0.5,
        to_dict=lambda: {"passes": False},
    )
    import backend.bot.gates.catalyst_gate as cg_mod
    monkeypatch.setattr(cg_mod, "check", lambda *a, **kw: fake_gate)
    bf = rmod.rule_catalyst_gate(_ctx())
    assert bf is not None
    assert bf.legacy_status == "catalyst_gate"


# ── 7. analytics_build_failed ───────────────────────────────────────────

def test_rule_analytics_build_failed_on_exception():
    engine = MagicMock()
    engine.evaluate.side_effect = RuntimeError("boom")
    ctx = _ctx(analytics_engine=engine)
    bf = rmod.rule_analytics_build_failed(ctx)
    assert bf is not None
    assert bf.legacy_status == "analytics_failed"


def test_rule_analytics_build_failed_skips_hold():
    ctx = _ctx(signal=_signal(action=Action.HOLD),
                  analytics_engine=MagicMock())
    assert rmod.rule_analytics_build_failed(ctx) is None


# ── 8. signal_hold ──────────────────────────────────────────────────────

def test_rule_signal_hold_fires():
    ctx = _ctx(signal=_signal(action=Action.HOLD, reason="no setup"))
    bf = rmod.rule_signal_hold(ctx)
    assert bf is not None
    assert bf.legacy_status == "hold"
    assert bf.override_event_reason is False


# ── 9. low_confidence ───────────────────────────────────────────────────

def test_rule_low_confidence_fires_below_threshold():
    ctx = _ctx(signal=_signal(confidence=0.3))
    ctx.config["min_confidence"] = 0.6
    bf = rmod.rule_low_confidence(ctx)
    assert bf is not None
    assert bf.legacy_status == "low_confidence"
    assert bf.override_event_reason is False


def test_rule_low_confidence_skipped_above_threshold():
    ctx = _ctx(signal=_signal(confidence=0.9))
    assert rmod.rule_low_confidence(ctx) is None


# ── 10. drift_check_failed ──────────────────────────────────────────────

def test_rule_drift_check_failed_on_exception(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.drift.auto_halt.is_halted",
        lambda _s: (_ for _ in ()).throw(RuntimeError("vendor down")),
    )
    bf = rmod.rule_drift_check_failed(_ctx())
    assert bf is not None
    assert bf.legacy_status == "drift_check_failed"


# ── 11. drift_halt ──────────────────────────────────────────────────────

def test_rule_drift_halt_blocks():
    ctx = _ctx()
    ctx.scratch["drift_check"] = {"strategy": "foo", "halted": True}
    bf = rmod.rule_drift_halt(ctx)
    assert bf is not None
    assert bf.legacy_status == "drift_halt"


# ── 12. low_grade ───────────────────────────────────────────────────────

def test_rule_low_grade_blocks_when_grade_below_min(monkeypatch):
    # Stub the grade gate to refuse.
    monkeypatch.setattr(
        "backend.bot.analytics.gate_by_grade", lambda rank, mg: False,
    )
    monkeypatch.setattr(
        "backend.bot.gates.adaptive.adaptive_min_grade",
        lambda **kw: kw["configured_min_grade"],
    )
    monkeypatch.setattr(
        "backend.api.routes.metrics.build_summary", lambda: {"data": {}},
    )
    ctx = _ctx()
    ctx.analytics_cfg = {"enabled": True, "min_grade": "B"}
    analytics_result = SimpleNamespace(
        rank=SimpleNamespace(grade="C"),
        probability=SimpleNamespace(probability=0.55),
    )
    ctx.scratch["analytics_result"] = analytics_result
    bf = rmod.rule_low_grade(ctx)
    assert bf is not None
    assert bf.legacy_status == "low_grade"


# ── 13. iv_too_rich ─────────────────────────────────────────────────────

def test_rule_iv_too_rich_blocks_brain_long_premium():
    ctx = _ctx(signal=_signal(action=Action.BUY_CALL), use_brain=True)
    ctx.data["iv_rank"] = 85.0
    bf = rmod.rule_iv_too_rich(ctx)
    assert bf is not None
    assert bf.legacy_status == "iv_too_rich"


def test_rule_iv_too_rich_skips_below_threshold():
    ctx = _ctx(signal=_signal(action=Action.BUY_CALL), use_brain=True)
    ctx.data["iv_rank"] = 50.0
    assert rmod.rule_iv_too_rich(ctx) is None


# ── 14. meta_rejected + meta_ai_offline ─────────────────────────────────

def test_rule_meta_rejected_on_veto():
    meta_obj = SimpleNamespace(
        approve=False, reasoning=["risk too high"],
        to_dict=lambda: {"approve": False},
    )
    engine = MagicMock()
    engine.available = True
    engine.audit.return_value = meta_obj
    ctx = _ctx(meta_engine=engine)
    ctx.ai_config["meta_enabled"] = True
    ctx.scratch["analytics_result"] = SimpleNamespace()
    bf = rmod.rule_meta_rejected(ctx)
    assert bf is not None
    assert bf.legacy_status == "meta_rejected"


def test_rule_meta_ai_offline_on_stashed_failure():
    ctx = _ctx()
    ctx.scratch["meta_audit_failure"] = "TimeoutError: 30s"
    bf = rmod.rule_meta_ai_offline(ctx)
    assert bf is not None
    assert bf.severity == "soft"
    assert bf.sizing_penalty_pct == 5.0


# ── 15. consensus_exception ─────────────────────────────────────────────

def test_rule_consensus_exception_on_failure(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("council down")
    monkeypatch.setattr("backend.bot.agents.run_consensus", _boom)
    monkeypatch.setattr(
        "backend.bot.regime.vector.build_regime_vector",
        lambda **kw: SimpleNamespace(to_dict=lambda: {}),
    )
    bf = rmod.rule_consensus_exception(_ctx())
    assert bf is not None
    assert bf.legacy_status == "consensus_failed"


# ── 16. simulator_veto ──────────────────────────────────────────────────

def test_rule_simulator_veto_blocks():
    ctx = _ctx()
    cons = SimpleNamespace(simulator_verdict={"reject_reason": "below CVaR"})
    ctx.scratch["consensus_obj"] = cons
    bf = rmod.rule_simulator_veto(ctx)
    assert bf is not None
    assert bf.legacy_status == "simulator_veto"


# ── 17. portfolio_context_failed ────────────────────────────────────────

def test_rule_portfolio_context_failed_on_exception(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("yfinance down")
    monkeypatch.setattr(
        "backend.bot.portfolio_intel.portfolio_context.build_portfolio_context",
        _boom,
    )
    ctx = _ctx(executor=SimpleNamespace(),
                  account=SimpleNamespace(portfolio_value=10000.0))
    ctx.scratch["consensus_obj"] = SimpleNamespace()
    bf = rmod.rule_portfolio_context_failed(ctx)
    assert bf is not None
    assert bf.legacy_status == "portfolio_context_failed"


# ── 18. correlation_cap_block ───────────────────────────────────────────

def test_rule_correlation_cap_blocks(monkeypatch):
    corr_result = SimpleNamespace(
        blocked=True, reason="cap reached",
        to_dict=lambda: {"blocked": True, "reason": "cap reached"},
    )
    monkeypatch.setattr(
        "backend.bot.gates.correlation_cap_gate.check_correlation_cap",
        lambda **kw: corr_result,
    )
    ctx = _ctx()
    ctx.scratch["portfolio_context"] = SimpleNamespace()
    ctx.scratch["positions_snapshot"] = []
    ctx.scratch["candidate_direction"] = "LONG"
    bf = rmod.rule_correlation_cap_block(ctx)
    assert bf is not None
    assert bf.legacy_status == "correlation_cap_block"


# ── 19. consensus_abstain ───────────────────────────────────────────────

def test_rule_consensus_abstain_legacy_path():
    cons = SimpleNamespace(
        recommendation="abstain", abstain_count=4,
        votes=[1, 2, 3, 4, 5],
        to_dict=lambda: {"chairman_report": {}},
    )
    ctx = _ctx()
    ctx.ai_config["consensus_abstain_enabled"] = True
    ctx.scratch["consensus_obj"] = cons
    bf = rmod.rule_consensus_abstain(ctx)
    assert bf is not None
    assert bf.legacy_status == "consensus_abstain"


def test_rule_consensus_abstain_chairman_monitor():
    cons = SimpleNamespace(
        recommendation="buy", abstain_count=0,
        votes=[],
        to_dict=lambda: {"chairman_report": {"decision": "MONITOR",
                                                  "decision_reason": "vol high"}},
    )
    ctx = _ctx(use_brain=True)
    ctx.scratch["consensus_obj"] = cons
    bf = rmod.rule_consensus_abstain(ctx)
    assert bf is not None
    assert bf.legacy_status == "chairman_monitor"


# ── 20. already_held ────────────────────────────────────────────────────

def test_rule_already_held_blocks_stock():
    ctx = _ctx(held_tickers={"AAPL"})
    bf = rmod.rule_already_held(ctx)
    assert bf is not None
    assert bf.legacy_status == "already_held"


def test_rule_already_held_blocks_option():
    ctx = _ctx(signal=_signal(
        action=Action.BUY_CALL, strike=150.0, expiration="2026-07-18",
    ))
    ctx.held_option_keys = {("AAPL", "call", 150.0, "2026-07-18")}
    bf = rmod.rule_already_held(ctx)
    assert bf is not None
    assert bf.legacy_status == "already_held"


# ── 21. risk_manager_rejected ───────────────────────────────────────────

def test_rule_risk_manager_rejected_blocks():
    decision = SimpleNamespace(
        approved=False, reason="insufficient buying power",
        quantity=0.0,
    )
    rm = MagicMock()
    rm.evaluate.return_value = decision
    ctx = _ctx(
        risk_manager=rm,
        account=SimpleNamespace(buying_power=0.0, portfolio_value=0.0,
                                       open_positions=0, daily_pnl=0.0),
    )
    bf = rmod.rule_risk_manager_rejected(ctx)
    assert bf is not None
    assert bf.legacy_status == "rejected"
    assert bf.override_event_reason is False


# ── 22. dust_order (deferred — call directly) ───────────────────────────

def test_rule_dust_order_blocks_below_min_notional():
    decision = SimpleNamespace(quantity=0.1, reason="ok")
    ctx = _ctx()
    ctx.scratch["risk_decision"] = decision
    ctx.scratch["price"] = 50.0
    ctx.scratch["min_notional"] = 25.0
    bf = rmod.rule_dust_order(ctx)
    assert bf is not None
    assert bf.legacy_status == "too_small"


# ── 23. brain_cooldown ──────────────────────────────────────────────────

def test_rule_brain_cooldown_fires_on_cooldown_hold():
    ctx = _ctx(
        signal=_signal(action=Action.HOLD,
                          reason="cooldown: ticker rejected recently"),
        use_brain=True,
    )
    bf = rmod.rule_brain_cooldown(ctx)
    assert bf is not None
    assert bf.legacy_status == "hold"  # parity preserved
    assert bf.rule == "brain_cooldown"  # but the rule_name distinguishes


# ── 24. memory_bias_failed (soft) ───────────────────────────────────────

def test_rule_memory_bias_failed_on_stashed_failure():
    ctx = _ctx()
    ctx.scratch["memory_bias_failure"] = "NameError: ctx"
    bf = rmod.rule_memory_bias_failed(ctx)
    assert bf is not None
    assert bf.severity == "soft"
    assert bf.sizing_penalty_pct == 0.0


# ── 25. source_scores_unavailable (soft) ────────────────────────────────

def test_rule_source_scores_unavailable_on_stashed_failure():
    ctx = _ctx()
    ctx.scratch["source_scores_failure"] = "yfinance 429"
    bf = rmod.rule_source_scores_unavailable(ctx)
    assert bf is not None
    assert bf.severity == "soft"


# ── 26. cycle_budget_overrun ────────────────────────────────────────────

def test_rule_cycle_budget_overrun_on_stashed_seconds():
    ctx = _ctx()
    ctx.scratch["cycle_budget_overrun_seconds"] = 240.0
    bf = rmod.rule_cycle_budget_overrun(ctx)
    assert bf is not None
    assert bf.legacy_status == "cycle_budget_overrun"


# ── 27. analytics_result_undefined (latent) ─────────────────────────────

def test_rule_analytics_result_undefined_when_missing():
    ctx = _ctx()
    # scratch lacks analytics_result + analytics is enabled
    bf = rmod.rule_analytics_result_undefined(ctx)
    assert bf is not None
    assert bf.legacy_status == "analytics_result_undefined"


def test_rule_analytics_result_undefined_skipped_on_hold():
    ctx = _ctx(signal=_signal(action=Action.HOLD))
    assert rmod.rule_analytics_result_undefined(ctx) is None


# ── 28. memory_bias_dead_code (one-shot) ────────────────────────────────

def test_rule_memory_bias_dead_code_fires_once():
    rmod._MEMORY_BIAS_DEAD_CODE_SEEN["flag"] = False
    ctx = _ctx()
    bf1 = rmod.rule_memory_bias_dead_code(ctx)
    bf2 = rmod.rule_memory_bias_dead_code(ctx)
    assert bf1 is not None
    assert bf2 is None
