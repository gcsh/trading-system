"""Extra coverage for Executor: error paths, cancel, options sell, login flows."""
from unittest.mock import MagicMock

from backend.bot.executor import Executor


def test_unknown_action_returns_failure(mock_rh):
    ex = Executor(paper_mode=False, rh_module=mock_rh)
    ex._logged_in = True
    result = ex.place_stock_order("AAPL", "ROTATE", 1)
    assert not result.success
    assert "unknown action" in (result.error or "")


def test_robin_stocks_exception_captured(mock_rh):
    mock_rh.orders.order_buy_market.side_effect = RuntimeError("api down")
    ex = Executor(paper_mode=False, rh_module=mock_rh)
    ex._logged_in = True
    result = ex.place_stock_order("AAPL", "BUY", 1)
    assert not result.success
    assert "api down" in (result.error or "")


def test_options_sell_routes_to_sell_option_limit(mock_rh):
    ex = Executor(paper_mode=False, rh_module=mock_rh)
    ex._logged_in = True
    ex.place_options_order("AAPL", "SELL_PUT", 1, strike=200, expiration="2026-06-21")
    mock_rh.orders.order_sell_option_limit.assert_called_once()


def test_cancel_all_paper_mode_is_noop(mock_rh):
    ex = Executor(paper_mode=True, rh_module=mock_rh)
    ex.cancel_all_orders()
    mock_rh.orders.cancel_all_stock_orders.assert_not_called()


def test_cancel_all_live_calls_robin_stocks(mock_rh):
    ex = Executor(paper_mode=False, rh_module=mock_rh)
    ex.cancel_all_orders()
    mock_rh.orders.cancel_all_stock_orders.assert_called_once()
    mock_rh.orders.cancel_all_option_orders.assert_called_once()


def test_login_short_circuits_in_paper_mode(mock_rh):
    ex = Executor(paper_mode=True, rh_module=mock_rh)
    assert ex.login() is True
    mock_rh.login.assert_not_called()


def test_login_returns_false_without_credentials(monkeypatch, mock_rh):
    from backend import config as cfg

    monkeypatch.setattr(cfg.SETTINGS, "robinhood_username", "")
    monkeypatch.setattr(cfg.SETTINGS, "robinhood_password", "")
    ex = Executor(paper_mode=False, rh_module=mock_rh)
    assert ex.login() is False


def test_account_state_live_without_login_returns_zeros(mock_rh, monkeypatch):
    from backend import config as cfg

    monkeypatch.setattr(cfg.SETTINGS, "robinhood_username", "")
    ex = Executor(paper_mode=False, rh_module=mock_rh)
    state = ex.get_account_state()
    assert state["buying_power"] == 0.0


def test_options_order_failure_captured(mock_rh):
    mock_rh.orders.order_buy_option_limit.side_effect = RuntimeError("opt fail")
    ex = Executor(paper_mode=False, rh_module=mock_rh)
    ex._logged_in = True
    result = ex.place_options_order("AAPL", "BUY_CALL", 1, strike=200, expiration="2026-06-21")
    assert not result.success
