"""P2.2 — curated lesson seeds unit tests."""
from __future__ import annotations

from backend.bot.journal.curated import (
    CURATED_RULES,
    applicable_curated_lessons,
)


def _has_rule(matches, rule_id):
    return any(
        (l.condition_keys or {}).get("rule_id") == rule_id for l in matches
    )


def test_catalog_loaded():
    assert len(CURATED_RULES) >= 5
    ids = {r.rule_id for r in CURATED_RULES}
    assert "csp_earnings_blackout" in ids
    assert "iron_condor_low_iv" in ids


def test_csp_within_earnings_blackout_fires():
    matches = applicable_curated_lessons(
        strategy="cash_secured_put",
        regime_trend="ranging", volatility="normal", gamma="unknown",
        earnings_days=3, iv_rank=50, vix=15,
    )
    assert _has_rule(matches, "csp_earnings_blackout")
    assert any(l.size_multiplier == 0.0 for l in matches)


def test_csp_outside_earnings_does_not_fire():
    matches = applicable_curated_lessons(
        strategy="cash_secured_put",
        regime_trend="ranging", volatility="normal", gamma="unknown",
        earnings_days=30, iv_rank=50, vix=15,
    )
    assert not _has_rule(matches, "csp_earnings_blackout")


def test_iron_condor_low_iv_fires():
    matches = applicable_curated_lessons(
        strategy="iron_condor",
        regime_trend="ranging", volatility="normal", gamma="unknown",
        iv_rank=20,
    )
    assert _has_rule(matches, "iron_condor_low_iv")


def test_iron_condor_high_iv_does_not_fire():
    matches = applicable_curated_lessons(
        strategy="iron_condor",
        regime_trend="ranging", volatility="normal", gamma="unknown",
        iv_rank=60,
    )
    assert not _has_rule(matches, "iron_condor_low_iv")


def test_long_options_vix_spike_fires_at_30():
    matches = applicable_curated_lessons(
        strategy="zero_dte_scalp",
        regime_trend="neutral", volatility="high", gamma="unknown",
        vix=32,
    )
    assert _has_rule(matches, "long_options_vix_spike")
    rule_match = next(
        l for l in matches
        if (l.condition_keys or {}).get("rule_id") == "long_options_vix_spike"
    )
    assert rule_match.size_multiplier == 0.5


def test_long_options_iv_overpay_fires_at_high_iv():
    matches = applicable_curated_lessons(
        strategy="trend_pullback",
        regime_trend="uptrend", volatility="normal", gamma="unknown",
        iv_rank=85,
    )
    assert _has_rule(matches, "long_options_iv_overpay")


def test_mean_reversion_trending_market_fires():
    matches = applicable_curated_lessons(
        strategy="rsi_mean_reversion",
        regime_trend="trending", volatility="normal", gamma="unknown",
    )
    assert _has_rule(matches, "mean_reversion_trending_market")


def test_short_premium_expanding_vol_fires():
    matches = applicable_curated_lessons(
        strategy="iron_condor",
        regime_trend="ranging", volatility="normal", gamma="unknown",
        iv_regime={"regime": "expanding", "confidence": 0.8},
    )
    assert _has_rule(matches, "short_premium_in_volatility_expanding")


def test_friday_pm_credit_fires():
    matches = applicable_curated_lessons(
        strategy="cash_secured_put",
        regime_trend="ranging", volatility="normal", gamma="unknown",
        earnings_days=30,
        day_of_week="Friday",
    )
    assert _has_rule(matches, "friday_pm_credit_weekend_risk")


def test_inverted_yield_curve_fires():
    matches = applicable_curated_lessons(
        strategy="trend_pullback",
        regime_trend="uptrend", volatility="normal", gamma="unknown",
        yield_curve_inverted=True,
    )
    assert _has_rule(matches, "inverted_yield_curve_long_caution")


def test_lessons_carry_source_tag():
    matches = applicable_curated_lessons(
        strategy="cash_secured_put",
        regime_trend="ranging", volatility="normal", gamma="unknown",
        earnings_days=2,
    )
    assert matches, "expected at least one match"
    assert all(
        (l.condition_keys or {}).get("source") == "curated"
        for l in matches
    )


def test_no_match_returns_empty():
    # No rule should fire for a benign neutral context.
    matches = applicable_curated_lessons(
        strategy="trend_pullback",
        regime_trend="neutral", volatility="normal", gamma="unknown",
        earnings_days=30, iv_rank=50, vix=15,
        day_of_week="Wednesday",
        yield_curve_inverted=False,
    )
    assert matches == []
