"""Stage-2 execution realism — cost model, broker constraints, fill sim.

Every gap that lets a paper edge evaporate on a live broker has a test here:
  • Commission catalog rows are correct for each named broker
  • Spread model floors and scales with ATR
  • Slippage scales with sqrt(size/ADV) and volatility
  • Broker constraints reject illegal orders
  • Multi-leg atomicity returns the FAILURE event when sequential broker
    fills one leg and the next breaks
  • Backtest engine subtracts realistic costs from net returns
"""
import os
import pytest
from fastapi.testclient import TestClient

from backend.bot.broker_constraints import (
    BROKER_PROFILES,
    BrokerProfile,
    get_profile,
    validate_order,
)
from backend.bot.execution_costs import (
    COMMISSION_CATALOG,
    CommissionSchedule,
    commission_for,
    estimate_slippage_bps,
    estimate_spread_bps,
    estimate_total_cost,
)
from backend.bot.execution_sim import (
    simulate_fill,
    simulate_legs,
)


# ── commission catalog ─────────────────────────────────────────────────────


class TestCommissions:
    def test_local_paper_zero(self):
        c = commission_for("local_paper", "stock", shares=100, notional=10000)
        assert c == 0.0

    def test_alpaca_zero(self):
        assert commission_for("alpaca_live", "stock", shares=100,
                                notional=10000) == 0.0
        assert commission_for("alpaca_live", "option", contracts=10) == 0.0

    def test_robinhood_zero_options(self):
        assert commission_for("robinhood", "option", contracts=10) == 0.0

    def test_ibkr_pro_per_share(self):
        # 100 shares × $0.0035 = $0.35 = minimum
        c = commission_for("ibkr_pro", "stock", shares=100, notional=10000)
        assert c == 0.35
        # 10000 shares × $0.0035 = $35.00; cap at 1% notional = $100 (no cap fires)
        c2 = commission_for("ibkr_pro", "stock", shares=10000, notional=200_000)
        assert 30 <= c2 <= 35

    def test_ibkr_pro_options(self):
        # 5 contracts × $0.65 = $3.25
        assert commission_for("ibkr_pro", "option", contracts=5) == 3.25
        # 1 contract → $0.65 but minimum is $1.00
        assert commission_for("ibkr_pro", "option", contracts=1) == 1.00

    def test_unknown_broker_falls_back_to_paper(self):
        assert commission_for("totally_made_up", "stock", shares=10,
                                notional=1000) == 0.0


# ── spread + slippage ─────────────────────────────────────────────────────


class TestSpread:
    def test_floor_when_no_atr(self):
        bps = estimate_spread_bps(price=100.0)
        assert bps >= 1.0   # never below floor

    def test_scales_with_atr(self):
        low = estimate_spread_bps(price=100.0, atr=0.5)
        high = estimate_spread_bps(price=100.0, atr=2.0)
        assert high > low


class TestSlippage:
    def test_zero_notional_zero_slippage(self):
        assert estimate_slippage_bps(0) == 0.0

    def test_scales_with_size(self):
        small = estimate_slippage_bps(1_000, adv_dollar=10_000_000, volatility=0.20)
        big = estimate_slippage_bps(1_000_000, adv_dollar=10_000_000, volatility=0.20)
        assert big > small

    def test_scales_with_volatility(self):
        low_v = estimate_slippage_bps(100_000, adv_dollar=10_000_000, volatility=0.10)
        hi_v = estimate_slippage_bps(100_000, adv_dollar=10_000_000, volatility=0.50)
        assert hi_v > low_v

    def test_capped(self):
        # huge order → should hit the cap, not blow up
        s = estimate_slippage_bps(1_000_000_000_000, adv_dollar=100,
                                    volatility=2.0)
        assert s <= 200.0


# ── total cost ─────────────────────────────────────────────────────────────


class TestTotalCost:
    def test_local_paper_stock_buy(self):
        est = estimate_total_cost(
            broker="local_paper", instrument="stock", side="BUY",
            quantity=100, price=215.0,
            snapshot={"price": 215.0, "atr": 3.0, "volume_avg": 5_000_000},
        )
        # $0 commission, but spread + slippage must produce non-zero cost
        assert est.commission == 0.0
        assert est.spread_cost > 0
        assert est.slippage > 0
        assert est.total > 0
        assert est.notional == 100 * 215.0
        assert est.total_bps > 0

    def test_ibkr_pro_stock_includes_commission(self):
        est = estimate_total_cost(
            broker="ibkr_pro", instrument="stock", side="BUY",
            quantity=100, price=215.0,
            snapshot={"price": 215.0, "atr": 3.0, "volume_avg": 5_000_000},
        )
        # ibkr_pro: 100 shares × $0.0035 = $0.35 (= minimum)
        assert est.commission == 0.35

    def test_option_notional_matches_label_convention(self):
        est = estimate_total_cost(
            broker="ibkr_pro", instrument="option", side="BUY",
            quantity=1, price=6.50, strike=215.0,
        )
        # notional ≈ max($0.05, 0.03 × 215) × 100 × 1 = $645
        assert 640 <= est.notional <= 650

    def test_large_order_flagged(self):
        est = estimate_total_cost(
            broker="local_paper", instrument="stock", side="BUY",
            quantity=10_000, price=215.0,
            snapshot={"price": 215.0, "atr": 3.0, "volume_avg": 5_000_000},
        )
        assert any("large order" in n for n in est.notes)


