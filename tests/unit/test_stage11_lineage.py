"""Stage-11.2 Decision Lineage — build_lineage + endpoint.

Pinned:
  • Returns None for unknown trade_id
  • Surfaces every stage that has data in detail_json
  • Falls back to DecisionLog for regime/grade/probability/features when
    detail_json lacks an analytics block (legacy rows)
  • Cohort + autopsy keys are present (may be None when cohort empty
    or trade is open / never closed)
  • GET /lineage/trade/{id} → 404 / 200
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.lineage import build_lineage


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def _make_trade(detail: dict | None = None, **overrides) -> int:
    from backend.db import session_scope
    from backend.models.trade import Trade

    defaults = dict(
        ticker="NVDA", action="BUY_CALL", quantity=1, price=215,
        strategy="trend_pullback", signal_source="t",
        confidence=0.72, paper=1, status="open", instrument="option",
        option_type="call", strike=215, expiration="2026-06-21",
        stop_loss_price=204.25, take_profit_price=236.50,
    )
    defaults.update(overrides)
    with session_scope() as s:
        t = Trade(detail_json=json.dumps(detail) if detail else None, **defaults)
        s.add(t); s.flush()
        return t.id


# ── pure helpers ────────────────────────────────────────────────────────


class TestBuildLineage:
    def test_returns_none_for_unknown(self, temp_db):
        assert build_lineage(999999) is None

    def test_minimal_trade_returns_stages_scaffold(self, temp_db):
        tid = _make_trade()
        payload = build_lineage(tid)
        assert payload["trade_id"] == tid
        assert payload["ticker"] == "NVDA"
        assert payload["action"] == "BUY_CALL"
        # Every stage is in the dict, even if empty / None.
        for key in ("signal", "snapshot", "regime", "features", "confluence",
                     "probability", "rank", "abstain", "min_grade_tightened",
                     "meta_ai", "portfolio_risk", "risk", "audit",
                     "execution", "outcome", "autopsy", "cohort", "memo"):
            assert key in payload["stages"]
        # Signal is populated from the Trade row even with no detail.
        assert payload["stages"]["signal"]["strategy"] == "trend_pullback"
        assert payload["stages"]["signal"]["action"] == "BUY_CALL"
        # Execution is always at least the fill record.
        assert payload["stages"]["execution"]["fill_price"] == 215
        assert payload["stages"]["execution"]["instrument"] == "option"

    def test_full_detail_surfaces_every_stage(self, temp_db):
        detail = {
            "signal_reason": "5% pullback in uptrend",
            "confidence": 0.72,
            "stop_loss_pct": 5.0,
            "take_profit_pct": 10.0,
            "snapshot": {"price": 215, "rsi": 35, "macd": 0.4},
            "memo": {"thesis": "AI infra demand strong; NVDA flow bullish",
                       "confidence": "high", "source": "heuristic",
                       "schema_version": 1},
            "analytics": {
                "regime": {"trend": "bullish", "volatility": "normal",
                             "gamma": "long_gamma",
                             "label": "bullish · normal-vol · long gamma"},
                "rank": {"grade": "A", "score": 0.78,
                          "reasoning": ["high prob", "regime aligned"]},
                "probability": {"probability": 0.72, "direction": "LONG"},
                "features": {"composite_bias": 0.45,
                              "pinning_probability": 0.25},
                "confluence": {"score": 0.66, "components": []},
            },
            "abstain": {"triggered_rules": ["borderline_kelly"],
                          "size_multiplier": 0.5, "monitor_only": False},
            "meta": {"approve": True, "confidence": 0.75,
                       "risk_modifier": 0.9,
                       "reasoning": ["regime supports", "size sane"]},
            "portfolio_risk": {"net_beta": 0.8, "sector_concentration": 0.3},
            "min_grade_tightened": {"configured": None, "effective": "B",
                                       "reason": "calibration drift"},
            "risk_decision": "approved",
            "ai_components": {"rule": {"action": "BUY_CALL", "confidence": 0.7}},
        }
        tid = _make_trade(detail=detail)
        payload = build_lineage(tid)
        stages = payload["stages"]
        assert stages["regime"]["trend"] == "bullish"
        assert stages["rank"]["grade"] == "A"
        assert stages["probability"]["probability"] == 0.72
        assert stages["features"]["composite_bias"] == 0.45
        assert stages["confluence"]["score"] == 0.66
        assert stages["abstain"]["size_multiplier"] == 0.5
        assert stages["meta_ai"]["risk_modifier"] == 0.9
        assert stages["portfolio_risk"]["net_beta"] == 0.8
        assert stages["min_grade_tightened"]["effective"] == "B"
        assert stages["risk"]["reason"] == "approved"
        assert stages["memo"]["thesis"].startswith("AI infra")
        assert stages["signal"]["ai_components"]["rule"]["confidence"] == 0.7

    def test_falls_back_to_decision_log_for_legacy_row(self, temp_db):
        """A pre-Stage-11.2 trade has no analytics in detail_json, but the
        DecisionLog row (logged by the same engine cycle) carries enough
        regime/grade/probability/features to fill the lineage."""
        from backend.db import session_scope
        from backend.models.decision_log import DecisionLog

        tid = _make_trade(detail={"signal_reason": "legacy", "confidence": 0.6})
        with session_scope() as s:
            s.add(DecisionLog(
                ticker="NVDA", action="BUY_CALL", strategy="trend_pullback",
                confidence=0.6, status="submitted",
                regime_trend="bullish", regime_volatility="normal",
                regime_gamma="long_gamma",
                regime_label="bullish · normal-vol · long gamma",
                grade="A", win_probability=0.66, trade_id=tid,
                features_json=json.dumps({"rsi_14": 35, "composite_bias": 0.2}),
            ))
        payload = build_lineage(tid)
        assert payload["stages"]["regime"]["trend"] == "bullish"
        assert payload["stages"]["rank"]["grade"] == "A"
        assert payload["stages"]["probability"]["probability"] == 0.66
        assert payload["stages"]["features"]["rsi_14"] == 35

    def test_outcome_filled_when_trade_closed(self, temp_db):
        tid = _make_trade(status="closed",
                           detail={"signal_reason": "x", "confidence": 0.6})
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            s.get(Trade, tid).pnl = 42.5
        payload = build_lineage(tid)
        assert payload["stages"]["outcome"]["status"] == "closed"
        assert payload["stages"]["outcome"]["pnl"] == 42.5


# ── endpoint ────────────────────────────────────────────────────────────


class TestLineageEndpoint:
    def test_404_for_unknown_trade(self, client):
        assert client.get("/lineage/trade/999999").status_code == 404

    def test_200_with_full_chain_for_known_trade(self, client):
        tid = _make_trade(detail={
            "signal_reason": "pullback", "confidence": 0.7,
            "snapshot": {"price": 215, "rsi": 35},
            "analytics": {
                "regime": {"trend": "bullish", "label": "bullish"},
                "rank": {"grade": "A"},
                "probability": {"probability": 0.7, "direction": "LONG"},
                "features": {"composite_bias": 0.3},
            },
            "memo": {"thesis": "t", "confidence": "high", "source": "heuristic",
                       "schema_version": 1},
        })
        r = client.get(f"/lineage/trade/{tid}")
        assert r.status_code == 200
        body = r.json()
        assert body["trade_id"] == tid
        assert body["stages"]["regime"]["trend"] == "bullish"
        assert body["stages"]["memo"]["thesis"] == "t"
        # Sanity-check every stage key is present.
        for k in ("signal", "snapshot", "regime", "features", "probability",
                   "rank", "abstain", "meta_ai", "execution", "outcome",
                   "autopsy", "cohort", "memo"):
            assert k in body["stages"]
