"""MITS-P10.2 — Option promotion tests.

Verifies the ``signal_promote.promote()`` matrix translates stock
BUY/SELL signals into option-leg signals when IV-rank + catalyst
conditions are favourable, and leaves them unchanged otherwise.
"""
from __future__ import annotations

from backend.bot.theories.schema import Signal
from backend.bot.theories.signal_promote import (
    promote, promote_all,
    IV_LOW_THRESHOLD, IV_HIGH_THRESHOLD,
    EARNINGS_BUFFER_DAYS,
    DTE_LONG_PREMIUM, DTE_DEFINED_RISK,
)


def _stock_buy() -> Signal:
    return Signal(
        action="BUY", ts="2026-06-08T00:00:00Z", price=100.0,
        confidence=0.7, reasoning="Bollinger lower-band tag",
        instrument="stock",
    )


def _stock_sell() -> Signal:
    return Signal(
        action="SELL", ts="2026-06-08T00:00:00Z", price=100.0,
        confidence=0.7, reasoning="Bollinger upper-band tag",
        instrument="stock",
    )


def test_buy_low_iv_promotes_to_long_call():
    s = promote(_stock_buy(),
                market_context={"iv_rank": 20, "days_to_earnings": 30})
    assert s.action == "BUY_CALL"
    assert s.instrument == "call"
    assert s.dte_target == DTE_LONG_PREMIUM
    assert "long call" in s.reasoning.lower()


def test_buy_high_iv_promotes_to_vertical_call():
    s = promote(_stock_buy(),
                market_context={"iv_rank": 80, "days_to_earnings": 30})
    assert s.action == "BUY_VERTICAL_CALL"
    assert s.instrument == "spread"
    assert s.dte_target == DTE_DEFINED_RISK
    assert "vertical call" in s.reasoning.lower()


def test_buy_mid_iv_stays_stock():
    s = promote(_stock_buy(),
                market_context={"iv_rank": 50, "days_to_earnings": 30})
    assert s.action == "BUY"
    assert s.instrument == "stock"


def test_buy_near_earnings_stays_stock():
    """McMillan's earnings buffer — never bet IV through earnings."""
    s = promote(_stock_buy(),
                market_context={"iv_rank": 20, "days_to_earnings": 5})
    assert s.action == "BUY"
    assert s.instrument == "stock"


def test_sell_low_iv_promotes_to_long_put():
    s = promote(_stock_sell(),
                market_context={"iv_rank": 15, "days_to_earnings": 60})
    assert s.action == "BUY_PUT"
    assert s.instrument == "put"
    assert "long put" in s.reasoning.lower()


def test_sell_high_iv_promotes_to_iron_condor():
    s = promote(_stock_sell(),
                market_context={"iv_rank": 85, "days_to_earnings": 60})
    assert s.action == "IRON_CONDOR"
    assert s.instrument == "spread"
    assert "iron condor" in s.reasoning.lower()


def test_watch_is_never_promoted():
    s_in = Signal(action="WATCH", ts="t", price=100.0, reasoning="r")
    s_out = promote(s_in, market_context={"iv_rank": 15, "days_to_earnings": 90})
    assert s_out.action == "WATCH"


def test_promotion_can_be_disabled():
    s = promote(_stock_buy(),
                market_context={"iv_rank": 15, "days_to_earnings": 90},
                enabled=False)
    assert s.action == "BUY"
    assert s.instrument == "stock"


def test_promote_all_handles_mixed_list():
    sigs = [_stock_buy(), _stock_sell(),
            Signal(action="WATCH", ts="t", price=50.0)]
    out = promote_all(sigs, market_context={"iv_rank": 15,
                                              "days_to_earnings": 60})
    assert len(out) == 3
    assert out[0].action == "BUY_CALL"
    assert out[1].action == "BUY_PUT"
    assert out[2].action == "WATCH"


def test_promote_default_context_keeps_stock():
    """Default IV-rank 50, far-from-earnings should keep stock."""
    s = promote(_stock_buy(), market_context={})
    assert s.action == "BUY"


def test_thresholds_are_sane():
    assert 0 < IV_LOW_THRESHOLD < IV_HIGH_THRESHOLD <= 100
    assert EARNINGS_BUFFER_DAYS >= 7
    assert DTE_LONG_PREMIUM > DTE_DEFINED_RISK
