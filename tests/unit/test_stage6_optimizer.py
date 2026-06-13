"""Stage-6 — Kelly + CVaR + vol target + drawdown + cluster caps + allocator.

Pinned behavior:
  • Kelly math against textbook examples (positive edge → positive fraction,
    negative edge → 0)
  • CVaR sizing scales inversely with sigma + with budget
  • Vol-target inversely scales with asset vol
  • Drawdown multiplier ∈ [floor, 1], monotonic, hits floor by 4× cut DD
  • Cluster check correctly blocks AT/over cap, allows below
  • Allocator floor + cap respected; high-Sharpe strategies dominate
  • Optimizer pipeline: cluster cap binding → cuts size; never increases
"""
import os
import pytest
from fastapi.testclient import TestClient

from backend.bot.portfolio_optimizer import (
    allocate_capital,
    check_cluster_cap,
    cluster_exposures,
    cvar_size_fraction,
    drawdown_size_multiplier,
    kelly_fraction,
    optimize_size,
    vol_target_fraction,
)


# ── Kelly ────────────────────────────────────────────────────────────────


class TestKelly:
    def test_positive_edge_returns_positive(self):
        # 60% win × $200 win / $100 loss → b=2, p=0.6, q=0.4
        # f* = (2·0.6 - 0.4) / 2 = 0.4 ; quarter-Kelly default = 0.10
        f = kelly_fraction(win_rate=0.6, avg_win=200, avg_loss=-100)
        assert f == pytest.approx(0.10, abs=1e-4)

    def test_negative_edge_returns_zero(self):
        f = kelly_fraction(win_rate=0.4, avg_win=100, avg_loss=-150)
        assert f == 0.0

    def test_breakeven_returns_zero(self):
        f = kelly_fraction(win_rate=0.5, avg_win=100, avg_loss=-100)
        # f* = (1·0.5 - 0.5)/1 = 0 → quarter Kelly = 0
        assert f == 0.0

    def test_zero_win_rate(self):
        assert kelly_fraction(win_rate=0, avg_win=100, avg_loss=-50) == 0.0

    def test_perfect_win_returns_zero_due_to_clamp(self):
        # win_rate==1 is excluded (degenerate). Returns 0.
        assert kelly_fraction(win_rate=1.0, avg_win=100, avg_loss=-50) == 0.0


# ── CVaR sizing ─────────────────────────────────────────────────────────


class TestCVaR:
    def test_smaller_with_higher_sigma(self):
        small_vol = cvar_size_fraction(equity=10_000, daily_loss_budget=200,
                                          sigma_pct=0.10)
        big_vol = cvar_size_fraction(equity=10_000, daily_loss_budget=200,
                                        sigma_pct=0.30)
        assert big_vol < small_vol

    def test_bigger_with_higher_budget(self):
        small_b = cvar_size_fraction(equity=10_000, daily_loss_budget=50,
                                        sigma_pct=0.20)
        big_b = cvar_size_fraction(equity=10_000, daily_loss_budget=400,
                                      sigma_pct=0.20)
        assert big_b > small_b

    def test_zero_equity_returns_zero(self):
        assert cvar_size_fraction(equity=0, daily_loss_budget=200,
                                     sigma_pct=0.20) == 0.0

    def test_capped_at_one(self):
        # huge budget → would otherwise blow past 1.0
        f = cvar_size_fraction(equity=100, daily_loss_budget=1_000_000,
                                  sigma_pct=0.20)
        assert f <= 1.0


# ── vol target ─────────────────────────────────────────────────────────


class TestVolTarget:
    def test_inverse_to_asset_vol(self):
        f1 = vol_target_fraction(target_vol=0.15, asset_vol=0.10)
        f2 = vol_target_fraction(target_vol=0.15, asset_vol=0.30)
        assert f1 > f2

    def test_equals_one_at_target(self):
        assert vol_target_fraction(target_vol=0.20, asset_vol=0.20) == 1.0

    def test_zero_inputs(self):
        assert vol_target_fraction(target_vol=0, asset_vol=0.20) == 0.0


