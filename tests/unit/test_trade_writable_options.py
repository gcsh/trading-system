"""Fix N=5 — (action, option_type) consistency check in
``verify_trade_writable``.

Defense in depth: the executor (Fix N=1) maps action → option_type
explicitly; even if that regresses, the Trade row insert hits this
invariant and refuses to land.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.bot.audit import AuditViolation, verify_trade_writable


def _trade(action: str, instrument: str, option_type=None,
              ticker: str = "AAPL", strategy: str = "ai_brain"):
    """Minimal duck-typed Trade row. ``verify_trade_writable`` only
    reads attributes via ``getattr``, so a SimpleNamespace works."""
    return SimpleNamespace(
        action=action, instrument=instrument, option_type=option_type,
        ticker=ticker, strategy=strategy,
    )


# ── consistent pairs pass ──────────────────────────────────────────────


def test_sell_csp_with_put_passes():
    verify_trade_writable(_trade("SELL_CSP", "option", "put"))


def test_sell_covered_call_with_call_passes():
    verify_trade_writable(_trade("SELL_COVERED_CALL", "option", "call"))


def test_buy_call_with_call_passes():
    verify_trade_writable(_trade("BUY_CALL", "option", "call"))


def test_buy_put_with_put_passes():
    verify_trade_writable(_trade("BUY_PUT", "option", "put"))


def test_sell_call_with_call_passes():
    """Action consistency only — naked SELL_CALL is policy-blocked
    (Fix N=4), but the audit row check is purely about the field
    pair being internally consistent."""
    verify_trade_writable(_trade("SELL_CALL", "option", "call"))


# ── the original bug: mismatched pair raises ──────────────────────────


def test_sell_csp_with_call_blocks_the_original_bug():
    """THE ORIGINAL 2026-06-13 BUG. Executor mis-derived option_type
    as 'call' for SELL_CSP. Trade row would have landed unless this
    audit caught it. Must raise AuditViolation."""
    with pytest.raises(AuditViolation) as ei:
        verify_trade_writable(_trade("SELL_CSP", "option", "call"))
    assert ei.value.name == "trade_action_option_type_mismatch"


def test_buy_call_with_put_blocks():
    with pytest.raises(AuditViolation) as ei:
        verify_trade_writable(_trade("BUY_CALL", "option", "put"))
    assert ei.value.name == "trade_action_option_type_mismatch"


def test_sell_covered_call_with_put_blocks():
    with pytest.raises(AuditViolation):
        verify_trade_writable(
            _trade("SELL_COVERED_CALL", "option", "put"),
        )


# ── stock + null option_type unaffected ────────────────────────────────


def test_buy_stock_with_stock_instrument_unaffected():
    """Stock rows have option_type=None — the invariant doesn't fire."""
    verify_trade_writable(
        _trade("BUY_STOCK", "stock", option_type=None,
                  strategy="ai_brain"),
    )


def test_sell_stock_unaffected():
    verify_trade_writable(
        _trade("SELL_STOCK", "stock", option_type=None,
                  strategy="ai_brain"),
    )


def test_option_row_without_option_type_unaffected():
    """If an option row hasn't yet had option_type stamped (mid-pipeline
    writes), other audit checks (audit_order_plan) cover it. This
    invariant only enforces consistency of a non-null pair."""
    verify_trade_writable(
        _trade("BUY_CALL", "option", option_type=None,
                  strategy="ai_brain"),
    )


def test_spread_instrument_unaffected():
    """spread rows are handled by the legs payload; the pair check
    targets single-leg option rows only."""
    verify_trade_writable(
        _trade("IRON_CONDOR", "spread", option_type=None,
                  strategy="ai_brain"),
    )


# ── option_type is case-tolerant ───────────────────────────────────────


def test_uppercase_option_type_normalized():
    """If a future writer stamps 'CALL' instead of 'call', the
    pair-match must still succeed — case-fold and compare."""
    verify_trade_writable(_trade("BUY_CALL", "option", "CALL"))


# ── CLOSE_OPTION exit manager rows (call or put) ───────────────────────


def test_close_option_call_passes():
    """The exit manager writes CLOSE_OPTION rows when force-closing
    a long call at TP/SL/expiry. option_type reflects the original
    contract."""
    verify_trade_writable(
        _trade("CLOSE_OPTION", "option", "call", strategy="exit_manager"),
    )


def test_close_option_put_passes():
    verify_trade_writable(
        _trade("CLOSE_OPTION", "option", "put", strategy="exit_manager"),
    )
