"""MITS Phase 18-FU Gap 2 — strategy attribution unit tests.

Covers ``_extract_strategy_from_matrix``, ``_resolve_strategy_attribution``,
the ``_ClosedDecision.strategy_provenance`` field, and the
``compute_strategy_calibration`` end-to-end change that surfaces
non-``exit_manager`` strategies and provenance_breakdown.

Honesty guardrails the suite exercises:

  * Empty / missing candidates[] → UNATTRIBUTED (not silent default).
  * Trade.strategy='exit_manager' alone → UNATTRIBUTED + clear note.
  * Non-exit-side Trade.strategy fallback works when matrix is absent.
  * Back-compat: dataclass shape preserved (strategy_name, n_closed,
    etc.); the new provenance_breakdown is additive.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

import pytest

from backend.bot.learning.attribution import (
    UNATTRIBUTED_STRATEGY,
    StrategyCalibration,
    _ClosedDecision,
    _extract_strategy_from_matrix,
    _provenance_breakdown,
    _resolve_strategy_attribution,
    compute_strategy_calibration,
)


pytestmark = [pytest.mark.unit]


# ── Helpers ──────────────────────────────────────────────────────────


def _decision(
    *,
    trade_id: int,
    pnl_pct: float,
    strategy_name: str,
    strategy_provenance: str,
    days_ago: int = 1,
    regime_trend: Optional[str] = "trending_up",
) -> _ClosedDecision:
    return _ClosedDecision(
        trade_id=trade_id,
        pnl_pct=pnl_pct,
        pnl_raw=pnl_pct * 100.0,
        win=1 if pnl_pct > 0 else 0,
        decision_timestamp=datetime.utcnow() - timedelta(days=days_ago),
        agent_outputs=[],
        consensus={},
        confidence_breakdown={},
        strategy_name=strategy_name,
        regime_trend=regime_trend,
        strategy_provenance=strategy_provenance,
    )


# ── _extract_strategy_from_matrix ─────────────────────────────────────


def test_extract_from_candidates_first_when_populated():
    """The 15.C builder sorts candidates by final_score desc, so
    candidates[0] is the winner."""
    matrix = {
        "ticker": "AAPL",
        "candidates": [
            {"strategy_name": "long_call_5dte", "final_score": 0.92},
            {"strategy_name": "covered_call", "final_score": 0.71},
        ],
        "top_strategy": {"strategy_name": "long_call_5dte"},
    }
    assert _extract_strategy_from_matrix(matrix) == "long_call_5dte"


def test_extract_falls_back_to_top_strategy_when_candidates_missing():
    """When ``candidates[]`` is missing or empty, the mirror field
    on the matrix gets used instead."""
    matrix = {
        "ticker": "AAPL",
        "candidates": [],
        "top_strategy": {"strategy_name": "iron_condor"},
    }
    assert _extract_strategy_from_matrix(matrix) == "iron_condor"


def test_extract_returns_none_when_both_missing():
    matrix = {"ticker": "AAPL", "candidates": [], "top_strategy": {}}
    assert _extract_strategy_from_matrix(matrix) is None


def test_extract_none_on_non_dict_input():
    """Defensive: bad input returns None, never raises."""
    assert _extract_strategy_from_matrix(None) is None
    assert _extract_strategy_from_matrix("not_a_dict") is None
    assert _extract_strategy_from_matrix([]) is None


# ── _resolve_strategy_attribution ────────────────────────────────────


def test_resolve_uses_matrix_when_available():
    name, prov = _resolve_strategy_attribution(
        strategy_matrix={
            "candidates": [{"strategy_name": "covered_call"}],
        },
        trade_strategy="exit_manager",
    )
    assert name == "covered_call"
    assert prov == "strategy_matrix_top_candidate"


def test_resolve_top_strategy_field_provenance():
    name, prov = _resolve_strategy_attribution(
        strategy_matrix={
            "candidates": [],
            "top_strategy": {"strategy_name": "bull_call_spread"},
        },
        trade_strategy="exit_manager",
    )
    assert name == "bull_call_spread"
    assert prov == "strategy_matrix_top_strategy_field"


def test_resolve_fallback_to_trade_strategy_when_matrix_absent():
    name, prov = _resolve_strategy_attribution(
        strategy_matrix=None,
        trade_strategy="custom_strategy",
    )
    assert name == "custom_strategy"
    assert prov == "fallback_trade_strategy"


def test_resolve_unattributed_when_no_matrix_and_exit_manager():
    """The load-bearing case the Gap 2 fix addresses."""
    name, prov = _resolve_strategy_attribution(
        strategy_matrix=None,
        trade_strategy="exit_manager",
    )
    assert name == UNATTRIBUTED_STRATEGY
    assert prov == "unattributed_no_strategy_matrix"


def test_resolve_unattributed_when_no_matrix_and_empty_trade_strategy():
    name, prov = _resolve_strategy_attribution(
        strategy_matrix=None, trade_strategy="",
    )
    assert name == UNATTRIBUTED_STRATEGY
    assert prov == "unattributed_no_strategy_matrix"


def test_resolve_unattributed_when_no_matrix_and_none_trade_strategy():
    name, prov = _resolve_strategy_attribution(
        strategy_matrix=None, trade_strategy=None,
    )
    assert name == UNATTRIBUTED_STRATEGY
    assert prov == "unattributed_no_strategy_matrix"


# ── compute_strategy_calibration end-to-end ──────────────────────────


def test_strategy_calibration_surfaces_real_strategy_names():
    """Sample with 10 closed decisions all sharing the same matrix
    strategy. After 18-FU Gap 2, the calibration emits one bucket
    keyed by the matrix strategy (not 'exit_manager')."""
    decisions = [
        _decision(
            trade_id=i, pnl_pct=2.5 if i % 2 == 0 else -1.0,
            strategy_name="long_call_5dte",
            strategy_provenance="strategy_matrix_top_candidate",
        )
        for i in range(12)
    ]
    out = compute_strategy_calibration(decisions=decisions, min_n=10)
    names = {c.strategy_name for c in out}
    assert "long_call_5dte" in names
    assert "exit_manager" not in names
    bucket = next(c for c in out if c.strategy_name == "long_call_5dte")
    assert bucket.n_closed == 12
    assert bucket.hit_rate is not None
    # Provenance breakdown surfaces the entry-side count.
    assert bucket.provenance_breakdown.get(
        "strategy_matrix_top_candidate"
    ) == 12


def test_strategy_calibration_unattributed_bucket():
    """Closed trades without entry-side matrix surface in the
    UNATTRIBUTED sentinel bucket with a clear note."""
    decisions = [
        _decision(
            trade_id=i, pnl_pct=0.5,
            strategy_name=UNATTRIBUTED_STRATEGY,
            strategy_provenance="unattributed_no_strategy_matrix",
        )
        for i in range(15)
    ]
    out = compute_strategy_calibration(decisions=decisions, min_n=10)
    bucket = next(c for c in out if c.strategy_name == UNATTRIBUTED_STRATEGY)
    assert "no_strategy_matrix_at_entry" in bucket.notes
    assert bucket.n_closed == 15


def test_strategy_calibration_back_compat_dataclass_shape():
    """The dataclass still carries the pre-18-FU fields so existing
    UI consumers don't break. provenance_breakdown is additive."""
    decisions = [
        _decision(
            trade_id=i, pnl_pct=1.0,
            strategy_name="covered_call",
            strategy_provenance="strategy_matrix_top_candidate",
        )
        for i in range(11)
    ]
    out = compute_strategy_calibration(decisions=decisions, min_n=10)
    assert len(out) == 1
    bucket = out[0]
    d = bucket.to_dict()
    # Original fields all present.
    for key in (
        "strategy_name", "n_closed", "hit_rate",
        "hit_rate_wilson_lower", "hit_rate_wilson_upper",
        "mean_pnl_pct", "median_pnl_pct", "by_regime",
        "sample_age_days", "notes",
    ):
        assert key in d
    # New field also present.
    assert "provenance_breakdown" in d
    assert isinstance(d["provenance_breakdown"], dict)


