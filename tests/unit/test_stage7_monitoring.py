"""Stage-7 — drift detection, monitoring SLOs, attribution, explainability.

Pinned behavior:
  • PSI: identical samples ⇒ ~0; large shifts ⇒ ≥ 0.25 (institutional retrain trigger)
  • severity_for: 0/None → ok; 0.10–0.25 → watch; ≥ 0.25 → critical
  • feed_health: records success + failure; computes p50/p95/p99 from latencies
  • SLO breach when last_success exceeds the per-feed stale threshold
  • Attribution: buckets sum to total; PF "inf" sentinel JSON-safe
  • Explainability: pulls trade + decision + execution; 404 for unknown id
"""
import os
import time

import pytest
from fastapi.testclient import TestClient

from backend.bot.drift import (
    DriftReport,
    assess_feature_drift,
    assess_prediction_drift,
    psi,
    severity_for,
)
from backend.bot.monitoring import (
    _STATE,
    feed_health,
    feed_summary,
    record_failure,
    record_success,
    timing,
)


# ── drift / PSI math ─────────────────────────────────────────────────────


class TestPSI:
    def test_identical_returns_near_zero(self):
        sample = list(range(100))
        out = psi(sample, sample)
        assert out is not None and out < 0.01

    def test_shift_returns_large_psi(self):
        baseline = list(range(100))
        # Shift current by adding 100 to everything (extreme shift)
        current = [v + 100 for v in baseline]
        out = psi(baseline, current)
        assert out is not None and out >= 0.25

    def test_subset_shift_moderate(self):
        baseline = list(range(100))
        current = list(range(50, 150))   # half overlap, half new
        out = psi(baseline, current)
        # Should be in the "watch" range or higher
        assert out is not None and out >= 0.05

    def test_empty_returns_none(self):
        assert psi([], [1, 2, 3]) is None
        assert psi([1, 2, 3], []) is None


class TestSeverity:
    def test_thresholds(self):
        assert severity_for(0.0) == "ok"
        assert severity_for(0.09) == "ok"
        assert severity_for(0.10) == "watch"
        assert severity_for(0.249) == "watch"
        assert severity_for(0.25) == "critical"
        assert severity_for(1.0) == "critical"
        assert severity_for(None) == "ok"


class TestFeatureDriftReport:
    def test_clean_data_overall_ok(self):
        report = assess_feature_drift(
            baseline_numeric={"rsi": [40, 50, 60, 70] * 10},
            current_numeric={"rsi": [40, 50, 60, 70] * 10},
        )
        assert report.overall == "ok"
        assert all(s.severity == "ok" for s in report.signals)

    def test_critical_propagates(self):
        report = assess_feature_drift(
            baseline_numeric={"x": [1, 2, 3] * 30},
            current_numeric={"x": [100, 200, 300] * 30},
        )
        assert report.overall == "critical"

    def test_categorical_drift(self):
        report = assess_feature_drift(
            baseline_numeric={},
            current_numeric={},
            baseline_categorical={"regime": ["bullish"] * 30 + ["choppy"] * 10},
            current_categorical={"regime": ["bearish"] * 35 + ["choppy"] * 5},
        )
        assert report.overall == "critical"   # regime fully flipped


class TestPredictionDrift:
    def test_signal_shape(self):
        out = assess_prediction_drift(
            baseline_preds=[0.5] * 50,
            current_preds=[0.9] * 50,
        )
        assert out.name == "predicted_probability"
        assert out.severity in ("critical", "watch", "ok")


# ── monitoring ─────────────────────────────────────────────────────────


class TestMonitoring:
    def setup_method(self):
        _STATE.clear()

    def test_no_observations(self):
        h = feed_health("yfinance")
        assert h.success_count == 0
        assert "no observations" in (h.notes[0] if h.notes else "")

    def test_record_success_updates_latency_percentiles(self):
        for lat in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
            record_success("yfinance", lat)
        h = feed_health("yfinance")
        assert h.success_count == 10
        assert h.p50_ms is not None
        assert 40 <= h.p50_ms <= 60
        assert h.p95_ms is not None and h.p95_ms >= h.p50_ms
        assert h.p99_ms is not None and h.p99_ms >= h.p95_ms

    def test_failure_increments(self):
        record_failure("yfinance", error="ConnectionError")
        h = feed_health("yfinance")
        assert h.failure_count == 1
        assert h.last_failure_at is not None

    def test_slo_breach_when_stale(self):
        # 31 minutes since last success → breach (SLO = 30 min for yfinance)
        record_success("yfinance", 10.0)
        # Roll back the recorded timestamp
        _STATE["yfinance"]["last_success_ts"] = time.time() - 31 * 60
        h = feed_health("yfinance")
        assert h.slo_breached

    def test_within_slo_not_breached(self):
        record_success("yfinance", 10.0)
        h = feed_health("yfinance")
        assert not h.slo_breached

    def test_timing_context_records_on_success(self):
        with timing("anthropic"):
            time.sleep(0.001)
        h = feed_health("anthropic")
        assert h.success_count == 1

    def test_timing_context_records_on_exception(self):
        try:
            with timing("cboe"):
                raise RuntimeError("nope")
        except RuntimeError:
            pass
        h = feed_health("cboe")
        assert h.failure_count == 1

    def test_summary_lists_breaches(self):
        record_success("yfinance", 5.0)
        _STATE["yfinance"]["last_success_ts"] = time.time() - 60 * 60
        record_success("anthropic", 5.0)
        s = feed_summary()
        assert "yfinance" in s["breached_feeds"]
        assert s["any_breach"]
        assert "anthropic" not in s["breached_feeds"]


