"""Stage-11.5 Memory Layer — episodes + recall + endpoints.

Pinned:
  • build_episodes groups consecutive same-regime rows
  • Episode roll-up computes wins/losses/total_pnl correctly
  • recall_similar returns nothing when no closed trades exist
  • recall_similar surfaces analogues ranked by similarity
  • Recall skips the source trade itself (no self-match)
  • Endpoints work and 404 cleanly
"""
import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.memory import (
    build_episodes,
    recall_similar,
    recall_summary,
    MemoryMatch,
    RegimeEpisode,
)


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def _make_decision(*,
                    ticker="NVDA", action="BUY_CALL", strategy="trend_pullback",
                    trend="bullish", vol="normal", gamma="long_gamma",
                    grade="A", win_prob=0.7, status="submitted",
                    pnl=None, features=None,
                    ts_offset_min=0, trade_id=None):
    from backend.db import session_scope
    from backend.models.decision_log import DecisionLog
    with session_scope() as s:
        row = DecisionLog(
            ticker=ticker, action=action, strategy=strategy,
            confidence=0.7, status=status,
            regime_trend=trend, regime_volatility=vol, regime_gamma=gamma,
            regime_label=f"{trend} · {vol}-vol · {gamma}",
            grade=grade, win_probability=win_prob,
            trade_id=trade_id,
            outcome_pnl=pnl,
            outcome_status="closed" if pnl is not None else None,
            features_json=json.dumps(features) if features else None,
        )
        # Override the auto-timestamp so episodes order deterministically.
        row.timestamp = datetime.utcnow() + timedelta(minutes=ts_offset_min)
        s.add(row); s.flush()
        return row.id


# ── episodes ─────────────────────────────────────────────────────────────


class TestEpisodes:
    def test_returns_empty_on_no_data(self, temp_db):
        assert build_episodes() == []

    def test_groups_consecutive_same_regime(self, temp_db):
        # 3 bullish, then 2 bearish, then 1 bullish → 3 episodes.
        for i in range(3):
            _make_decision(trend="bullish", ts_offset_min=i, pnl=50.0)
        for i in range(2):
            _make_decision(trend="bearish", action="BUY_PUT",
                             ts_offset_min=10 + i, pnl=-30.0)
        _make_decision(trend="bullish", ts_offset_min=20, pnl=100.0)
        eps = build_episodes()
        assert len(eps) == 3
        # Newest-first → bullish (1 trade), bearish (2), bullish (3)
        assert eps[0].regime_trend == "bullish" and eps[0].decisions == 1
        assert eps[1].regime_trend == "bearish" and eps[1].decisions == 2
        assert eps[2].regime_trend == "bullish" and eps[2].decisions == 3

    def test_rollup_counts_wins_losses(self, temp_db):
        _make_decision(pnl=100.0)
        _make_decision(pnl=200.0, ts_offset_min=1)
        _make_decision(pnl=-150.0, ts_offset_min=2)
        eps = build_episodes()
        assert len(eps) == 1
        e = eps[0]
        assert e.closed == 3
        assert e.wins == 2 and e.losses == 1
        assert e.win_rate == round(2 / 3, 3)
        assert e.total_pnl == 150.0
        assert isinstance(e, RegimeEpisode)

    def test_ignores_unsubmitted_in_outcome_tally(self, temp_db):
        _make_decision(status="abstain", pnl=None)
        _make_decision(status="submitted", pnl=80.0, ts_offset_min=1)
        eps = build_episodes()
        assert eps[0].decisions == 2
        assert eps[0].submitted == 1
        assert eps[0].closed == 1
        assert eps[0].total_pnl == 80.0


# ── recall ───────────────────────────────────────────────────────────────


