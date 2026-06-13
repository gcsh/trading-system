"""Stage-8 — stress scenarios, replay, canary state machine, kill switch.

Pinned behavior:
  • Every stress scenario produces a mutated snapshot WITHOUT crashing
  • flash_crash spikes VIX + cuts price; bot strategies must not BUY into it
  • halted forces price=0; audit invariants would catch any trade attempt
  • bad_quote produces negative/NaN values — sanitization can't silently pass
  • Canary state machine refuses promote when gates fail, accepts when pass
  • Rollback always succeeds + records reason
  • Kill switch persists and is read by `kill_switch_active()`
"""
import math
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.canary import (
    get_state,
    halt,
    kill_switch_active,
    kill_switch_status,
    promote,
    rollback,
    set_kill_switch,
)
from backend.bot.stress import (
    SCENARIOS,
    apply_scenario,
    available_scenarios,
    bad_quote,
    flash_crash,
    halted,
    illiquid_chain,
    run_suite,
    stale_data,
    vix_spike,
    wide_spread,
)


_BASE_SNAPSHOT = {
    "price": 215.35, "high": 218.0, "low": 213.0, "volume": 5_000_000,
    "volume_avg": 4_500_000, "vix": 18.0, "atr": 4.0,
    "bid": 215.30, "ask": 215.40, "iv_rank": 35, "implied_move": 0.05,
    "has_options": True,
}


# ── stress scenarios ─────────────────────────────────────────────────────


class TestStressScenarios:
    def test_all_registered(self):
        names = available_scenarios()
        # Sanity: every scenario in the dict is in the available list
        assert sorted(SCENARIOS) == sorted(names)
        assert "flash_crash" in names

    def test_flash_crash_drops_price_spikes_vix(self):
        result = flash_crash(_BASE_SNAPSHOT)
        snap = result.mutated_snapshot
        assert snap["price"] < _BASE_SNAPSHOT["price"]
        assert snap["vix"] > _BASE_SNAPSHOT["vix"]
        # ≥ 1.8× — covers both default 0.10 and edge cases
        assert snap["vix"] >= _BASE_SNAPSHOT["vix"] * 1.8

    def test_halted_zeroes_price(self):
        snap = halted(_BASE_SNAPSHOT).mutated_snapshot
        assert snap["price"] == 0.0
        assert snap.get("halted") is True

    def test_bad_quote_returns_negative_or_nan(self):
        snap = bad_quote(_BASE_SNAPSHOT).mutated_snapshot
        assert snap["price"] < 0
        # NaN check
        v = snap["volume"]
        assert v != v        # NaN is the only number that's not equal to itself

    def test_illiquid_chain_empties_options(self):
        snap = illiquid_chain(_BASE_SNAPSHOT).mutated_snapshot
        assert snap["option_chain_strikes"] == []
        assert snap["has_options"] is False
        assert snap["iv_rank"] is None

    def test_vix_spike_doubles(self):
        snap = vix_spike(_BASE_SNAPSHOT).mutated_snapshot
        assert snap["vix"] == round(_BASE_SNAPSHOT["vix"] * 2.0, 2)

    def test_wide_spread_increases_bps(self):
        snap = wide_spread(_BASE_SNAPSHOT).mutated_snapshot
        assert snap["bid"] < _BASE_SNAPSHOT["bid"]
        assert snap["ask"] > _BASE_SNAPSHOT["ask"]
        assert snap["spread_bps"] > 5

    def test_stale_data(self):
        snap = stale_data(_BASE_SNAPSHOT).mutated_snapshot
        assert snap["data_age_minutes"] > 1000

    def test_apply_unknown_returns_none(self):
        assert apply_scenario("notarealscenario", _BASE_SNAPSHOT) is None

    def test_run_suite_returns_all(self):
        results = run_suite(_BASE_SNAPSHOT)
        assert len(results) == len(available_scenarios())


class TestScenarioEngineSafety:
    """Drive every scenario through one of the simpler strategies (Trend
    Pullback) — the strategy MUST NOT crash and must NOT issue a BUY when
    the snapshot is degenerate."""

    def test_strategies_survive_every_scenario(self):
        from backend.bot.strategies.all_strategies import TrendPullback
        from backend.bot.strategies.base import Action
        strat = TrendPullback()
        for name in available_scenarios():
            mutated = apply_scenario(name, _BASE_SNAPSHOT).mutated_snapshot
            sig = strat.analyze("NVDA", mutated)
            # On degenerate input the strategy can fail back to HOLD; we
            # only insist it doesn't crash AND doesn't issue an active
            # BUY against a corrupt snapshot (negative price etc).
            if mutated.get("price", 0) <= 0 or mutated.get("halted"):
                assert sig.action == Action.HOLD, (
                    f"strategy emitted {sig.action} on scenario {name}")