# ── attribution ─────────────────────────────────────────────────────────


class TestAttribution:
    def test_empty(self, temp_db):
        from backend.bot.attribution import (
            attribution_by_strategy,
            attribution_by_regime,
        )
        assert attribution_by_strategy() == []
        assert attribution_by_regime() == []

    def test_seeded_buckets_sum(self, temp_db):
        from backend.bot.attribution import attribution_by_strategy
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            s.add(Trade(ticker="A", action="BUY_STOCK", quantity=1, price=100,
                         strategy="alpha", signal_source="t", confidence=0.7,
                         paper=1, status="closed", pnl=20, instrument="stock"))
            s.add(Trade(ticker="B", action="BUY_STOCK", quantity=1, price=100,
                         strategy="alpha", signal_source="t", confidence=0.7,
                         paper=1, status="closed", pnl=-5, instrument="stock"))
            s.add(Trade(ticker="C", action="BUY_STOCK", quantity=1, price=100,
                         strategy="beta", signal_source="t", confidence=0.7,
                         paper=1, status="closed", pnl=15, instrument="stock"))
        buckets = attribution_by_strategy()
        by_key = {b["key"]: b for b in buckets}
        assert by_key["alpha"]["closed"] == 2
        assert by_key["alpha"]["total_pnl"] == 15.0
        assert by_key["beta"]["closed"] == 1
        # Contribution percentages sum to ~1 (within rounding)
        total = sum(b["pnl_contribution_pct"] for b in buckets
                     if b["pnl_contribution_pct"] is not None)
        assert abs(total - 1.0) < 0.02


# ── live endpoints ──────────────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    _STATE.clear()
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_drift_psi_inline(self, client):
        body = client.get("/drift/psi?baseline=1,2,3,4,5&current=10,20,30,40,50").json()
        assert "psi" in body
        assert body["psi"] is not None and body["psi"] >= 0.25
        assert body["severity"] == "critical"

    def test_drift_feature_post(self, client):
        body = client.post("/drift/feature", json={
            "baseline_numeric": {"x": list(range(50))},
            "current_numeric": {"x": list(range(50))},
        }).json()
        assert body["overall"] == "ok"

    def test_drift_prediction_post(self, client):
        body = client.post("/drift/prediction", json={
            "baseline_preds": [0.5] * 100, "current_preds": [0.9] * 100,
        }).json()
        assert body["severity"] in ("critical", "watch", "ok")

    def test_monitoring_record_and_health(self, client):
        client.post("/monitoring/record", json={
            "feed": "test_feed", "latency_ms": 25, "success": True,
        })
        body = client.get("/monitoring/health").json()
        assert any(f["name"] == "test_feed" for f in body["feeds"])

    def test_monitoring_feed_detail(self, client):
        client.post("/monitoring/record", json={
            "feed": "yfinance", "latency_ms": 50, "success": True,
        })
        body = client.get("/monitoring/feed/yfinance").json()
        assert body["success_count"] >= 1

    def test_attribution_endpoints_shape(self, client):
        for path in ("/attribution/by-strategy", "/attribution/by-regime",
                      "/attribution/by-grade"):
            body = client.get(path).json()
            assert "buckets" in body
            assert isinstance(body["buckets"], list)

    def test_explain_not_found(self, client):
        assert client.get("/explain/trade/999999").status_code == 404

    def test_explain_existing_trade(self, client):
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            t = Trade(ticker="NVDA", action="BUY_STOCK", quantity=1, price=200,
                       strategy="trend", signal_source="t", confidence=0.7,
                       paper=1, status="closed", pnl=10, instrument="stock",
                       reason="bullish trend pullback")
            s.add(t)
            s.flush()
            tid = t.id
        body = client.get(f"/explain/trade/{tid}").json()
        assert body["trade_id"] == tid
        assert "headline" in body
        assert "outcome" in body and body["outcome"]["pnl"] == 10