# ── drawdown ─────────────────────────────────────────────────────────────


class TestDrawdownMultiplier:
    def test_one_below_cut(self):
        assert drawdown_size_multiplier(current_drawdown_pct=0.02) == 1.0

    def test_monotonic(self):
        a = drawdown_size_multiplier(current_drawdown_pct=0.06)
        b = drawdown_size_multiplier(current_drawdown_pct=0.10)
        c = drawdown_size_multiplier(current_drawdown_pct=0.18)
        assert 1.0 >= a >= b >= c

    def test_hits_floor_by_four_x_cut(self):
        # default cut=0.05; 4× = 0.20 should hit the floor
        m = drawdown_size_multiplier(current_drawdown_pct=0.20)
        assert m == pytest.approx(0.25, abs=1e-3)


# ── cluster caps ─────────────────────────────────────────────────────────


class TestClusterExposure:
    def test_empty_positions(self):
        assert cluster_exposures([], equity=10_000) == []

    def test_real_positions(self):
        positions = [
            {"ticker": "NVDA", "market_value": 2000, "kind": "stock"},
            {"ticker": "AMD",  "market_value": 1500, "kind": "stock"},
            {"ticker": "MSFT", "market_value": 1000, "kind": "stock"},
        ]
        exposures = cluster_exposures(positions, equity=10_000)
        names = {c.cluster for c in exposures}
        # NVDA/AMD share "AI infrastructure" & "Semis"
        assert "AI infrastructure" in names
        assert "Semis" in names
        ai = next(c for c in exposures if c.cluster == "AI infrastructure")
        assert ai.market_value == 3500     # NVDA + AMD
        assert ai.pct_of_equity == 0.35


class TestClusterCap:
    def test_blocks_when_over_cap(self):
        # Existing big NVDA + AMD in "AI infrastructure" + "Semis";
        # adding more NVDA pushes over 50%.
        positions = [
            {"ticker": "NVDA", "market_value": 3000, "kind": "stock"},
            {"ticker": "AMD",  "market_value": 2000, "kind": "stock"},
        ]
        res = check_cluster_cap(ticker="NVDA", new_value=2000,
                                  positions=positions, equity=10_000)
        # cluster after = (3000 + 2000 + 2000)/10000 = 70% > 50%
        assert res.blocked
        assert res.cluster_after >= 0.50

    def test_allows_below_cap(self):
        positions = [
            {"ticker": "NVDA", "market_value": 500, "kind": "stock"},
        ]
        res = check_cluster_cap(ticker="NVDA", new_value=500,
                                  positions=positions, equity=10_000)
        assert not res.blocked


# ── allocator ────────────────────────────────────────────────────────────


class TestAllocator:
    def test_empty_metrics(self):
        assert allocate_capital({}) == []

    def test_floor_applied(self):
        metrics = {
            "trend": {"closed": 1, "win_rate": 1.0, "expectancy": 10.0,
                       "profit_factor": 5.0},
            "vwap":  {"closed": 0},
        }
        allocs = allocate_capital(metrics)
        assert all(a.share >= 0.05 for a in allocs)

    def test_high_sharpe_dominates(self):
        metrics = {
            "good": {"closed": 30, "win_rate": 0.7, "expectancy": 30.0,
                      "profit_factor": 3.0},
            "bad":  {"closed": 30, "win_rate": 0.3, "expectancy": -10.0,
                      "profit_factor": 0.5},
        }
        allocs = {a.strategy: a for a in allocate_capital(metrics)}
        assert allocs["good"].share > allocs["bad"].share

    def test_max_cap(self):
        metrics = {"only": {"closed": 100, "win_rate": 0.9,
                              "expectancy": 100.0, "profit_factor": 10.0}}
        allocs = allocate_capital(metrics)
        assert allocs[0].share <= 0.40


# ── optimizer pipeline ───────────────────────────────────────────────────


