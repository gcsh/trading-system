from backend.bot.executor import Executor


def test_paper_mode_short_circuits_orders(mock_rh):
    ex = Executor(paper_mode=True, rh_module=mock_rh)
    result = ex.place_stock_order("AAPL", "BUY", 1)
    assert result.success
    assert result.paper
    mock_rh.orders.order_buy_market.assert_not_called()


def test_live_mode_places_market_order(mock_rh):
    ex = Executor(paper_mode=False, rh_module=mock_rh)
    ex._logged_in = True  # skip login
    result = ex.place_stock_order("AAPL", "BUY", 5)
    assert result.success
    assert not result.paper
    mock_rh.orders.order_buy_market.assert_called_once_with("AAPL", 5)


def test_live_mode_places_limit_order(mock_rh):
    ex = Executor(paper_mode=False, rh_module=mock_rh)
    ex._logged_in = True
    result = ex.place_stock_order("AAPL", "SELL", 2, order_type="limit", limit_price=199.5)
    assert result.success
    mock_rh.orders.order_sell_limit.assert_called_once_with("AAPL", 2, 199.5)


def test_options_buy_call_routes_correctly(mock_rh):
    ex = Executor(paper_mode=False, rh_module=mock_rh)
    ex._logged_in = True
    ex.place_options_order("AAPL", "BUY_CALL", 1, strike=200, expiration="2026-06-21")
    assert mock_rh.orders.order_buy_option_limit.called


def test_paper_options_order_no_call(mock_rh):
    ex = Executor(paper_mode=True, rh_module=mock_rh)
    result = ex.place_options_order("AAPL", "BUY_CALL", 1, strike=200, expiration="2026-06-21")
    assert result.success
    assert result.paper
    mock_rh.orders.order_buy_option_limit.assert_not_called()


def test_account_state_paper_returns_defaults(mock_rh):
    ex = Executor(paper_mode=True, rh_module=mock_rh)
    state = ex.get_account_state()
    assert state["buying_power"] == 10000.0


def test_account_state_live_uses_robin_stocks(mock_rh):
    ex = Executor(paper_mode=False, rh_module=mock_rh)
    ex._logged_in = True
    state = ex.get_account_state()
    assert state["buying_power"] == 5000.0
    assert state["portfolio_value"] == 25000.0