class TestRecall:
    def test_empty_when_no_closed_trades(self, temp_db):
        _make_decision(pnl=None)        # open
        out = recall_similar({"ticker": "X", "action": "BUY_STOCK"})
        assert out == []

    def test_finds_analogous_regime(self, temp_db):
        # 2 bullish past wins, 1 bearish past loss
        _make_decision(trend="bullish", vol="normal", gamma="long_gamma",
                         features={"rsi_14": 70, "trend_bias": 0.5,
                                       "vix": 14}, pnl=100.0, ts_offset_min=0)
        _make_decision(trend="bullish", vol="normal", gamma="long_gamma",
                         features={"rsi_14": 65, "trend_bias": 0.4,
                                       "vix": 15}, pnl=150.0, ts_offset_min=10)
        _make_decision(trend="bearish", vol="elevated", gamma="short_gamma",
                         action="BUY_PUT",
                         features={"rsi_14": 30, "trend_bias": -0.4,
                                       "vix": 28}, pnl=-80.0, ts_offset_min=20)
        # Bullish target → should return the two bullish wins ahead of the bearish loss.
        out = recall_similar({
            "ticker": "NVDA", "action": "BUY_CALL",
            "analytics": {
                "regime": {"trend": "bullish", "volatility": "normal",
                              "gamma": "long_gamma"},
                "features": {"rsi_14": 68, "trend_bias": 0.45, "vix": 14},
            },
        }, k=5)
        assert len(out) == 2     # bearish row falls below min_similarity
        assert all(m.regime_label.startswith("bullish") for m in out)
        assert out[0].similarity >= out[1].similarity
        assert all(isinstance(m, MemoryMatch) for m in out)

    def test_skips_self(self, temp_db):
        did = _make_decision(trade_id=999, pnl=100.0,
                                features={"rsi_14": 60, "trend_bias": 0.3, "vix": 15})
        out = recall_similar({
            "trade_id": 999, "ticker": "NVDA", "action": "BUY_CALL",
            "analytics": {
                "regime": {"trend": "bullish", "volatility": "normal",
                              "gamma": "long_gamma"},
                "features": {"rsi_14": 60, "trend_bias": 0.3, "vix": 15},
            },
        })
        assert all(m.trade_id != 999 for m in out)

    def test_summary_rolls_up(self, temp_db):
        matches = [
            MemoryMatch(trade_id=1, decision_id=1, similarity=0.8,
                          timestamp="", ticker="X", strategy="s",
                          regime_label="bull", grade="A",
                          win_probability=0.7,
                          outcome_pnl=100.0, outcome_status="closed", win=True),
            MemoryMatch(trade_id=2, decision_id=2, similarity=0.7,
                          timestamp="", ticker="X", strategy="s",
                          regime_label="bull", grade="B",
                          win_probability=0.6,
                          outcome_pnl=-50.0, outcome_status="closed", win=False),
        ]
        s = recall_summary(matches)
        assert s["matches"] == 2
        assert s["wins"] == 1 and s["losses"] == 1
        assert s["hit_rate"] == 0.5
        assert s["total_pnl"] == 50.0

    def test_summary_empty(self):
        s = recall_summary([])
        assert s["matches"] == 0 and s["hit_rate"] is None


# ── endpoints ────────────────────────────────────────────────────────────


class TestMemoryEndpoints:
    def test_episodes_returns_list(self, client, temp_db):
        _make_decision(pnl=100.0)
        _make_decision(trend="bearish", action="BUY_PUT", ts_offset_min=10,
                         pnl=-50.0)
        body = client.get("/memory/episodes").json()
        assert body["count"] >= 2
        assert "episodes" in body
        assert all("regime_label" in e for e in body["episodes"])

    def test_recall_endpoint_works(self, client, temp_db):
        _make_decision(pnl=80.0,
                         features={"rsi_14": 65, "trend_bias": 0.4, "vix": 14})
        body = client.post("/memory/recall", json={
            "ticker": "NVDA", "action": "BUY_CALL",
            "analytics": {
                "regime": {"trend": "bullish", "volatility": "normal",
                              "gamma": "long_gamma"},
                "features": {"rsi_14": 60, "trend_bias": 0.35, "vix": 14},
            },
            "k": 5,
        }).json()
        assert "matches" in body and "summary" in body
        assert len(body["matches"]) >= 1

    def test_recall_for_trade_404_unknown(self, client):
        assert client.get("/memory/recall/trade/999999").status_code == 404

    def test_recall_for_trade_uses_persisted_context(self, client, temp_db):
        from backend.db import session_scope
        from backend.models.trade import Trade
        # 1 past closed bullish trade
        _make_decision(pnl=120.0, features={"rsi_14": 65, "trend_bias": 0.4,
                                                  "vix": 14})
        # the trade we'll query for
        detail = {
            "analytics": {
                "regime": {"trend": "bullish", "volatility": "normal",
                              "gamma": "long_gamma"},
                "features": {"rsi_14": 60, "trend_bias": 0.35, "vix": 14},
            },
        }
        with session_scope() as s:
            t = Trade(ticker="NVDA", action="BUY_CALL", quantity=1, price=215,
                       strategy="trend_pullback", signal_source="t",
                       confidence=0.7, paper=1, status="open",
                       instrument="option",
                       detail_json=json.dumps(detail))
            s.add(t); s.flush()
            tid = t.id
        body = client.get(f"/memory/recall/trade/{tid}").json()
        assert body["trade_id"] == tid
        assert body["summary"]["matches"] >= 1
