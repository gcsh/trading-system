"""Stage-17 — Trade Journal Intelligence + Promotion Readiness."""
import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.journal import (
    Lesson,
    applicable_lessons,
    build_lessons,
    trade_size_multiplier,
)


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def _seed_trades(*, n_wins, n_losses, strategy="opening_range_breakout",
                    regime="bullish", volatility="normal",
                    gamma="long_gamma", earnings_days=None, iv_rank=None,
                    win_pnl=100.0, loss_pnl=-50.0, vix=None,
                    ts_start_offset_min=0):
    from backend.db import session_scope
    from backend.models.decision_log import DecisionLog
    from backend.models.trade import Trade
    detail = {
        "analytics": {
            "regime": {"trend": regime, "volatility": volatility,
                          "gamma": gamma},
            "features": {
                **({"earnings_days": earnings_days} if earnings_days is not None else {}),
                **({"iv_rank": iv_rank} if iv_rank is not None else {}),
                **({"vix": vix} if vix is not None else {}),
            },
        },
    }
    with session_scope() as s:
        for i in range(n_wins):
            t = Trade(ticker="X", action="BUY_STOCK", quantity=1, price=100,
                        strategy=strategy, signal_source="t",
                        confidence=0.7, paper=1, status="closed",
                        instrument="stock", pnl=win_pnl,
                        detail_json=json.dumps(detail))
            t.timestamp = datetime.utcnow() + timedelta(minutes=ts_start_offset_min + i)
            s.add(t); s.flush()
            s.add(DecisionLog(
                ticker="X", action="BUY_STOCK", strategy=strategy,
                confidence=0.7, status="submitted",
                regime_trend=regime, regime_volatility=volatility,
                regime_gamma=gamma, regime_label=f"{regime}",
                grade="A", win_probability=0.6,
                trade_id=t.id, outcome_pnl=win_pnl, outcome_status="closed",
            ))
        for i in range(n_losses):
            t = Trade(ticker="X", action="BUY_STOCK", quantity=1, price=100,
                        strategy=strategy, signal_source="t",
                        confidence=0.7, paper=1, status="closed",
                        instrument="stock", pnl=loss_pnl,
                        detail_json=json.dumps(detail))
            t.timestamp = datetime.utcnow() + timedelta(
                minutes=ts_start_offset_min + n_wins + i)
            s.add(t); s.flush()
            s.add(DecisionLog(
                ticker="X", action="BUY_STOCK", strategy=strategy,
                confidence=0.7, status="submitted",
                regime_trend=regime, regime_volatility=volatility,
                regime_gamma=gamma, regime_label=f"{regime}",
                grade="C", win_probability=0.5,
                trade_id=t.id, outcome_pnl=loss_pnl, outcome_status="closed",
            ))


# ── build_lessons ───────────────────────────────────────────────────────


