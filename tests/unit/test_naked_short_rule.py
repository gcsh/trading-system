"""Fix N=4 — declarative PolicyRule that refuses naked option writes
and unbacked covered structures.

Three branches:
* Naked SELL_CALL / SELL_PUT  → always reject (unlimited loss)
* SELL_COVERED_CALL          → require 100 × contracts long shares
* SELL_CSP                   → require strike × 100 × contracts cash
* Anything else              → pass through (rule doesn't fire)
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.bot.decision import rules as rmod
from backend.bot.decision.policy import PolicyContext
from backend.bot.strategies.base import Action, Signal


def _signal(action: Action, *, strike: float = 100.0,
                contracts: int = 1, ticker: str = "AAPL") -> Signal:
    return Signal(
        action=action, ticker=ticker, confidence=0.9,
        reason="test", strategy="t_strat",
        strike=strike, dte=30,
        metadata={"contracts": contracts, "expiration": "2030-06-21"},
    )


def _fake_executor(positions: list[dict]):
    """Minimal executor stub exposing only ``.positions()`` — the rule
    reads no other methods."""
    return SimpleNamespace(positions=lambda: list(positions))


def _ctx(signal: Signal, *, cash: float = 0.0,
            positions: list[dict] | None = None) -> PolicyContext:
    return PolicyContext(
        ticker=signal.ticker,
        signal=signal,
        event={"ticker": signal.ticker},
        data={"price": 100.0},
        analytics_cfg={"enabled": True},
        ai_config={},
        config={},
        kill_active=False,
        portfolio_risk_dict=None,
        eod_bias_map={},
        brain_cooldown={},
        use_brain=False,
        cycle_id="cycle-1",
        account=SimpleNamespace(cash=cash),
        executor=_fake_executor(positions or []),
    )


# ── naked shorts: always reject ────────────────────────────────────────
#
# SELL_CALL / SELL_PUT aren't in the project's ``Action`` enum (the
# engine only exposes CSP / COVERED_CALL on the SELL side). The rule
# still needs to refuse them in case a future signal source emits an
# ad-hoc action string. We synthesize a duck-typed signal via
# SimpleNamespace so we hit the rule's action-string branch directly.


def _naked_signal(action_str: str, ticker: str = "AAPL") -> SimpleNamespace:
    return SimpleNamespace(
        action=SimpleNamespace(value=action_str),
        ticker=ticker, strike=100.0, confidence=0.9, reason="t",
        strategy="t_strat", metadata={"contracts": 1},
    )


def _naked_ctx(signal, *, cash: float = 1_000_000.0) -> PolicyContext:
    return PolicyContext(
        ticker=signal.ticker, signal=signal, event={}, data={},
        analytics_cfg={}, ai_config={}, config={}, kill_active=False,
        portfolio_risk_dict=None, eod_bias_map={}, brain_cooldown={},
        use_brain=False, cycle_id="cycle-1",
        account=SimpleNamespace(cash=cash),
        executor=_fake_executor([]),
    )


def test_naked_sell_call_blocked():
    """Naked short call has unlimited upside loss. Always reject."""
    bf = rmod.rule_naked_short_block(
        _naked_ctx(_naked_signal("SELL_CALL")),
    )
    assert bf is not None
    assert "naked" in bf.reason.lower()
    assert "SELL_CALL" in bf.reason


def test_naked_sell_put_blocked():
    """SELL_PUT (the naked variant, not SELL_CSP) is also unlimited
    risk in the cash sense; refuse it."""
    bf = rmod.rule_naked_short_block(
        _naked_ctx(_naked_signal("SELL_PUT")),
    )
    assert bf is not None
    assert "naked" in bf.reason.lower()


# ── SELL_COVERED_CALL: shares quantity check ──────────────────────────


def test_covered_call_with_exactly_100_shares_held_approved():
    """qty=1 contract → 100 shares required; 100 held → OK."""
    positions = [{"kind": "stock", "ticker": "AAPL", "quantity": 100}]
    ctx = _ctx(
        _signal(Action.SELL_COVERED_CALL, contracts=1),
        positions=positions,
    )
    assert rmod.rule_naked_short_block(ctx) is None


def test_covered_call_with_50_shares_held_rejected():
    """qty=1 contract → 100 shares required; 50 held → REJECT."""
    positions = [{"kind": "stock", "ticker": "AAPL", "quantity": 50}]
    ctx = _ctx(
        _signal(Action.SELL_COVERED_CALL, contracts=1),
        positions=positions,
    )
    bf = rmod.rule_naked_short_block(ctx)
    assert bf is not None
    assert bf.evidence["shares_required"] == 100
    assert bf.evidence["shares_have"] == 50


def test_covered_call_with_200_shares_qty_2_approved():
    """qty=2 contracts → 200 shares; 200 held → OK."""
    positions = [{"kind": "stock", "ticker": "AAPL", "quantity": 200}]
    ctx = _ctx(
        _signal(Action.SELL_COVERED_CALL, contracts=2),
        positions=positions,
    )
    assert rmod.rule_naked_short_block(ctx) is None


def test_covered_call_with_200_shares_qty_3_rejected():
    """qty=3 contracts → 300 shares; 200 held → REJECT."""
    positions = [{"kind": "stock", "ticker": "AAPL", "quantity": 200}]
    ctx = _ctx(
        _signal(Action.SELL_COVERED_CALL, contracts=3),
        positions=positions,
    )
    bf = rmod.rule_naked_short_block(ctx)
    assert bf is not None
    assert bf.evidence["shares_required"] == 300
    assert bf.evidence["shares_have"] == 200


def test_covered_call_with_other_ticker_shares_rejected():
    """Holding 100 shares of MSFT does NOT cover a SELL_COVERED_CALL
    on AAPL. The rule must read ticker."""
    positions = [{"kind": "stock", "ticker": "MSFT", "quantity": 100}]
    ctx = _ctx(
        _signal(Action.SELL_COVERED_CALL, ticker="AAPL", contracts=1),
        positions=positions,
    )
    bf = rmod.rule_naked_short_block(ctx)
    assert bf is not None
    assert bf.evidence["shares_have"] == 0


# ── SELL_CSP: cash collateral check ────────────────────────────────────


def test_csp_with_exact_collateral_approved():
    """strike=10 × qty=1 = $1,000 required; cash=$1,000 → OK."""
    ctx = _ctx(
        _signal(Action.SELL_CSP, strike=10.0, contracts=1),
        cash=1_000.0,
    )
    assert rmod.rule_naked_short_block(ctx) is None


def test_csp_one_dollar_short_rejected():
    """$999 cash vs $1000 required → REJECT (locks the strict-less-than
    semantics, not <=)."""
    ctx = _ctx(
        _signal(Action.SELL_CSP, strike=10.0, contracts=1),
        cash=999.0,
    )
    bf = rmod.rule_naked_short_block(ctx)
    assert bf is not None
    assert bf.evidence["cash_required"] == 1000.0
    assert bf.evidence["cash_have"] == 999.0


def test_csp_gs_1010_strike_with_391_cash_rejected():
    """The smoking-gun case from the 2026-06-13 cascade. GS strike
    $1010 × 1 contract = $101,000 required; $391.66 cash → REJECT.

    Evidence must surface ``cash_required=101_000`` so the operator
    sees the gap at a glance."""
    ctx = _ctx(
        _signal(Action.SELL_CSP, strike=1010.0, contracts=1,
                  ticker="GS"),
        cash=391.66,
    )
    bf = rmod.rule_naked_short_block(ctx)
    assert bf is not None
    assert bf.evidence["cash_required"] == 101_000.0
    assert bf.evidence["cash_have"] == 391.66
    assert "101,000" in bf.reason


# ── pass-through for non-short actions ─────────────────────────────────


def test_buy_stock_does_not_fire():
    """The rule MUST be a pass-through for stock and long-option
    actions — the policy chain has other gates for those."""
    ctx = _ctx(_signal(Action.BUY_STOCK), cash=10_000.0)
    assert rmod.rule_naked_short_block(ctx) is None


def test_buy_call_does_not_fire():
    ctx = _ctx(_signal(Action.BUY_CALL), cash=10_000.0)
    assert rmod.rule_naked_short_block(ctx) is None


def test_hold_does_not_fire():
    ctx = _ctx(_signal(Action.HOLD), cash=10_000.0)
    assert rmod.rule_naked_short_block(ctx) is None


# ── rule is registered + ordered correctly ─────────────────────────────


def test_rule_is_registered_before_correlation_cap():
    """Registration order matters — Fix N=4 must fire BEFORE
    correlation_cap_block so a naked short never reaches the
    correlation gate."""
    from backend.bot.decision.policy import DecisionPolicy
    from backend.bot.decision.rules import _register_all
    policy = DecisionPolicy()
    _register_all(policy)
    names = [r.name for r in policy.all_rules()]
    assert "naked_short_block" in names
    assert names.index("naked_short_block") < names.index(
        "correlation_cap_block"
    )
