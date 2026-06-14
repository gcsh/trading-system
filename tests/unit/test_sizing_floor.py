"""Fix N=3 — drop the ``max(1, …)`` sizing floor.

The legacy ``build_order_plan`` had:

    contracts = max(1, int(quantity * price / 10_000)) if price else 1

That floor force-promoted ANY sub-$10k notional to 1 contract — the
RiskManager's careful $5k-account sizing decision was being silently
overridden, which is how the 2026-06-13 trial wrote 14 options
contracts. Now a sub-threshold size returns 0 contracts and the
build_order_plan stamps ``skip=True`` so ``_finalize_execution``
records a transparent ``skipped`` event instead of force-writing
junk.
"""
from __future__ import annotations

from backend.bot.engine import BotEngine
from backend.bot.strategies.base import Action, Signal


def _engine() -> BotEngine:
    """Bare BotEngine — build_order_plan is method-only on this class
    and doesn't need brokers / market-data wired."""
    return BotEngine.__new__(BotEngine)


def _signal(action: Action, strike: float = 100.0) -> Signal:
    return Signal(
        action=action, ticker="AAPL", confidence=0.9,
        reason="test", strategy="t_strat",
        strike=strike, dte=30, metadata={"expiration": "2030-06-21"},
    )


# ── below-threshold size returns 0 contracts + skip marker ─────────────


def test_buy_call_below_threshold_returns_zero_contracts():
    """quantity × price / 10_000 = 0.05 → 0 contracts. Plan must be
    marked skip so _finalize_execution skips submission."""
    eng = _engine()
    sig = _signal(Action.BUY_CALL)
    # 0.05 × 100 / 10_000 = 0.0005 → int() = 0
    plan = eng.build_order_plan(sig, quantity=0.05, price=100.0)
    assert plan["contracts"] == 0
    assert plan["quantity"] == 0
    assert plan.get("skip") is True
    assert plan.get("skip_reason") == "risk_size_below_one_contract"


def test_sell_csp_below_threshold_returns_zero_contracts():
    """Same floor applies to the short-option path. RiskManager said
    'no' → engine respects it."""
    eng = _engine()
    sig = _signal(Action.SELL_CSP)
    plan = eng.build_order_plan(sig, quantity=0.05, price=100.0)
    assert plan["contracts"] == 0
    assert plan.get("skip") is True


def test_buy_call_at_threshold_returns_one_contract():
    """quantity × price / 10_000 = 1.5 → int() = 1 contract."""
    eng = _engine()
    sig = _signal(Action.BUY_CALL)
    # 150 × 100 / 10_000 = 1.5
    plan = eng.build_order_plan(sig, quantity=150.0, price=100.0)
    assert plan["contracts"] == 1
    assert plan["quantity"] == 1
    assert not plan.get("skip")


def test_buy_call_well_above_threshold_returns_floor_int():
    """quantity × price / 10_000 = 3.2 → int() = 3 contracts. Locks
    in the truncation behavior (no rounding up)."""
    eng = _engine()
    sig = _signal(Action.BUY_CALL)
    # 320 × 100 / 10_000 = 3.2
    plan = eng.build_order_plan(sig, quantity=320.0, price=100.0)
    assert plan["contracts"] == 3
    assert plan["quantity"] == 3


def test_buy_call_zero_price_returns_zero_contracts():
    """A zero price (market data hole) used to force-promote to 1
    contract via the ``if price else 1`` arm. With the floor removed
    we get 0 + skip, which is the safe behavior when price is bad."""
    eng = _engine()
    sig = _signal(Action.BUY_CALL)
    plan = eng.build_order_plan(sig, quantity=100.0, price=0.0)
    assert plan["contracts"] == 0
    assert plan.get("skip") is True


def test_sell_covered_call_below_threshold_returns_zero_contracts():
    """Cover the SINGLE_LEG_SHORT_OPTIONS branch for SELL_COVERED_CALL
    too, not just SELL_CSP."""
    eng = _engine()
    sig = _signal(Action.SELL_COVERED_CALL)
    plan = eng.build_order_plan(sig, quantity=0.01, price=100.0)
    assert plan["contracts"] == 0
    assert plan.get("skip") is True


def test_iron_condor_below_threshold_returns_zero_contracts():
    """Multi-leg / spread branch was also force-promoting."""
    eng = _engine()
    sig = _signal(Action.IRON_CONDOR)
    plan = eng.build_order_plan(sig, quantity=0.01, price=100.0)
    assert plan["contracts"] == 0
    assert plan.get("skip") is True
    assert plan["instrument"] == "spread"