def test_strategy_calibration_mixed_provenance_breakdown():
    """A bucket with mixed entry-side + fallback rows should record
    each provenance separately so the operator can spot fallback-heavy
    buckets."""
    decisions = []
    for i in range(8):
        decisions.append(_decision(
            trade_id=i, pnl_pct=1.0,
            strategy_name="custom_strategy",
            strategy_provenance="strategy_matrix_top_candidate",
        ))
    for i in range(5):
        decisions.append(_decision(
            trade_id=100 + i, pnl_pct=-0.5,
            strategy_name="custom_strategy",
            strategy_provenance="fallback_trade_strategy",
        ))
    out = compute_strategy_calibration(decisions=decisions, min_n=10)
    bucket = next(c for c in out if c.strategy_name == "custom_strategy")
    assert bucket.provenance_breakdown.get(
        "strategy_matrix_top_candidate"
    ) == 8
    assert bucket.provenance_breakdown.get(
        "fallback_trade_strategy"
    ) == 5


def test_provenance_breakdown_counts_helper():
    decisions = [
        _decision(
            trade_id=i, pnl_pct=0.1,
            strategy_name="X",
            strategy_provenance="strategy_matrix_top_candidate",
        )
        for i in range(3)
    ] + [
        _decision(
            trade_id=10, pnl_pct=0.1,
            strategy_name="X",
            strategy_provenance="fallback_trade_strategy",
        ),
    ]
    counts = _provenance_breakdown(decisions)
    assert counts == {
        "strategy_matrix_top_candidate": 3,
        "fallback_trade_strategy": 1,
    }


def test_strategy_calibration_unattributed_below_min_n_carries_note():
    """An UNATTRIBUTED bucket BELOW min_n still surfaces the note so
    the operator sees the gap, not silence."""
    decisions = [
        _decision(
            trade_id=i, pnl_pct=0.1,
            strategy_name=UNATTRIBUTED_STRATEGY,
            strategy_provenance="unattributed_no_strategy_matrix",
        )
        for i in range(3)
    ]
    out = compute_strategy_calibration(decisions=decisions, min_n=10)
    bucket = next(c for c in out if c.strategy_name == UNATTRIBUTED_STRATEGY)
    assert any(
        "insufficient_sample_size" in n for n in bucket.notes
    )
    assert "no_strategy_matrix_at_entry" in bucket.notes