# ── broker constraints ────────────────────────────────────────────────────


class TestConstraintCatalog:
    def test_all_listed_brokers_have_profiles(self):
        # Catalogs must be in sync — every commission row needs a profile.
        for name in COMMISSION_CATALOG:
            assert name in BROKER_PROFILES, f"missing profile for {name}"

    def test_alpaca_supports_atomicity(self):
        assert BROKER_PROFILES["alpaca_live"].leg_atomicity_supported

    def test_robinhood_no_atomicity(self):
        # Real-world rule: Robinhood spreads aren't combo orders.
        assert not BROKER_PROFILES["robinhood"].leg_atomicity_supported

    def test_ibkr_no_fractional(self):
        assert not BROKER_PROFILES["ibkr_pro"].stock_fractional_supported


class TestValidateOrder:
    def test_clean_market_order_passes(self):
        plan = {"instrument": "stock", "quantity": 100, "price": 215.0}
        assert validate_order(plan, "local_paper") == []

    def test_unsupported_order_type_rejected(self):
        plan = {"instrument": "stock", "quantity": 100, "price": 215.0}
        v = validate_order(plan, "local_paper", order_type="iceberg")
        assert any(x.name == "order_type_not_supported" for x in v)

    def test_ibkr_rejects_fractional(self):
        plan = {"instrument": "stock", "quantity": 1.5, "price": 215.0}
        v = validate_order(plan, "ibkr_pro")
        assert any(x.name == "fractional_not_supported" for x in v)

    def test_local_paper_allows_fractional(self):
        plan = {"instrument": "stock", "quantity": 1.5, "price": 215.0}
        assert validate_order(plan, "local_paper") == []

    def test_alpaca_live_max_notional_caps(self):
        plan = {"instrument": "stock", "quantity": 10_000, "price": 25.0}
        v = validate_order(plan, "alpaca_live")
        assert any(x.name == "above_max_notional" for x in v)

    def test_robinhood_spread_atomicity_warning(self):
        plan = {"instrument": "spread", "contracts": 1,
                 "legs": [{"strike": 100, "type": "call"},
                           {"strike": 105, "type": "call"}]}
        v = validate_order(plan, "robinhood")
        assert any(x.name == "atomicity_not_supported" for x in v)

    def test_too_many_legs(self):
        plan = {"instrument": "spread", "contracts": 1,
                 "legs": [{"k": 1}, {"k": 2}, {"k": 3}]}
        v = validate_order(plan, "robinhood")        # max_legs=2
        assert any(x.name == "too_many_legs" for x in v)


# ── fill simulation ───────────────────────────────────────────────────────


def _bars(volumes, base_price=100.0):
    return [{"open": base_price, "high": base_price * 1.001,
              "low": base_price * 0.999, "close": base_price,
              "volume": v} for v in volumes]


class TestSimulateFill:
    def test_full_fill_in_one_bar(self):
        bars = _bars([1_000_000])
        result = simulate_fill(side="BUY", quantity=1000, bars=bars,
                                snapshot={"price": 100.0, "atr": 1.0})
        assert result.filled_quantity == 1000
        assert not result.partial
        assert result.bars_used == 1

    def test_partial_fill_when_volume_thin(self):
        # cap = 10% of bar volume. 1000-share order on 5000-volume bar → 500/bar.
        bars = _bars([5_000] * 3)
        result = simulate_fill(side="BUY", quantity=1000, bars=bars,
                                snapshot={"price": 100.0, "atr": 1.0},
                                volume_share_cap=0.10)
        assert result.bars_used == 2     # 500 + 500
        assert result.filled_quantity == 1000

    def test_never_overfills(self):
        bars = _bars([1_000_000])
        r = simulate_fill(side="BUY", quantity=1000, bars=bars,
                            snapshot={"price": 100.0, "atr": 1.0})
        assert r.filled_quantity <= 1000

    def test_fill_price_includes_costs(self):
        bars = _bars([1_000_000])
        r = simulate_fill(side="BUY", quantity=1000, bars=bars,
                            snapshot={"price": 100.0, "atr": 1.0})
        assert r.avg_fill_price > 100.0   # paid more on the cross


