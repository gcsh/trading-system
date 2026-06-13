"""Deterministic data tests: seed a known trade set, assert EXACT outputs.

This is the opposite of asserting against whatever the live market produces — the
numbers below are fully determined by backend/bot/seed.py, so any drift in the
trades/list, trades/summary or drill-in logic fails here.
"""
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload

    from backend import main as main_mod

    reload(main_mod)
    c = TestClient(main_mod.app)

    from backend.bot.seed import seed_demo
    from backend.db import session_scope

    with session_scope() as session:
        seed_demo(session, force=True)
    return c


def test_seed_is_idempotent(temp_db):
    from backend.bot.seed import has_demo, seed_demo
    from backend.db import session_scope

    with session_scope() as s:
        first = seed_demo(s)
        assert len(first) == 5
        assert has_demo(s) is True
        again = seed_demo(s)          # idempotent — no duplicate rows
        assert again == []


def test_trades_list_returns_seeded_rows(client):
    rows = client.get("/trades/list").json()
    assert len(rows) == 5
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"AAPL", "TSLA", "NVDA", "SPY", "QQQ"}
    # The option trade carries its contract detail.
    spy = next(r for r in rows if r["ticker"] == "SPY")
    assert spy["instrument"] == "option" and spy["option_type"] == "call"
    assert spy["strike"] == 755.0 and spy["contracts"] == 2


def test_trades_summary_exact_math(client):
    s = client.get("/trades/summary").json()
    # AAPL +240, TSLA -120, NVDA +560, SPY -90 closed; QQQ open (pnl=None).
    assert s["trade_count"] == 5
    assert s["closed_count"] == 4
    assert s["win_rate"] == 0.5            # 2 wins / 4 closed
    assert s["total_pnl"] == 590.0         # 240 - 120 + 560 - 90
    assert s["avg_gain"] == 400.0          # (240 + 560) / 2
    assert s["avg_loss"] == -105.0         # (-120 + -90) / 2


def test_trade_drill_in_and_missing(client):
    rows = client.get("/trades/list").json()
    nvda = next(r for r in rows if r["ticker"] == "NVDA")
    detail = client.get(f"/trades/{nvda['id']}").json()
    assert detail["ticker"] == "NVDA"
    assert detail["pnl"] == 560.0
    assert detail["strategy"] == "ai_brain"
    # detail_json is parsed into `detail`
    assert detail["detail"] and detail["detail"].get("seed") is True

    missing = client.get("/trades/99999999")
    assert missing.status_code == 404


def test_clear_demo_only_removes_demo(client):
    from backend.bot.seed import clear_demo, has_demo
    from backend.db import session_scope

    with session_scope() as session:
        removed = clear_demo(session)
        assert removed == 5
        assert has_demo(session) is False
    assert client.get("/trades/list").json() == []
