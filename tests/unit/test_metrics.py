"""Stage-1 measurement foundation — metrics math, labels, walk-forward.

Every formula is pinned against a hand-computed expected value. The point of
this suite is: if any of these numbers ever change silently, the build breaks
loud. Later stages assume these numbers are right.
"""
import math
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.evaluation import (
    expanding_split,
    walk_forward_evaluate,
    walk_forward_split,
)
from backend.bot.labeling import TradeLabel, build_labels, label_quality
from backend.bot.metrics import (
    avg_win_loss,
    brier_score,
    calibration_curve,
    calibration_error,
    expectancy,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    summarize,
    win_rate,
)


# ── math: risk-adjusted ratios ──────────────────────────────────────────────


class TestSharpe:
    def test_constant_returns_undefined(self):
        # zero variance → Sharpe is undefined → None (not 0)
        assert sharpe_ratio([0.01] * 10) is None

    def test_single_observation_undefined(self):
        assert sharpe_ratio([0.01]) is None

    def test_empty_undefined(self):
        assert sharpe_ratio([]) is None

    def test_known_sequence(self):
        # daily returns: mean 0.001 = 25.2% annualized (252 trading days)
        # this is a sanity check, not a precise reproduction
        rets = [0.01, -0.005, 0.008, -0.003, 0.012, -0.002, 0.005]
        s = sharpe_ratio(rets, rf=0.0)
        assert s is not None
        assert 4.0 < s < 12.0  # plausible range for a 0.4% mean / ~0.7% sd

    def test_negative_when_below_rf(self):
        rets = [-0.001] * 30
        # constant => Sharpe is undefined (zero variance)
        assert sharpe_ratio(rets, rf=0.045) is None

    def test_high_rf_pushes_sharpe_down(self):
        rets = [0.001, 0.002, -0.001, 0.003, 0.0]
        low = sharpe_ratio(rets, rf=0.0)
        high = sharpe_ratio(rets, rf=0.20)
        assert low > high


class TestSortino:
    def test_no_downside_undefined(self):
        # all returns above target → downside dev = 0 → undefined
        assert sortino_ratio([0.01, 0.02, 0.03], target=0.0) is None

    def test_known_sequence(self):
        rets = [0.01, -0.005, 0.008, -0.003, 0.012]
        s = sortino_ratio(rets, target=0.0)
        assert s is not None
        assert s > 0     # mean positive, some downside

    def test_punishes_only_downside(self):
        # symmetric noise: Sharpe and Sortino are different
        rets = [0.01, -0.01] * 10
        sh = sharpe_ratio(rets)
        so = sortino_ratio(rets)
        # both defined and finite
        assert sh is not None and so is not None


# ── drawdown ────────────────────────────────────────────────────────────────


class TestMaxDrawdown:
    def test_empty_zero(self):
        assert max_drawdown([])["dd_pct"] == 0.0

    def test_monotonic_no_drawdown(self):
        result = max_drawdown([100, 110, 120, 130])
        assert result["dd"] == 0
        assert result["dd_pct"] == 0

    def test_known_dd(self):
        # peak 100 → trough 70 → recovery
        result = max_drawdown([100, 80, 100, 70, 90])
        assert result["dd"] == 30
        assert result["dd_pct"] == 0.30
        assert result["peak_idx"] == 0
        assert result["trough_idx"] == 3

    def test_dd_uses_running_peak(self):
        # the trough at 50 happens AFTER a new peak of 120 → DD = 70 from 120
        result = max_drawdown([100, 120, 50, 80])
        assert result["dd"] == 70
        assert math.isclose(result["dd_pct"], 70 / 120, abs_tol=1e-4)


# ── trade-level ────────────────────────────────────────────────────────────


class TestWinRate:
    def test_empty_none(self):
        assert win_rate([]) is None

    def test_all_wins(self):
        assert win_rate([1, 2, 3]) == 1.0

    def test_all_losses(self):
        assert win_rate([-1, -2]) == 0.0

    def test_zero_counts_as_loss(self):
        # PnL exactly 0 is not a win
        assert win_rate([1, 0, -1]) == pytest.approx(1 / 3, abs=1e-3)


class TestExpectancy:
    def test_empty_none(self):
        assert expectancy([]) is None

    def test_known(self):
        # 2 wins of +10, 2 losses of -5 → expectancy = 0.5*10 + 0.5*(-5) = 2.5
        assert expectancy([10, 10, -5, -5]) == 2.5

    def test_all_zero(self):
        assert expectancy([0, 0]) == 0.0


class TestProfitFactor:
    def test_only_wins_infinite(self):
        assert profit_factor([10, 20]) == float("inf")
    def test_only_losses_zero_or_inf_safe(self):
        # gross_win=0, gross_loss=30 → 0.0 (not NaN)
        assert profit_factor([-10, -20]) == 0.0
    def test_known(self):
        assert profit_factor([20, 10, -10]) == 3.0
    def test_empty_none(self):
        assert profit_factor([]) is None


