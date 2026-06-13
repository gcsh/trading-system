"""End-to-end paper-trading lifecycle — the behavior the dashboard depends on
and the gap that let 'Total P&L $0 while equity moved' slip through:

buy → price moves → UNREALIZED P&L shows up in equity + positions → sell →
REALIZED P&L books. Uses a controllable price function (no network).
"""
from backend.bot.paper_executor import PaperExecutor


def _executor(price_ref):
    ex = PaperExecutor(starting_cash=5000.0, price_fn=lambda t: price_ref.get(t, 0.0))
    ex.reset(starting_cash=5000.0)
    return ex


def test_equity_marks_to_market_then_realizes_on_close(temp_db):
    price = {"AAPL": 100.0}
    ex = _executor(price)

    # Buy 10 @ $100 → $1,000 deployed, equity unchanged at cost basis.
    assert ex.place_stock_order("AAPL", "BUY", 10).success
    assert round(ex.get_account_state()["portfolio_value"], 2) == 5000.0

    # Price rises to $110 → equity must reflect the +$100 UNREALIZED gain.
    price["AAPL"] = 110.0
    state = ex.get_account_state()
    assert round(state["portfolio_value"], 2) == 5100.0
    pos = ex.positions()[0]
    assert pos["ticker"] == "AAPL"
    assert pos["unrealized_pnl"] == 100.0
    assert pos["current_price"] == 110.0

    # Sell 10 @ $110 → realizes +$100, flat afterwards.
    assert ex.place_stock_order("AAPL", "SELL", 10).success
    after = ex.get_account_state()
    assert round(after["realized_pnl"], 2) == 100.0
    assert round(after["portfolio_value"], 2) == 5100.0
    assert ex.positions() == []


def test_equity_drops_on_unrealized_loss(temp_db):
    # Regression for the reported screenshot: account down on open positions but
    # P&L looked like $0 — equity must move with price even before any close.
    price = {"NVDA": 200.0}
    ex = _executor(price)
    assert ex.place_stock_order("NVDA", "BUY", 5).success     # $1,000 in
    price["NVDA"] = 180.0                                      # -10%
    state = ex.get_account_state()
    assert round(state["portfolio_value"], 2) == 4900.0       # 4000 cash + 5*180
    assert ex.positions()[0]["unrealized_pnl"] == -100.0
    assert round(state["realized_pnl"], 2) == 0.0             # nothing closed yet
