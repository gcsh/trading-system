"""Stage-1.5 — experiment tracking + numeric promotion gates.

Pins:
  • dataset_hash is deterministic (same input → same hash)
  • record_experiment persists immutably and round-trips through get
  • compare_experiments correctly flags same_dataset / same_code
  • Gate verdicts: pass / fail / insufficient_data
  • Gate catalog is stable (the institutional contract)
"""
import os
import pytest
from fastapi.testclient import TestClient

from backend.bot.experiments import (
    code_sha,
    compare_experiments,
    dataset_hash,
    get_experiment,
    list_experiments,
    record_experiment,
)
from backend.bot.gates import CATALOG, GateCheck, evaluate_gates


# ── dataset hash ───────────────────────────────────────────────────────────


class TestDatasetHash:
    def test_deterministic(self, temp_db):
        data = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
        assert dataset_hash(data) == dataset_hash(data)

    def test_order_dependent_is_intentional(self, temp_db):
        # If order differs, hash differs. Users sort BEFORE hashing if they
        # want order-invariance — but the harness sorts for them.
        a = [{"a": 1}, {"a": 2}]
        b = [{"a": 2}, {"a": 1}]
        assert dataset_hash(a) != dataset_hash(b)

    def test_key_order_does_NOT_matter(self, temp_db):
        # dict key insertion order must not change the hash
        a = [{"a": 1, "b": 2}]
        b = [{"b": 2, "a": 1}]
        assert dataset_hash(a) == dataset_hash(b)

    def test_empty_safe(self, temp_db):
        assert isinstance(dataset_hash([]), str)


# ── experiment recorder ────────────────────────────────────────────────────


class TestExperimentRecorder:
    def test_roundtrip(self, temp_db):
        exp_id = record_experiment(
            name="t1", dataset_hash="abc", seed=42,
            params={"k": 1}, metrics={"sharpe": 0.85},
            label_quality={"ok": True, "closed": 100},
            notes="unit",
        )
        loaded = get_experiment(exp_id)
        assert loaded is not None
        assert loaded["name"] == "t1"
        assert loaded["dataset_hash"] == "abc"
        assert loaded["seed"] == 42
        assert loaded["params"] == {"k": 1}
        assert loaded["metrics"]["sharpe"] == 0.85
        assert loaded["label_quality"]["closed"] == 100

    def test_list_filters_by_name(self, temp_db):
        record_experiment(name="a")
        record_experiment(name="b")
        record_experiment(name="a")
        only_a = list_experiments(name="a")
        assert all(e["name"] == "a" for e in only_a)
        assert len(only_a) == 2

    def test_compare(self, temp_db):
        a = record_experiment(name="x", dataset_hash="h1",
                                metrics={"sharpe": 0.5, "win_rate": 0.45})
        b = record_experiment(name="x", dataset_hash="h1",
                                metrics={"sharpe": 0.8, "win_rate": 0.52})
        diff = compare_experiments(a, b)
        assert diff["same_dataset"] is True
        sharpe = next(d for d in diff["diffs"] if d["metric"] == "sharpe")
        assert sharpe["delta"] == pytest.approx(0.3, abs=1e-3)

    def test_compare_missing(self, temp_db):
        diff = compare_experiments(99999, 99998)
        assert "error" in diff


# ── gates ──────────────────────────────────────────────────────────────────


class TestGateCheck:
    def test_pass_lte(self):
        gc = GateCheck(name="x", threshold=0.10, direction="lte",
                        metric_path="data.calibration_error")
        result = gc.evaluate({"data": {"calibration_error": 0.04}})
        assert result["verdict"] == "pass"

    def test_fail_lte(self):
        gc = GateCheck(name="x", threshold=0.10, direction="lte",
                        metric_path="data.calibration_error")
        result = gc.evaluate({"data": {"calibration_error": 0.20}})
        assert result["verdict"] == "fail"

    def test_pass_gte(self):
        gc = GateCheck(name="s", threshold=1.0, direction="gte",
                        metric_path="data.sharpe")
        result = gc.evaluate({"data": {"sharpe": 1.5}})
        assert result["verdict"] == "pass"

    def test_insufficient_when_missing(self):
        gc = GateCheck(name="x", threshold=0, direction="gte",
                        metric_path="data.nope")
        result = gc.evaluate({"data": {}})
        assert result["verdict"] == "insufficient_data"

    def test_insufficient_when_thin(self):
        gc = GateCheck(name="x", threshold=0.5, direction="gte",
                        metric_path="data.win_rate", minimum_sample=100)
        result = gc.evaluate({"data": {"win_rate": 0.6}}, sample_size=10)
        assert result["verdict"] == "insufficient_data"
        assert "only 10" in result["reason"]