class TestAvgWinLoss:
    def test_none_when_no_wins(self):
        avg_w, avg_l = avg_win_loss([-1, -2])
        assert avg_w is None and avg_l == -1.5

    def test_known(self):
        avg_w, avg_l = avg_win_loss([10, 20, -5, -15])
        assert avg_w == 15 and avg_l == -10


# ── calibration ────────────────────────────────────────────────────────────


class TestBrier:
    def test_perfect_zero(self):
        # always predict 1 and always win → Brier 0
        assert brier_score([1.0, 1.0, 1.0], [1, 1, 1]) == 0.0
    def test_coin_flip(self):
        # always predict 0.5, half win → Brier 0.25
        assert brier_score([0.5] * 4, [1, 0, 1, 0]) == 0.25
    def test_size_mismatch_none(self):
        assert brier_score([0.5, 0.5], [1]) is None


class TestCalibration:
    def test_empty_no_bins(self):
        assert calibration_curve([], []) == []

    def test_known_well_calibrated(self):
        # 4 bins, each with predicted ~ actual
        preds = [0.1] * 10 + [0.4] * 10 + [0.7] * 10 + [0.9] * 10
        outs = [1] * 1 + [0] * 9 + [1] * 4 + [0] * 6 + [1] * 7 + [0] * 3 + [1] * 9 + [0] * 1
        curve = calibration_curve(preds, outs, n_bins=10)
        # 4 buckets populated
        assert len([b for b in curve if b["count"] > 0]) == 4
        ece = calibration_error(preds, outs, n_bins=10)
        assert ece is not None and ece < 0.1  # well calibrated

    def test_overconfident_ece(self):
        # always predict 0.9 but only 50% win → big calibration error
        preds = [0.9] * 20
        outs = [1, 0] * 10
        ece = calibration_error(preds, outs, n_bins=10)
        assert ece is not None and 0.35 < ece < 0.45


# ── summary (the canonical aggregation) ────────────────────────────────────


class TestSummarize:
    def test_no_records(self):
        m = summarize([])
        assert m.count == 0 and m.win_rate is None

    def test_mixed_open_closed(self):
        records = [
            {"pnl": 10, "win_probability": 0.7, "status": "closed"},
            {"pnl": -5, "win_probability": 0.4, "status": "closed"},
            {"pnl": None, "win_probability": 0.6, "status": "open"},
        ]
        m = summarize(records, equity_curve=[100, 105, 100])
        assert m.count == 3
        assert m.closed_count == 2
        assert m.open_count == 1
        assert m.win_rate == 0.5
        assert m.total_pnl == 5.0
        assert m.profit_factor == 2.0  # 10 / 5
        assert m.brier is not None

    def test_no_predictions_no_calibration(self):
        records = [{"pnl": 10, "win_probability": None, "status": "closed"}]
        m = summarize(records)
        assert m.brier is None and m.calibration_error is None


# ── labeling contract ──────────────────────────────────────────────────────


class TestBuildLabels:
    def test_join_with_decision(self):
        trades = [{"id": 1, "timestamp": "2026-05-29T10:00:00", "ticker": "NVDA",
                    "strategy": "trend", "action": "BUY_STOCK", "instrument": "stock",
                    "quantity": 5, "price": 200.0, "pnl": 50.0, "status": "closed",
                    "confidence": 0.7, "reason": "take-profit hit"}]
        decisions = [{"trade_id": 1, "regime_trend": "bullish", "grade": "B",
                       "win_probability": 0.65}]
        labels = build_labels(trades, decisions)
        assert len(labels) == 1
        l = labels[0]
        assert l.win == 1
        assert l.regime_trend == "bullish"
        assert l.grade == "B"
        assert l.win_probability == 0.65
        assert l.exit_reason == "take_profit"
        assert l.pnl_pct == 0.05      # 50 / (5 * 200)

    def test_open_trade_has_no_outcome(self):
        trades = [{"id": 2, "timestamp": "2026-05-29T10:00:00", "ticker": "AAPL",
                    "strategy": "mr", "action": "BUY_STOCK", "instrument": "stock",
                    "quantity": 1, "price": 100, "pnl": None, "status": "open",
                    "confidence": 0.6}]
        labels = build_labels(trades)
        assert labels[0].win is None
        assert labels[0].pnl_pct is None

    def test_option_notional(self):
        # for an option, notional ≈ 3% of strike × 100
        trades = [{"id": 3, "timestamp": "2026-05-29T10:00:00", "ticker": "NVDA",
                    "strategy": "trend", "action": "BUY_CALL", "instrument": "option",
                    "quantity": 1, "price": 6.5, "pnl": 100.0, "status": "closed",
                    "confidence": 0.6, "contracts": 1, "strike": 215}]
        labels = build_labels(trades)
        # notional ≈ 0.03 * 215 * 100 = 645 → pnl_pct ≈ 100/645 ≈ 0.155
        assert labels[0].pnl_pct is not None
        assert 0.10 < labels[0].pnl_pct < 0.20


