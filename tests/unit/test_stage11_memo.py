"""Stage-11 Trade Memo — heuristic builder + Claude path + endpoints.

Pinned:
  • Heuristic memo always returns all required fields, source='heuristic'
  • Confidence labels map correctly through bands
  • Claude path (mocked) returns source='claude' + parses JSON
  • Claude path falls back to heuristic on bad JSON
  • Memo persists in Trade.detail_json via engine integration
  • GET /memo/trade/{id} returns 404 when no memo, 200 with memo when present
  • POST /memo/preview generates without DB
  • POST /memo/regenerate updates an existing row
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.memo import (
    MEMO_SCHEMA_VERSION,
    MemoGenerator,
    TradeMemo,
    build_heuristic_memo,
    get_generator,
    reset_generator,
)
from backend.bot.memo.templates import confidence_label


# ── confidence labeling ───────────────────────────────────────────────────


class TestConfidenceLabel:
    def test_bands(self):
        assert confidence_label(0.95) == "very_high"
        assert confidence_label(0.80) == "very_high"
        assert confidence_label(0.75) == "high"
        assert confidence_label(0.68) == "high"
        assert confidence_label(0.60) == "medium"
        assert confidence_label(0.55) == "medium"
        assert confidence_label(0.40) == "low"
        assert confidence_label(0.0) == "low"

    def test_none_safe(self):
        assert confidence_label(None) == "medium"


# ── heuristic builder ─────────────────────────────────────────────────────


class TestHeuristicMemo:
    def _ctx(self):
        return {
            "ticker": "NVDA", "action": "BUY_CALL", "strategy": "trend_pullback",
            "signal_reason": "5% pullback in uptrend",
            "confidence_num": 0.72,
            "regime": {"trend": "bullish", "volatility": "normal",
                        "gamma": "long_gamma",
                        "label": "bullish · normal-vol · long gamma"},
            "analytics": {
                "rank": {"grade": "A",
                          "reasoning": ["high win probability (72%)",
                                          "multi-timeframe confluence (bullish)"]},
                "probability": {"probability": 0.72, "direction": "LONG"},
            },
            "features": {"composite_bias": 0.45, "pinning_probability": 0.25},
            "cross_asset": {"equities": "risk_on", "volatility": "compressed",
                             "yields": "falling",
                             "regime_label": "risk_on_compressed_vol"},
            "stop_pct": 0.05, "take_profit_pct": 0.10,
        }

    def test_returns_complete_memo(self):
        memo = build_heuristic_memo(**self._ctx())
        assert isinstance(memo, TradeMemo)
        assert memo.thesis
        assert memo.confidence in ("low", "medium", "high", "very_high")
        assert memo.bull_case and memo.bear_case
        assert memo.invalidation
        assert memo.exit_plan
        assert memo.risk_factors
        assert memo.regime_context
        assert memo.source == "heuristic"
        assert memo.schema_version == MEMO_SCHEMA_VERSION

    def test_thesis_mentions_grade_and_strategy(self):
        memo = build_heuristic_memo(**self._ctx())
        assert "A" in memo.thesis
        assert "trend" in memo.thesis.lower()
        assert "NVDA" in memo.thesis

    def test_invalidation_mentions_stop_pct(self):
        memo = build_heuristic_memo(**self._ctx())
        assert "5.0%" in memo.invalidation or "5%" in memo.invalidation

    def test_exit_plan_mentions_staged(self):
        memo = build_heuristic_memo(**self._ctx())
        text = memo.exit_plan.lower()
        assert "tp1" in text and "trail" in text

    def test_short_signal_inverts_direction_in_invalidation(self):
        ctx = self._ctx()
        ctx["action"] = "BUY_PUT"
        memo = build_heuristic_memo(**ctx)
        assert "above" in memo.invalidation

    def test_handles_empty_context(self):
        memo = build_heuristic_memo(
            ticker="X", action="BUY_STOCK", strategy="s",
            signal_reason="",
        )
        assert memo.thesis
        # Bull/bear should at minimum have a fallback line
        assert memo.bull_case and memo.bear_case

    def test_risk_factors_picks_up_drawdown(self):
        ctx = self._ctx()
        ctx["optimizer"] = {"drawdown_pct": 0.08,
                              "requested_dollar": 1000, "recommended_dollar": 800}
        memo = build_heuristic_memo(**ctx)
        joined = " ".join(memo.risk_factors)
        assert "drawdown" in joined.lower()

    def test_high_pin_probability_flagged(self):
        ctx = self._ctx()
        ctx["features"]["pinning_probability"] = 0.85
        memo = build_heuristic_memo(**ctx)
        joined = " ".join(memo.risk_factors).lower()
        assert "pin" in joined


# ── Claude path with mocked client ───────────────────────────────────────


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, text):
        self._text = text
    def create(self, **kw):
        return _Resp(self._text)


class FakeClient:
    def __init__(self, text):
        self.messages = _Messages(text)


class TestClaudePath:
    def test_falls_back_when_no_key(self):
        gen = MemoGenerator(api_key="")
        ctx = {"ticker": "X", "action": "BUY_STOCK", "strategy": "s",
                "signal_reason": "x"}
        memo = gen.generate(context=ctx)
        assert memo.source == "heuristic"

    def test_uses_claude_when_client_provided(self):
        payload = json.dumps({
            "thesis": "AI infra theme strong; NVDA flow bullish",
            "confidence": "high",
            "bull_case": ["dealer regime long gamma", "flow direction bullish"],
            "bear_case": ["pin risk near $215 wall"],
            "invalidation": "close below 50MA",
            "exit_plan": "TP1 50% off at +10%, trail rest on ATR",
            "risk_factors": ["earnings in 7 days", "OPEX week"],
            "regime_context": "Risk-on compressed vol, semi leadership",
        })
        gen = MemoGenerator(client=FakeClient(payload))
        memo = gen.generate(context={"ticker": "NVDA", "action": "BUY_CALL",
                                         "strategy": "trend_pullback",
                                         "signal_reason": "pullback"})
        assert memo.source == "claude"
        assert "NVDA" in memo.thesis or "AI" in memo.thesis
        assert memo.confidence == "high"
        assert len(memo.bull_case) >= 2

    def test_falls_back_on_bad_json(self):
        gen = MemoGenerator(client=FakeClient("not JSON at all"))
        memo = gen.generate(context={"ticker": "X", "action": "BUY_STOCK",
                                         "strategy": "s", "signal_reason": ""})
        assert memo.source == "heuristic"

    def test_singleton_resets(self):
        reset_generator()
        g1 = get_generator()
        g2 = get_generator()
        assert g1 is g2
        reset_generator()
        g3 = get_generator()
        assert g3 is not g1


# ── live endpoints ──────────────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    reset_generator()
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_preview_returns_heuristic_when_forced(self, client):
        body = client.post("/memo/preview", json={
            "ticker": "NVDA", "action": "BUY_CALL", "strategy": "trend_pullback",
            "signal_reason": "pullback", "confidence_num": 0.7,
            "stop_pct": 0.05, "take_profit_pct": 0.10,
            "force_heuristic": True,
        }).json()
        assert body["memo"]["source"] == "heuristic"
        assert body["memo"]["thesis"]

    def test_get_memo_404_for_unknown_trade(self, client):
        assert client.get("/memo/trade/999999").status_code == 404

    def test_get_memo_404_when_no_memo_persisted(self, client):
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            t = Trade(ticker="X", action="BUY_STOCK", quantity=1, price=100,
                       strategy="s", signal_source="t", confidence=0.7,
                       paper=1, status="open", instrument="stock")
            s.add(t); s.flush()
            tid = t.id
        # detail_json is None → no memo
        assert client.get(f"/memo/trade/{tid}").status_code == 404

    def test_regenerate_writes_memo(self, client):
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            t = Trade(ticker="NVDA", action="BUY_CALL", quantity=1, price=215,
                       strategy="trend_pullback", signal_source="t",
                       confidence=0.7, paper=1, status="open",
                       instrument="option")
            s.add(t); s.flush()
            tid = t.id
        r = client.post(f"/memo/regenerate/{tid}")
        assert r.status_code == 200
        # Now GET should return the memo
        body = client.get(f"/memo/trade/{tid}").json()
        assert body["trade_id"] == tid
        assert body["memo"]["thesis"]

    def test_regenerate_404_for_unknown(self, client):
        assert client.post("/memo/regenerate/999999").status_code == 404
