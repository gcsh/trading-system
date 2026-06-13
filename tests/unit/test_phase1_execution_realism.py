"""Phase 1 execution-realism tests — P1.8/P1.9.

Verifies the IBKR-baseline commission + bid/ask spread math, and the
multi-leg per-leg pricing for iron condors / spreads.
"""
from __future__ import annotations

import pytest

from backend.bot.paper_executor import (
    _stock_commission,
    _option_commission,
    _apply_stock_spread,
    _apply_option_spread,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


@pytest.mark.execution_realism if hasattr(pytest.mark, "execution_realism") else pytest.mark.unit
class TestStockCommission:
    def test_at_minimum(self):
        # 100 shares × $0.005 = $0.50 → floor at $1.00.
        assert _stock_commission(100) == pytest.approx(1.00)

    def test_above_minimum(self):
        # 1000 shares × $0.005 = $5.00.
        assert _stock_commission(1000) == pytest.approx(5.00)

    def test_zero_quantity_still_pays_minimum(self):
        # Zero shouldn't happen via the engine, but defensive.
        assert _stock_commission(0) >= 1.00


class TestOptionCommission:
    def test_one_contract_at_minimum(self):
        # 1 × $0.65 = $0.65 → floor $1.00.
        assert _option_commission(1) == pytest.approx(1.00)

    def test_three_contracts_at_minimum(self):
        # 3 × $0.65 = $1.95.
        assert _option_commission(3) == pytest.approx(1.95)

    def test_iron_condor_4_legs_correctly_priced(self):
        # 4 legs → 4 × $0.65 = $2.60.
        assert _option_commission(4) == pytest.approx(2.60)


class TestStockSpread:
    def test_buy_pays_above_mid(self):
        # 1bp spread = $0.01 per $100 → half = $0.005.
        fill = _apply_stock_spread(100.0, "BUY")
        assert fill > 100.0
        assert fill == pytest.approx(100.005, abs=1e-6)

    def test_sell_receives_below_mid(self):
        fill = _apply_stock_spread(100.0, "SELL")
        assert fill < 100.0
        assert fill == pytest.approx(99.995, abs=1e-6)


class TestOptionSpread:
    def test_buy_premium_inflated(self):
        # 2% spread → half = 1% above mid for a BUY.
        fill = _apply_option_spread(5.00, "BUY")
        assert fill == pytest.approx(5.05, abs=1e-6)

    def test_sell_premium_discounted(self):
        fill = _apply_option_spread(5.00, "SELL")
        assert fill == pytest.approx(4.95, abs=1e-6)


@pytest.mark.invariant
class TestRoundTripCost:
    """A round-trip option trade must cost at least 2 × min-commission
    + spread. Without commissions the wheel strategy was 1-2% optimistic."""

    def test_round_trip_charges_commission_twice(self):
        c_open = _option_commission(1)
        c_close = _option_commission(1)
        total = c_open + c_close
        assert total >= 2.0   # $1 min × 2

    def test_round_trip_spread_drag_for_options(self):
        # Open at mid+1%, close at mid-1% → 2% drag round trip.
        mid = 5.0
        open_fill = _apply_option_spread(mid, "BUY")
        close_fill = _apply_option_spread(mid, "SELL")
        drag = open_fill - close_fill
        assert drag == pytest.approx(2 * 0.05, abs=1e-6)  # 2 × half-spread
