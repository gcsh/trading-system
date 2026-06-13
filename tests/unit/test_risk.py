from datetime import datetime

from backend.bot.risk import AccountState, RiskManager


def base_config(**overrides):
    config = {
        "risk": {
            "max_position_size_usd": 1000,
            "max_open_positions": 3,
            "daily_loss_limit_usd": 200,
            "stop_loss_pct": 5,
            "take_profit_pct": 10,
            "max_cash_usage_pct": 50,
        }
    }
    config["risk"].update(overrides)
    return config


def test_circuit_breaker_blocks_when_daily_loss_hit():
    risk = RiskManager(base_config())
    account = AccountState(buying_power=5000, portfolio_value=25000, open_positions=0, daily_pnl=-201)
    decision = risk.evaluate("BUY_STOCK", price=100, account=account)
    assert not decision.approved
    assert "circuit breaker" in decision.reason


def test_position_size_is_capped_to_max_position_usd():
    risk = RiskManager(base_config())
    account = AccountState(buying_power=50000, portfolio_value=100000, open_positions=0)
    decision = risk.evaluate(
        "BUY_STOCK", price=100, account=account, now=datetime(2026, 5, 26, 13, 0)
    )
    assert decision.approved
    assert decision.quantity * 100 <= 1000.0001


def test_max_open_positions_blocks_further_buys():
    risk = RiskManager(base_config())
    account = AccountState(buying_power=5000, portfolio_value=25000, open_positions=3)
    decision = risk.evaluate(
        "BUY_STOCK", price=100, account=account, now=datetime(2026, 5, 26, 13, 0)
    )
    assert not decision.approved


def test_stop_loss_and_take_profit_computed_for_buy():
    risk = RiskManager(base_config())
    account = AccountState(buying_power=5000, portfolio_value=25000, open_positions=0)
    decision = risk.evaluate(
        "BUY_STOCK", price=100, account=account, now=datetime(2026, 5, 26, 13, 0)
    )
    assert decision.stop_loss_price == 95.0
    assert decision.take_profit_price == 110.0


def test_intraday_eod_cutoff_blocks_new_buys():
    risk = RiskManager(base_config())
    account = AccountState(buying_power=5000, portfolio_value=25000, open_positions=0)
    decision = risk.evaluate(
        "BUY_STOCK", price=100, account=account, now=datetime(2026, 5, 26, 15, 50)
    )
    assert not decision.approved
    assert "EOD" in decision.reason


def test_no_buying_power_rejects():
    risk = RiskManager(base_config())
    account = AccountState(buying_power=0, portfolio_value=0, open_positions=0)
    decision = risk.evaluate(
        "BUY_STOCK", price=100, account=account, now=datetime(2026, 5, 26, 13, 0)
    )
    assert not decision.approved


def test_opex_day_shrinks_position_size():
    from backend.config import TUNABLES

    risk = RiskManager(base_config())
    account = AccountState(buying_power=50000, portfolio_value=100000, open_positions=0)
    # 2026-05-15 is the May monthly OPEX (3rd Friday); 2026-05-14 is the Thursday.
    opex = risk.evaluate("BUY_STOCK", price=100, account=account, now=datetime(2026, 5, 15, 13, 0))
    normal = risk.evaluate("BUY_STOCK", price=100, account=account, now=datetime(2026, 5, 14, 13, 0))
    assert opex.approved and normal.approved
    assert opex.quantity == round(normal.quantity * TUNABLES.opex_size_factor, 4)
