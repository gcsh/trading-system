"""MITS Phase 16.D — individual opportunity-committee reviewer behavior.

Each reviewer is tested with fixtures that target a single decision
branch. The bulk-blender tests in test_opportunity_committee.py cover
the composite path; this file covers the leaves.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.bot.ai.opportunity_brain import OpportunityHypothesis
from backend.bot.decision.opportunity_committee import (
    STANCE_ABSTAIN,
    STANCE_REJECT,
    STANCE_SUPPORT,
    agent_opportunity_analog,
    agent_opportunity_devils_advocate,
    agent_opportunity_risk,
)


@pytest.fixture(autouse=True)
def _stub_pgvector(monkeypatch):
    import backend.bot.ai.vector_store as vs
    monkeypatch.setattr(vs, "embed", lambda text: [0.1] * 384)
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])


def _hyp(**kw) -> OpportunityHypothesis:
    base = dict(
        ticker="QQQ", direction="long_put", dte_bucket="1d",
        conviction=0.75, regime_state="capitulation",
        thesis="t", notes="n", from_cache=False,
    )
    base.update(kw)
    return OpportunityHypothesis(**base)


def _ctx(**kw) -> dict:
    base = {
        "snapshot": {"price": 380.0, "vix": 25.0, "iv_rank": 60},
        "regime_state": "capitulation",
        "account": SimpleNamespace(
            portfolio_value=5000.0, drawdown_pct=0.0),
        "opportunistic_concurrent_open": 0,
        "live_context": {},
        "simulator_verdict": {},
    }
    base.update(kw)
    return base


# ── risk reviewer ──────────────────────────────────────────────────────


def test_risk_supports_clean_defined_risk_long_put():
    vote = agent_opportunity_risk(_hyp(), _ctx())
    assert vote.agent == "opportunity_risk"
    assert vote.stance == STANCE_SUPPORT
    assert 0.5 <= vote.confidence <= 1.0
    assert any("defined-risk" in s for s in vote.supporting_factors)


def test_risk_rejects_undefined_risk_direction():
    vote = agent_opportunity_risk(_hyp(direction="naked_short"), _ctx())
    assert vote.stance == STANCE_REJECT
    assert any("defined-risk" in c for c in vote.concerns)


def test_risk_rejects_when_concurrency_cap_hit():
    vote = agent_opportunity_risk(
        _hyp(), _ctx(opportunistic_concurrent_open=10),
    )
    assert vote.stance == STANCE_REJECT
    assert any("concurrent_open" in c for c in vote.concerns)


def test_risk_rejects_0dte_with_panic_vix():
    vote = agent_opportunity_risk(
        _hyp(dte_bucket="0d"),
        _ctx(snapshot={"price": 380.0, "vix": 38.0, "iv_rank": 80}),
    )
    assert vote.stance == STANCE_REJECT
    assert any("0DTE" in c or "VIX" in c for c in vote.concerns)


def test_risk_allows_0dte_when_vix_is_normal():
    vote = agent_opportunity_risk(
        _hyp(dte_bucket="0d"),
        _ctx(snapshot={"price": 380.0, "vix": 22.0, "iv_rank": 60}),
    )
    assert vote.stance == STANCE_SUPPORT


# ── analog reviewer ────────────────────────────────────────────────────


def _stub_cohort(monkeypatch, *, n: int, mean_pct: float,
                  sector_fallback: bool = False):
    import backend.bot.ai.vector_store as vs
    import backend.bot.corpus.analog_retrieval as ar
    from backend.bot.corpus.analog_retrieval import AnalogHit

    monkeypatch.setattr(
        vs, "similarity_search",
        lambda ns, vec, k=None: [SimpleNamespace(metadata={"date": "2025-01-01"})],
    )
    rows = [
        AnalogHit(
            observation_id=i, ticker="QQQ",
            timestamp=datetime(2025, 1, 1),
            distance=0.1, cosine=0.9,
            regime_label="capitulation",
            pattern_set=[],
            realized_return_pct=mean_pct + (i - n / 2) * 0.5,
            horizon="1d",
        )
        for i in range(n)
    ]

    def _stub(hits, *, ticker, horizon):
        if sector_fallback:
            return rows[:3] if ticker == "QQQ" else rows
        return rows if ticker == "QQQ" else []

    monkeypatch.setattr(ar, "_outcomes_for_hits", _stub)


def test_analog_rejects_below_minimum_cohort(monkeypatch):
    _stub_cohort(monkeypatch, n=2, mean_pct=2.0)
    vote = agent_opportunity_analog(_hyp(), _ctx())
    assert vote.stance == STANCE_REJECT
    assert any("cohort_size" in c for c in vote.concerns)


def test_analog_weak_support_between_3_and_8(monkeypatch):
    _stub_cohort(monkeypatch, n=5, mean_pct=2.0)
    vote = agent_opportunity_analog(_hyp(), _ctx())
    assert vote.stance == STANCE_SUPPORT
    assert any("weak precedent" in s for s in vote.supporting_factors)


def test_analog_strong_support_at_or_above_8(monkeypatch):
    _stub_cohort(monkeypatch, n=10, mean_pct=2.0)
    vote = agent_opportunity_analog(_hyp(), _ctx())
    assert vote.stance == STANCE_SUPPORT
    assert any("strong precedent" in s for s in vote.supporting_factors)
    # Confidence reflects strong precedent + positive mean.
    assert vote.confidence >= 0.80


def test_analog_sector_fallback_logged_as_concern(monkeypatch):
    _stub_cohort(monkeypatch, n=10, mean_pct=2.0, sector_fallback=True)
    vote = agent_opportunity_analog(_hyp(), _ctx())
    # Soft penalty per spec — concern logged, stance stays SUPPORT.
    assert vote.stance == STANCE_SUPPORT
    assert any("sector fallback" in c for c in vote.concerns)


def test_analog_negative_mean_drags_confidence(monkeypatch):
    _stub_cohort(monkeypatch, n=10, mean_pct=-3.0)
    vote_neg = agent_opportunity_analog(_hyp(), _ctx())
    _stub_cohort(monkeypatch, n=10, mean_pct=3.0)
    vote_pos = agent_opportunity_analog(_hyp(), _ctx())
    assert vote_neg.confidence < vote_pos.confidence


# ── devils-advocate reviewer ───────────────────────────────────────────


def test_devils_supports_aligned_long_put_on_panic():
    """long_put on capitulation regime is aligned (short-side bet on
    bearish tape); devils-advocate has nothing to red-team against
    given a low-vix snapshot → HOLD inner stance → SUPPORT."""
    vote = agent_opportunity_devils_advocate(
        _hyp(direction="long_put"),
        _ctx(snapshot={"price": 380.0, "vix": 22.0, "iv_rank": 60}),
    )
    assert vote.stance == STANCE_SUPPORT


def test_devils_rejects_long_signal_into_bearish_tape():
    """long_call on capitulation regime → inner devils-advocate fires
    'long signal but tape is bearish' → SELL → projects to REJECT."""
    vote = agent_opportunity_devils_advocate(
        _hyp(direction="long_call"),
        _ctx(),
    )
    assert vote.stance == STANCE_REJECT


def test_devils_abstain_on_multiple_concerns_projects_to_reject():
    """Many concerns → inner agent ABSTAINS with high confidence →
    committee projects to REJECT."""
    # Drive multiple concerns: long_call + bearish trend + earnings + vix.
    vote = agent_opportunity_devils_advocate(
        _hyp(direction="long_call"),
        _ctx(snapshot={
            "price": 100.0, "vix": 30.0, "iv_rank": 85,
            "earnings_days": 1,
        }),
    )
    # Two+ concerns inside → ABSTAIN with confidence ~0.75 → REJECT.
    assert vote.stance == STANCE_REJECT
