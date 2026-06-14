"""Unit tests for the local paper-trading simulator."""
from backend.bot.paper_executor import PaperExecutor


def _price(_ticker):
    # Deterministic price for tests.
    return 100.0


def test_starts_with_seeded_cash(temp_db):
    ex = PaperExecutor(starting_cash=1000.0, price_fn=_price)
    state = ex.get_account_state()
    assert state["cash"] == 1000.0
    assert state["portfolio_value"] == 1000.0
    assert state["open_positions"] == 0


def test_buy_decreases_cash_and_opens_position(temp_db):
    # Bumped cash + tolerant assertions to absorb P1.8 commission realism
    # (per-share commission + minimum) — exact-dollar checks predated it.
    ex = PaperExecutor(starting_cash=2000.0, price_fn=_price)
    result = ex.place_stock_order("AAPL", "BUY", 5)
    assert result.success
    state = ex.get_account_state()
    # 5 shares × $100 = $500 debit, plus ~$1 commission, plus spread drag.
    expected_debit = 500.0
    assert 499 < (2000.0 - state["cash"]) < 510, (
        f"unexpected debit: ${2000.0 - state['cash']:.2f}"
    )
    assert state["open_positions"] == 1
    # portfolio_value should round-trip close to starting (loss is just
    # the commission/spread on a fresh-bought-at-same-mark position).
    assert 1990.0 <= state["portfolio_value"] <= 2000.0


def test_cannot_overspend(temp_db):
    ex = PaperExecutor(starting_cash=1000.0, price_fn=_price)
    result = ex.place_stock_order("AAPL", "BUY", 50)  # $5000 needed
    assert not result.success
    assert "insufficient cash" in (result.error or "")


def test_sell_realizes_pnl(temp_db):
    ex = PaperExecutor(starting_cash=2000.0, price_fn=lambda _t: 100.0)
    ex.place_stock_order("AAPL", "BUY", 5)
    # Bump price for the sell
    ex._price_fn = lambda _t: 110.0
    result = ex.place_stock_order("AAPL", "SELL", 5)
    assert result.success
    state = ex.get_account_state()
    # Net P&L = 5 × ($110 - $100) - 2× commission = $50 - ~$2 = ~$48.
    # P1.8 commission realism makes the exact-number check stale; use a
    # band that asserts the win was banked while tolerating fees.
    assert 45.0 < state["realized_pnl"] < 50.5, (
        f"realized_pnl = {state['realized_pnl']}"
    )
    assert state["cash"] > 2040.0, "post-sell cash should reflect gain"
    assert state["open_positions"] == 0


def test_cannot_sell_what_you_dont_own(temp_db):
    ex = PaperExecutor(starting_cash=1000.0, price_fn=_price)
    result = ex.place_stock_order("AAPL", "SELL", 5)
    assert not result.success
    assert "no shares" in (result.error or "")


def test_partial_sell_reduces_quantity(temp_db):
    # Originally $1000 starting / 10 shares at $100; the buy now fails
    # for $1.05 of commission (10 × $0.005 + $1 min). Use $2000 so the
    # buy clears and the partial-sell logic gets a chance to run.
    ex = PaperExecutor(starting_cash=2000.0, price_fn=_price)
    buy = ex.place_stock_order("AAPL", "BUY", 10)
    assert buy.success, f"BUY failed: {buy.error}"
    ex._price_fn = lambda _t: 110.0
    sell = ex.place_stock_order("AAPL", "SELL", 4)
    assert sell.success, f"SELL failed: {sell.error}"
    positions = ex.positions()
    assert len(positions) == 1
    assert positions[0]["quantity"] == 6


def test_reset_clears_positions(temp_db):
    ex = PaperExecutor(starting_cash=1000.0, price_fn=_price)
    ex.place_stock_order("AAPL", "BUY", 3)
    state = ex.reset(starting_cash=500.0)
    assert state["cash"] == 500.0
    assert ex.positions() == []


def test_paper_options_buy_debits_cash(temp_db):
    # P2.1 replaced the "3% of underlying" stub with real Black-Scholes
    # pricing — premium now depends on spot/strike/DTE/IV, not a fixed
    # 3%. For an ATM call $100/$100 with ~16 DTE and the default IV, BS
    # returns roughly $2.50-3.00/share = $250-$300 per contract. Assert
    # that SOME premium + commission was debited (in a sensible range)
    # rather than pinning to the old stub's exact number.
    ex = PaperExecutor(starting_cash=10_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "BUY_CALL", 1, strike=100.0, expiration="2026-06-21",
    )
    assert result.success
    state = ex.get_account_state()
    debit = 10_000.0 - state["cash"]
    # Sanity envelope: a 1-contract ATM call should debit between $30
    # (very cheap OTM near-expiry) and $1500 (extreme IV / deep ITM).
    assert 30.0 < debit < 1500.0, (
        f"unexpected option debit: ${debit:.2f}"
    )


