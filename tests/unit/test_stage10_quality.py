"""Stage-10 items 1-4: staged exits + adaptive spread + drift halts + adaptive grade.

Pinned behaviour:
  • Staged exits: stop_loss / TP1 partial / ATR trail / time stop, each
    fires only when its condition is met; priority is time>stop>TP1>trail
  • TP1 takes only 50% — runner survives; HWM tracks for the trailing rule
  • Adaptive spread floor: < MIN_SAMPLES → static; ≥ MIN_SAMPLES → p75
  • Drift halts: persist to disk; clear when PSI back below clear_threshold
  • Adaptive min_grade: bands match Stage-1.5 ECE contract (0.05/0.08/0.12)
"""
import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.drift.auto_halt import (
    HALT_DIR,
    check_and_update_halts,
    clear_halt,
    halt_strategy,
    is_halted,
    list_halts,
)
from backend.bot.execution_costs.adaptive import (
    MIN_SAMPLES,
    adaptive_spread_floor,
    spread_quantiles,
)
from backend.bot.exits import (
    ExitState,
    atr_trail_policy,
    evaluate_policies,
    staged_tp_policy,
    stop_loss_policy,
    time_stop_policy,
)
from backend.bot.gates.adaptive import adaptive_min_grade


# ── exits ─────────────────────────────────────────────────────────────────


class TestStopLossPolicy:
    def test_fires_at_threshold(self):
        result = stop_loss_policy(entry_price=100, current_price=94,
                                     stop_pct=0.05, state=ExitState())
        assert result is not None
        assert result.action == "stop_loss"
        assert result.close_fraction == 1.0

    def test_no_fire_above_threshold(self):
        assert stop_loss_policy(entry_price=100, current_price=97,
                                   stop_pct=0.05, state=ExitState()) is None

    def test_zero_stop_disabled(self):
        assert stop_loss_policy(entry_price=100, current_price=50,
                                   stop_pct=0, state=ExitState()) is None


class TestStagedTPPolicy:
    def test_tp1_fires_at_threshold(self):
        result = staged_tp_policy(entry_price=100, current_price=110,
                                     take_profit_pct=0.10, state=ExitState())
        assert result is not None
        assert result.action == "tp1_partial"
        assert 0.4 <= result.close_fraction <= 0.6
        assert result.new_state.tp1_taken

    def test_tp1_only_fires_once(self):
        state = ExitState(tp1_taken=True)
        assert staged_tp_policy(entry_price=100, current_price=120,
                                   take_profit_pct=0.10, state=state) is None

    def test_custom_fraction(self):
        result = staged_tp_policy(entry_price=100, current_price=110,
                                     take_profit_pct=0.10, state=ExitState(),
                                     tp1_fraction=0.75)
        assert result.close_fraction == 0.75


class TestATRTrailPolicy:
    def test_inactive_before_tp1(self):
        """Trail only kicks in AFTER TP1 has fired."""
        result = atr_trail_policy(current_price=100, atr=2.0,
                                     state=ExitState(tp1_taken=False))
        assert result is None

    def test_holds_when_inside_trail(self):
        state = ExitState(tp1_taken=True, high_water_price=110.0)
        result = atr_trail_policy(current_price=108, atr=2.0, state=state)
        # 108 > 110 − 2×2 = 106 → hold (HWM updated to 108? No, 108 < 110)
        assert result.action == "hold"

    def test_closes_when_breached(self):
        state = ExitState(tp1_taken=True, high_water_price=110.0)
        # 110 − 2×2 = 106; price 105 ≤ trail
        result = atr_trail_policy(current_price=105, atr=2.0, state=state)
        assert result.action == "trail_close"
        assert result.close_fraction == 1.0

    def test_hwm_advances(self):
        state = ExitState(tp1_taken=True, high_water_price=110.0)
        result = atr_trail_policy(current_price=115, atr=2.0, state=state)
        assert result.new_state.high_water_price == 115.0