class TestBuildLessons:
    def test_empty_corpus(self, temp_db):
        report = build_lessons()
        assert report.lessons == []
        assert report.total_closed_trades == 0

    def test_bucket_with_low_win_rate_emits_lesson(self, temp_db):
        # Global baseline: 20 wins / 30 trades = 67%
        _seed_trades(n_wins=20, n_losses=10, strategy="trend_pullback",
                        regime="bullish")
        # Lesson bucket: 3 wins / 12 trades in choppy regime = 25%, well below 67%
        _seed_trades(n_wins=3, n_losses=9,
                        strategy="opening_range_breakout", regime="choppy",
                        ts_start_offset_min=1000)
        report = build_lessons(min_samples=8, delta_threshold=0.10)
        assert len(report.lessons) >= 1
        # The losing-bucket lesson should appear with reduce / abstain action
        bad = [l for l in report.lessons
                if "opening_range_breakout" in l.pattern and "choppy" in l.pattern]
        assert bad, f"missing expected lesson in {[l.pattern for l in report.lessons]}"
        assert bad[0].size_multiplier <= 1.0
        assert bad[0].severity in ("warn", "alert")
        assert bad[0].sample_size == 12

    def test_bucket_with_high_win_rate_suggests_boost(self, temp_db):
        # Baseline: 5 wins / 30 trades = 17% (very low)
        _seed_trades(n_wins=5, n_losses=25, strategy="x", regime="bullish")
        # Hot bucket: 12 wins / 12 trades = 100% — way above baseline
        _seed_trades(n_wins=12, n_losses=0, strategy="macd_momentum",
                        regime="bullish", ts_start_offset_min=1000)
        report = build_lessons(min_samples=8, delta_threshold=0.10)
        boosts = [l for l in report.lessons if l.size_multiplier > 1.0]
        assert boosts, f"missing boost in {[(l.pattern, l.size_multiplier) for l in report.lessons]}"

    def test_min_samples_filters_noise(self, temp_db):
        # Only 5 trades in the bucket — below default min_samples=8
        _seed_trades(n_wins=1, n_losses=4, strategy="x", regime="bullish")
        report = build_lessons(min_samples=8)
        assert report.lessons == []        # nothing surfaced

    def test_lesson_carries_wilson_bounds(self, temp_db):
        _seed_trades(n_wins=3, n_losses=15, strategy="x", regime="bullish")
        report = build_lessons(min_samples=8, delta_threshold=0.0)
        l = report.lessons[0]
        assert 0.0 <= l.confidence_bound_lo <= l.win_rate <= l.confidence_bound_hi <= 1.0

    def test_lesson_carries_expectancy_r(self, temp_db):
        _seed_trades(n_wins=3, n_losses=10, strategy="x",
                        win_pnl=100, loss_pnl=-50)
        report = build_lessons(min_samples=8, delta_threshold=0.0)
        # avg_loss = -50, expectancy = (3*100 - 10*50)/13 ≈ -15.4
        # expectancy_r = -15.4 / 50 ≈ -0.31
        l = report.lessons[0]
        assert l.expectancy_r is not None and l.expectancy_r < 0


# ── applicable_lessons + trade_size_multiplier ─────────────────────────


class TestApplicable:
    def test_picks_matching_strategy_regime(self, temp_db):
        _seed_trades(n_wins=20, n_losses=10, strategy="trend_pullback",
                        regime="bullish")
        _seed_trades(n_wins=2, n_losses=12,
                        strategy="opening_range_breakout", regime="choppy",
                        ts_start_offset_min=1000)
        matches = applicable_lessons(
            strategy="opening_range_breakout", regime_trend="choppy",
            volatility="normal", gamma="long_gamma",
        )
        # Should match the choppy lesson
        assert any("choppy" in m.pattern for m in matches)

    def test_size_multiplier_picks_most_penalizing(self, temp_db):
        _seed_trades(n_wins=20, n_losses=10, strategy="trend_pullback",
                        regime="bullish")
        _seed_trades(n_wins=2, n_losses=12,
                        strategy="opening_range_breakout", regime="choppy",
                        ts_start_offset_min=1000)
        mult, matches = trade_size_multiplier(
            strategy="opening_range_breakout", regime_trend="choppy",
            volatility="normal", gamma="long_gamma",
        )
        assert mult <= 1.0

    def test_unknown_context_returns_default_multiplier(self, temp_db):
        mult, matches = trade_size_multiplier(
            strategy="nonexistent", regime_trend="bullish",
            volatility="normal", gamma="long_gamma",
        )
        assert mult == 1.0
        assert matches == []


# ── endpoints ──────────────────────────────────────────────────────────


class TestJournalEndpoints:
    def test_lessons_endpoint_returns_report(self, client):
        body = client.get("/journal/lessons").json()
        assert "lessons" in body and "baseline_win_rate" in body

    def test_applicable_endpoint(self, client):
        body = client.get("/journal/applicable?strategy=trend_pullback"
                            "&regime_trend=bullish").json()
        assert "matches" in body


class TestTrialReadiness:
    def test_empty_system_need_more_data(self, client):
        body = client.get("/trial/readiness?min_trades=100").json()
        assert body["verdict"]["status"] == "need_more_data"
        assert body["verdict"]["trades_to_go"] == 100
        assert body["progress"]["sample_size"]["current"] == 0
        assert "trial" in body and "gates" in body

    def test_progress_pct_clamped(self, client):
        body = client.get("/trial/readiness?min_trades=10&target_trades=100").json()
        assert 0.0 <= body["progress"]["sample_size"]["min_pct"] <= 1.0
        assert 0.0 <= body["progress"]["sample_size"]["target_pct"] <= 1.0
