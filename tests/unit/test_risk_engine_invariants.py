"""Risk-engine business invariants — the daily-loss circuit breaker
and position-sizing math have direct money impact. Every test here
exists because a bug in this class would either lose real money
(blow through the loss limit) or silently allow excessive risk
(over-sized positions).

QA framework category: Risk Engine, Trading Controls (sections 23-24).
"""
from __future__ import annotations

import pytest

from backend.bot.risk import AccountState, RiskManager


@pytest.fixture
def base_account():
    return AccountState(
        buying_power=5000.0,
        portfolio_value=5000.0,
        open_positions=0,
        daily_pnl=0.0,
    )


@pytest.fixture
def risk():
    return RiskManager({
        "risk": {
            "max_position_size_usd": 1000.0,
            "max_open_positions": 5,
            "daily_loss_limit_usd": 300.0,
            "stop_loss_pct": 5.0,
            "take_profit_pct": 10.0,
            "max_cash_usage_pct": 50.0,
        }
    })


@pytest.mark.risk
@pytest.mark.unit
class TestDailyLossCircuitBreaker:
    """The breaker is a hard stop. A bug here loses real money."""

    def test_breaker_fires_at_exact_limit(self, risk, base_account):
        base_account.daily_pnl = -300.0
        assert risk.circuit_breaker_tripped(base_account) is True

    def test_breaker_fires_below_limit(self, risk, base_account):
        base_account.daily_pnl = -300.01
        assert risk.circuit_breaker_tripped(base_account) is True

    def test_breaker_quiet_above_limit(self, risk, base_account):
        base_account.daily_pnl = -299.99
        assert risk.circuit_breaker_tripped(base_account) is False

    def test_breaker_uses_abs_so_positive_limit_works(self, risk, base_account):
        # Operators may write the limit as a positive number, the breaker
        # must still treat it as -300.
        risk.daily_loss_limit_usd = 300.0
        base_account.daily_pnl = -350.0
        assert risk.circuit_breaker_tripped(base_account) is True

    def test_evaluate_rejects_trade_when_breaker_tripped(self, risk, base_account):
        base_account.daily_pnl = -500.0
        decision = risk.evaluate("BUY", 100.0, base_account, is_paper=True)
        assert decision.approved is False
        assert "circuit breaker" in decision.reason.lower()


@pytest.mark.risk
@pytest.mark.unit
class TestPositionSizing:
    """Sizing must respect EVERY cap, not just one of them."""

    def test_size_capped_by_max_position_size(self, risk, base_account):
        # Cash and BP are huge; the per-position cap should bind.
        base_account.buying_power = 100_000
        base_account.portfolio_value = 100_000
        decision = risk.evaluate("BUY", 50.0, base_account, is_paper=True)
        assert decision.approved
        # The quantity must respect max_position_size_usd=1000 → 20 shares max.
        assert decision.quantity <= 1000.0 / 50.0 + 1e-9

    def test_size_capped_by_max_cash_usage(self, risk, base_account):
        # Per-position cap is huge, cash cap should bind at 50% of portfolio.
        risk.max_position_size_usd = 100_000
        decision = risk.evaluate("BUY", 50.0, base_account, is_paper=True)
        assert decision.approved
        # 5000 × 0.5 = 2500 cap → 50 shares.
        assert decision.quantity <= 2500.0 / 50.0 + 1e-9

    def test_size_capped_by_buying_power(self, risk, base_account):
        # BP is the binding constraint.
        risk.max_position_size_usd = 100_000
        risk.max_cash_usage_pct = 1.0  # 100% allowed
        base_account.buying_power = 250.0
        base_account.portfolio_value = 50_000  # don't let cash cap bind
        decision = risk.evaluate("BUY", 50.0, base_account, is_paper=True)
        assert decision.approved
        assert decision.quantity <= 250.0 / 50.0 + 1e-9

    def test_no_trade_when_zero_buying_power(self, risk, base_account):
        base_account.buying_power = 0.0
        decision = risk.evaluate("BUY", 50.0, base_account, is_paper=True)
        assert decision.approved is False


@pytest.mark.risk
@pytest.mark.unit
class TestOpenPositionsCap:
    def test_blocks_when_at_max_open(self, risk, base_account):
        base_account.open_positions = 5
        decision = risk.evaluate("BUY", 100.0, base_account, is_paper=True)
        assert decision.approved is False
        assert "max open positions" in decision.reason.lower()

    def test_allows_when_below_max(self, risk, base_account):
        base_account.open_positions = 4
        decision = risk.evaluate("BUY", 100.0, base_account, is_paper=True)
        assert decision.approved is True


@pytest.mark.risk
@pytest.mark.invariant
class TestDailyPnlResetContract:
    """The scheduler MUST reset daily_pnl at the close — otherwise the
    breaker drifts. Without this reset, a $-500 cumulative across a week
    silently disables the breaker forever."""

    def test_scheduler_post_market_zeroes_daily_pnl(self):
        from backend.bot.scheduler import BotScheduler
        import inspect
        src = inspect.getsource(BotScheduler._post_market)
        assert "daily_pnl = 0" in src or "daily_pnl=0" in src, (
            "BotScheduler._post_market must reset engine.status.daily_pnl. "
            "Without this, the daily-loss circuit breaker becomes "
            "permanently tripped after the first multi-day drawdown."
        )


@pytest.mark.risk
@pytest.mark.invariant
class TestInvalidPrice:
    @pytest.mark.parametrize("price", [0.0, -1.0, -100.0])
    def test_rejects_non_positive_price(self, risk, base_account, price):
        decision = risk.evaluate("BUY", price, base_account, is_paper=True)
        assert decision.approved is False
        assert "invalid price" in decision.reason.lower()