def test_paper_options_sell_credits_cash(temp_db):
    # Fix N=2 (2026-06-13) — SELL_CSP now requires strike × 100
    # cash collateral up front. Bumped from $1k to $11k so the
    # collateral check (strike 100 × 100 = $10k) passes and the
    # original assertion (cash credited above starting balance)
    # still holds.
    ex = PaperExecutor(starting_cash=11_000.0, price_fn=_price)
    ex.place_options_order("AAPL", "SELL_CSP", 1, strike=100.0, expiration="2026-06-21")
    state = ex.get_account_state()
    assert state["cash"] > 11_000.0  # we collected premium


def test_login_always_succeeds(temp_db):
    ex = PaperExecutor(price_fn=_price)
    assert ex.login() is True


# ── Item #3 — complex-instrument MTM ───────────────────────────────────


def test_complex_short_put_position_marks_intrinsic(temp_db):
    """SELL_CSP lands as kind='complex'. positions() must populate
    current_price + market_value + unrealized_pnl rather than fall
    through both stock/option branches."""
    from backend.db import session_scope
    from backend.models.paper import PaperPosition
    import json

    ex = PaperExecutor(starting_cash=10_000.0, price_fn=lambda t: 95.0)
    # Manually seed a complex position because place_options_order may not
    # create kind=complex for SELL_CSP yet — engine.py is what tags them.
    with session_scope() as s:
        s.add(PaperPosition(
            ticker="AAPL", kind="complex", quantity=1,
            avg_cost=2.50,
            meta=json.dumps({
                "action": "SELL_CSP", "strike": 100.0,
                "expiration": "2026-07-19",
            }),
        ))
    positions = ex.positions()
    assert len(positions) == 1
    p = positions[0]
    # spot=95, strike=100, put intrinsic = 5 → mark = -5 × 100 = -500 (liability)
    assert p["market_value"] == -500.0
    # pnl = entry (2.50) + mark (-500/100) per contract → for 1 contract:
    # entry credit = 2.50 × 1 = $2.50; mark liability = -$500 → pnl = 2.50 - 500
    # = -$497.50. Wait — let me re-check the math:
    # entry = avg_cost (2.50) × contracts (1) = 2.50
    # mark = -500 (already × 100 × contracts)
    # pnl = entry + mark = 2.50 - 500 = -497.50
    assert p["unrealized_pnl"] == -497.5
    assert p["side"] == "SHORT"
    assert p["action"] == "SELL_CSP"


def test_complex_iron_condor_marks_all_legs(temp_db):
    """Iron condor: short call+put (liabilities) + long call+put (assets)."""
    from backend.db import session_scope
    from backend.models.paper import PaperPosition
    import json

    ex = PaperExecutor(starting_cash=10_000.0, price_fn=lambda t: 100.0)
    with session_scope() as s:
        s.add(PaperPosition(
            ticker="AAPL", kind="complex", quantity=1,
            avg_cost=1.00,
            meta=json.dumps({
                "action": "IRON_CONDOR",
                "call_short": 105.0, "call_long": 110.0,
                "put_short": 95.0, "put_long": 90.0,
                "expiration": "2026-07-19",
            }),
        ))
    positions = ex.positions()
    # All four strikes are OTM at spot=100 → all intrinsics are zero.
    # mark = 0; pnl = entry (credit 1.00) + 0 = +1.00
    p = positions[0]
    assert p["market_value"] == 0.0
    assert p["unrealized_pnl"] == 1.0
    assert p["side"] == "SHORT"


def test_complex_falls_back_gracefully_for_unknown_shape(temp_db):
    """Unknown action label should produce 0 mark with COMPLEX label —
    NEVER raise, NEVER leave nulls."""
    from backend.db import session_scope
    from backend.models.paper import PaperPosition
    import json

    ex = PaperExecutor(starting_cash=10_000.0, price_fn=lambda t: 100.0)
    with session_scope() as s:
        s.add(PaperPosition(
            ticker="AAPL", kind="complex", quantity=1,
            avg_cost=2.00,
            meta=json.dumps({"action": "SOMETHING_NEW"}),
        ))
    positions = ex.positions()
    p = positions[0]
    assert p["market_value"] == 0.0
    assert p["current_price"] == 0.0
    assert p["action"] == "SOMETHING_NEW"
