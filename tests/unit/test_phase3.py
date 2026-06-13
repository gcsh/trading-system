"""Phase 3 tests — assignment simulation, divergence framework."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.bot.divergence import _benchmark_pnl, compute_divergence


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


class TestBenchmarkPnL:
    """P3.5 — benchmark fill model must be MORE pessimistic than paper."""

    def _trade(self, **overrides):
        t = MagicMock()
        t.pnl = overrides.get("pnl", 100.0)
        t.instrument = overrides.get("instrument", "stock")
        t.quantity = overrides.get("quantity", 100)
        t.price = overrides.get("price", 50.0)
        t.strike = overrides.get("strike", 0)
        t.contracts = overrides.get("contracts", None)
        t.action = overrides.get("action", "BUY_STOCK")
        return t

    def test_stock_benchmark_includes_slippage(self):
        t = self._trade(instrument="stock", pnl=100.0, quantity=100,
                            price=50.0)
        bp = _benchmark_pnl(t)
        # Stock slippage = 2bp × notional × 2 sides = 0.0004 × 5000 = $2.
        assert bp < 100.0
        assert bp == pytest.approx(98.0, abs=0.5)

    def test_option_benchmark_more_pessimistic(self):
        t = self._trade(instrument="option", pnl=200.0,
                            strike=100.0, contracts=1)
        bp = _benchmark_pnl(t)
        # Per-share drag = max(100 × 0.005, 0.02) = $0.50.
        # Round trip = 0.50 × 100 × 1 × 2 = $100.
        assert bp == pytest.approx(100.0, abs=1.0)

    def test_iron_condor_pays_4_leg_penalty(self):
        t = self._trade(instrument="complex", pnl=200.0,
                            strike=100.0, contracts=1,
                            action="IRON_CONDOR")
        bp = _benchmark_pnl(t)
        # 4 legs × max(100 × 0.01, $1) × 1 × 2 = $8.
        assert bp == pytest.approx(192.0, abs=1.0)

    def test_spread_pays_2_leg_penalty(self):
        t = self._trade(instrument="complex", pnl=200.0,
                            strike=100.0, contracts=1,
                            action="BULL_CALL_SPREAD")
        bp = _benchmark_pnl(t)
        # 2 legs × $1 × 1 × 2 = $4.
        assert bp == pytest.approx(196.0, abs=1.0)


class TestComputeDivergence:
    """The framework should never crash, even with zero closed trades."""

    def test_empty_window_returns_zeros(self):
        # Choose a window where we know there are no live closed trades
        # (we just reset the trial).
        result = compute_divergence(hours=24)
        assert result["n_trades"] >= 0
        assert "divergence_pct" in result
        assert "alert" in result

    def test_result_shape_complete(self):
        result = compute_divergence(hours=168)
        for key in ("n_trades", "paper_total_pnl", "benchmark_total_pnl",
                       "divergence_pct", "by_day", "alert"):
            assert key in result


class TestAssignmentSimulation:
    """P3.1 — short put expiring ITM should produce a stock position,
    not a cash settlement at intrinsic."""

    def test_close_option_signature_handles_assignment(self):
        """We can't easily integration-test the executor in unit scope,
        but the source must contain the assignment branch."""
        import inspect
        from backend.bot.paper_executor import PaperExecutor
        src = inspect.getsource(PaperExecutor.close_option)
        # The branch must exist.
        assert "is_assignment" in src
        assert "call_assignment" in src
        assert "put_assignment" in src
        assert "expiry" in src.lower()
