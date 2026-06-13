"""P2.4 — cohort matrix research priors unit tests."""
from __future__ import annotations

from backend.bot.cohort_matrix.priors import (
    COHORT_PRIORS,
    FALLBACK_PRIOR_N,
    FALLBACK_PRIOR_WR,
    apply_priors_to_cells,
    blend,
    list_priors,
    lookup_prior,
)


def test_catalog_is_loaded():
    assert len(COHORT_PRIORS) >= 10
    strategies = {p.strategy for p in COHORT_PRIORS}
    assert "cash_secured_put" in strategies
    assert "iron_condor" in strategies


def test_lookup_exact_match():
    p = lookup_prior("cash_secured_put", "uptrend", "—")
    assert p is not None
    assert 0.6 < p.prior_win_rate < 0.85


def test_lookup_falls_through_grade_wildcard():
    # No grade-A entry — should fall through to '—'.
    p = lookup_prior("cash_secured_put", "uptrend", "A")
    assert p is not None
    assert p.grade == "—"


def test_lookup_no_match_returns_none():
    p = lookup_prior("nonexistent_strategy", "uptrend", "A")
    assert p is None


def test_blend_pure_prior_when_no_observations():
    # obs_n=0 → posterior == prior
    p = lookup_prior("cash_secured_put", "uptrend", "—")
    result = blend(obs_win_rate=None, obs_n=0, prior=p)
    assert result["posterior_win_rate"] == round(p.prior_win_rate, 4)
    assert result["obs_n"] == 0


def test_blend_pulls_toward_observation_as_n_grows():
    p = lookup_prior("cash_secured_put", "uptrend", "—")
    # Prior is ~0.74, n=15. Observe 200 trades with 50% win rate.
    result = blend(obs_win_rate=0.5, obs_n=200, prior=p)
    # Posterior should be much closer to 0.5 than to 0.74.
    assert abs(result["posterior_win_rate"] - 0.5) < 0.05
    assert result["obs_n"] == 200


def test_blend_uses_fallback_when_no_prior():
    result = blend(obs_win_rate=0.7, obs_n=10, prior=None,
                       baseline_wr=0.5)
    assert result["source"] == "fallback_baseline"
    # Posterior = (5 * 0.5 + 10 * 0.7) / 15 = 0.633...
    assert abs(result["posterior_win_rate"] - (5 * 0.5 + 10 * 0.7) / 15) < 1e-3


def test_blend_uses_default_baseline_when_no_baseline_given():
    result = blend(obs_win_rate=None, obs_n=0, prior=None)
    assert result["posterior_win_rate"] == FALLBACK_PRIOR_WR
    assert result["prior_n"] == FALLBACK_PRIOR_N


def test_apply_priors_decorates_cells():
    cells = [
        {"strategy": "cash_secured_put", "regime": "uptrend", "grade": "—",
         "win_rate": 0.6, "closed": 10},
        {"strategy": "iron_condor", "regime": "trending", "grade": "—",
         "win_rate": 0.2, "closed": 5},
    ]
    decorated = apply_priors_to_cells(cells, baseline_wr=0.5)
    assert len(decorated) == 2
    for d in decorated:
        assert "prior" in d
        assert "posterior_win_rate" in d
        assert d["prior"]["source"] in ("curated_research", "fallback_baseline")


def test_apply_priors_does_not_mutate_input():
    cells = [{"strategy": "cash_secured_put", "regime": "uptrend",
                  "grade": "—", "win_rate": 0.6, "closed": 10}]
    snapshot = cells[0].copy()
    apply_priors_to_cells(cells)
    assert cells[0] == snapshot, "original cell dict should be untouched"


def test_list_priors_exposes_citations():
    listing = list_priors()
    assert listing, "list_priors should return at least one row"
    assert all("citation" in row for row in listing)
    assert all(row["citation"] for row in listing), \
        "every prior must carry a citation"