class TestOptimizerPipeline:
    def test_zero_equity_returns_zero(self):
        decision = optimize_size(ticker="NVDA", strategy="trend",
                                    requested_dollar=1000, equity=0)
        assert decision.recommended_dollar == 0.0

    def test_never_increases_size(self):
        # ask for $100; even with maximum confidence in everything the
        # optimizer must not return > requested
        decision = optimize_size(
            ticker="NVDA", strategy="trend",
            requested_dollar=100, equity=10_000,
            by_strategy_metrics={"trend": {"closed": 30, "win_rate": 0.7,
                                              "expectancy": 30.0,
                                              "profit_factor": 3.0,
                                              "avg_win": 50, "avg_loss": -20}},
            asset_volatility=0.10, daily_loss_budget=500,
        )
        assert decision.recommended_dollar <= 100

    def test_cluster_binding_cuts(self):
        positions = [
            {"ticker": "NVDA", "market_value": 4500, "kind": "stock"},
        ]
        decision = optimize_size(
            ticker="NVDA", strategy="trend",
            requested_dollar=2000, equity=10_000,
            positions=positions,
            by_strategy_metrics={"trend": {"closed": 30, "win_rate": 0.7,
                                              "expectancy": 30.0,
                                              "profit_factor": 3.0,
                                              "avg_win": 50, "avg_loss": -20}},
            asset_volatility=0.10, daily_loss_budget=500,
        )
        assert decision.cluster_blocked
        # cluster cap binding → recommend can't push the cluster over 50%
        # AI infrastructure already at 4500 → 500 max additional
        assert decision.recommended_dollar <= 500

    def test_drawdown_cuts(self):
        d_zero = optimize_size(
            ticker="X", strategy="trend", requested_dollar=1000,
            equity=10_000, drawdown_pct=0.0,
            by_strategy_metrics={"trend": {"closed": 30, "win_rate": 0.7,
                                              "expectancy": 30.0,
                                              "profit_factor": 3.0,
                                              "avg_win": 50, "avg_loss": -20}},
        )
        d_big = optimize_size(
            ticker="X", strategy="trend", requested_dollar=1000,
            equity=10_000, drawdown_pct=0.15,
            by_strategy_metrics={"trend": {"closed": 30, "win_rate": 0.7,
                                              "expectancy": 30.0,
                                              "profit_factor": 3.0,
                                              "avg_win": 50, "avg_loss": -20}},
        )
        assert d_big.drawdown_multiplier < d_zero.drawdown_multiplier
        assert d_big.recommended_dollar <= d_zero.recommended_dollar


# ── live API integration ────────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_sizing_primitives(self, client):
        body = client.get(
            "/portfolio/optimizer/sizing/primitives"
            "?win_rate=0.6&avg_win=200&avg_loss=-100"
            "&equity=10000&daily_loss_budget=200&sigma_pct=0.20"
            "&target_vol=0.15&drawdown_pct=0"
        ).json()
        assert body["kelly_fraction"] > 0
        assert body["cvar_fraction"] > 0
        assert body["vol_target_fraction"] > 0
        assert body["drawdown_multiplier"] == 1.0

    def test_preview_endpoint(self, client):
        body = client.post("/portfolio/optimizer/preview", json={
            "ticker": "NVDA", "strategy": "trend",
            "requested_dollar": 1000, "equity": 10_000,
            "drawdown_pct": 0.0,
            "by_strategy_metrics": {
                "trend": {"closed": 30, "win_rate": 0.7,
                           "expectancy": 30.0, "profit_factor": 3.0,
                           "avg_win": 50, "avg_loss": -20},
            },
        }).json()
        assert body["recommended_dollar"] <= body["requested_dollar"]
        assert "sizing" in body and "reasoning" in body

    def test_allocation_endpoint(self, client):
        body = client.get("/portfolio/optimizer/allocation").json()
        assert "allocations" in body and "cash_reserve_pct" in body

    def test_clusters_endpoint(self, client):
        body = client.get("/portfolio/optimizer/clusters").json()
        assert "clusters" in body
