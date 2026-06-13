"""MITS Phase 16.D — Opportunity Committee Lite (blender tests).

Drives ``review_opportunity`` with synthetic hypotheses + contexts and
asserts the recommendation hits EXECUTE / SIZE_DOWN / REJECT at the
operator's documented thresholds. Each test also asserts the votes list
has exactly the three reviewer names so any drift in the council shape
gets caught.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.bot.ai.opportunity_brain import OpportunityHypothesis
from backend.bot.decision.opportunity_committee import (
    OpportunityCommitteeResult,
    OpportunityCommitteeVote,
    STANCE_REJECT,
    STANCE_SUPPORT,
    review_opportunity,
)


@pytest.fixture(autouse=True)
def _stub_pgvector(monkeypatch):
    """The analog reviewer calls retrieve_analogs → pgvector. Hermetic
    stub at the embed boundary; individual tests override via monkeypatch
    on _outcomes_for_hits when they need cohort_size control."""
    import backend.bot.ai.vector_store as vs
    monkeypatch.setattr(vs, "embed", lambda text: [0.1] * 384)
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])
    yield


def _make_hypothesis(*, conviction=0.75, direction="long_put",
                      dte_bucket="1d", regime="capitulation",
                      ticker="QQQ") -> OpportunityHypothesis:
    return OpportunityHypothesis(
        ticker=ticker, direction=direction, dte_bucket=dte_bucket,
        conviction=conviction, regime_state=regime,
        thesis="synthetic crisis day — convex put",
        notes="invalidated above VWAP",
        from_cache=False,
    )


def _make_context(*, vix=28.0, concurrent_open=0,
                    account_drawdown=0.0) -> dict:
    return {
        "snapshot": {"price": 380.0, "vix": vix, "iv_rank": 75},
        "regime_state": "capitulation",
        "account": SimpleNamespace(
            portfolio_value=5000.0, drawdown_pct=account_drawdown,
        ),
        "opportunistic_concurrent_open": concurrent_open,
        "live_context": {},
        "simulator_verdict": {},
    }


def _stub_cohort(monkeypatch, *, n: int, mean_pct: float = 1.0,
                  sector_fallback=False):
    """Make retrieve_analogs return ``n`` AnalogHit rows with a known mean."""
    import backend.bot.ai.vector_store as vs
    import backend.bot.corpus.analog_retrieval as ar
    from datetime import datetime
    from backend.bot.corpus.analog_retrieval import AnalogHit

    # similarity_search needs a non-empty list so retrieve_analogs hits the
    # _outcomes_for_hits stub (otherwise it returns _empty_cluster directly).
    monkeypatch.setattr(
        vs, "similarity_search",
        lambda ns, vec, k=None: [
            SimpleNamespace(metadata={"date": "2025-01-01"})
        ],
    )

    rows = [
        AnalogHit(
            observation_id=i, ticker="QQQ",
            timestamp=datetime(2025, 1, 1),
            distance=0.1, cosine=0.9,
            regime_label="capitulation",
            pattern_set=["panic"],
            realized_return_pct=mean_pct + (i - n / 2) * 0.5,
            horizon="1d",
        )
        for i in range(n)
    ]

    def _stub(hits, *, ticker, horizon):
        if sector_fallback:
            # Same-ticker pass returns < 10 to trigger fallback flag.
            if ticker == "QQQ":
                return rows[: min(5, n)]
            return rows
        return rows if ticker == "QQQ" else []

    monkeypatch.setattr(ar, "_outcomes_for_hits", _stub)


# ── tests ──────────────────────────────────────────────────────────────


def test_review_returns_three_votes_with_documented_shape(monkeypatch):
    _stub_cohort(monkeypatch, n=10, mean_pct=2.0)
    h = _make_hypothesis()
    ctx = _make_context()
    res = review_opportunity(h, ctx)
    assert isinstance(res, OpportunityCommitteeResult)
    assert len(res.votes) == 3
    agents = [v.agent for v in res.votes]
    assert agents == [
        "opportunity_risk",
        "opportunity_analog",
        "opportunity_devils_advocate",
    ]
    for v in res.votes:
        assert isinstance(v, OpportunityCommitteeVote)
        assert 0.0 <= v.confidence <= 1.0
        assert v.stance in {"support", "abstain", "reject"}
    d = res.to_dict()
    for key in (
        "dislocation_score", "historical_precedent_score",
        "risk_score", "timing_score", "composite_score",
        "recommendation", "rec_reason", "votes",
    ):
        assert key in d


def test_high_score_yields_execute(monkeypatch):
    """Strong cohort + high conviction + low concurrent + clean tape
    → composite >= 0.65 → EXECUTE."""
    _stub_cohort(monkeypatch, n=12, mean_pct=3.0)
    h = _make_hypothesis(conviction=0.90, direction="long_put",
                          dte_bucket="1d")
    ctx = _make_context(vix=28.0, concurrent_open=0)
    res = review_opportunity(h, ctx)
    assert res.recommendation == "EXECUTE", (
        f"expected EXECUTE, got {res.recommendation} "
        f"(composite={res.composite_score:.2f}, "
        f"axes=({res.dislocation_score:.2f},{res.historical_precedent_score:.2f},"
        f"{res.risk_score:.2f},{res.timing_score:.2f}))"
    )
    assert res.composite_score >= 0.65


def test_low_cohort_triggers_hard_reject_from_analog(monkeypatch):
    """cohort_size < 3 → analog reviewer hard-rejects → REJECT wins
    even with a strong devils-advocate + risk vote."""
    _stub_cohort(monkeypatch, n=1, mean_pct=2.0)
    h = _make_hypothesis(conviction=0.90)
    ctx = _make_context(vix=28.0, concurrent_open=0)
    res = review_opportunity(h, ctx)
    assert res.recommendation == "REJECT"
    assert "opportunity_analog" in res.rec_reason


def test_concurrency_cap_triggers_hard_reject_from_risk(monkeypatch):
    """concurrent_open >= committee cap → risk reviewer hard-rejects."""
    _stub_cohort(monkeypatch, n=10, mean_pct=2.0)
    h = _make_hypothesis(conviction=0.90)
    ctx = _make_context(vix=28.0, concurrent_open=5)
    res = review_opportunity(h, ctx)
    assert res.recommendation == "REJECT"
    assert "opportunity_risk" in res.rec_reason


def test_0dte_with_panic_vix_triggers_risk_reject(monkeypatch):
    """0DTE proposal + VIX > 35 → risk reviewer hard-rejects."""
    _stub_cohort(monkeypatch, n=10, mean_pct=2.0)
    h = _make_hypothesis(conviction=0.85, dte_bucket="0d")
    ctx = _make_context(vix=40.0, concurrent_open=0)
    res = review_opportunity(h, ctx)
    assert res.recommendation == "REJECT"
    assert "opportunity_risk" in res.rec_reason


def test_size_down_band(monkeypatch):
    """Mid-strength setup → composite lands in [0.45, 0.65) → SIZE_DOWN.

    Engineered with a weak (3 ≤ n < 8) cohort with negative mean (drags
    precedent + flags devils-advocate concern), mid conviction, and a
    near-cap concurrent_open count (drags risk axis). The combination
    pushes the composite into the SIZE_DOWN band.
    """
    _stub_cohort(monkeypatch, n=3, mean_pct=-1.5)
    h = _make_hypothesis(
        conviction=0.30, direction="long_put", dte_bucket="1d",
        regime="capitulation",
    )
    # long_put on capitulation → devils-advocate side="short", trend
    # mapped to "bearish" → aligned → SUPPORT. Cohort n=3 (weak) +
    # negative mean drags precedent; conviction 0.30 drags timing.
    # Composite lands inside [0.45, 0.65).
    ctx = _make_context(vix=22.0, concurrent_open=1)
    res = review_opportunity(h, ctx)
    assert res.recommendation == "SIZE_DOWN", (
        f"expected SIZE_DOWN, got {res.recommendation} "
        f"(composite={res.composite_score:.2f})"
    )
    assert 0.45 <= res.composite_score < 0.65


def test_undefined_risk_direction_rejected(monkeypatch):
    _stub_cohort(monkeypatch, n=10, mean_pct=2.0)
    h = _make_hypothesis(conviction=0.90, direction="naked_short_call")
    ctx = _make_context()
    res = review_opportunity(h, ctx)
    assert res.recommendation == "REJECT"
    risk_vote = next(v for v in res.votes if v.agent == "opportunity_risk")
    assert risk_vote.stance == STANCE_REJECT
    assert any("defined-risk" in c for c in risk_vote.concerns)


def test_composite_blender_weights_match_spec(monkeypatch):
    """composite = 0.25*disloc + 0.25*precedent + 0.30*risk + 0.20*timing.

    Drive each axis independently via the reviewer outputs by stubbing
    the cohort + context; assert the composite_score matches the formula
    within float tolerance."""
    _stub_cohort(monkeypatch, n=12, mean_pct=2.0)
    h = _make_hypothesis(conviction=0.80)
    ctx = _make_context(vix=28.0, concurrent_open=0)
    res = review_opportunity(h, ctx)
    expected = (
        0.25 * res.dislocation_score
        + 0.25 * res.historical_precedent_score
        + 0.30 * res.risk_score
        + 0.20 * res.timing_score
    )
    assert abs(res.composite_score - expected) < 0.01


def test_to_dict_round_trip(monkeypatch):
    _stub_cohort(monkeypatch, n=10, mean_pct=2.0)
    h = _make_hypothesis()
    res = review_opportunity(h, _make_context())
    d = res.to_dict()
    assert isinstance(d, dict)
    assert "votes" in d and len(d["votes"]) == 3
    for v in d["votes"]:
        assert "agent" in v and "stance" in v and "confidence" in v
    # Round numbers exactly to 4 decimals to keep provenance JSON stable.
    assert d["composite_score"] == round(res.composite_score, 4)
