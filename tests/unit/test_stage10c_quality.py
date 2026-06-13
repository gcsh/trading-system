"""Stage-10 items 7 / 9 / 13 — beta guardrail + threshold sweep + IV-aware exits.

Pinned behavior:
  • Beta guardrail: only triggers at the right (net_beta × vol) intersections;
    grade floor escalates from None → A → A+
  • IV-aware exits: low risk no change; moderate × 0.70; high × 0.50;
    reasoning explains the trigger
  • Threshold sweep: builds full grid; frontier filters Pareto-dominated
    points; min_trades + max_dd_cap honored; best is the single highest-Sharpe
    frontier point
"""
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.exits.iv_aware import (
    IVAdjustedExit,
    _crush_risk,
    adjust_tp_sl_for_iv_crush,
)
from backend.bot.labeling import TradeLabel
from backend.bot.portfolio_optimizer.beta_guardrail import (
    BetaGuardrailDecision,
    evaluate_beta_guardrail,
)
from backend.bot.sweeps import (
    GRADE_ORDER,
    _eligible,
    sweep_threshold_frontier,
)


# ── beta guardrail ───────────────────────────────────────────────────────


class TestBetaGuardrail:
    def test_clean_no_trigger(self):
        d = evaluate_beta_guardrail(net_beta=0.8, vol_label="compressed")
        assert not d.triggered
        assert d.min_grade_floor is None

    def test_high_beta_spiking_requires_aplus(self):
        d = evaluate_beta_guardrail(net_beta=2.0, vol_label="spiking")
        assert d.triggered
        assert d.min_grade_floor == "A+"
        assert "A+" in d.reason

    def test_moderate_beta_spiking_requires_a(self):
        d = evaluate_beta_guardrail(net_beta=1.2, vol_label="spiking")
        assert d.triggered
        assert d.min_grade_floor == "A"

    def test_high_beta_elevated_requires_a(self):
        d = evaluate_beta_guardrail(net_beta=2.0, vol_label="elevated")
        assert d.triggered
        assert d.min_grade_floor == "A"

    def test_custom_threshold(self):
        # threshold = 1.0; net_beta > 1.0 + spiking → A+
        d = evaluate_beta_guardrail(net_beta=1.1, vol_label="spiking",
                                       beta_threshold=1.0)
        assert d.triggered
        assert d.min_grade_floor == "A+"

    def test_unknown_vol_label_safe(self):
        d = evaluate_beta_guardrail(net_beta=2.0, vol_label="unknown")
        assert not d.triggered


# ── IV-aware exits ──────────────────────────────────────────────────────


class TestCrushRiskClassifier:
    def test_low_risk_default(self):
        assert _crush_risk(None, None, False) == "low"

    def test_moderate_iv_only(self):
        assert _crush_risk(70, None, False) == "moderate"

    def test_moderate_opex_only(self):
        assert _crush_risk(None, None, True) == "moderate"

    def test_high_iv_and_earnings(self):
        # high IV AND earnings within 1 day → high
        assert _crush_risk(80, 0, False) == "high"

    def test_high_earnings_and_opex(self):
        assert _crush_risk(None, 1, True) == "high"


class TestAdjustTPSL:
    def test_no_change_low_risk(self):
        out = adjust_tp_sl_for_iv_crush(
            take_profit_pct=0.10, stop_loss_pct=0.05,
            iv_rank=30, earnings_days=30, opex_week=False,
        )
        assert out.tighten_factor == 1.0
        assert out.take_profit_pct == 0.10
        assert out.stop_loss_pct == 0.05
        assert out.crush_risk == "low"

    def test_moderate_tightens_70pct(self):
        out = adjust_tp_sl_for_iv_crush(
            take_profit_pct=0.10, stop_loss_pct=0.05,
            iv_rank=70, earnings_days=30, opex_week=False,
        )
        assert out.crush_risk == "moderate"
        assert out.tighten_factor == 0.70
        assert out.take_profit_pct == pytest.approx(0.07, abs=1e-6)
        assert out.stop_loss_pct == pytest.approx(0.035, abs=1e-6)

    def test_high_tightens_50pct(self):
        out = adjust_tp_sl_for_iv_crush(
            take_profit_pct=0.10, stop_loss_pct=0.05,
            iv_rank=80, earnings_days=0, opex_week=False,
        )
        assert out.crush_risk == "high"
        assert out.tighten_factor == 0.50
        assert out.take_profit_pct == pytest.approx(0.05, abs=1e-6)

    def test_reasoning_lines_present(self):
        out = adjust_tp_sl_for_iv_crush(
            take_profit_pct=0.10, stop_loss_pct=0.05,
            iv_rank=80, earnings_days=0, opex_week=True,
        )
        joined = " ".join(out.reasoning)
        assert "HIGH" in joined.upper()
        assert "tightening" in joined


