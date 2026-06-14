"""Fix N=1 — explicit option_type derivation in PaperExecutor.

The legacy executor used a substring check ("CALL" in action) with
"call" as the silent default. SELL_CSP, BUY_CSP, and any future action
without "CALL" / "PUT" in its name fell through to "call" — the bug
that turned 14 cash-secured puts into naked short calls on a $5k
account on 2026-06-13.

These tests lock the explicit ``ACTION_TO_OPTION_TYPE`` mapping +
the ``option_type_override`` belt-and-braces kwarg.
"""
from __future__ import annotations

from backend.bot.paper_executor import (
    ACTION_TO_OPTION_TYPE,
    PaperExecutor,
)


def _price(_ticker):
    return 100.0


# ── pure-mapping tests (no DB) ─────────────────────────────────────────


def test_mapping_sell_csp_is_put():
    assert ACTION_TO_OPTION_TYPE["SELL_CSP"] == "put"


def test_mapping_sell_covered_call_is_call():
    assert ACTION_TO_OPTION_TYPE["SELL_COVERED_CALL"] == "call"


def test_mapping_buy_call_is_call():
    assert ACTION_TO_OPTION_TYPE["BUY_CALL"] == "call"


def test_mapping_buy_put_is_put():
    assert ACTION_TO_OPTION_TYPE["BUY_PUT"] == "put"


def test_mapping_sell_call_is_call():
    assert ACTION_TO_OPTION_TYPE["SELL_CALL"] == "call"


def test_mapping_sell_put_is_put():
    assert ACTION_TO_OPTION_TYPE["SELL_PUT"] == "put"


def test_mapping_buy_csp_close_is_put():
    """Closing a CSP is a buy-to-close of a put. Mapping must reflect
    the underlying option's right, not the side."""
    assert ACTION_TO_OPTION_TYPE["BUY_CSP"] == "put"


def test_mapping_buy_covered_call_close_is_call():
    assert ACTION_TO_OPTION_TYPE["BUY_COVERED_CALL"] == "call"


# ── end-to-end through place_options_order (uses temp DB) ──────────────


def test_sell_csp_routes_to_put(temp_db):
    """SELL_CSP must produce a PaperPosition with option_type='put'.
    This is the smoking-gun assertion: the 2026-06-13 cascade wrote
    option_type='call' for every SELL_CSP because the legacy substring
    check fell through to the 'call' default."""
    ex = PaperExecutor(starting_cash=20_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "SELL_CSP", 1, strike=100.0, expiration="2030-06-21",
    )
    assert result.success, f"SELL_CSP should succeed: {result.error}"
    positions = ex.positions()
    assert len(positions) == 1
    assert positions[0]["option_type"] == "put"


def test_sell_covered_call_routes_to_call(temp_db):
    """SELL_COVERED_CALL must produce option_type='call'. Test in
    isolation (no shares held) is fine — Fix N=4 enforces the shares
    requirement upstream; the executor only needs cash for commission."""
    ex = PaperExecutor(starting_cash=20_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "SELL_COVERED_CALL", 1,
        strike=100.0, expiration="2030-06-21",
    )
    assert result.success, (
        f"SELL_COVERED_CALL should succeed at executor: {result.error}"
    )
    positions = ex.positions()
    assert positions[0]["option_type"] == "call"


def test_buy_call_routes_to_call(temp_db):
    ex = PaperExecutor(starting_cash=20_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "BUY_CALL", 1, strike=100.0, expiration="2030-06-21",
    )
    assert result.success
    assert ex.positions()[0]["option_type"] == "call"


def test_buy_put_routes_to_put(temp_db):
    ex = PaperExecutor(starting_cash=20_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "BUY_PUT", 1, strike=100.0, expiration="2030-06-21",
    )
    assert result.success
    assert ex.positions()[0]["option_type"] == "put"


# ── unknown / malformed actions (fail loud) ────────────────────────────


def test_garbage_action_rejected(temp_db):
    """Unknown action returns failure — never silently defaults."""
    ex = PaperExecutor(starting_cash=20_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "GARBAGE", 1, strike=100.0, expiration="2030-06-21",
    )
    assert not result.success
    assert "unknown" in (result.error or "").lower()


def test_empty_action_rejected(temp_db):
    ex = PaperExecutor(starting_cash=20_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "", 1, strike=100.0, expiration="2030-06-21",
    )
    assert not result.success
    assert "unknown" in (result.error or "").lower()


def test_lowercase_action_is_uppercased(temp_db):
    """``action.upper()`` is applied inside place_options_order, so
    lowercase callers still get the correct mapping. Tests the
    contract — operators may pass strategy strings from JSON unmodified."""
    ex = PaperExecutor(starting_cash=20_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "sell_csp", 1, strike=100.0, expiration="2030-06-21",
    )
    assert result.success
    assert ex.positions()[0]["option_type"] == "put"


# ── option_type_override (belt-and-braces engine path) ────────────────


def test_option_type_override_wins_over_action(temp_db):
    """The engine passes ``plan["option_type"]`` through to defend
    against an executor regression. When provided, the override wins
    over the action map. This is the belt-and-braces seatbelt.

    Pathological case: action says SELL_CSP (→ put) but the engine
    builder accidentally stamped option_type='call' on the plan. The
    override path forwards what the plan said; the discrepancy will
    then surface in the audit chain (Fix N=5)."""
    ex = PaperExecutor(starting_cash=20_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "SELL_CSP", 1, strike=100.0, expiration="2030-06-21",
        option_type_override="call",
    )
    # The position writes with the override; downstream audit catches
    # the action/option_type mismatch.
    assert result.success
    assert ex.positions()[0]["option_type"] == "call"


def test_option_type_override_invalid_value_rejected(temp_db):
    """``option_type_override`` must be 'call' or 'put'. Garbage is
    rejected so the seatbelt itself can't introduce inconsistency."""
    ex = PaperExecutor(starting_cash=20_000.0, price_fn=_price)
    result = ex.place_options_order(
        "AAPL", "SELL_CSP", 1, strike=100.0, expiration="2030-06-21",
        option_type_override="bogus",
    )
    assert not result.success
    assert "invalid" in (result.error or "").lower()
