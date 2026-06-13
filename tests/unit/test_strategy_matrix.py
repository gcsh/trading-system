"""MITS Phase 15.C — StrategyMatrix matcher tests.

The KG fallback + analog retrieval are stubbed via monkeypatch so the
tests are hermetic: no DB, no pgvector, no live RegimeVector build.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Dict, Optional

import pytest

from backend.bot.analysis import strategy_matrix as sm_mod
from backend.bot.analysis.strategy_matrix import (
    StrategyMatrix, build_strategy_matrix,
)
from backend.bot.analysis.strategy_templates import (
    load_strategy_templates, reset_templates_cache,
)
from backend.bot.corpus.analog_retrieval import AnalogCluster, AnalogHit


# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_templates_cache():
    reset_templates_cache()
    yield
    reset_templates_cache()


def _rv(*, trend: str = "bullish", vol: str = "normal",
        intraday: str = "normal", gamma: str = "long_gamma",
        iv_regime: str = "stable_low", macro: str = "expansion",
        health: str = "green"):
    """Minimal RegimeVector-shaped namespace for the matcher."""
    return SimpleNamespace(
        ticker="AAPL",
        trend=SimpleNamespace(value=trend),
        volatility_state=SimpleNamespace(value=vol),
        iv_rank=SimpleNamespace(value=None),
        iv_regime=SimpleNamespace(value=iv_regime),
        intraday_regime=SimpleNamespace(value=intraday),
        gamma_state=SimpleNamespace(value={"regime": gamma}),
        macro_regime=SimpleNamespace(value=macro),
        health=health,
    )


def _analog_cluster(*, n: int = 30, win_rate: float = 0.6,
                    mean_payoff: float = 2.0,
                    p_max_loss_frac: float = 0.1) -> AnalogCluster:
    """Build an AnalogCluster with `n` rows whose realized_return_pct is
    arranged so that ``win_rate`` fraction are > 0 and ``p_max_loss_frac``
    fraction are < -10."""
    wins = int(round(n * win_rate))
    losses = int(round(n * p_max_loss_frac))
    others = n - wins - losses
    if others < 0:
        others = 0
        wins = n - losses
    rows = []
    for i in range(wins):
        rows.append(AnalogHit(
            observation_id=i, ticker="AAPL",
            timestamp=datetime(2025, 1, 1),
            distance=0.1, cosine=0.9, regime_label="bullish",
            pattern_set=["bull_flag"], realized_return_pct=2.0,
            horizon="5d",
        ))
    for i in range(losses):
        rows.append(AnalogHit(
            observation_id=1000 + i, ticker="AAPL",
            timestamp=datetime(2025, 1, 1),
            distance=0.1, cosine=0.9, regime_label="bullish",
            pattern_set=["bull_flag"], realized_return_pct=-15.0,
            horizon="5d",
        ))
    for i in range(others):
        rows.append(AnalogHit(
            observation_id=2000 + i, ticker="AAPL",
            timestamp=datetime(2025, 1, 1),
            distance=0.1, cosine=0.9, regime_label="bullish",
            pattern_set=["bull_flag"], realized_return_pct=-0.5,
            horizon="5d",
        ))
    return AnalogCluster(
        query_state={"ticker": "AAPL", "regime": "bullish",
                       "vol_state": "normal", "pattern": "bull_flag",
                       "horizon": "5d", "k": 50},
        analogs=rows,
        outcome_distribution={"mean": mean_payoff, "std": 1.0},
        cohort_size=n,
        sector_fallback_used=False,
        freshness_seconds=0.0,
    )


def _kg_stub(*, posterior: float = 0.62, n: int = 120,
             lower: float = 0.55, upper: float = 0.69,
             source: str = "cell"):
    """Returns a fake ``get_posterior_with_fallback`` impl + a recorder."""
    calls = []

    def _impl(*, ticker, pattern, regime, vol_state,
              time_bucket="rth", horizon="5d", sample_split="combined"):
        calls.append({
            "ticker": ticker, "pattern": pattern, "regime": regime,
            "vol_state": vol_state, "time_bucket": time_bucket,
            "horizon": horizon, "sample_split": sample_split,
        })
        return {
            "posterior": posterior, "n": n,
            "confidence_lower": lower, "confidence_upper": upper,
            "source": source,
        }
    return _impl, calls


# ── tests ───────────────────────────────────────────────────────────────


def test_templates_load_count_is_ten():
    """All 10 YAML templates load without raising and round-trip
    through pydantic."""
    tpls = load_strategy_templates()
    assert len(tpls) == 10
    expected = {
        "long_call", "long_put", "bull_put_spread", "bear_call_spread",
        "call_debit_spread", "put_debit_spread", "iron_condor",
        "iron_butterfly", "cash_secured_put", "covered_call",
    }
    assert set(tpls.keys()) == expected
    # Every template carries a non-empty edge_keys + scoring_weights.
    for name, tpl in tpls.items():
        assert tpl.edge_keys, f"{name}: empty edge_keys"
        assert tpl.scoring_weights is not None, f"{name}: missing weights"
        assert tpl.invalidation_default, f"{name}: empty invalidation_default"


def test_build_strategy_matrix_returns_at_least_three_candidates(monkeypatch):
    """Synthetic bullish state + matched pattern hits → ≥ 3 candidates
    with distinct fit_score values."""
    impl, _calls = _kg_stub()
    monkeypatch.setattr(sm_mod, "get_posterior_with_fallback", impl)

    rv = _rv()
    hits = [
        {"pattern": "bull_flag", "regime": "bullish", "vol_state": "normal"},
        {"pattern": "higher_low", "regime": "bullish", "vol_state": "normal"},
    ]
    matrix = build_strategy_matrix(
        ticker="AAPL", regime_vector=rv, pattern_hits=hits,
        analogs=_analog_cluster(), iv_state={"iv_rank": 35.0},
    )
    assert isinstance(matrix, StrategyMatrix)
    assert len(matrix.candidates) >= 3, (
        f"expected ≥ 3 candidates, got {len(matrix.candidates)}: "
        f"{[c.strategy_name for c in matrix.candidates]}"
    )
    scores = [c.fit_score for c in matrix.candidates]
    assert len(set(round(s, 4) for s in scores)) >= 2, (
        f"fit_score not differentiating: {scores}"
    )
    # top_strategy is the highest-ranked one
    assert matrix.top_strategy is matrix.candidates[0]
    assert matrix.top_strategy.ranked_position == 1


def test_panic_intraday_excludes_iron_condor(monkeypatch):
    """Iron condor declares ``intraday_regime: not_in: [panic, ...]``;
    panic intraday must hard-gate it out."""
    impl, _calls = _kg_stub()
    monkeypatch.setattr(sm_mod, "get_posterior_with_fallback", impl)

    rv = _rv(trend="choppy", intraday="panic")
    matrix = build_strategy_matrix(
        ticker="SPY", regime_vector=rv, pattern_hits=[],
        analogs=_analog_cluster(n=5), iv_state={"iv_rank": 55.0},
    )
    names = [c.strategy_name for c in matrix.candidates]
    assert "iron_condor" not in names, (
        f"iron_condor leaked through panic gate: {names}"
    )
    assert "iron_butterfly" not in names


def test_top_strategy_none_when_no_template_passes_hard_gate(monkeypatch):
    """An impossible-to-satisfy state (unknown trend + panic intraday +
    short_gamma) excludes every template."""
    impl, _calls = _kg_stub()
    monkeypatch.setattr(sm_mod, "get_posterior_with_fallback", impl)

    # bearish trend + panic intraday + extreme IV blocks most;
    # combine with short_gamma to block the rest (covered_call has no
    # gamma constraint but requires trend ∈ [bullish, choppy] so the
    # bearish trend excludes it).
    rv = _rv(trend="bearish", intraday="panic", gamma="short_gamma")
    matrix = build_strategy_matrix(
        ticker="AAPL", regime_vector=rv, pattern_hits=[],
        analogs=_analog_cluster(n=0), iv_state={"iv_rank": 50.0},
    )
    assert matrix.top_strategy is None
    assert matrix.candidates == []


def test_cohort_lookup_uses_correct_kwargs(monkeypatch):
    """Matcher must call ``get_posterior_with_fallback`` with the exact
    axes declared in each ``edge_keys`` entry."""
    impl, calls = _kg_stub()
    monkeypatch.setattr(sm_mod, "get_posterior_with_fallback", impl)

    rv = _rv(trend="bullish")
    hits = [{"pattern": "vwap_reclaim", "regime": "trending_up",
             "vol_state": "__ANY__"}]
    build_strategy_matrix(
        ticker="MSFT", regime_vector=rv, pattern_hits=hits,
        analogs=_analog_cluster(n=20), iv_state={"iv_rank": 40.0},
    )
    # At least one call should have been routed for long_call's
    # vwap_reclaim edge_key with regime=trending_up, horizon=5d,
    # sample_split=combined.
    matched = [c for c in calls
               if c["ticker"] == "MSFT"
               and c["pattern"] == "vwap_reclaim"
               and c["regime"] == "trending_up"
               and c["vol_state"] == "__ANY__"
               and c["horizon"] == "5d"
               and c["sample_split"] == "combined"]
    assert matched, (
        f"no get_posterior_with_fallback call matched expected kwargs: "
        f"sampled={calls[:3]}"
    )
    # time_bucket should always be 'rth' per matcher contract
    assert all(c["time_bucket"] == "rth" for c in matched)


def test_pattern_alignment_lifts_fit_when_edge_keys_match(monkeypatch):
    """When pattern_hits exactly match a template's edge_keys, its
    pattern_alignment subscore saturates to 1.0 — that strategy should
    out-rank one whose edge_keys don't appear in pattern_hits."""
    impl, _calls = _kg_stub()
    monkeypatch.setattr(sm_mod, "get_posterior_with_fallback", impl)

    rv = _rv(trend="bullish")
    # All long_call edge_keys present + nothing for iron_condor.
    hits = [
        {"pattern": "bull_flag", "regime": "bullish", "vol_state": "normal"},
        {"pattern": "higher_low", "regime": "bullish", "vol_state": "normal"},
        {"pattern": "rsi_oversold", "regime": "bullish", "vol_state": "low"},
        {"pattern": "ma_cross_bullish", "regime": "bullish",
         "vol_state": "normal"},
    ]
    matrix = build_strategy_matrix(
        ticker="AAPL", regime_vector=rv, pattern_hits=hits,
        analogs=_analog_cluster(n=30), iv_state={"iv_rank": 35.0},
    )
    by_name = {c.strategy_name: c for c in matrix.candidates}
    assert "long_call" in by_name
    # long_call should have all 4 edge_keys satisfied → fit_score
    # benefits from the full 0.35 pattern weight.
    assert by_name["long_call"].fit_score > 0.5


def test_matrix_to_dict_round_trip(monkeypatch):
    """Output must be JSON-serializable shape."""
    impl, _calls = _kg_stub()
    monkeypatch.setattr(sm_mod, "get_posterior_with_fallback", impl)

    rv = _rv()
    matrix = build_strategy_matrix(
        ticker="AAPL", regime_vector=rv,
        pattern_hits=[{"pattern": "bull_flag", "regime": "bullish",
                        "vol_state": "normal"}],
        analogs=_analog_cluster(n=10), iv_state={"iv_rank": 30.0},
    )
    d = matrix.to_dict()
    assert d["ticker"] == "AAPL"
    assert "candidates" in d
    assert "top_strategy" in d
    assert "query_state" in d
    assert "regime_health" in d
    if d["candidates"]:
        c0 = d["candidates"][0]
        for k in ("strategy_name", "label", "direction", "fit_score",
                   "cohort_win_rate", "cohort_n", "analog_win_rate",
                   "ranked_position", "supporting_patterns",
                   "invalidation", "requires_passed", "requires_failed"):
            assert k in c0, f"missing key {k!r} in candidate dict"