# ── threshold sweep ─────────────────────────────────────────────────────


def _make_label(grade: str, win_prob: float, pnl: float, i: int = 0):
    return TradeLabel(
        trade_id=i, timestamp=f"2026-05-{(i % 28) + 1:02d}T10:00:00",
        ticker="X", strategy="s", action="BUY_STOCK", instrument="stock",
        grade=grade, win_probability=win_prob, pnl=pnl,
        win=1 if pnl > 0 else 0,
    )


class TestEligible:
    def test_grade_floor(self):
        label = _make_label("B", 0.6, 10)
        assert _eligible(label, min_grade="C", prob_floor=0.5)
        assert _eligible(label, min_grade="B", prob_floor=0.5)
        assert not _eligible(label, min_grade="A", prob_floor=0.5)

    def test_prob_floor(self):
        label = _make_label("A", 0.55, 10)
        assert _eligible(label, min_grade="C", prob_floor=0.50)
        assert not _eligible(label, min_grade="C", prob_floor=0.60)


class TestSweepFrontier:
    def test_empty_returns_no_frontier(self):
        result = sweep_threshold_frontier([])
        assert result.frontier == []
        assert result.best is None

    def test_synthetic_strong_a_cohort_picked(self):
        """A-grade cohort with 100% win rate should dominate the frontier."""
        labels: list = []
        # 30 A trades at p=0.7 — all wins
        for i in range(30):
            labels.append(_make_label("A", 0.70, 25.0, i))
        # 30 C trades at p=0.45 — all losses
        for i in range(30, 60):
            labels.append(_make_label("C", 0.45, -10.0, i))
        result = sweep_threshold_frontier(
            labels, max_dd_cap_pct=0.50, min_trades=10,
        )
        assert result.best is not None
        assert result.best["min_grade"] in ("A+", "A")
        # The suggested diff includes the best point
        assert "analytics.min_grade" in result.suggested_config_diff

    def test_min_trades_enforced(self):
        """No frontier when no point clears min_trades."""
        labels = [_make_label("A", 0.7, 10, i) for i in range(5)]
        result = sweep_threshold_frontier(
            labels, max_dd_cap_pct=0.50, min_trades=10,
        )
        assert result.best is None
        assert "no frontier point" in result.notes[0]

    def test_drawdown_cap_excludes_volatile(self):
        """Big losses pushed in series → max DD blows past cap, no acceptance."""
        labels: list = []
        for i in range(40):
            # alternate small wins + huge losses → big drawdowns
            pnl = 5.0 if i % 2 == 0 else -50.0
            labels.append(_make_label("A", 0.70, pnl, i))
        result = sweep_threshold_frontier(
            labels, max_dd_cap_pct=0.05, min_trades=10,
        )
        # max DD will be way above 5% → no accepted point
        assert result.best is None


# ── live API integration ────────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_beta_guardrail_preview(self, client):
        body = client.get(
            "/portfolio/beta-guardrail/preview?net_beta=2.0&vol_label=spiking"
        ).json()
        assert body["triggered"]
        assert body["min_grade_floor"] == "A+"

    def test_beta_guardrail_live(self, client):
        body = client.get("/portfolio/beta-guardrail/live").json()
        for key in ("net_beta", "vol_label", "decision"):
            assert key in body

    def test_iv_exit_preview_low(self, client):
        body = client.post("/exits/iv-aware/preview", json={
            "take_profit_pct": 0.10, "stop_loss_pct": 0.05,
            "iv_rank": 30, "earnings_days": 30, "opex_week": False,
        }).json()
        assert body["tighten_factor"] == 1.0
        assert body["crush_risk"] == "low"

    def test_iv_exit_preview_high(self, client):
        body = client.post("/exits/iv-aware/preview", json={
            "take_profit_pct": 0.10, "stop_loss_pct": 0.05,
            "iv_rank": 80, "earnings_days": 0, "opex_week": False,
        }).json()
        assert body["tighten_factor"] == 0.50
        assert body["crush_risk"] == "high"

    def test_sweep_frontier_get(self, client):
        body = client.get("/sweeps/frontier").json()
        assert "grid" in body
        assert "frontier" in body
        assert "suggested_config_diff" in body

    def test_sweep_frontier_post_with_payload(self, client):
        labels = []
        for i in range(20):
            labels.append({
                "trade_id": i, "timestamp": f"2026-05-{(i % 28) + 1:02d}T10:00:00",
                "ticker": "X", "strategy": "s", "action": "BUY_STOCK",
                "instrument": "stock", "grade": "A",
                "win_probability": 0.7, "pnl": 25.0, "win": 1,
            })
        body = client.post("/sweeps/frontier", json={
            "labels": labels, "max_dd_cap_pct": 0.5, "min_trades": 5,
        }).json()
        assert body["best"] is not None
