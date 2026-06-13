"""MITS Phase 16.E — /decision/cockpit/{identifier} integration.

Seeds one DecisionProvenance row with realistic JSON in every column
+ a Trade row pointed at it, then exercises the three identifier shapes
the cockpit accepts:

  * by trade_id (int)
  * by decision_provenance.id (int, fallback when trade_id misses)
  * by ticker (latest)

For each shape the response MUST carry every top-level panel the UI
renders so a regression here is caught before the page goes white.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance
from backend.models.trade import Trade


pytestmark = [pytest.mark.integration]


_TOP_KEYS = {
    "decision_id", "trade_id", "ticker", "event_status",
    "decision_timestamp",
    "policy_result", "council_breakdown", "chairman_memo",
    "portfolio_impact", "decision_quality_score",
    "simulator_scenarios",
}


def _seed_row(
    *, ticker: str = "NVDA", event_status: str = "submitted",
    trade_id_attached: bool = True,
    decision_ts: datetime | None = None,
) -> tuple[int, int | None]:
    """Insert a Trade (optional) + a DecisionProvenance row pointing at
    it. Returns (decision_id, trade_id)."""
    decision_ts = decision_ts or datetime.utcnow()
    trade_id = None
    with session_scope() as s:
        if trade_id_attached:
            tr = Trade(
                ticker=ticker, action="BUY_STOCK",
                quantity=10.0, price=120.0, status="open",
                strategy="rsi_mean_reversion", paper=True,
                signal_source="rsi_mean_reversion",
                pnl=None,
            )
            s.add(tr)
            s.flush()
            trade_id = int(tr.id)

        prov = DecisionProvenance(
            trade_id=trade_id,
            event_status=event_status,
            ticker=ticker,
            decision_timestamp=decision_ts,
            cycle_id=decision_ts.isoformat(),
            regime_vector_json=json.dumps({
                "ticker": ticker,
                "trend": {
                    "value": "bullish", "freshness_seconds": 0.0,
                    "source": "regime", "health": "green",
                },
                "iv_rank": {
                    "value": 32.0, "freshness_seconds": 12.0,
                    "source": "iv_regime_cache", "health": "green",
                },
                "health": "green",
            }),
            strategy_matrix_json=json.dumps({
                "top_strategy": "rsi_mean_reversion", "score": 0.71,
            }),
            agent_inputs_json=json.dumps({
                "ticker": ticker, "action": "BUY_STOCK",
                "proposed_direction": "long",
            }),
            agent_outputs_json=json.dumps([
                {
                    "agent": f"agent_{i}",
                    "role": f"role_{i}",
                    "stance": "buy",
                    "confidence": 65 + i,
                    "weight": 1.0,
                    "reasoning_type": "contributing",
                    "supporting_factors": ["test driver"],
                    "concerns": [],
                }
                for i in range(8)
            ]),
            consensus_json=json.dumps({
                "stance": "buy",
                "confidence": 0.72,
                "disagreement_score": 0.18,
                "recommendation": "execute",
                "size_multiplier": 1.0,
                "votes": [],
                "confidence_breakdown": {
                    "market_structure": 0.7, "technical": 0.65,
                    "options": 0.55, "historical_analog": 0.60,
                    "simulator": 0.5, "macro": 0.45,
                    "composite": 0.72,
                    "axis_health": {
                        "market_structure": "green",
                        "technical": "green",
                        "options": "green",
                        "historical_analog": "yellow",
                        "simulator": "yellow",
                        "macro": "yellow",
                    },
                    "axis_n": {
                        "market_structure": 3, "technical": 4,
                        "options": 2, "historical_analog": 1,
                        "simulator": 1, "macro": 1,
                    },
                },
                "chairman_report": {
                    "decision": "EXECUTE",
                    "decision_reason": "high_conviction",
                    "kill_condition": "RSI back above 70",
                    "structured_why": [
                        "alpha: oversold bounce setup",
                        "beta: macro tailwind",
                    ],
                    "main_risk": "gamma: dealer hedge unwind",
                    "confidence_pct": 72,
                    "conviction": 0.72,
                    "position_size_modifier": 1.0,
                    "evidence_correlation": "independent",
                    "independent_signal_count": 4,
                    "bull_case": "tape-driven mean reversion",
                    "bear_case": "macro tail risk on FOMC",
                    "dissent": {
                        "primary_dissenter": "risk_officer",
                        "dissent_share": 0.18,
                    },
                },
            }),
            chairman_memo_json=json.dumps({
                "decision": "EXECUTE",
                "kill_condition": "RSI back above 70",
                "structured_why": [
                    "alpha: oversold bounce setup",
                ],
                "main_risk": "gamma: dealer hedge unwind",
                "confidence_pct": 72,
            }),
            policy_result_json=json.dumps({
                "eligible": True,
                "blocking_factors": [],
                "soft_penalties_total_pct": 5.0,
                "evaluated_at": decision_ts.isoformat(),
                "rule_evaluations": [],
            }),
            simulator_verdict_json=json.dumps({
                "mode": "ensemble",
                "expected_payoff": 1.42,
                "p_win": 0.58,
                "p_max_loss": 0.12,
                "payoff_std": 1.1,
                "max_drawdown_pctile_5": -2.4,
                "conviction_score": 0.61,
                "sample_size": 50,
                "cache_hit": False,
                "reject_reason": None,
                "scenarios": [
                    {"label": "continuation", "probability": 0.35,
                     "expected_payoff": 2.4, "payoff_std": 1.0,
                     "n_analogs": 18},
                    {"label": "fake_breakout", "probability": 0.25,
                     "expected_payoff": 0.3, "payoff_std": 0.9,
                     "n_analogs": 12},
                    {"label": "stop_out", "probability": 0.28,
                     "expected_payoff": -1.5, "payoff_std": 0.8,
                     "n_analogs": 14},
                    {"label": "macro_shock", "probability": 0.12,
                     "expected_payoff": -3.2, "payoff_std": 1.4,
                     "n_analogs": 6},
                ],
            }),
            correlation_cap_json=json.dumps({
                "blocked": False, "hard_block": False,
                "sizing_multiplier": 0.85,
                "reason": "soft cap: |rho|=0.62 vs AMD (LONG)",
                "worst_peer": "AMD", "worst_rho": 0.62,
                "candidate_direction": "LONG",
            }),
            portfolio_context_json=json.dumps({
                "equity": 10_000.0,
                "long_pct": 0.40,
                "short_pct": 0.0,
                "by_sector": {"Technology": 0.30, "Finance": 0.10},
                "computed_at": decision_ts.isoformat() + "Z",
            }),
            decision_quality_score_json=json.dumps({
                "analysis_quality": 68.0,
                "council_agreement": 62.0,
                "risk_quality": 71.0,
                "execution_quality": 58.0,
                "composite": 65.2,
                "components": {
                    "regime_health": 1.0,
                    "ensemble_agreement": 0.7,
                },
            }),
        )
        s.add(prov)
        s.flush()
        return int(prov.id), trade_id


def test_cockpit_by_trade_id(temp_db):
    decision_id, trade_id = _seed_row(ticker="NVDA")
    assert trade_id is not None
    with TestClient(app) as client:
        resp = client.get(f"/decision/cockpit/{trade_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert _TOP_KEYS <= set(body.keys()), (
        f"missing top-level keys: {_TOP_KEYS - set(body.keys())}"
    )
    assert body["decision_id"] == decision_id
    assert body["trade_id"] == trade_id
    assert body["ticker"] == "NVDA"
    assert body["event_status"] == "submitted"

    # Policy panel
    assert body["policy_result"]["eligible"] is True
    assert body["policy_result"]["soft_penalties_total_pct"] == 5.0

    # Council panel
    council = body["council_breakdown"]
    assert council["consensus"]["stance"] == "buy"
    assert len(council["agent_outputs"]) == 8

    # Chairman memo
    cm = body["chairman_memo"]
    assert cm["decision"] == "EXECUTE"
    assert cm["kill_condition"] == "RSI back above 70"
    assert cm["confidence_pct"] == 72
    assert cm["main_risk"].startswith("gamma:")
    assert len(cm["structured_why"]) >= 1

    # Portfolio impact
    pi = body["portfolio_impact"]
    assert pi["portfolio_context"]["equity"] == 10_000.0
    assert pi["correlation_cap"]["worst_peer"] == "AMD"
    assert abs(pi["correlation_cap"]["sizing_multiplier"] - 0.85) < 1e-6

    # Decision quality score
    dqs = body["decision_quality_score"]
    assert abs(dqs["composite"] - 65.2) < 1e-6
    for axis in (
        "analysis_quality", "council_agreement",
        "risk_quality", "execution_quality",
    ):
        assert dqs[axis] is not None

    # Simulator scenarios — must have all 4 cluster labels
    labels = {sc["label"] for sc in body["simulator_scenarios"]}
    assert labels == {
        "continuation", "fake_breakout", "stop_out", "macro_shock",
    }


def test_cockpit_by_ticker_returns_latest(temp_db):
    """Two rows for AAPL — the cockpit by-ticker lookup returns the
    most recent decision_timestamp."""
    older = datetime.utcnow() - timedelta(hours=3)
    older_id, _ = _seed_row(
        ticker="AAPL", trade_id_attached=False, decision_ts=older,
    )
    newer_id, _ = _seed_row(ticker="AAPL", trade_id_attached=True)

    with TestClient(app) as client:
        resp = client.get("/decision/cockpit/AAPL")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision_id"] == newer_id
    assert body["decision_id"] != older_id
    assert body["ticker"] == "AAPL"
    assert _TOP_KEYS <= set(body.keys())


def test_cockpit_by_decision_id_fallback(temp_db):
    """No trade_id attached — lookup by trade_id misses, falls back to
    direct DecisionProvenance.id."""
    decision_id, _ = _seed_row(
        ticker="MSFT", event_status="consensus_abstain",
        trade_id_attached=False,
    )
    with TestClient(app) as client:
        resp = client.get(f"/decision/cockpit/{decision_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision_id"] == decision_id
    assert body["trade_id"] is None
    assert body["event_status"] == "consensus_abstain"


def test_cockpit_unknown_identifier_404(temp_db):
    with TestClient(app) as client:
        resp = client.get("/decision/cockpit/ZZZZZZ_NEVER_TRADED")
    assert resp.status_code == 404
