"""MITS Phase 18-FU integration tests (Gaps 1, 2, 4).

End-to-end pass exercising:
  * Gap 2 — full attribution_report against DB rows that carry
    strategy_matrix_json + Trade.strategy='exit_manager'. Confirms the
    aggregated report emits strategy_name == matrix template (not
    exit_manager) for matrix-tagged closed trades AND emits an
    UNATTRIBUTED bucket for trades without matrix.
  * Gap 1 — synthetic policy_tunings approve → flag flip on →
    PolicyContext scratch carries the override → resolve_threshold
    returns the override + correct source id. Demonstrates the
    Approve button is wired end-to-end.
  * Gap 4 — backfill route dry-run + live-run against a seeded
    corpus; confirms default attribution still ignores the synthetic
    rows.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Dict

import pytest

from backend.bot.decision.policy import (
    BlockingFactor,
    DecisionPolicy,
    PolicyContext,
    PolicyRule,
)
from backend.bot.learning.attribution import (
    UNATTRIBUTED_STRATEGY,
    compute_attribution_report,
)
from backend.bot.learning.backfill import (
    SYNTHETIC_SIGNAL_SOURCE,
    SYNTHETIC_SOURCE_KIND,
    backfill_learning_from_historical_replay,
)
from backend.bot.learning.policy_apply import (
    apply_to_tunable_context,
    invalidate_cache,
    resolve_threshold,
)
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome
from backend.models.policy_tuning import PolicyTuning
from backend.models.trade import Trade


pytestmark = [pytest.mark.integration]


def _seed_closed_trade_with_matrix(
    *, ticker: str, pnl: float, days_ago: int,
    matrix_strategy: str = "long_call_5dte",
) -> int:
    """Plant one closed Trade + a paired DecisionProvenance row whose
    ``strategy_matrix_json.candidates[0].strategy_name`` is set, and
    whose Trade.strategy is the close-side ``exit_manager`` legacy
    value."""
    ts = datetime.utcnow() - timedelta(days=days_ago)
    matrix = {
        "ticker": ticker,
        "as_of": ts.isoformat(),
        "candidates": [
            {"strategy_name": matrix_strategy, "final_score": 0.85},
        ],
        "top_strategy": {"strategy_name": matrix_strategy},
    }
    with session_scope() as s:
        trade = Trade(
            timestamp=ts,
            ticker=ticker,
            action="SELL_STOCK",
            quantity=1.0,
            price=100.0,
            strategy="exit_manager",   # close-side legacy field
            signal_source="live_engine",
            confidence=0.55,
            reason="close",
            paper=1,
            pnl=pnl,
            status="closed",
            instrument="stock",
            pricing_source="alpaca",
            accounting_version=2,
            source_kind="live",
        )
        s.add(trade)
        s.flush()
        prov = DecisionProvenance(
            trade_id=int(trade.id),
            event_status="executed",
            ticker=ticker,
            decision_timestamp=ts,
            cycle_id=f"test_{trade.id}",
            strategy_matrix_json=json.dumps(matrix),
            agent_outputs_json=json.dumps([]),
            consensus_json=json.dumps({"confidence_breakdown": {}}),
            regime_vector_json=json.dumps({"trend": "trending_up"}),
            source_kind="live",
        )
        s.add(prov)
        s.flush()
        return int(trade.id)


def _seed_closed_trade_without_matrix(
    *, ticker: str, pnl: float, days_ago: int,
) -> int:
    """Plant a closed Trade + provenance with strategy_matrix_json
    NULL (the 'old' rows that surfaced as exit_manager pre-fix)."""
    ts = datetime.utcnow() - timedelta(days=days_ago)
    with session_scope() as s:
        trade = Trade(
            timestamp=ts,
            ticker=ticker,
            action="SELL_STOCK",
            quantity=1.0,
            price=100.0,
            strategy="exit_manager",
            signal_source="live_engine",
            confidence=0.55,
            reason="close",
            paper=1,
            pnl=pnl,
            status="closed",
            instrument="stock",
            pricing_source="alpaca",
            accounting_version=2,
            source_kind="live",
        )
        s.add(trade)
        s.flush()
        prov = DecisionProvenance(
            trade_id=int(trade.id),
            event_status="executed",
            ticker=ticker,
            decision_timestamp=ts,
            cycle_id=f"test_{trade.id}",
            strategy_matrix_json=None,
            agent_outputs_json=json.dumps([]),
            consensus_json=json.dumps({"confidence_breakdown": {}}),
            regime_vector_json=json.dumps({"trend": "trending_up"}),
            source_kind="live",
        )
        s.add(prov)
        s.flush()
        return int(trade.id)


# ── Gap 2 end-to-end ─────────────────────────────────────────────────


def test_gap2_attribution_surfaces_matrix_strategy(temp_db):
    """After Gap 2, compute_attribution_report's strategies list
    contains the matrix template name (NOT exit_manager) for trades
    whose entry decision wrote strategy_matrix_json. UNATTRIBUTED
    appears for trades with no matrix."""
    # 12 closed trades with matrix tagged 'long_call_5dte'.
    for i in range(12):
        _seed_closed_trade_with_matrix(
            ticker=f"M{i:02d}",
            pnl=1.5 if i % 2 == 0 else -0.5,
            days_ago=i + 1,
            matrix_strategy="long_call_5dte",
        )
    # 4 closed trades with no matrix → UNATTRIBUTED.
    for i in range(4):
        _seed_closed_trade_without_matrix(
            ticker=f"U{i:02d}",
            pnl=0.2,
            days_ago=i + 1,
        )
    report = compute_attribution_report(
        window_days=90, min_n_strategy=10,
    )
    strategy_names = {s["strategy_name"] for s in report["strategies"]}
    assert "long_call_5dte" in strategy_names
    assert "exit_manager" not in strategy_names
    assert UNATTRIBUTED_STRATEGY in strategy_names
    matrix_row = next(
        s for s in report["strategies"]
        if s["strategy_name"] == "long_call_5dte"
    )
    assert matrix_row["n_closed"] == 12
    assert matrix_row["hit_rate"] is not None
    assert matrix_row["provenance_breakdown"].get(
        "strategy_matrix_top_candidate"
    ) == 12


# ── Gap 1 end-to-end ─────────────────────────────────────────────────


def test_gap1_approve_button_wires_through_policy(temp_db, monkeypatch):
    """Synthetic policy_tunings approve + auto-apply flag flip:
    PolicyContext.scratch carries the override AND resolve_threshold
    returns it with the right source id. Demonstrates the Approve
    button is no longer inert."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    # Write a fake approved tuning row.
    with session_scope() as s:
        row = PolicyTuning(
            computed_at=datetime.utcnow(),
            rule_name="low_confidence",
            threshold_attr="config.min_confidence",
            current_value=0.60,
            recommended_value=0.30,
            recommendation_confidence="high",
            rationale="test",
            payload_json="{}",
            operator_reviewed=1,
            operator_approved=1,
        )
        s.add(row)
        s.flush()
        row_id = int(row.id)
    # Build a minimal PolicyContext.
    ctx = PolicyContext(
        ticker="AAPL",
        signal=None, event={}, data={}, analytics_cfg={},
        ai_config={}, config={}, kill_active=False,
        portfolio_risk_dict=None, eod_bias_map={},
        brain_cooldown={}, use_brain=False, cycle_id="test",
    )
    apply_to_tunable_context(ctx)
    assert ctx.scratch["applied_thresholds"].get(
        "config.min_confidence"
    ) == pytest.approx(0.30)
    value, evidence = resolve_threshold(
        ctx, threshold_attr="config.min_confidence",
        tunable_default=0.60,
    )
    assert value == pytest.approx(0.30)
    assert evidence["threshold_source"] == f"policy_tunings_id_{row_id}"
    # Flip OFF — instantly disables on next cycle.
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", False,
    )
    invalidate_cache()
    ctx2 = PolicyContext(
        ticker="AAPL",
        signal=None, event={}, data={}, analytics_cfg={},
        ai_config={}, config={}, kill_active=False,
        portfolio_risk_dict=None, eod_bias_map={},
        brain_cooldown={}, use_brain=False, cycle_id="test2",
    )
    apply_to_tunable_context(ctx2)
    assert ctx2.scratch["applied_thresholds"] == {}


