"""MITS Phase 17.D — engine writes Trade.chain_selection_json on the
option path; NULL on the stock path (back-compat).

Strategy:
  • Drive ``build_order_plan`` for a BUY_CALL signal — plan["chain_selection"]
    must be a non-None ChainSelectionProvenance (paper_stub or thetadata).
  • Drive ``_persist_trade`` directly with a synthesized plan to confirm
    the column round-trips through the ORM and back into ``to_dict``.
  • Drive a BUY_STOCK signal through the same plan-builder + persistence
    pipeline and assert ``chain_selection_json`` stays NULL.

Why not run a full engine cycle? The 17.C integration test already covers
the stock path through a real cycle. The chain-selection plumbing is on
the OPTION path, which the production cycle reaches through the AI Brain
on opportunistic regimes — too many moving parts to wire deterministically
in CI. Plumbing-level integration on ``build_order_plan`` + ``_persist_trade``
is the appropriate granularity for the back-compat invariant.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.bot.engine import BotEngine
from backend.bot.market_data import MarketSnapshot
from backend.bot.paper_executor import PaperExecutor
from backend.bot.strategies.base import Action, Signal
from backend.db import session_scope
from backend.models.trade import Trade


pytestmark = [pytest.mark.integration]


def _engine() -> BotEngine:
    adapter = MagicMock()
    adapter.snapshot.return_value = MarketSnapshot(data={"price": 200.0}, source_errors=[])
    return BotEngine(
        executor=PaperExecutor(starting_cash=10_000.0, price_fn=lambda _t: 200.0),
        market_data=adapter,
    )


def test_option_plan_carries_chain_selection_provenance(temp_db):
    """Even with no ThetaData reachable, the plan must carry a
    paper_stub provenance — never None."""
    eng = _engine()
    signal = Signal(
        action=Action.BUY_CALL, ticker="AAPL", confidence=0.9,
        reason="t", strategy="t_strat", metadata={"source": "test"},
    )
    plan = eng.build_order_plan(signal, quantity=1, price=200.0)
    assert plan["instrument"] == "option"
    cs = plan.get("chain_selection")
    assert cs is not None, (
        "Phase 17.D contract: build_order_plan must stamp a "
        "ChainSelectionProvenance (or paper_stub fallback) on every "
        "option-path plan."
    )
    # The dataclass exposes to_dict; verify the shape.
    d = cs.to_dict()
    for key in ("ticker", "direction", "requested_dte",
                "requested_delta_band", "candidates",
                "chosen_strike", "chosen_option_type",
                "chosen_reason", "chain_source", "captured_at"):
        assert key in d, f"chain_selection missing field {key!r}"
    assert d["ticker"] == "AAPL"
    assert d["direction"] == "long_call"
    assert isinstance(d["candidates"], list)
    assert len(d["candidates"]) >= 1


def test_stock_plan_omits_chain_selection(temp_db):
    eng = _engine()
    signal = Signal(
        action=Action.BUY_STOCK, ticker="AAPL", confidence=0.9,
        reason="t", strategy="t_strat", metadata={"source": "test"},
    )
    plan = eng.build_order_plan(signal, quantity=10, price=200.0)
    assert plan["instrument"] == "stock"
    # Stock plans never set chain_selection (no chain to choose from).
    assert plan.get("chain_selection") is None


def test_persist_trade_writes_chain_selection_json(temp_db):
    """Synthetic option plan that carries a chain_selection_json string
    round-trips into Trade.chain_selection_json and back through to_dict."""
    eng = _engine()
    signal = Signal(
        action=Action.BUY_CALL, ticker="AAPL", confidence=0.9,
        reason="t", strategy="t_strat", metadata={"source": "test"},
    )
    fake_prov = {
        "ticker": "AAPL", "direction": "long_call",
        "requested_dte": 30,
        "requested_delta_band": [0.30, 0.45],
        "underlying_spot": 200.0,
        "candidates": [
            {"expiry": "2026-06-19", "strike": 205.0, "option_type": "C",
             "delta": 0.38, "rejection_reason": None},
            {"expiry": "2026-06-19", "strike": 200.0, "option_type": "C",
             "delta": 0.55, "rejection_reason": "wrong_delta_band"},
        ],
        "chosen_expiry": "2026-06-19", "chosen_strike": 205.0,
        "chosen_option_type": "C",
        "chosen_reason": "delta=0.38 in [0.30,0.45] band, source=thetadata",
        "freshness_seconds": 2.4,
        "chain_source": "thetadata",
        "captured_at": "2026-06-12T12:34:56",
    }
    plan = {
        "instrument": "option", "side": "BUY",
        "option_type": "call",
        "strike": 205.0, "expiration": "2026-06-19", "contracts": 1,
        "pricing_source": "thetadata",
        "chain_selection_json": json.dumps(fake_prov),
    }
    trade_id = eng._persist_trade(
        signal, 1, 2.50, paper=True, status="open",
        plan=plan, snapshot={"price": 200.0}, event={},
    )
    with session_scope() as s:
        row = s.get(Trade, trade_id)
        assert row.chain_selection_json is not None
        decoded = json.loads(row.chain_selection_json)
        assert decoded["chosen_strike"] == 205.0
        assert decoded["chain_source"] == "thetadata"
        assert decoded["candidates"][0]["rejection_reason"] is None
        # to_dict surfaces the parsed object under "chain_selection".
        as_dict = row.to_dict()
        assert as_dict["chain_selection"]["chosen_strike"] == 205.0


def test_persist_trade_stock_path_writes_null_chain_selection(temp_db):
    """BUY_STOCK persistence — Phase 17.D back-compat invariant: stock
    Trade rows must keep chain_selection_json = NULL so the column is
    no-op for the pre-17.D common path. The operator's "real money"
    hard rule explicitly demands this."""
    eng = _engine()
    signal = Signal(
        action=Action.BUY_STOCK, ticker="AAPL", confidence=0.9,
        reason="t", strategy="t_strat", metadata={"source": "test"},
    )
    plan = {
        "instrument": "stock", "side": "BUY",
        "quantity": 10,
        "pricing_source": "alpaca",
        # No chain_selection_json key — engine.build_order_plan for
        # stock paths never sets it. Confirm _persist_trade gracefully
        # writes NULL.
    }
    trade_id = eng._persist_trade(
        signal, 10, 200.0, paper=True, status="open",
        plan=plan, snapshot={"price": 200.0}, event={},
    )
    with session_scope() as s:
        row = s.get(Trade, trade_id)
        assert row.instrument == "stock"
        assert row.chain_selection_json is None, (
            "back-compat: stock trades must NEVER have a chain_selection_json"
        )
        # to_dict surface is also None.
        as_dict = row.to_dict()
        assert as_dict["chain_selection"] is None
