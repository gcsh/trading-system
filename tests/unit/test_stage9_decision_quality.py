"""Stage-9 — abstain & throttle + loss autopsy + cohort matrix.

Pinned behavior:
  • No-trade band: p ∈ [0.50, 0.58] AND high cost → abstain
  • Outside band → no abstain even if cost is high
  • Regime transition flag → size multiplier < 1
  • Cohort below floor → throttle or monitor-only based on severity
  • Loss autopsy: profitable trades return None; losers return a bundle
    with at least one flip_hypothesis evaluated and a tag
  • Cohort matrix: baseline computed; cells include lift; sorts by closed desc
  • Endpoint integration: every Stage-9 route returns a valid JSON shape
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.abstain import (
    AbstainDecision,
    abstain_and_throttle,
    cohort_below_floor,
    cost_exceeds_edge,
    in_no_trade_band,
    is_regime_transition,
)
from backend.bot.autopsy import autopsy_recent_losses, autopsy_trade
from backend.bot.cohort_matrix import build_cohort_matrix, cohort_win_rate


# ── abstain primitives ────────────────────────────────────────────────────


class TestNoTradeBand:
    def test_inside_band(self):
        assert in_no_trade_band(0.52)
        assert in_no_trade_band(0.50)
        assert in_no_trade_band(0.58)
    def test_outside_band(self):
        assert not in_no_trade_band(0.45)
        assert not in_no_trade_band(0.61)
        assert not in_no_trade_band(None)
    def test_custom_range(self):
        assert in_no_trade_band(0.55, lo=0.51, hi=0.59)
        assert not in_no_trade_band(0.50, lo=0.51, hi=0.59)


class TestCostExceedsEdge:
    def test_cost_higher_than_edge_returns_true(self):
        # marginal p, huge cost → no economic edge
        assert cost_exceeds_edge(probability=0.52, expected_move_pct=0.01,
                                    total_cost_bps=200.0)
    def test_strong_edge_passes_even_with_cost(self):
        assert not cost_exceeds_edge(probability=0.80, expected_move_pct=0.05,
                                        total_cost_bps=50.0)
    def test_zero_cost_returns_false(self):
        assert not cost_exceeds_edge(probability=0.52, expected_move_pct=0.05,
                                        total_cost_bps=0.0)


class TestRegimeTransition:
    def test_mixed_regime_label(self):
        assert is_regime_transition("mixed")
        assert is_regime_transition("rally_with_fear")
        assert is_regime_transition("tighten_pressure")
    def test_clean_regime(self):
        assert not is_regime_transition("risk_on_compressed_vol")
        assert not is_regime_transition("risk_off_high_vol")
    def test_explicit_snapshot_flag(self):
        assert is_regime_transition(None, snapshot={"regime_transition": True})


class TestCohortBelowFloor:
    def test_below_floor(self):
        assert cohort_below_floor(cohort_win_rate=0.35, cohort_closed=20)
    def test_above_floor(self):
        assert not cohort_below_floor(cohort_win_rate=0.60, cohort_closed=20)
    def test_insufficient_sample(self):
        assert not cohort_below_floor(cohort_win_rate=0.10, cohort_closed=5)
    def test_none_safe(self):
        assert not cohort_below_floor(cohort_win_rate=None, cohort_closed=50)


# ── combined abstain & throttle ─────────────────────────────────────────


class TestAbstainAndThrottle:
    def test_non_buy_short_circuits(self):
        d = abstain_and_throttle(action="HOLD", probability=0.55,
                                   total_cost_bps=200.0)
        assert not d.abstain and d.size_multiplier == 1.0

    def test_marginal_buy_with_high_cost_abstains(self):
        d = abstain_and_throttle(action="BUY_STOCK", probability=0.55,
                                   expected_move_pct=0.005,
                                   total_cost_bps=300.0)
        assert d.abstain
        assert d.monitor_only
        assert "no_trade_band" in d.triggered_rules

    def test_strong_buy_passes_through(self):
        d = abstain_and_throttle(action="BUY_STOCK", probability=0.85,
                                   expected_move_pct=0.05,
                                   total_cost_bps=10.0,
                                   regime_label="risk_on_compressed_vol",
                                   cohort_win_rate=0.65, cohort_closed=30)
        assert not d.abstain
        assert d.size_multiplier == 1.0
        assert d.triggered_rules == []

    def test_transition_throttles_size(self):
        d = abstain_and_throttle(action="BUY_STOCK", probability=0.80,
                                   regime_label="rally_with_fear",
                                   cohort_win_rate=0.60, cohort_closed=30)
        assert not d.abstain
        assert d.size_multiplier < 1.0
        assert "regime_transition" in d.triggered_rules

    def test_severe_cohort_failure_monitors(self):
        d = abstain_and_throttle(action="BUY_STOCK", probability=0.70,
                                   cohort_win_rate=0.20, cohort_closed=30)
        assert d.abstain
        assert d.monitor_only

    def test_moderate_cohort_failure_throttles(self):
        d = abstain_and_throttle(action="BUY_STOCK", probability=0.70,
                                   cohort_win_rate=0.38, cohort_closed=15)
        # 0.38 is below floor=0.40 but not by 10pp → throttle, not abstain
        assert not d.abstain
        assert d.size_multiplier < 1.0


# ── loss autopsy ─────────────────────────────────────────────────────────


class TestAutopsy:
    def _seed_loss_trade(self, db_session, *, pnl=-100.0, strategy="t",
                            grade="C", win_prob=0.55, slippage_bps=80.0,
                            quantity=10, price=200) -> int:
        from backend.models.decision_log import DecisionLog
        from backend.models.execution_log import ExecutionLog
        from backend.models.trade import Trade
        t = Trade(ticker="NVDA", action="BUY_STOCK", quantity=quantity,
                   price=price, strategy=strategy, signal_source="test",
                   confidence=0.7, paper=1, status="closed", pnl=pnl,
                   instrument="stock", reason="stop-loss hit: -5%")
        db_session.add(t)
        db_session.flush()
        db_session.add(DecisionLog(
            ticker="NVDA", action="BUY_STOCK", strategy=strategy,
            confidence=0.7, status="submitted",
            regime_trend="bullish", regime_volatility="normal",
            regime_gamma="long_gamma", regime_label="risk_on",
            grade=grade, win_probability=win_prob, trade_id=t.id,
            features_json=json.dumps({"composite_bias": 0.4}),
        ))
        db_session.add(ExecutionLog(
            trade_id=t.id, ticker="NVDA", side="BUY",
            quantity=quantity, expected_price=float(price),
            fill_price=float(price) + abs(slippage_bps) * float(price) / 1e4,
            slippage_bps=slippage_bps, is_adverse=True,
        ))
        return int(t.id)

    def test_profitable_trade_returns_none(self, temp_db):
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            t = Trade(ticker="X", action="BUY_STOCK", quantity=1, price=100,
                       strategy="z", signal_source="t", confidence=0.7,
                       paper=1, status="closed", pnl=25.0, instrument="stock")
            s.add(t); s.flush()
            tid = t.id
        assert autopsy_trade(tid) is None

    def test_loss_returns_bundle(self, temp_db):
        from backend.db import session_scope
        with session_scope() as s:
            tid = self._seed_loss_trade(s, slippage_bps=80.0,
                                            grade="C", win_prob=0.55)
        bundle = autopsy_trade(tid)
        assert bundle is not None
        assert bundle.pnl < 0
        assert bundle.flip_hypotheses
        names = [h["name"] for h in bundle.flip_hypotheses]
        for expected in ("event_hold", "abstain_band", "spread_too_wide",
                            "low_grade", "kelly_oversize"):
            assert expected in names

    def test_marginal_loss_tagged_avoidable(self, temp_db):
        from backend.db import session_scope
        with session_scope() as s:
            tid = self._seed_loss_trade(s, slippage_bps=120.0,   # wide spread
                                            grade="C",            # low grade
                                            win_prob=0.55,        # band
                                            quantity=20, price=200)  # oversize
        bundle = autopsy_trade(tid)
        assert bundle.avoidable_tag in ("avoidable", "mixed")

    def test_clean_loss_tagged_variance(self, temp_db):
        from backend.db import session_scope
        with session_scope() as s:
            tid = self._seed_loss_trade(s, slippage_bps=5.0,    # tight
                                            grade="A",           # strong
                                            win_prob=0.75,       # outside band
                                            quantity=2, price=200)  # small
        bundle = autopsy_trade(tid)
        # Single weak signal at most → variance or mixed
        assert bundle.avoidable_tag in ("variance", "mixed")

    def test_unknown_trade_returns_none(self, temp_db):
        assert autopsy_trade(999_999) is None

    def test_batch_autopsy_summary(self, temp_db):
        from backend.db import session_scope
        with session_scope() as s:
            for _ in range(3):
                self._seed_loss_trade(s, slippage_bps=150.0, grade="C",
                                          win_prob=0.55, quantity=20)
        summary = autopsy_recent_losses(limit=10)
        assert summary["n_losses_analyzed"] == 3
        assert sum(summary["by_tag"].values()) == 3


# ── cohort matrix ────────────────────────────────────────────────────────


class TestCohortMatrix:
    def test_empty(self, temp_db):
        result = build_cohort_matrix()
        assert result["cells"] == []
        assert result["baseline"] is None

    def test_baseline_and_lift(self, temp_db):
        """Plant 4 (strategy, regime, grade) cohorts and verify lift maths."""
        import json as _json
        from backend.db import session_scope
        from backend.models.decision_log import DecisionLog
        from backend.models.trade import Trade
        with session_scope() as s:
            # Strategy A in bullish regime, all wins
            for i in range(5):
                t = Trade(ticker=f"T{i}", action="BUY_STOCK", quantity=1,
                           price=100, strategy="A", signal_source="t",
                           confidence=0.7, paper=1, status="closed",
                           pnl=20.0, instrument="stock")
                s.add(t); s.flush()
                s.add(DecisionLog(
                    ticker=f"T{i}", action="BUY_STOCK", strategy="A",
                    confidence=0.7, status="submitted",
                    regime_trend="bullish", regime_volatility="normal",
                    regime_gamma="long_gamma", grade="A",
                    win_probability=0.7, trade_id=t.id,
                ))
            # Strategy B in bearish regime, all losses
            for i in range(5):
                t = Trade(ticker=f"X{i}", action="BUY_STOCK", quantity=1,
                           price=100, strategy="B", signal_source="t",
                           confidence=0.5, paper=1, status="closed",
                           pnl=-10.0, instrument="stock")
                s.add(t); s.flush()
                s.add(DecisionLog(
                    ticker=f"X{i}", action="BUY_STOCK", strategy="B",
                    confidence=0.5, status="submitted",
                    regime_trend="bearish", regime_volatility="normal",
                    regime_gamma="long_gamma", grade="C",
                    win_probability=0.3, trade_id=t.id,
                ))
        result = build_cohort_matrix()
        assert result["baseline"]["win_rate"] == 0.5     # 5 wins / 10 closed
        cells_by_key = {(c["strategy"], c["regime"]): c
                         for c in result["cells"]}
        a_cell = cells_by_key[("A", "bullish")]
        b_cell = cells_by_key[("B", "bearish")]
        assert a_cell["lift"] == 2.0    # 1.0 / 0.5
        assert b_cell["lift"] == 0.0    # 0.0 / 0.5

    def test_rolling_cohort_win_rate(self, temp_db):
        """``cohort_win_rate`` returns (None, 0) when no data."""
        wr, n = cohort_win_rate("nothing", "void")
        assert wr is None and n == 0


# ── live endpoints ─────────────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_abstain_preview(self, client):
        body = client.post("/abstain/preview", json={
            "action": "BUY_STOCK", "probability": 0.55,
            "expected_move_pct": 0.005, "total_cost_bps": 250.0,
            "regime_label": "risk_on",
        }).json()
        assert body["abstain"] is True
        assert "no_trade_band" in body["triggered_rules"]

    def test_cohort_matrix_empty(self, client):
        body = client.get("/cohorts/matrix").json()
        assert body["cells"] == []

    def test_rolling_cohort(self, client):
        body = client.get("/cohorts/rolling/trend/bullish").json()
        assert body["strategy"] == "trend"
        assert body["regime"] == "bullish"

    def test_autopsy_trade_404_for_missing(self, client):
        assert client.get("/autopsy/trade/999999").status_code == 404

    def test_autopsy_recent_empty(self, client):
        body = client.get("/autopsy/recent").json()
        assert body["n_losses_analyzed"] == 0
