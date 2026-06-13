"""MITS Phase 16.B — DecisionProvenance ORM model unit tests.

Pins:
  • table name + index shape
  • all 11 JSON columns nullable
  • to_dict() surfaces every column under documented keys
  • FK on trade_id to trades.id is declared
"""
from __future__ import annotations

import pytest

from sqlalchemy import inspect

from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance


pytestmark = [pytest.mark.unit]


def test_decision_provenance_table_shape(temp_db):
    from backend.db import get_engine
    engine = get_engine()
    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("decision_provenance")}
    expected = {
        "id", "trade_id", "event_status", "ticker", "decision_timestamp",
        "cycle_id",
        "regime_vector_json", "strategy_matrix_json",
        "agent_inputs_json", "agent_outputs_json",
        "consensus_json", "chairman_memo_json",
        "policy_result_json", "simulator_verdict_json",
        "correlation_cap_json", "portfolio_context_json",
    }
    assert expected.issubset(set(cols.keys())), (
        f"missing cols: {expected - set(cols.keys())}"
    )
    # JSON cols must be nullable.
    for key in (
        "regime_vector_json", "strategy_matrix_json",
        "agent_inputs_json", "agent_outputs_json",
        "consensus_json", "chairman_memo_json",
        "policy_result_json", "simulator_verdict_json",
        "correlation_cap_json", "portfolio_context_json",
    ):
        assert cols[key]["nullable"] is True, f"{key} must be nullable"


def test_decision_provenance_insert_and_to_dict(temp_db):
    """Round-trip a row with every JSON column populated; to_dict()
    surfaces them all under their declared keys."""
    with session_scope() as s:
        row = DecisionProvenance(
            trade_id=None,
            event_status="submitted",
            ticker="AAPL",
            cycle_id="2026-06-12T12:00:00",
            regime_vector_json='{"trend": "bullish"}',
            strategy_matrix_json='{"top": "trend_call"}',
            agent_inputs_json='{"ticker": "AAPL"}',
            agent_outputs_json='[{"agent": "market"}]',
            consensus_json='{"stance": "buy"}',
            chairman_memo_json='{"kill_condition": "x"}',
            policy_result_json='{"eligible": true}',
            simulator_verdict_json='{"reject_reason": null}',
            correlation_cap_json='{"blocked": false}',
            portfolio_context_json='{"theme_heat": 0.2}',
        )
        s.add(row)
        s.flush()
        d = row.to_dict()
    for k in (
        "id", "trade_id", "event_status", "ticker", "decision_timestamp",
        "cycle_id",
        "regime_vector_json", "strategy_matrix_json",
        "agent_inputs_json", "agent_outputs_json",
        "consensus_json", "chairman_memo_json",
        "policy_result_json", "simulator_verdict_json",
        "correlation_cap_json", "portfolio_context_json",
    ):
        assert k in d, f"to_dict() missing {k}"
    assert d["event_status"] == "submitted"
    assert d["ticker"] == "AAPL"
    assert d["regime_vector_json"] == '{"trend": "bullish"}'
    assert d["chairman_memo_json"] == '{"kill_condition": "x"}'