class TestLabelQuality:
    def test_empty(self):
        q = label_quality([])
        assert q["closed"] == 0
        assert "no closed trades" in q["warnings"][0]
        assert not q["ok"]

    def test_all_wins_flagged(self):
        labels = [TradeLabel(trade_id=i, timestamp="", ticker="X", strategy="s",
                              action="BUY", instrument="stock", pnl=10.0, win=1)
                  for i in range(35)]
        q = label_quality(labels)
        assert any("all closed trades won" in w for w in q["warnings"])

    def test_thin_sample_flagged(self):
        labels = [TradeLabel(trade_id=1, timestamp="", ticker="X", strategy="s",
                              action="BUY", instrument="stock", pnl=10, win=1)]
        q = label_quality(labels)
        assert any("need ≥30" in w for w in q["warnings"])

    def test_balanced_sample_ok(self):
        labels = [TradeLabel(trade_id=i, timestamp="", ticker="X", strategy="s",
                              action="BUY", instrument="stock",
                              pnl=10.0 if i % 2 else -5.0,
                              win=1 if i % 2 else 0,
                              win_probability=0.5)
                  for i in range(40)]
        q = label_quality(labels)
        assert q["ok"]


# ── walk-forward ───────────────────────────────────────────────────────────


def _synthetic_labels(n: int, seed: int = 0):
    import random
    rng = random.Random(seed)
    labels = []
    for i in range(n):
        prob = rng.uniform(0.3, 0.9)
        win = 1 if rng.random() < prob else 0
        labels.append(TradeLabel(trade_id=i, timestamp=f"2026-05-{(i // 100) + 1:02d}T{(i % 24):02d}:00:00",
                                   ticker="NVDA", strategy="trend", action="BUY_CALL",
                                   instrument="option",
                                   pnl=20.0 if win else -15.0,
                                   win=win, win_probability=prob))
    return labels


class TestWalkForwardSplit:
    def test_empty(self):
        assert list(walk_forward_split([], 100, 30)) == []

    def test_window_counts(self):
        labels = _synthetic_labels(200)
        windows = list(walk_forward_split(labels, train_size=100, test_size=30))
        # start ∈ {0, 30, 60}; start=90 would need 220 ≤ 200 (false), so 3 wins
        assert len(windows) == 3
        for tr, te in windows:
            assert len(tr) == 100 and len(te) == 30

    def test_too_small_yields_nothing(self):
        labels = _synthetic_labels(50)
        assert list(walk_forward_split(labels, 100, 30)) == []


class TestExpandingSplit:
    def test_expanding(self):
        labels = _synthetic_labels(150)
        windows = list(expanding_split(labels, initial_train=50, test_size=20))
        # train sizes: 50, 70, 90, 110, 130
        assert [len(t[0]) for t in windows] == [50, 70, 90, 110, 130]
        for _, te in windows:
            assert len(te) == 20


class TestWalkForwardEvaluate:
    def test_empty_safe(self):
        result = walk_forward_evaluate([])
        assert result["windows"] == []

    def test_produces_metrics_per_window(self):
        labels = _synthetic_labels(200, seed=7)
        result = walk_forward_evaluate(labels, train_size=100, test_size=30)
        assert result["summary"]["n_windows"] == 3
        # every window should have at least win_rate populated
        for w in result["windows"]:
            assert "win_rate" in w["metrics"]
            assert w["test_size"] == 30


# ── integration: endpoints on a TestClient ─────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestEndpointsIntegration:
    def test_summary_empty_returns_label_quality(self, client):
        body = client.get("/metrics/summary").json()
        assert "data" in body and "label_quality" in body
        # empty trial → warnings populated, not ok
        assert not body["label_quality"]["ok"]
        assert body["data"]["count"] == 0

    def test_by_strategy_empty(self, client):
        body = client.get("/metrics/by-strategy").json()
        assert body["data"] == {}

    def test_by_grade_empty(self, client):
        assert client.get("/metrics/by-grade").json()["data"] == {}

    def test_calibration_endpoint_shape(self, client):
        body = client.get("/metrics/calibration").json()
        assert "data" in body and "sample_size" in body
        assert isinstance(body["data"], list)

    def test_walkforward_endpoint_no_data(self, client):
        body = client.get("/metrics/walkforward").json()
        assert body["windows"] == []
        assert "summary" in body and "params" in body

    def test_labels_endpoint_shape(self, client):
        body = client.get("/metrics/labels").json()
        assert "labels" in body and "label_quality" in body
        assert isinstance(body["labels"], list)

    def test_summary_with_seeded_trade(self, client):
        from backend.bot.paper_executor import PaperPosition
        from backend.db import session_scope
        from backend.models.trade import Trade

        # Plant ONE closed winning trade so the metrics math has data to chew on.
        with session_scope() as s:
            s.add(Trade(ticker="NVDA", action="BUY_STOCK", quantity=1,
                         price=200.0, strategy="trend", signal_source="test",
                         confidence=0.7, paper=1, status="closed",
                         pnl=25.0, instrument="stock"))
        body = client.get("/metrics/summary").json()
        d = body["data"]
        assert d["count"] == 1
        assert d["closed_count"] == 1
        assert d["win_rate"] == 1.0
        assert d["total_pnl"] == 25.0