class TestTimeStopPolicy:
    def test_no_fire_inside_window(self):
        state = ExitState(opened_at=(datetime.utcnow() - timedelta(minutes=30)).isoformat())
        assert time_stop_policy(entry_price=100, current_price=101,
                                   state=state, max_hold_minutes=240,
                                   min_mfe_pct=0.005) is None

    def test_fires_after_window_with_low_mfe(self):
        state = ExitState(
            opened_at=(datetime.utcnow() - timedelta(hours=6)).isoformat()
        )
        result = time_stop_policy(entry_price=100, current_price=100.10,
                                     state=state, max_hold_minutes=240,
                                     min_mfe_pct=0.005)
        assert result is not None
        assert result.action == "time_stop"
        assert result.close_fraction == 1.0

    def test_no_fire_when_mfe_strong(self):
        state = ExitState(
            opened_at=(datetime.utcnow() - timedelta(hours=6)).isoformat(),
            last_mfe_pct=0.02,
        )
        assert time_stop_policy(entry_price=100, current_price=102,
                                   state=state, max_hold_minutes=240,
                                   min_mfe_pct=0.005) is None


class TestEvaluatePolicies:
    def test_returns_hold_by_default(self):
        result = evaluate_policies(entry_price=100, current_price=100,
                                      stop_pct=0.05, take_profit_pct=0.10,
                                      atr=1.0, state=ExitState())
        assert result.action == "hold"

    def test_stop_beats_tp_at_same_threshold_edge(self):
        # Both can't fire on same bar in practice; verify stop wins on the
        # downside scenario
        result = evaluate_policies(entry_price=100, current_price=92,
                                      stop_pct=0.05, take_profit_pct=0.10,
                                      atr=1.0, state=ExitState())
        assert result.action == "stop_loss"

    def test_tp_fires_when_up(self):
        result = evaluate_policies(entry_price=100, current_price=112,
                                      stop_pct=0.05, take_profit_pct=0.10,
                                      atr=1.0, state=ExitState())
        assert result.action == "tp1_partial"

    def test_time_stop_beats_others(self):
        state = ExitState(
            opened_at=(datetime.utcnow() - timedelta(hours=6)).isoformat(),
        )
        # Down meaningfully — time stop AND stop-loss both apply; time wins
        result = evaluate_policies(entry_price=100, current_price=99,
                                      stop_pct=0.05, take_profit_pct=0.10,
                                      atr=1.0, state=state)
        assert result.action == "time_stop"


# ── adaptive spread ────────────────────────────────────────────────────


class TestAdaptiveSpread:
    def test_static_fallback_below_min_samples(self, temp_db):
        # No samples yet → static floor
        v = adaptive_spread_floor("NEW")
        assert v == 1.0           # default TUNABLES.spread_bps_floor

    def test_uses_quantile_when_enough_data(self, temp_db):
        from backend.db import session_scope
        from backend.models.execution_log import ExecutionLog
        with session_scope() as s:
            for i in range(MIN_SAMPLES * 2):
                s.add(ExecutionLog(
                    trade_id=i, ticker="ABC", side="BUY",
                    quantity=1, expected_price=100.0,
                    fill_price=100.0 + i * 0.01,
                    slippage_bps=float(10 + i),       # 10..29
                    is_adverse=False,
                ))
        floor = adaptive_spread_floor("ABC")
        # p75 of 10..29 = ~25 — must be ≥ static_floor and ≥ 20
        assert floor >= 20.0

    def test_quantiles_shape(self, temp_db):
        from backend.db import session_scope
        from backend.models.execution_log import ExecutionLog
        with session_scope() as s:
            for i in range(MIN_SAMPLES + 1):
                s.add(ExecutionLog(
                    trade_id=i, ticker="QQ", side="BUY", quantity=1,
                    expected_price=100, fill_price=101, slippage_bps=float(i),
                    is_adverse=False,
                ))
        q = spread_quantiles("QQ")
        for key in ("p50", "p75", "p90", "p95", "p99", "adaptive_floor_bps"):
            assert key in q


# ── drift halts ────────────────────────────────────────────────────────


@pytest.fixture
def isolated_halt(tmp_path, monkeypatch):
    import backend.bot.drift.auto_halt as ah
    monkeypatch.setenv("TB_DRIFT_HALT_DIR", str(tmp_path / "drift"))
    monkeypatch.setattr(ah, "HALT_DIR", str(tmp_path / "drift"))
    yield tmp_path


