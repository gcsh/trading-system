"""Stage-12.B6 AI Cost telemetry."""
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.ai_cost import (
    PRICING,
    alpha_per_dollar,
    by_surface,
    record_from_response,
    record_usage,
    recent_entries,
    reset,
    totals,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset()
    yield
    reset()


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class _FakeUsage:
    def __init__(self, in_tok, out_tok):
        self.input_tokens = in_tok
        self.output_tokens = out_tok


class _FakeResp:
    def __init__(self, in_tok, out_tok):
        self.usage = _FakeUsage(in_tok, out_tok)


class TestRecordUsage:
    def test_basic_recording(self):
        e = record_usage(surface="memo", model="claude-sonnet-4-6",
                            tokens_in=1000, tokens_out=500)
        assert e.cost_usd > 0
        t = totals()
        assert t["calls"] == 1 and t["tokens_in"] == 1000

    def test_pricing_matches_tier(self):
        # Sonnet $3/M in + $15/M out → 1k in + 1k out = $0.003 + $0.015 = $0.018
        e = record_usage(surface="memo", model="claude-sonnet-4-6",
                            tokens_in=1000, tokens_out=1000)
        assert e.cost_usd == 0.018

    def test_opus_costs_more(self):
        opus = record_usage(surface="memo", model="claude-opus-4-7",
                                tokens_in=1000, tokens_out=1000)
        sonnet_after_reset = PRICING["claude-sonnet-4-6"]
        # Opus is $15/M in + $75/M out
        assert opus.cost_usd == 0.090
        # And opus is 5x more than sonnet for same tokens
        assert opus.cost_usd > 0.018 * 4

    def test_unknown_model_falls_back(self):
        e = record_usage(surface="memo", model="claude-future-model",
                            tokens_in=1000, tokens_out=1000)
        # Default tier = sonnet
        assert e.cost_usd == 0.018

    def test_record_from_response(self):
        e = record_from_response(surface="brain", model="claude-sonnet-4-6",
                                    response=_FakeResp(2000, 800))
        assert e is not None
        assert e.tokens_in == 2000 and e.tokens_out == 800

    def test_record_from_response_no_usage(self):
        class NoUsage: pass
        assert record_from_response(surface="x", model="y",
                                        response=NoUsage()) is None

    def test_by_surface_aggregates(self):
        record_usage(surface="memo", model="claude-sonnet-4-6",
                        tokens_in=1000, tokens_out=500)
        record_usage(surface="memo", model="claude-sonnet-4-6",
                        tokens_in=2000, tokens_out=1000)
        record_usage(surface="narrative", model="claude-sonnet-4-6",
                        tokens_in=500, tokens_out=200)
        agg = by_surface()
        assert agg["memo"]["calls"] == 2
        assert agg["memo"]["tokens_in"] == 3000
        assert agg["narrative"]["calls"] == 1

    def test_recent_entries_newest_first(self):
        record_usage(surface="a", model="claude-sonnet-4-6",
                        tokens_in=100, tokens_out=100)
        record_usage(surface="b", model="claude-sonnet-4-6",
                        tokens_in=200, tokens_out=200)
        e = recent_entries(limit=2)
        assert e[0]["surface"] == "b"
        assert e[1]["surface"] == "a"


class TestAlphaPerDollar:
    def test_no_attribution_when_empty(self, temp_db):
        out = alpha_per_dollar()
        assert out["attributed_cost_usd"] == 0
        assert out["alpha_per_dollar"] is None

    def test_attributes_to_closed_trade(self, temp_db):
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            t = Trade(ticker="X", action="BUY_STOCK", quantity=1, price=100,
                        strategy="s", signal_source="t", confidence=0.7,
                        paper=1, status="closed", instrument="stock", pnl=10.0)
            s.add(t); s.flush()
            tid = t.id
        record_usage(surface="memo", model="claude-sonnet-4-6",
                        tokens_in=1000, tokens_out=500, trade_id=tid)
        out = alpha_per_dollar()
        assert out["attributed_pnl_usd"] == 10.0
        assert out["alpha_per_dollar"] is not None
        assert out["alpha_per_dollar"] > 0


class TestEndpoints:
    def test_summary_endpoint(self, client):
        record_usage(surface="memo", model="claude-sonnet-4-6",
                        tokens_in=100, tokens_out=50)
        body = client.get("/ai-cost/summary").json()
        assert body["totals"]["calls"] == 1
        assert "memo" in body["by_surface"]

    def test_recent_endpoint(self, client):
        record_usage(surface="memo", model="claude-sonnet-4-6",
                        tokens_in=100, tokens_out=50)
        body = client.get("/ai-cost/recent?limit=10").json()
        assert len(body["entries"]) == 1

    def test_alpha_ratio_endpoint(self, client):
        body = client.get("/ai-cost/alpha-ratio").json()
        assert "attributed_cost_usd" in body