class TestCatalog:
    def test_catalog_is_stable(self):
        # If the catalog changes, the doc artifact must change too.
        names = {g.name for g in CATALOG}
        assert names == {
            "brier_ok", "calibration_error_ok", "sharpe_floor",
            "max_drawdown_ceiling", "win_rate_floor", "profit_factor_floor",
            "expectancy_positive",
            # Stage-11.8 stability gates.
            "brier_stability_ok", "calibration_error_stability_ok",
        }

    def test_thresholds_match_doc(self):
        by_name = {g.name: g for g in CATALOG}
        assert by_name["brier_ok"].threshold == 0.22
        assert by_name["calibration_error_ok"].threshold == 0.05
        assert by_name["sharpe_floor"].threshold == 1.2
        assert by_name["max_drawdown_ceiling"].threshold == 0.15
        assert by_name["win_rate_floor"].threshold == 0.45
        assert by_name["profit_factor_floor"].threshold == 1.5


class TestEvaluateGates:
    def test_empty_metrics_insufficient(self):
        result = evaluate_gates({"data": {}, "label_quality": {"closed": 0}})
        assert result["overall"] == "insufficient_data"
        assert result["fail_count"] == 0

    def test_all_pass(self):
        summary = {
            "data": {
                "sharpe": 1.5, "calibration_error": 0.03, "brier": 0.18,
                "max_drawdown_pct": 0.08, "win_rate": 0.55,
                "profit_factor": 2.1, "expectancy": 12.5,
            },
            "label_quality": {"closed": 150},
        }
        result = evaluate_gates(summary)
        assert result["overall"] == "pass"
        assert result["fail_count"] == 0

    def test_one_fail_makes_overall_fail(self):
        summary = {
            "data": {
                "sharpe": 0.5,        # fails (< 1.2)
                "calibration_error": 0.03, "brier": 0.18,
                "max_drawdown_pct": 0.08, "win_rate": 0.55,
                "profit_factor": 2.1, "expectancy": 12.5,
            },
            "label_quality": {"closed": 150},
        }
        result = evaluate_gates(summary)
        assert result["overall"] == "fail"
        assert result["fail_count"] == 1
        sharpe = next(g for g in result["gates"] if g["name"] == "sharpe_floor")
        assert sharpe["verdict"] == "fail"


# ── live API integration ───────────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_gates_catalog_endpoint(self, client):
        body = client.get("/gates/catalog").json()
        # Stage-1.5 contract = 7 gates + Stage-11.8 stability = 9.
        assert len(body["gates"]) == 9
        names = {g["name"] for g in body["gates"]}
        assert "brier_ok" in names
        assert "brier_stability_ok" in names

    def test_gates_status_empty_data(self, client):
        body = client.get("/gates/status").json()
        # No trades → insufficient_data overall
        assert body["overall"] == "insufficient_data"

    def test_experiments_empty(self, client):
        body = client.get("/experiments").json()
        assert body["experiments"] == []
        assert "code_sha" in body

    def test_experiments_missing_404(self, client):
        assert client.get("/experiments/9999").status_code == 404

    def test_run_walkforward_persists(self, client):
        # No labels yet → walkforward returns empty summary but still records.
        r = client.post("/experiments/run/walkforward?train_size=20&test_size=5")
        assert r.status_code == 200
        body = r.json()
        assert "experiment_id" in body
        assert body["n_windows"] == 0
        # Now the list endpoint should show it
        listed = client.get("/experiments").json()
        assert len(listed["experiments"]) == 1
        assert listed["experiments"][0]["name"] == "walkforward"