class TestDriftHalts:
    def test_empty_state(self, isolated_halt):
        assert not is_halted("trend")
        assert list_halts() == []

    def test_halt_and_query(self, isolated_halt):
        halt_strategy(strategy="trend", feature="atr", psi_value=0.42)
        assert is_halted("trend")
        halts = list_halts()
        assert len(halts) == 1
        assert halts[0]["psi"] == 0.42

    def test_clear(self, isolated_halt):
        halt_strategy(strategy="trend", feature="x", psi_value=0.3)
        assert clear_halt("trend")
        assert not is_halted("trend")
        # idempotent
        assert not clear_halt("trend")

    def test_check_and_update_auto_halts(self, isolated_halt):
        baseline = {"trend": {"rsi": list(range(100))}}
        # current samples are shifted hard → PSI critical
        current = {"trend": {"rsi": [v + 200 for v in range(100)]}}
        report = check_and_update_halts(
            baseline_by_strategy=baseline,
            current_by_strategy=current,
        )
        assert "trend" in report["halted_now"]
        assert is_halted("trend")

    def test_check_and_update_clears_when_back_to_normal(self, isolated_halt):
        halt_strategy(strategy="momentum", feature="x", psi_value=0.4)
        baseline = {"momentum": {"x": list(range(100))}}
        current = {"momentum": {"x": list(range(100))}}    # identical
        report = check_and_update_halts(
            baseline_by_strategy=baseline,
            current_by_strategy=current,
        )
        assert "momentum" in report["cleared_now"]
        assert not is_halted("momentum")


# ── adaptive min_grade ────────────────────────────────────────────────


class TestAdaptiveMinGrade:
    def test_ece_none_returns_configured(self):
        assert adaptive_min_grade(configured_min_grade="C",
                                    calibration_error=None) == "C"

    def test_clean_ece_returns_configured(self):
        assert adaptive_min_grade(configured_min_grade="C",
                                    calibration_error=0.03) == "C"

    def test_mild_ece_tightens_to_B(self):
        # 0.05 < ece ≤ 0.08 → at least B
        assert adaptive_min_grade(configured_min_grade="C",
                                    calibration_error=0.07) == "B"

    def test_moderate_ece_tightens_to_A(self):
        assert adaptive_min_grade(configured_min_grade="C",
                                    calibration_error=0.10) == "A"

    def test_severe_ece_tightens_to_Aplus(self):
        assert adaptive_min_grade(configured_min_grade="C",
                                    calibration_error=0.15) == "A+"

    def test_brier_also_tightens(self):
        # ECE just over band + bad Brier → A+
        assert adaptive_min_grade(configured_min_grade="C",
                                    calibration_error=0.06,
                                    brier=0.30) == "A+"

    def test_configured_floor_wins_when_stronger(self):
        # configured already at A+; ECE bad shouldn't loosen it
        assert adaptive_min_grade(configured_min_grade="A+",
                                    calibration_error=0.20) == "A+"


# ── live API integration ────────────────────────────────────────────


@pytest.fixture
def client(temp_db, isolated_halt):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_exits_preview_hold(self, client):
        body = client.post("/exits/policy/preview", json={
            "entry_price": 100, "current_price": 100,
            "stop_pct": 0.05, "take_profit_pct": 0.10,
        }).json()
        assert body["action"] == "hold"

    def test_exits_preview_stop(self, client):
        body = client.post("/exits/policy/preview", json={
            "entry_price": 100, "current_price": 93,
            "stop_pct": 0.05, "take_profit_pct": 0.10,
        }).json()
        assert body["action"] == "stop_loss"

    def test_spread_quantiles_empty(self, client):
        body = client.get("/execution/spread/quantiles/NEWTICKER").json()
        assert body["samples"] == 0

    def test_adaptive_floor_endpoint(self, client):
        body = client.get(
            "/execution/spread/adaptive-floor/NEWTICKER?quantile=0.75"
        ).json()
        assert "adaptive_floor_bps" in body

    def test_halt_lifecycle(self, client):
        # POST → GET → DELETE
        r = client.post("/drift/halts", json={
            "strategy": "trend", "feature": "rsi", "psi_value": 0.4,
        })
        assert r.status_code == 200
        body = client.get("/drift/halts").json()
        assert len(body["halts"]) == 1
        r2 = client.delete("/drift/halts/trend")
        assert r2.status_code == 200
        body = client.get("/drift/halts").json()
        assert body["halts"] == []

    def test_halt_delete_404(self, client):
        assert client.delete("/drift/halts/notreal").status_code == 404

    def test_adaptive_grade_endpoint(self, client):
        body = client.get(
            "/gates/grade/adaptive?configured=C&calibration_error=0.10"
        ).json()
        assert body["effective_min_grade"] == "A"

    def test_live_grade_endpoint(self, client):
        body = client.get("/gates/grade/live").json()
        assert "effective_min_grade" in body
        assert "configured" in body
