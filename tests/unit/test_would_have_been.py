"""MITS Phase 19 — would_have_been execution panel for HOLD decisions.

Pins:
  * ``BotEngine._compute_would_have_been`` produces a 4-key dict with
    fill / sizing / chain / exit projections off the live event.
  * The ``would_have_been_json`` column is auto-migrated onto
    ``decision_provenance`` and round-trips through ``to_dict``.
  * The cockpit endpoint surfaces ``would_have_been`` for non-submitted
    rows (HOLDs / blocked).
"""
from __future__ import annotations

import json

import pytest

from backend.bot.engine import BotEngine
from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance


pytestmark = [pytest.mark.unit]


def _engine() -> BotEngine:
    return BotEngine.__new__(BotEngine)


def test_compute_would_have_been_basic_shape(monkeypatch):
    """The helper returns the 4 documented keys with non-empty strings."""
    # Stub the quote source so the test never hits yfinance.
    import backend.bot.data.quote_source as qs_mod

    class _Q:
        price = 195.50
        source = "stub"
        age_seconds = 0.0

    monkeypatch.setattr(qs_mod, "get_quote", lambda t: _Q())

    eng = _engine()
    ev = {
        "ticker": "AAPL",
        "action": "HOLD",
        "snapshot": {"price": 195.50},
        "_config": {"risk": {
            "max_position_size_usd": 1000.0,
            "max_cash_usage_pct": 50.0,
            "take_profit_pct": 12.0,
            "stop_loss_pct": 6.0,
        }},
    }
    wb = eng._compute_would_have_been(ev, signal=None)
    assert wb is not None
    for key in ("fill_snapshot", "sizing_chain",
                "chain_selection", "exit_policy_result"):
        assert key in wb
        assert isinstance(wb[key], str)
        assert wb[key]  # non-empty
    # Substring checks — keep the contract operator-readable.
    assert "bid=" in wb["fill_snapshot"]
    assert "ask=" in wb["fill_snapshot"]
    assert "Risk-baseline" in wb["sizing_chain"]
    assert "+12.0%" in wb["exit_policy_result"]
    assert "-6.0%" in wb["exit_policy_result"]
    # Stock-direction HOLD → chain panel says no chain selected.
    assert "no option chain" in wb["chain_selection"].lower() or \
           "stock-direction" in wb["chain_selection"].lower()


def test_compute_would_have_been_option_action_surfaces_strike(monkeypatch):
    """When the proposed action is BUY_CALL the chain panel projects a
    strike near spot rather than saying "no chain"."""
    import backend.bot.data.quote_source as qs_mod

    class _Q:
        price = 410.25
        source = "stub"
        age_seconds = 0.0

    monkeypatch.setattr(qs_mod, "get_quote", lambda t: _Q())

    eng = _engine()
    ev = {
        "ticker": "SPY",
        "action": "BUY_CALL",
        "snapshot": {"price": 410.25},
    }
    wb = eng._compute_would_have_been(ev, signal=None)
    assert wb is not None
    chain_str = wb["chain_selection"]
    assert "strike" in chain_str.lower()
    assert "410" in chain_str  # spot-anchored projection
    assert "delta target" in chain_str.lower()


def test_would_have_been_column_round_trips(temp_db):
    """The new ``would_have_been_json`` column persists and decodes
    cleanly via DecisionProvenance.to_dict()."""
    payload = {
        "fill_snapshot": "If executed: bid=$100, ask=$100.2, mid=$100.1",
        "sizing_chain": "Risk-baseline qty=10 @ $100",
        "chain_selection": "Would have targeted strike ~$100",
        "exit_policy_result": "Would have armed: take_profit +10%, "
                              "stop -5%",
    }
    with session_scope() as s:
        row = DecisionProvenance(
            trade_id=None,
            event_status="hold",
            ticker="AAPL",
            cycle_id="2026-06-13T12:00:00",
            would_have_been_json=json.dumps(payload),
        )
        s.add(row)
        s.flush()
        d = row.to_dict()
    assert "would_have_been_json" in d
    assert json.loads(d["would_have_been_json"]) == payload


def test_cockpit_surfaces_would_have_been_on_hold(temp_db):
    """``GET /decision/cockpit/{id}`` returns ``would_have_been`` decoded
    as a dict when the underlying row carries the JSON."""
    from fastapi.testclient import TestClient

    payload = {
        "fill_snapshot": "If executed: mid=$50",
        "sizing_chain": "Risk-baseline qty=20",
        "chain_selection": "Stock-direction — no chain",
        "exit_policy_result": "Would have armed: +10% / -5%",
    }
    with session_scope() as s:
        row = DecisionProvenance(
            trade_id=None,
            event_status="hold",
            ticker="AAPL",
            cycle_id="2026-06-13T12:00:00",
            consensus_json='{"stance": "abstain"}',
            would_have_been_json=json.dumps(payload),
        )
        s.add(row)
        s.flush()
        prov_id = int(row.id)

    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    client = TestClient(main_mod.app)
    r = client.get(f"/decision/cockpit/{prov_id}")
    assert r.status_code == 200
    body = r.json()
    assert "would_have_been" in body
    assert body["would_have_been"] == payload
    # Submitted rows still surface null (legacy rows + executed trades).
    assert body["event_status"] == "hold"
