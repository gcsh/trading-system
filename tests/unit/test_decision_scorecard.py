"""MITS Phase 16.C — Decision Quality Scorecard unit tests.

Synthetic provenance bags → expected sub-scores per formula.
"""
from __future__ import annotations

from backend.bot.decision.scorecard import (
    DecisionQualityScore,
    _DEFAULT_WEIGHTS,
    score_decision,
)


def test_empty_provenance_returns_neutral_score():
    """Empty bag → all defaults; composite in [0, 100]."""
    dqs = score_decision({})
    assert isinstance(dqs, DecisionQualityScore)
    for axis in (
        dqs.analysis_quality, dqs.council_agreement,
        dqs.risk_quality, dqs.execution_quality, dqs.composite,
    ):
        assert 0.0 <= axis <= 100.0


def test_analysis_quality_green_regime_max_fit():
    """green regime + fit_score=1.0 → analysis_quality at the top."""
    bag = {
        "regime_vector": {"health": "green"},
        "strategy_matrix": {"top_strategy": {"fit_score": 1.0}},
    }
    dqs = score_decision(bag)
    # (1.0 + 0.75 + 1.0) / 3 * 100 = 91.67
    assert abs(dqs.analysis_quality - 91.67) < 0.05


def test_analysis_quality_red_regime():
    bag = {
        "regime_vector": {"health": "red"},
        "strategy_matrix": {"top_strategy": {"fit_score": 0.4}},
    }
    dqs = score_decision(bag)
    # (0.2 + 0.75 + 0.4) / 3 * 100 = 45.0
    assert abs(dqs.analysis_quality - 45.0) < 0.05


def test_council_agreement_no_dissent_high_confidence():
    bag = {
        "consensus": {"confidence": 0.8},
        "chairman_memo": {"dissent": {"dissent_share": 0.0}},
    }
    dqs = score_decision(bag)
    # 100 * (1 - 0) * min(1.0, 0.8 * 1.25) = 100.0
    assert abs(dqs.council_agreement - 100.0) < 0.05


def test_council_agreement_half_dissent():
    bag = {
        "consensus": {"confidence": 0.8},
        "chairman_memo": {"dissent": {"dissent_share": 0.5}},
    }
    dqs = score_decision(bag)
    # 100 * 0.5 * 1.0 = 50.0
    assert abs(dqs.council_agreement - 50.0) < 0.05


def test_risk_quality_low_correlation_no_penalties():
    bag = {
        "correlation_cap": {"worst_rho": 0.3},
        "policy_result": {"soft_penalties_total_pct": 0.0},
    }
    dqs = score_decision(bag)
    # correlation_score = 1.0 (|rho|<=0.5), no penalties → 100.0
    assert abs(dqs.risk_quality - 100.0) < 0.05


def test_risk_quality_high_correlation_with_penalties():
    bag = {
        "correlation_cap": {"worst_rho": 0.8},
        "policy_result": {"soft_penalties_total_pct": 20.0},
    }
    dqs = score_decision(bag)
    # correlation_score = 1 - (0.8 - 0.5)/0.5 = 0.4
    # 100 * 0.4 * (1 - 20/100) = 100 * 0.4 * 0.8 = 32.0
    assert abs(dqs.risk_quality - 32.0) < 0.05


def test_execution_quality_fresh_iv_default_others():
    bag = {
        "regime_vector": {
            "iv_rank": {"freshness_seconds": 100.0},
        },
    }
    dqs = score_decision(bag)
    # spread_score = 1 - min(1, 0.5) = 0.5
    # iv_freshness_score = 1.0 (< 300s)
    # liq_score = 0.5
    # 100 * (0.4*0.5 + 0.3*1.0 + 0.3*0.5) = 100 * (0.2 + 0.3 + 0.15) = 65.0
    assert abs(dqs.execution_quality - 65.0) < 0.05


def test_execution_quality_stale_iv():
    bag = {
        "regime_vector": {
            "iv_rank": {"freshness_seconds": 1800.0},
        },
    }
    dqs = score_decision(bag)
    # iv_freshness_score = max(0.2, 1 - 1800/1800) = 0.2
    # 100 * (0.4*0.5 + 0.3*0.2 + 0.3*0.5) = 100*(0.2+0.06+0.15) = 41.0
    assert abs(dqs.execution_quality - 41.0) < 0.05


def test_composite_uses_default_weights():
    bag = {
        "regime_vector": {
            "health": "green",
            "iv_rank": {"freshness_seconds": 100.0},
        },
        "strategy_matrix": {"top_strategy": {"fit_score": 1.0}},
        "consensus": {"confidence": 0.8},
        "chairman_memo": {"dissent": {"dissent_share": 0.0}},
        "correlation_cap": {"worst_rho": 0.3},
        "policy_result": {"soft_penalties_total_pct": 0.0},
    }
    dqs = score_decision(bag)
    expected = (
        _DEFAULT_WEIGHTS["analysis_quality"] * dqs.analysis_quality
        + _DEFAULT_WEIGHTS["council_agreement"] * dqs.council_agreement
        + _DEFAULT_WEIGHTS["risk_quality"] * dqs.risk_quality
        + _DEFAULT_WEIGHTS["execution_quality"] * dqs.execution_quality
    )
    assert abs(dqs.composite - expected) < 0.05


def test_custom_weights_override():
    bag = {"regime_vector": {"health": "red"}}
    custom = {
        "analysis_quality": 0.0, "council_agreement": 1.0,
        "risk_quality": 0.0, "execution_quality": 0.0,
    }
    dqs = score_decision(bag, weights=custom)
    # Composite is then exactly council_agreement.
    assert abs(dqs.composite - dqs.council_agreement) < 0.05


def test_to_dict_round_trip_shapes():
    dqs = score_decision({
        "regime_vector": {"health": "yellow"},
        "consensus": {"confidence": 0.6},
        "correlation_cap": {"worst_rho": 0.4},
    })
    out = dqs.to_dict()
    for k in ("analysis_quality", "council_agreement", "risk_quality",
              "execution_quality", "composite"):
        assert k in out
        assert isinstance(out[k], float)
        assert 0.0 <= out[k] <= 100.0
    assert "components" in out
    assert "analysis.regime_health" in out["components"]


def test_malformed_values_dont_raise():
    """Non-numeric inputs fall back to defaults; no crash."""
    bag = {
        "regime_vector": {"health": "unknown"},
        "strategy_matrix": {"top_strategy": {"fit_score": "garbage"}},
        "consensus": {"confidence": "nope"},
        "chairman_memo": {"dissent": {"dissent_share": None}},
        "correlation_cap": {"worst_rho": "?"},
        "policy_result": {"soft_penalties_total_pct": None},
    }
    dqs = score_decision(bag)
    assert 0.0 <= dqs.composite <= 100.0
