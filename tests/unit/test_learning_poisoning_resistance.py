"""Learning-system poisoning resistance — every signal the agents
consume must resist adversarial/biased/duplicate data. Today's bug
(2026-06-03) was exactly this class: the synthetic-corpus backfill
poisoned the live calibration gate and blocked ALL real trades.

QA framework: Learning System Validation (section 25), Data Integrity (22).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.learning_safety
@pytest.mark.invariant
class TestSyntheticCorpusGateSeparation:
    """The grade gate consumes /metrics/summary. If synthetic outcomes
    poison the metric, the gate auto-tightens and blocks live trades —
    exactly what we saw today."""

    def test_build_summary_defaults_live_only(self):
        body = (ROOT / "backend/api/routes/metrics.py").read_text()
        assert re.search(
            r"def build_summary\([^)]*live_only:\s*bool\s*=\s*True",
            body, re.DOTALL,
        ), (
            "build_summary must default live_only=True. The adaptive grade "
            "gate at engine.py:1173 consumes this; synthetic poisoning "
            "would auto-tighten the gate to A+ and block live trades."
        )

    def test_load_labels_filters_decision_log_when_live_only(self):
        body = (ROOT / "backend/api/routes/metrics.py").read_text()
        # The DecisionLog branch must filter historical_replay_closed
        # rows when include_synthetic is False (the live-only path).
        assert "historical_replay_closed" in body, (
            "_load_labels must filter DecisionLog by status != "
            "'historical_replay_closed' on the live-only path. Without "
            "this the DecisionLog half is unfiltered and the gate sees "
            "synthetic win_probability/outcome_pnl rows."
        )


@pytest.mark.learning_safety
@pytest.mark.invariant
class TestCohortPriorFloor:
    """Priors with too-few observations must fall back to a sensible
    default — otherwise the bot trades on noise."""

    def test_blend_with_zero_observations_returns_prior_only(self):
        from backend.bot.cohort_matrix.priors import blend, CohortPrior
        prior = CohortPrior(
            strategy="cash_secured_put",
            regime="neutral",
            grade="—",
            prior_win_rate=0.72,
            prior_n=10,
            citation="test",
        )
        result = blend(obs_win_rate=None, obs_n=0, prior=prior)
        assert result["obs_n"] == 0
        assert result["posterior_win_rate"] == pytest.approx(0.72, abs=1e-6)

    def test_blend_with_outlier_obs_tilts_but_does_not_jump(self):
        from backend.bot.cohort_matrix.priors import blend, CohortPrior
        prior = CohortPrior(strategy="x", regime="x", grade="—",
                                 prior_win_rate=0.50, prior_n=10, citation="")
        # One observation at 100% must not pin the posterior at 100%.
        result = blend(obs_win_rate=1.0, obs_n=1, prior=prior)
        # Posterior = (10×0.50 + 1×1.0) / 11 ≈ 0.545
        assert 0.50 < result["posterior_win_rate"] < 0.60

    def test_fallback_prior_used_when_no_curated_match(self):
        from backend.bot.cohort_matrix.priors import blend
        result = blend(obs_win_rate=0.60, obs_n=20, prior=None,
                          baseline_wr=0.55)
        assert result["source"] == "fallback_baseline"


@pytest.mark.learning_safety
@pytest.mark.invariant
class TestCurriculatedRuleStability:
    """Curated rules must fire deterministically — a re-entry of the
    same trade shouldn't double-count, and the rule catalog can't be
    corrupted by missing keys."""

    def test_curated_rules_accept_minimal_context(self):
        from backend.bot.journal.curated import applicable_curated_lessons
        matches = applicable_curated_lessons(
            strategy="cash_secured_put",
            regime_trend="neutral",
            volatility="normal",
            gamma="unknown",
        )
        # Should not raise; may return empty list.
        assert isinstance(matches, list)

    def test_csp_earnings_blackout_fires_within_7d(self):
        from backend.bot.journal.curated import applicable_curated_lessons
        matches = applicable_curated_lessons(
            strategy="cash_secured_put",
            regime_trend="neutral",
            volatility="normal",
            gamma="unknown",
            earnings_days=5,
        )
        assert any("earnings" in m.condition_keys.get("rule_id", "")
                       for m in matches), (
            "csp_earnings_blackout must fire within 7d of earnings — "
            "this guards against the single largest loss bucket in CSP."
        )

    def test_curated_rule_does_not_fire_far_from_earnings(self):
        from backend.bot.journal.curated import applicable_curated_lessons
        matches = applicable_curated_lessons(
            strategy="cash_secured_put",
            regime_trend="neutral",
            volatility="normal",
            gamma="unknown",
            earnings_days=30,
        )
        assert not any("earnings_blackout" in m.condition_keys.get("rule_id", "")
                          for m in matches)


@pytest.mark.learning_safety
@pytest.mark.invariant
class TestSyntheticTagPersistence:
    """The signal_source tag must survive every persistence path.
    A bug here means synthetic rows become indistinguishable from live."""

    def test_historical_replay_constant_matches_filter_string(self):
        """The constant used by the writer must match the filter string
        used everywhere else. Typo here → instant leak."""
        writer = (ROOT / "backend/bot/backfill/historical_replay.py").read_text()
        assert 'HISTORICAL_REPLAY_SOURCE = "historical_replay"' in writer, (
            "Writer constant drift would silently include synthetic rows."
        )
        # Spot-check a downstream filter file uses the same string.
        portfolio = (ROOT / "backend/api/routes/portfolio.py").read_text()
        assert '"historical_replay"' in portfolio
