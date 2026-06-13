"""Stage-13.D10 Decision Marketplace — score + select candidates."""
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.marketplace import (
    Candidate,
    SelectionResult,
    candidate_from,
    select,
)


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def _c(ticker, *, prob=0.6, ret_pct=0.05, risk_pct=0.03, cap=1000.0,
         conf=0.7, liq=1.0):
    return candidate_from(
        ticker=ticker, action="BUY_STOCK", strategy="trend_pullback",
        stop_pct=risk_pct * 100, take_profit_pct=ret_pct * 100,
        probability=prob, capital_required=cap,
        confidence=conf, liquidity_score=liq,
    )


class TestCandidateFrom:
    def test_synthesizes_with_defaults(self):
        c = candidate_from(ticker="NVDA", action="BUY_CALL", strategy="s",
                              stop_pct=None, take_profit_pct=None,
                              probability=None, capital_required=500)
        assert isinstance(c, Candidate)
        assert c.expected_return_pct == 0.05      # default
        assert c.probability == 0.55              # default
        assert c.capital_required == 500.0

    def test_computes_expected_value(self):
        c = _c("NVDA", prob=0.7, ret_pct=0.10, risk_pct=0.05, cap=1000)
        # EV pct = 0.7 * 0.10 - 0.3 * 0.05 = 0.07 - 0.015 = 0.055 → $55 on $1k
        assert c.expected_value == 55.0
        assert c.score > 0
        assert c.score_per_dollar > 0

    def test_score_grows_with_confidence(self):
        low = _c("NVDA", conf=0.2)
        high = _c("NVDA", conf=0.9)
        assert high.score > low.score


class TestSelect:
    def test_picks_highest_score_per_dollar(self):
        a = _c("A", prob=0.7, ret_pct=0.10, cap=500)   # higher score per $
        b = _c("B", prob=0.55, ret_pct=0.04, cap=2000) # lower
        r = select([a, b], capital_available=1000, max_positions=10)
        assert isinstance(r, SelectionResult)
        assert any(c.ticker == "A" for c in r.selected)
        assert all(c.ticker != "B" for c in r.selected)   # B needs more capital

    def test_respects_capital_cap(self):
        cands = [_c(t, cap=600) for t in ("A", "B", "C")]
        r = select(cands, capital_available=1000, max_positions=10)
        # Only one $600 fits
        assert len(r.selected) == 1
        assert all(c.rejection_reason for c in r.rejected)

    def test_respects_max_positions(self):
        cands = [_c(t, cap=100) for t in ("A", "B", "C", "D", "E")]
        r = select(cands, capital_available=10000, max_positions=2)
        assert len(r.selected) == 2

    def test_skips_negative_ev(self):
        loser = _c("L", prob=0.3, ret_pct=0.02, risk_pct=0.10)
        # EV = 0.3*0.02 - 0.7*0.10 = 0.006 - 0.07 = -0.064 → negative
        r = select([loser], capital_available=10000)
        assert loser in r.rejected
        assert "expected value" in (loser.rejection_reason or "")

    def test_handles_empty_pool(self):
        r = select([], capital_available=1000)
        assert r.selected == []
        assert r.total_capital_used == 0.0


class TestEndpoint:
    def test_preview_endpoint(self, client):
        body = client.post("/marketplace/preview", json={
            "candidates": [
                {"ticker": "A", "stop_pct": 3.0, "take_profit_pct": 10.0,
                  "probability": 0.65, "capital_required": 500},
                {"ticker": "B", "stop_pct": 5.0, "take_profit_pct": 4.0,
                  "probability": 0.50, "capital_required": 500},
            ],
            "capital_available": 750,
            "max_positions": 5,
        }).json()
        assert "selected" in body and "rejected" in body
        # A has positive EV, B has negative — A selected
        selected_tickers = [c["ticker"] for c in body["selected"]]
        assert "A" in selected_tickers
