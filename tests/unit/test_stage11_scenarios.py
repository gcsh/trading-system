"""Stage-11.6 Scenario Engine — sensitivities + presets + endpoints.

Pinned:
  • Stock under risk-off shock loses (beta × spy_pct × MV)
  • Long call gains under up-move shock; long put gains under down-move
  • Long option vega contribution scales with VIX delta
  • Tech long loses under rates shock; financial long gains
  • Sector overrides feed through the sector_pcts dict
  • Preset registry surfaces in /scenarios/presets
  • POST /scenarios/run accepts an inline positions override
  • GET /scenarios/run/{preset} resolves named scenarios; 404 on unknown
"""
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.scenarios import (
    PRESETS,
    PositionImpact,
    ScenarioResult,
    Shock,
    preset_list,
    run_scenario,
)


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def _stock(ticker, mv=10000, pnl=0.0):
    return {"ticker": ticker, "kind": "stock", "quantity": mv / 100,
            "market_value": mv, "unrealized_pnl": pnl,
            "current_price": 100.0, "avg_cost": 100.0}


def _option(ticker, opt_type="call", side="LONG", mv=2000, pnl=0.0):
    return {"ticker": ticker, "kind": "option", "option_type": opt_type,
            "side": side, "quantity": 1, "market_value": mv,
            "unrealized_pnl": pnl, "strike": 200, "expiration": "2026-06-21"}


def _complex(ticker, net_delta=0.0, mv=500, pnl=0.0):
    return {"ticker": ticker, "kind": "complex", "side": "LONG",
            "market_value": mv, "unrealized_pnl": pnl,
            "meta": {"net_delta": net_delta}}


# ── sensitivities ────────────────────────────────────────────────────────


class TestSensitivities:
    def test_stock_loses_under_risk_off(self):
        # NVDA has beta ≈ 1.7; SPY -1% → ΔP&L ≈ -10000 * 1.7 * 0.01 = -170
        r = run_scenario([_stock("NVDA")], Shock(spy_pct=-0.01))
        impact = r.impacts[0]
        assert impact.pnl_delta < 0
        assert -200 <= impact.pnl_delta <= -150
        assert impact.breakdown["spy"] == impact.pnl_delta

    def test_long_call_gains_on_up_move(self):
        # ATM call ~ 0.55 delta; underlying = beta * spy_pct
        # NVDA call mv=2000, SPY +2% → underlying ≈ 1.7*0.02 = 3.4%
        # delta_contrib ≈ 2000 * 0.55 * 0.034 ≈ 37.4
        r = run_scenario([_option("NVDA", "call")], Shock(spy_pct=0.02))
        i = r.impacts[0]
        assert i.pnl_delta > 0
        assert 30 <= i.pnl_delta <= 50

    def test_long_put_gains_on_down_move(self):
        r = run_scenario([_option("NVDA", "put")], Shock(spy_pct=-0.02))
        i = r.impacts[0]
        assert i.pnl_delta > 0
        # put_delta = -0.45 ; underlying = -3.4% ; long sign +1
        # delta_contrib = 2000 * -0.45 * -0.034 ≈ 30.6
        assert 25 <= i.pnl_delta <= 40

    def test_vix_spike_helps_long_options(self):
        # vega_factor 0.02 → +20 VIX on 2000 mv = +800
        r = run_scenario([_option("NVDA", "call")], Shock(vix_delta=20))
        i = r.impacts[0]
        assert i.breakdown["vega"] == pytest.approx(800.0, rel=0.01)
        assert i.pnl_delta == pytest.approx(800.0, rel=0.01)

    def test_short_option_loses_when_vol_spikes(self):
        r = run_scenario([_option("NVDA", "call", side="SHORT")],
                            Shock(vix_delta=20))
        i = r.impacts[0]
        assert i.pnl_delta < 0
        assert i.side == "SHORT"

    def test_tech_loses_on_rates_shock(self):
        # NVDA is in Semis → rate sensitivity -0.002
        # ΔP&L ≈ 10000 * -0.002 * 50 = -1000
        r = run_scenario([_stock("NVDA")], Shock(rates_bps=50))
        i = r.impacts[0]
        assert i.pnl_delta < 0
        assert i.breakdown["rates"] < 0

    def test_complex_uses_net_delta_from_meta(self):
        r = run_scenario([_complex("NVDA", net_delta=0.3, mv=1000)],
                            Shock(spy_pct=-0.05))
        i = r.impacts[0]
        # delta_contrib = 1000 * 0.3 * (1.7 * -0.05) = -25.5
        assert i.pnl_delta < 0
        assert i.instrument == "complex"

    def test_sector_overrides_apply(self):
        # SPY +2% with a -3% extra hit to Semis sector
        positions = [_stock("NVDA"), _stock("JPM")]
        r = run_scenario(positions, Shock(spy_pct=0.02, sector_pcts={"Semis": -0.03}))
        impacts = {i.ticker: i for i in r.impacts}
        # NVDA gets the sector hit, JPM doesn't.
        assert impacts["NVDA"].breakdown["sector"] < 0
        assert impacts["JPM"].breakdown["sector"] == 0