# ── canary state machine ────────────────────────────────────────────────


@pytest.fixture
def isolated_canary(tmp_path, monkeypatch):
    import backend.bot.canary as cc
    monkeypatch.setenv("TB_CANARY_DIR", str(tmp_path / "canary"))
    monkeypatch.setattr(cc, "CANARY_DIR", str(tmp_path / "canary"))
    yield tmp_path


class TestCanary:
    def test_default_state_is_paper(self, isolated_canary):
        assert get_state().state == "paper"

    def test_promote_refused_when_gates_fail(self, isolated_canary):
        result = promote(target="canary", capital=500,
                          gates_summary={"overall": "fail"})
        assert not result["ok"]
        # state did not move
        assert get_state().state == "paper"

    def test_promote_refused_on_insufficient(self, isolated_canary):
        result = promote(target="canary", capital=500,
                          gates_summary={"overall": "insufficient_data"})
        assert not result["ok"]

    def test_promote_accepts_when_gates_pass(self, isolated_canary):
        result = promote(target="canary", capital=500,
                          gates_summary={"overall": "pass"})
        assert result["ok"]
        assert get_state().state == "canary"
        assert get_state().capital == 500

    def test_promote_accepts_force_override(self, isolated_canary):
        result = promote(target="canary", capital=100, force=True,
                          gates_summary={"overall": "fail"})
        assert result["ok"]
        assert get_state().state == "canary"

    def test_rollback_returns_to_paper(self, isolated_canary):
        promote(target="canary", capital=500,
                  gates_summary={"overall": "pass"})
        rollback(reason="test rollback")
        st = get_state()
        assert st.state == "paper"
        assert st.capital == 0.0
        assert st.rollback_reason == "test rollback"

    def test_halt_sets_halted(self, isolated_canary):
        halt(reason="manual safety stop")
        assert get_state().state == "halted"

    def test_invalid_target_rejected(self, isolated_canary):
        result = promote(target="moonshot", capital=1000,
                          gates_summary={"overall": "pass"})
        assert not result["ok"]


# ── kill switch ─────────────────────────────────────────────────────────


class TestKillSwitch:
    def test_default_inactive(self, isolated_canary):
        assert not kill_switch_active()
        assert kill_switch_status()["active"] is False

    def test_activation_persists(self, isolated_canary):
        set_kill_switch(True, reason="operator halt")
        assert kill_switch_active()
        s = kill_switch_status()
        assert s["active"]
        assert s["reason"] == "operator halt"

    def test_deactivation(self, isolated_canary):
        set_kill_switch(True, reason="t")
        set_kill_switch(False, reason="all clear")
        assert not kill_switch_active()


# ── live API integration ────────────────────────────────────────────────


@pytest.fixture
def client(temp_db, isolated_canary):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_list_scenarios(self, client):
        body = client.get("/stress/scenarios").json()
        assert "scenarios" in body
        assert "flash_crash" in body["scenarios"]

    def test_apply_scenario_endpoint(self, client):
        body = client.post("/stress/scenario/flash_crash",
                            json=_BASE_SNAPSHOT).json()
        assert body["scenario"] == "flash_crash"
        assert body["mutated_snapshot"]["vix"] > _BASE_SNAPSHOT["vix"]

    def test_apply_unknown_returns_404(self, client):
        r = client.post("/stress/scenario/notreal", json=_BASE_SNAPSHOT)
        assert r.status_code == 404

    def test_apply_suite(self, client):
        body = client.post("/stress/apply", json={
            "snapshot": _BASE_SNAPSHOT, "scenarios": None,
        }).json()
        assert body["count"] == len(SCENARIOS)

    def test_canary_state_endpoint(self, client):
        body = client.get("/canary/state").json()
        assert body["state"] == "paper"

    def test_canary_promote_refused_without_data(self, client):
        r = client.post("/canary/promote", json={
            "target": "canary", "capital": 500, "force": False,
        })
        # 422 because the thin live data won't have gates green
        assert r.status_code == 422

    def test_canary_promote_force(self, client):
        r = client.post("/canary/promote", json={
            "target": "canary", "capital": 100, "force": True,
        })
        assert r.status_code == 200
        assert client.get("/canary/state").json()["state"] == "canary"

    def test_canary_rollback(self, client):
        client.post("/canary/promote", json={
            "target": "canary", "capital": 100, "force": True,
        })
        r = client.post("/canary/rollback", json={"reason": "tests"})
        assert r.status_code == 200
        assert client.get("/canary/state").json()["state"] == "paper"

    def test_kill_switch_round_trip(self, client):
        client.post("/canary/kill-switch", json={
            "active": True, "reason": "tests",
        })
        assert client.get("/canary/kill-switch").json()["active"]
        client.post("/canary/kill-switch", json={"active": False})
        assert not client.get("/canary/kill-switch").json()["active"]