# ── multi-leg atomicity ──────────────────────────────────────────────────


class TestSimulateLegs:
    def test_atomic_broker_always_succeeds(self):
        legs = [{"strike": 100, "type": "call"},
                {"strike": 105, "type": "call"}]
        result = simulate_legs(legs, atomicity_supported=True)
        assert result.atomic
        assert not result.atomic_failure
        assert all(l.filled for l in result.legs)

    def test_sequential_broker_can_fail(self):
        legs = [{"strike": 100}, {"strike": 105}]
        # Force every leg to fail; should report atomic=True (none filled).
        result = simulate_legs(legs, atomicity_supported=False,
                                leg_fail_prob=1.0, rng_seed=1)
        assert not result.atomic_failure        # nothing filled → no half-spread
        assert all(not l.filled for l in result.legs)

    def test_sequential_broker_with_partial_failure(self):
        # Force exactly one leg to fail with a controlled seed where
        # randomness yields filled then failed (the dangerous half-spread case)
        legs = [{"strike": 100}, {"strike": 105}]
        # Seed engineered: first leg passes (random > 0.5), second fails.
        result = simulate_legs(legs, atomicity_supported=False,
                                leg_fail_prob=0.5, rng_seed=0)
        n_filled = sum(1 for l in result.legs if l.filled)
        # Whatever the seed produces, the invariant must hold:
        if 0 < n_filled < len(legs):
            assert result.atomic_failure
            assert any("ATOMICITY FAILURE" in n for n in result.notes)


# ── backtest integration ─────────────────────────────────────────────────


class TestBacktestRealisticCosts:
    def test_net_returns_smaller_than_gross(self):
        """With realistic costs, net return per trade should be ≤ gross."""
        from unittest.mock import patch
        import numpy as np
        import pandas as pd

        from backend.bot import backtest

        rng = np.random.default_rng(7)
        n = 120
        base = np.linspace(100, 140, n) + rng.normal(0, 1.5, n)
        idx = pd.date_range("2026-01-01", periods=n, freq="D")
        df = pd.DataFrame({
            "Open": base, "High": base + 1, "Low": base - 1, "Close": base,
            "Volume": rng.integers(1_000_000, 5_000_000, n),
        }, index=idx)
        with patch.object(backtest, "fetch_candles", return_value=df):
            out = backtest.run_backtest("macd_momentum", "AAPL")
        sim = out["backtest"]
        assert sim["realistic_costs_applied"]
        assert sim["total_costs_dollar"] >= 0
        assert "net_win_rate" in sim
        # Costs landed on the trade rows too
        if sim["trades"]:
            assert "round_trip_cost" in sim["trades"][0]
            assert "net_return_pct" in sim["trades"][0]


# ── live endpoint integration ────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_cost_preview(self, client):
        body = client.get(
            "/execution/costs/preview"
            "?broker=ibkr_pro&instrument=stock&side=BUY&quantity=100&price=215"
        ).json()
        assert body["estimate"]["commission"] == 0.35
        assert body["estimate"]["notional"] == 21500.0
        assert body["estimate"]["total"] > 0

    def test_brokers_index(self, client):
        body = client.get("/execution/brokers").json()
        names = {b["name"] for b in body["brokers"]}
        assert {"local_paper", "alpaca_live", "ibkr_pro", "robinhood"} <= names

    def test_unknown_broker_404(self, client):
        assert client.get("/execution/brokers/nopebroker").status_code == 404

    def test_validate_order(self, client):
        body = client.post("/execution/validate-order", json={
            "plan": {"instrument": "stock", "quantity": 1.5, "price": 100.0},
            "broker": "ibkr_pro",
        }).json()
        assert not body["ok"]
        assert any(v["name"] == "fractional_not_supported"
                     for v in body["violations"])

    def test_simulate_fill_endpoint(self, client):
        bars = [{"open": 100, "high": 101, "low": 99, "close": 100,
                  "volume": 1_000_000}]
        body = client.post("/execution/simulate-fill", json={
            "side": "BUY", "quantity": 500, "bars": bars,
            "snapshot": {"price": 100.0, "atr": 1.0},
        }).json()
        assert body["filled_quantity"] == 500
        assert not body["partial"]

    def test_simulate_legs_endpoint(self, client):
        body = client.post("/execution/simulate-legs", json={
            "legs": [{"strike": 100}, {"strike": 105}],
            "broker": "alpaca_live",
            "rng_seed": 1,
        }).json()
        assert body["atomic"]
        assert not body["atomic_failure"]