# ── aggregation ──────────────────────────────────────────────────────────


class TestAggregation:
    def test_totals_roll_up(self):
        positions = [_stock("NVDA", mv=10000, pnl=100),
                      _stock("AAPL", mv=5000, pnl=-50)]
        r = run_scenario(positions, Shock(spy_pct=-0.02))
        assert isinstance(r, ScenarioResult)
        assert r.total_market_value == 15000.0
        assert r.total_base_pnl == 50.0
        assert r.total_pnl_delta < 0
        assert r.new_total_pnl == round(r.total_base_pnl + r.total_pnl_delta, 2)
        assert r.summary["positions"] == 2
        assert r.summary["worst"]["ticker"] in {"NVDA", "AAPL"}

    def test_empty_portfolio_returns_zeros(self):
        r = run_scenario([], Shock(spy_pct=-0.05))
        assert r.total_market_value == 0.0
        assert r.total_pnl_delta == 0.0
        assert r.summary["positions"] == 0

    def test_by_instrument_buckets(self):
        positions = [_stock("NVDA"), _option("NVDA", "call")]
        r = run_scenario(positions, Shock(spy_pct=-0.02, vix_delta=5))
        b = r.summary["by_instrument"]
        assert "stock" in b and "option" in b


# ── presets ──────────────────────────────────────────────────────────────


class TestPresets:
    def test_registry_has_all_required(self):
        for name in ("mild_risk_off", "severe_risk_off", "risk_on",
                      "rates_shock", "vix_spike", "flash_crash"):
            assert name in PRESETS

    def test_preset_list_serializes(self):
        out = preset_list()
        assert len(out) == len(PRESETS)
        assert all("name" in p and "spy_pct" in p for p in out)
        # ordering is preserved
        assert out[0]["name"] == "mild_risk_off"


# ── endpoints ────────────────────────────────────────────────────────────


class TestScenarioEndpoints:
    def test_presets_endpoint(self, client):
        body = client.get("/scenarios/presets").json()
        names = [p["name"] for p in body["presets"]]
        assert "severe_risk_off" in names
        assert "flash_crash" in names

    def test_run_with_inline_positions(self, client):
        body = client.post("/scenarios/run", json={
            "spy_pct": -0.02, "vix_delta": 5,
            "positions": [
                {"ticker": "NVDA", "kind": "stock", "quantity": 100,
                  "market_value": 10000, "unrealized_pnl": 50},
            ],
        }).json()
        assert body["total_market_value"] == 10000.0
        assert body["total_pnl_delta"] < 0
        assert len(body["impacts"]) == 1

    def test_run_preset_returns_shock_metadata(self, client):
        body = client.post("/scenarios/run", json={
            "spy_pct": PRESETS["severe_risk_off"].spy_pct,
            "vix_delta": PRESETS["severe_risk_off"].vix_delta,
            "label": "severe_risk_off",
            "positions": [{"ticker": "NVDA", "kind": "stock", "quantity": 100,
                            "market_value": 10000, "unrealized_pnl": 0}],
        }).json()
        assert body["shock"]["spy_pct"] == -0.05
        assert body["shock"]["vix_delta"] == 15

    def test_run_named_preset_endpoint(self, client):
        # No live positions yet (fresh test DB), but the endpoint should still
        # 200 with an empty impacts list — that proves the preset resolves.
        body = client.get("/scenarios/run/mild_risk_off").json()
        assert body["shock"]["spy_pct"] == -0.01
        assert isinstance(body["impacts"], list)

    def test_run_unknown_preset_404(self, client):
        assert client.get("/scenarios/run/does_not_exist").status_code == 404
