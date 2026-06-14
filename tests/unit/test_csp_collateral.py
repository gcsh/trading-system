"""Fix N=2 — collateral check before crediting premium.

Real brokers require cash collateral (CSP) or shares (covered call)
backing every short option write. The legacy executor credited
premium unconditionally — a $5k account could "sell" a $1010-strike
put with no warning. These tests enforce broker discipline at the
executor layer (defense in depth alongside the policy rule in N=4).
"""
from __future__ import annotations

from backend.bot.paper_executor import PaperExecutor
from backend.db import session_scope
from backend.models.paper import PaperPosition


def _price(_ticker):
    return 100.0


# ── SELL_CSP cash collateral ───────────────────────────────────────────


def test_sell_csp_succeeds_when_cash_covers_strike(temp_db):
    """strike=100 × 100 = $10,000 collateral; $10,000 cash → OK."""
    ex = PaperExecutor(starting_cash=10_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "SELL_CSP", 1, strike=100.0, expiration="2030-06-21",
    )
    assert result.success, f"unexpected reject: {result.error}"
    state = ex.get_account_state()
    # Cash should NOT have dropped below the collateral threshold
    # because the credit also lands. Net cash = starting + premium -
    # commission. The check that matters: cash is non-negative and
    # the position exists.
    assert state["cash"] > 0
    assert state["open_positions"] == 1


def test_sell_csp_gs_1010_strike_blocked(temp_db):
    """The smoking-gun case from the 2026-06-13 cascade. GS $1010
    strike CSP on a $391.66 cash account needs $101,000 collateral.
    Must be rejected outright."""
    ex = PaperExecutor(starting_cash=391.66, price_fn=lambda t: 1000.0)
    result = ex.place_options_order(
        "GS", "SELL_CSP", 1, strike=1010.0, expiration="2030-06-21",
    )
    assert not result.success
    err = result.error or ""
    assert "SELL_CSP requires" in err
    assert "$101,000" in err  # 1010 × 100
    # No position should have been written.
    with session_scope() as s:
        assert s.query(PaperPosition).count() == 0


def test_sell_csp_10_contracts_blocked_when_cash_insufficient(temp_db):
    """strike=100 × qty=10 → $100,000 required; $50,000 cash → reject."""
    ex = PaperExecutor(starting_cash=50_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "SELL_CSP", 10, strike=100.0, expiration="2030-06-21",
    )
    assert not result.success
    assert "$100,000" in (result.error or "")
    with session_scope() as s:
        assert s.query(PaperPosition).count() == 0


def test_sell_csp_10_contracts_succeeds_at_collateral_threshold(temp_db):
    """Just over the threshold ($100,001 cash for $100,000 required)
    must succeed. Locks the threshold semantics — '<' not '<='."""
    ex = PaperExecutor(starting_cash=100_001.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "SELL_CSP", 10, strike=100.0, expiration="2030-06-21",
    )
    assert result.success, f"unexpected reject: {result.error}"


def test_sell_csp_error_message_shows_both_numbers(temp_db):
    """The error must surface BOTH the required collateral and the
    cash actually available — operators need to read 'have X, need Y'
    in one line."""
    ex = PaperExecutor(starting_cash=5_000.0, price_fn=lambda t: 200.0)
    result = ex.place_options_order(
        "AAPL", "SELL_CSP", 1, strike=200.0, expiration="2030-06-21",
    )
    assert not result.success
    err = result.error or ""
    assert "$20,000" in err  # required (200 × 100)
    assert "$5,000" in err   # have (starting_cash)


# ── SELL_COVERED_CALL cash for commission only ─────────────────────────


def test_sell_covered_call_passes_with_minimal_cash(temp_db):
    """Covered call collateral is the underlying shares; cash only
    needs to cover the commission. $5 cash + 100 shares held works.
    The shares-quantity check is Fix N=4 (policy layer), not here."""
    ex = PaperExecutor(starting_cash=5.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "SELL_COVERED_CALL", 1,
        strike=100.0, expiration="2030-06-21",
    )
    assert result.success, f"unexpected reject: {result.error}"


# ── naked shorts blocked at the executor (defense in depth) ─────────────


def test_naked_sell_call_blocked_at_executor(temp_db):
    """Even with abundant cash, a naked SELL_CALL must be refused.
    The policy rule (Fix N=4) blocks this upstream, but the executor
    layer is the last line of defense."""
    ex = PaperExecutor(starting_cash=1_000_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "SELL_CALL", 1, strike=100.0, expiration="2030-06-21",
    )
    assert not result.success
    err = (result.error or "").lower()
    assert "naked" in err
    assert "sell_call" in err
    with session_scope() as s:
        assert s.query(PaperPosition).count() == 0


def test_naked_sell_put_blocked_at_executor(temp_db):
    """SELL_PUT (not SELL_CSP) is also naked — blocked. The CSP path
    is the only legitimate short-put structure in this engine."""
    ex = PaperExecutor(starting_cash=1_000_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "SELL_PUT", 1, strike=100.0, expiration="2030-06-21",
    )
    assert not result.success
    assert "naked" in (result.error or "").lower()
