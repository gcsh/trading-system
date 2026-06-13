"""MITS Phase 2 (P2.3) — memory-bias self-calibration tests.

Locks the `derive_bias_factor` contract:
  * neutral when posterior == 0.5
  * monotonically increasing with posterior
  * respects min/max clamps
  * returns 1.0 (neutral) below min_samples (thin-corpus floor)
  * integrates with apply_memory_bias through the votes path
"""
import pytest

from backend.bot.agent_context import (
    apply_memory_bias,
    derive_bias_factor,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


# ── derive_bias_factor unit tests ───────────────────────────────────────


class TestDeriveBiasFactor:
    def test_neutral_at_posterior_half(self):
        # posterior == 0.5 → 1.0 regardless of scale.
        assert derive_bias_factor(posterior=0.5, sample_size=100) == 1.0
        assert derive_bias_factor(posterior=0.5, sample_size=100,
                                            scale=0.50) == 1.0

    def test_monotonic_in_posterior(self):
        # Bias must increase monotonically with posterior.
        prior = None
        for p in (0.1, 0.3, 0.5, 0.7, 0.9):
            cur = derive_bias_factor(posterior=p, sample_size=100)
            if prior is not None:
                assert cur >= prior, f"non-monotonic at p={p}"
            prior = cur

    def test_legacy_plus_minus_10_at_default_scale(self):
        # At scale=0.20 (default), posterior=0.75 ⇒ 1.10, posterior=0.25 ⇒ 0.90
        # — matches Phase 1's hardcoded behaviour for those operating
        # points.
        assert derive_bias_factor(posterior=0.75, sample_size=100) == \
            pytest.approx(1.10, abs=1e-6)
        assert derive_bias_factor(posterior=0.25, sample_size=100) == \
            pytest.approx(0.90, abs=1e-6)

    def test_max_clamp(self):
        # posterior=1.0 with very large scale must clamp at max_factor.
        v = derive_bias_factor(posterior=1.0, sample_size=100,
                                       scale=5.0, max_factor=1.25)
        assert v == 1.25

    def test_min_clamp(self):
        # posterior=0.0 with very large scale must clamp at min_factor.
        v = derive_bias_factor(posterior=0.0, sample_size=100,
                                       scale=5.0, min_factor=0.80)
        assert v == 0.80

    def test_thin_corpus_returns_neutral(self):
        # sample_size < min_samples → 1.0 even with strong posterior.
        v = derive_bias_factor(posterior=0.95, sample_size=5,
                                       min_samples=20)
        assert v == 1.0

    def test_threshold_inclusive_on_min_samples(self):
        # sample_size == min_samples should now apply the bias.
        v = derive_bias_factor(posterior=0.75, sample_size=20,
                                       min_samples=20)
        assert v > 1.0

    def test_bad_inputs_return_neutral(self):
        # Garbage posterior → 1.0, not a crash.
        assert derive_bias_factor(posterior=float("nan"),  # type: ignore[arg-type]
                                            sample_size=100) in (1.0, 1.0)
        assert derive_bias_factor(posterior=None,  # type: ignore[arg-type]
                                            sample_size=100) == 1.0

    def test_custom_scale_widens_band(self):
        # Larger scale should push the bias further from 1.0 for the
        # same posterior.
        small = derive_bias_factor(posterior=0.75, sample_size=100, scale=0.10)
        big = derive_bias_factor(posterior=0.75, sample_size=100, scale=0.40)
        assert big > small


# ── apply_memory_bias integration ──────────────────────────────────────


class _FakeVote:
    def __init__(self, agent, confidence=0.5):
        self.agent = agent
        self.confidence = confidence
        self.reasoning = ""


class TestApplyMemoryBiasUsesCalibratedFactor:
    def test_strong_posterior_lifts_confidence(self):
        ctx = {
            "knowledge_evidence": {
                "cells": [
                    {"sample_size": 50, "posterior_win_rate": 0.80},
                ],
                "summary": "50 analogs · WR 80%",
            },
            "similar_trades": [],
            "journal_lessons": [],
            "recent_performance": {},
        }
        v = _FakeVote("market", confidence=0.50)
        apply_memory_bias([v], ctx)
        # With default scale (0.20) and posterior 0.80, raw bias = 1.12.
        # confidence ~= 0.50 * 1.12 = 0.56
        assert v.confidence > 0.50

    def test_weak_posterior_drops_confidence(self):
        ctx = {
            "knowledge_evidence": {
                "cells": [
                    {"sample_size": 50, "posterior_win_rate": 0.20},
                ],
                "summary": "50 analogs · WR 20%",
            },
            "similar_trades": [],
            "journal_lessons": [],
            "recent_performance": {},
        }
        v = _FakeVote("market", confidence=0.50)
        apply_memory_bias([v], ctx)
        assert v.confidence < 0.50

    def test_thin_corpus_no_change(self):
        # Total N below min_samples → no bias applied.
        ctx = {
            "knowledge_evidence": {
                "cells": [
                    {"sample_size": 5, "posterior_win_rate": 0.95},
                ],
                "summary": "5 analogs · WR 95%",
            },
            "similar_trades": [],
            "journal_lessons": [],
            "recent_performance": {},
        }
        v = _FakeVote("market", confidence=0.50)
        apply_memory_bias([v], ctx)
        # Below default min_samples=20, no knowledge boost. Final confidence
        # may still equal 0.50 (no other biases firing).
        assert v.confidence == pytest.approx(0.50, abs=1e-6)