# ── Gap 4 end-to-end ─────────────────────────────────────────────────


def test_gap4_backfill_writes_synthetic_and_default_excludes(
    temp_db, monkeypatch,
):
    """Full backfill cycle: seed corpus → flag-gated run → confirm
    synthetic rows exist BUT default attribution ignores them."""
    monkeypatch.setattr(TUNABLES, "learning_backfill_enabled", True)
    monkeypatch.setenv("TB_LEARNING_BACKFILL_ENABLED", "1")
    # Seed corpus.
    base_ts = datetime.utcnow() - timedelta(days=30)
    with session_scope() as s:
        for i in range(15):
            obs = MarketObservation(
                ticker=f"BK{i:02d}",
                pattern=f"pattern_{i % 3}",
                timestamp=base_ts + timedelta(hours=i),
                timeframe="1d",
                regime="trending_up",
                vol_state="normal",
                source="historical_replay",
                direction="long",
            )
            s.add(obs)
            s.flush()
            s.add(MarketOutcome(
                observation_id=int(obs.id),
                horizon="1d",
                entry_price=100.0,
                exit_price=100.0 + (i % 3) - 1.0,
                return_pct=((i % 3) - 1.0) / 100.0,
                was_winner=(i % 3) > 1,
            ))
        s.flush()
    # Dry-run first.
    dry = backfill_learning_from_historical_replay(
        days_back=90, max_synthetic_rows=10, dry_run=True,
    )
    assert dry.dry_run is True
    assert dry.n_to_write > 0
    assert dry.n_written == 0
    # Live run.
    live = backfill_learning_from_historical_replay(
        days_back=90, max_synthetic_rows=10, dry_run=False,
    )
    assert live.n_written > 0
    # Default attribution must NOT see the synthetic rows.
    default_rep = compute_attribution_report(window_days=90)
    assert default_rep["include_synthetic"] is False
    # With opt-in, synthetic rows participate.
    incl_rep = compute_attribution_report(
        window_days=90, include_synthetic=True,
    )
    assert (
        incl_rep["n_closed_decisions"] >=
        default_rep["n_closed_decisions"] + 1
    )
