"""Stage-11.8 Calibration Stability — rolling-window Brier/ECE std.

Pinned:
  • compute_stability returns std=None when fewer than min_windows complete
  • Perfectly stable labels → near-zero std for both Brier + ECE
  • Heterogeneous labels (good + bad windows) → non-trivial std
  • stability_summary plumbs the four scalars used by the gates
  • The two new gates appear in /gates/catalog and /gates/status
  • Gates report insufficient_data when not enough closed trades
  • Gates promote to pass when std stays under threshold
  • /gates/stability surfaces per-window detail
"""
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.gates import CATALOG, evaluate_gates
from backend.bot.gates.calibration_stability import (
    StabilityReport,
    compute_stability,
    stability_summary,
)
from backend.bot.labeling import TradeLabel


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def _label(*, prob, win, ts_offset_min=0, trade_id=1):
    return TradeLabel(
        trade_id=trade_id,
        timestamp=(datetime(2026, 5, 1) + timedelta(minutes=ts_offset_min)).isoformat(),
        ticker="NVDA", strategy="trend_pullback", action="BUY_CALL",
        instrument="option", regime_trend="bullish",
        regime_volatility="normal", regime_gamma="long_gamma",
        grade="A", confidence=0.7, win_probability=prob,
        pnl=100.0 if win else -50.0,
        pnl_pct=0.05 if win else -0.025,
        win=(1 if win else 0),
        exit_reason="manual",
    )


def _stable_labels(n=120, prob=0.6):
    """A stream where probability=0.6 and ~60% of outcomes are wins —
    Brier + ECE should be steady across windows."""
    import random
    random.seed(7)
    out = []
    for i in range(n):
        # ~60% wins so calibration is roughly correct in every window.
        out.append(_label(prob=prob, win=(random.random() < 0.6),
                            ts_offset_min=i, trade_id=i + 1))
    return out


def _unstable_labels(n=120):
    """First half: prob=0.7 with 70% wins (well-calibrated).
    Second half: prob=0.7 but only 20% wins (miscalibrated). Big ECE drift."""
    import random
    random.seed(13)
    out = []
    for i in range(n):
        if i < n // 2:
            win = random.random() < 0.7
        else:
            win = random.random() < 0.2
        out.append(_label(prob=0.7, win=win, ts_offset_min=i, trade_id=i + 1))
    return out


# ── pure helper ─────────────────────────────────────────────────────────


class TestComputeStability:
    def test_insufficient_data_returns_none(self):
        rpt = compute_stability(_stable_labels(n=20), window_size=30)
        assert isinstance(rpt, StabilityReport)
        assert rpt.n_windows == 0
        # No windows → no stats
        assert rpt.brier_std is None
        assert rpt.ece_std is None

    def test_min_windows_required(self):
        # 60 labels → 2 windows of 30 → below min_windows=3 → std=None
        rpt = compute_stability(_stable_labels(n=60), window_size=30,
                                  min_windows=3)
        assert rpt.n_windows == 2
        assert rpt.brier_std is None

    def test_stable_labels_low_std(self):
        rpt = compute_stability(_stable_labels(n=120), window_size=30)
        assert rpt.n_windows == 4
        assert rpt.brier_std is not None
        # Stable: should be well under 0.05
        assert rpt.brier_std < 0.05
        assert rpt.ece_std is not None
        assert rpt.ece_std < 0.10
        # Means populated
        assert rpt.brier_mean is not None
        assert rpt.ece_mean is not None

    def test_unstable_labels_high_std(self):
        rpt = compute_stability(_unstable_labels(n=120), window_size=30)
        # First half well-calibrated, second half not → wider spread
        assert rpt.ece_std > 0.05

    def test_skips_labels_without_prob_or_win(self):
        labels = _stable_labels(n=120)
        # Wipe out a third so they can't contribute
        for l in labels[::3]:
            l.win_probability = None
        rpt = compute_stability(labels, window_size=30)
        # ~80 calibrated labels → 2 windows
        assert rpt.n_windows == 2


# ── summary shape ───────────────────────────────────────────────────────


class TestStabilitySummary:
    def test_summary_keys(self):
        labels = _stable_labels(n=120)
        out = stability_summary(labels)
        for k in ("brier_stability_std", "calibration_error_stability_std",
                    "stability_n_windows", "stability_window_size",
                    "brier_stability_mean", "calibration_error_stability_mean"):
            assert k in out
        assert out["stability_n_windows"] == 4


# ── gate registration + evaluation ──────────────────────────────────────


class TestGateRegistration:
    def test_new_gates_in_catalog(self):
        names = {g.name for g in CATALOG}
        assert "brier_stability_ok" in names
        assert "calibration_error_stability_ok" in names

    def test_gates_insufficient_data_path(self):
        # No data → both new gates should report insufficient_data
        summary = {"data": {}, "label_quality": {"closed": 0}}
        result = evaluate_gates(summary)
        for gate in result["gates"]:
            if "stability" in gate["name"]:
                assert gate["verdict"] == "insufficient_data"

    def test_gates_pass_when_under_threshold(self):
        # Inject favorable scalars + enough sample
        summary = {
            "data": {
                "brier_stability_std": 0.02,
                "calibration_error_stability_std": 0.015,
            },
            "label_quality": {"closed": 200},
        }
        verdicts = {g["name"]: g["verdict"] for g in evaluate_gates(summary)["gates"]}
        assert verdicts["brier_stability_ok"] == "pass"
        assert verdicts["calibration_error_stability_ok"] == "pass"

    def test_gates_fail_when_over_threshold(self):
        summary = {
            "data": {
                "brier_stability_std": 0.12,
                "calibration_error_stability_std": 0.09,
            },
            "label_quality": {"closed": 200},
        }
        verdicts = {g["name"]: g["verdict"] for g in evaluate_gates(summary)["gates"]}
        assert verdicts["brier_stability_ok"] == "fail"
        assert verdicts["calibration_error_stability_ok"] == "fail"


# ── endpoints ───────────────────────────────────────────────────────────


class TestStabilityEndpoints:
    def test_gates_catalog_lists_stability_gates(self, client):
        body = client.get("/gates/catalog").json()
        names = {g["name"] for g in body["gates"]}
        assert "brier_stability_ok" in names
        assert "calibration_error_stability_ok" in names

    def test_gates_status_includes_stability(self, client):
        body = client.get("/gates/status").json()
        names = {g["name"] for g in body["gates"]}
        assert "brier_stability_ok" in names
        # Cold start → insufficient_data
        for g in body["gates"]:
            if "stability" in g["name"]:
                assert g["verdict"] == "insufficient_data"

    def test_stability_endpoint_returns_windows(self, client):
        body = client.get("/gates/stability?window_size=30").json()
        # No closed trades yet → 0 windows but scalars present
        assert "n_windows" in body and body["n_windows"] == 0
        assert body["window_size"] == 30
        assert body["closed_labels"] == 0
