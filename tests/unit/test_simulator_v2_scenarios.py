"""MITS Phase 16.D — Simulator v2 scenario decomposition.

Tests ``decompose_scenarios`` over handcrafted ``AnalogHit`` lists with
mixed realized returns. Asserts:
  * bucket boundaries match the operator's spec
  * probabilities sum to 1.0 (modulo float rounding)
  * empty buckets are omitted
  * per-cluster ``expected_payoff`` reflects the long-stock projection
"""
from __future__ import annotations

from datetime import datetime

import pytest

from backend.bot.analysis.simulator import (
    ScenarioCluster, decompose_scenarios,
)
from backend.bot.corpus.analog_retrieval import AnalogHit


def _hit(r: float, oid: int = 1) -> AnalogHit:
    return AnalogHit(
        observation_id=oid, ticker="AAPL",
        timestamp=datetime(2025, 1, 1),
        distance=0.1, cosine=0.9,
        regime_label="bullish",
        pattern_set=[],
        realized_return_pct=r,
        horizon="1d",
    )


def test_empty_analogs_returns_empty_list():
    assert decompose_scenarios(
        [], direction="long_stock", spot=100.0,
    ) == []


def test_buckets_match_operator_spec():
    """One hit per bucket → 4 clusters, probability 0.25 each."""
    analogs = [
        _hit(+10.0, 1),    # continuation
        _hit(+2.0, 2),     # fake_breakout
        _hit(-5.0, 3),     # stop_out
        _hit(-15.0, 4),    # macro_shock
    ]
    clusters = decompose_scenarios(
        analogs, direction="long_stock", spot=100.0,
    )
    assert len(clusters) == 4
    labels = {c.label for c in clusters}
    assert labels == {"continuation", "fake_breakout",
                      "stop_out", "macro_shock"}
    for c in clusters:
        assert c.n_analogs == 1
        assert c.probability == pytest.approx(0.25)


def test_probabilities_sum_to_one():
    """Mixed distribution → sum of probabilities ~= 1.0."""
    rets = [12.0, 8.0, 5.0,                  # continuation x3
            2.0, 1.0, 0.0, -1.0, -2.5,       # fake_breakout x5
            -3.0, -7.0,                       # stop_out x2
            -12.0]                            # macro_shock x1
    analogs = [_hit(r, i) for i, r in enumerate(rets)]
    clusters = decompose_scenarios(
        analogs, direction="long_stock", spot=100.0,
    )
    total = sum(c.probability for c in clusters)
    assert 0.99 <= total <= 1.01
    total_n = sum(c.n_analogs for c in clusters)
    assert total_n == len(rets)


def test_empty_buckets_omitted():
    """All hits in continuation → only one cluster returned."""
    analogs = [_hit(r, i) for i, r in enumerate([10.0, 12.0, 15.0])]
    clusters = decompose_scenarios(
        analogs, direction="long_stock", spot=100.0,
    )
    assert len(clusters) == 1
    assert clusters[0].label == "continuation"
    assert clusters[0].probability == pytest.approx(1.0)


def test_per_cluster_expected_payoff_for_long_stock():
    """continuation bucket (+10%) on a $100 long stock → ~$10/share."""
    analogs = [_hit(10.0, 1), _hit(12.0, 2)]
    clusters = decompose_scenarios(
        analogs, direction="long_stock", spot=100.0,
    )
    cont = next(c for c in clusters if c.label == "continuation")
    # mean return 11.0% × $100 = ~$11/share.
    assert 10.0 <= cont.expected_payoff <= 12.0


def test_per_cluster_expected_payoff_for_short_stock():
    """macro_shock (-15%) on a $100 short stock → +$15/share."""
    analogs = [_hit(-15.0, 1), _hit(-20.0, 2)]
    clusters = decompose_scenarios(
        analogs, direction="short_stock", spot=100.0,
    )
    shock = next(c for c in clusters if c.label == "macro_shock")
    assert 15.0 <= shock.expected_payoff <= 20.0


def test_to_dict_round_trip():
    analogs = [_hit(10.0, 1), _hit(-5.0, 2)]
    clusters = decompose_scenarios(
        analogs, direction="long_stock", spot=100.0,
    )
    for c in clusters:
        d = c.to_dict()
        assert set(d.keys()) == {
            "label", "probability", "expected_payoff",
            "payoff_std", "n_analogs",
        }
        assert d["probability"] == round(c.probability, 4)


def test_boundary_at_plus_5_pct():
    """r == +5.0 → continuation (>= +5 inclusive)."""
    clusters = decompose_scenarios(
        [_hit(5.0, 1)], direction="long_stock", spot=100.0,
    )
    assert clusters[0].label == "continuation"


def test_boundary_at_minus_3_pct():
    """r == -3.0 → stop_out (-10 <= r <= -3 inclusive)."""
    clusters = decompose_scenarios(
        [_hit(-3.0, 1)], direction="long_stock", spot=100.0,
    )
    assert clusters[0].label == "stop_out"


def test_boundary_at_minus_10_pct():
    """r == -10.0 → stop_out (inclusive upper boundary)."""
    clusters = decompose_scenarios(
        [_hit(-10.0, 1)], direction="long_stock", spot=100.0,
    )
    assert clusters[0].label == "stop_out"


def test_boundary_just_below_minus_10_is_macro_shock():
    clusters = decompose_scenarios(
        [_hit(-10.01, 1)], direction="long_stock", spot=100.0,
    )
    assert clusters[0].label == "macro_shock"


def test_options_path_uses_strike_and_dte():
    """Long-call branch needs strike + dte; payoff projection should
    return ScenarioCluster with non-zero expected_payoff on a bullish
    cohort."""
    analogs = [_hit(10.0, i) for i in range(5)]
    clusters = decompose_scenarios(
        analogs, direction="long_call", spot=100.0,
        strike=100.0, dte=10, iv_for_options=0.3,
    )
    assert len(clusters) == 1
    assert clusters[0].label == "continuation"
    # Long ATM call on a +10% move → positive payoff per contract.
    assert clusters[0].expected_payoff > 0
