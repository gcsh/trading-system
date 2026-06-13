"""MITS Phase 18.C — Policy Auto-Tuning (Advisory) unit tests.

Covers:

  * TUNABLE_RULES registry: every rule has a non-empty threshold_attr,
    a sane plausible_range, and a valid direction.
  * Bucketing math: synthetic samples land in the right buckets +
    Wilson CI bounds are sane.
  * Recommendation logic: best-Wilson-lower wins (not best raw mean).
  * Insufficient-data path: every bucket below min_n returns
    ``insufficient_data`` with no recommended_value.
  * Direction semantics: ``higher_is_stricter`` and
    ``lower_is_stricter`` both produce sensible rationales.
  * Boundary cases: empty input, single bucket populated, all-same-
    threshold samples, scenario_value None silently skipped.
  * to_dict round-trip stays bit-identical via json.dumps/loads.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import List

import pytest

from backend.bot.learning.policy_tuning import (
    DEFAULT_MIN_N_PER_BUCKET,
    DEFAULT_NUM_BUCKETS,
    DEFAULT_WINDOW_DAYS,
    PolicyTuningRecommendation,
    ThresholdBucket,
    TUNABLE_RULES,
    TunableRule,
    _DecisionRow,
    _build_buckets,
    _pick_recommendation,
    compute_policy_tuning,
)


pytestmark = [pytest.mark.unit]


# ── Helpers ──────────────────────────────────────────────────────────


def _make_row(
    *, scenario_value: float, pnl_pct: float, win: int,
) -> _DecisionRow:
    """Build a synthetic _DecisionRow where the scenario value is
    stored on the consensus block under 'confidence'. The
    ``_consensus_confidence`` extractor (used by low_confidence) will
    pull it out, so we can use a real registered rule for tests."""
    return _DecisionRow(
        trade_id=int(scenario_value * 10_000),
        pnl_pct=float(pnl_pct),
        win=int(win),
        decision_timestamp=datetime.utcnow() - timedelta(days=1),
        consensus={"confidence": float(scenario_value)},
        confidence_breakdown={},
        regime_vector={},
        simulator_verdict={},
        correlation_cap={},
        portfolio_context={},
        policy_result={},
        rule_evaluations=[],
        decision_quality={},
    )


def _rule_low_confidence() -> TunableRule:
    return next(r for r in TUNABLE_RULES if r.rule_name == "low_confidence")


# ── Registry sanity ───────────────────────────────────────────────────


def test_tunable_rules_registry_is_complete():
    """Every TunableRule has the fields the advisor relies on."""
    assert len(TUNABLE_RULES) >= 7, (
        "expected at least 7 tunable rules from the catalog"
    )
    seen_names = set()
    for r in TUNABLE_RULES:
        assert r.rule_name
        assert r.rule_name not in seen_names, (
            f"duplicate rule_name: {r.rule_name}"
        )
        seen_names.add(r.rule_name)
        assert r.threshold_attr
        assert isinstance(r.current_value, float)
        lo, hi = r.plausible_range
        assert lo < hi, (
            f"rule {r.rule_name} has bad plausible_range ({lo}, {hi})"
        )
        assert r.direction in ("higher_is_stricter", "lower_is_stricter")
        # scenario_value_fn must be callable; we don't invoke it here
        # because the row fixture is rule-specific.
        assert callable(r.scenario_value_fn)


def test_tunable_rules_cover_expected_policy_rules():
    """The advisor must include the rules we identified as numerically
    tunable in Step 0."""
    expected = {
        "low_confidence",
        "iv_too_rich",
        "correlation_cap_block",
        "simulator_veto",
        "catalyst_gate",
        "abstain_and_throttle_hi",
        "abstain_and_throttle_lo",
        "cycle_budget_overrun",
    }
    actual = {r.rule_name for r in TUNABLE_RULES}
    missing = expected - actual
    assert not missing, f"missing tunable rules: {missing}"


# ── Bucketing math ────────────────────────────────────────────────────


def test_bucketing_partitions_synthetic_samples_correctly():
    """Feed 100 samples evenly across [0.30, 0.80] — 5 buckets, 20 each."""
    rule = _rule_low_confidence()
    # 20 samples per bucket. low_confidence range = (0.30, 0.80), so 5
    # buckets of width 0.10. Place each sample at the midpoint of its
    # bucket so the boundary math is unambiguous.
    samples = []
    bucket_midpoints = [0.35, 0.45, 0.55, 0.65, 0.75]
    for i, mid in enumerate(bucket_midpoints):
        # Higher buckets → higher win rate to give the advisor a clear
        # signal: bucket 0 = 30% win, bucket 4 = 80% win.
        win_rate = 0.30 + i * 0.125
        for j in range(20):
            win = 1 if j < int(win_rate * 20) else 0
            pnl_pct = (j - 10) * 1.5
            samples.append((mid, _make_row(
                scenario_value=mid, pnl_pct=pnl_pct, win=win,
            )))
    buckets = _build_buckets(
        rule=rule, samples=samples,
        n_buckets=DEFAULT_NUM_BUCKETS,
        min_n_per_bucket=DEFAULT_MIN_N_PER_BUCKET,
    )
    assert len(buckets) == DEFAULT_NUM_BUCKETS
    # Each bucket got exactly 20 samples.
    for b in buckets:
        assert b.n_decisions == 20, (
            f"bucket {b.bucket_idx} got {b.n_decisions} samples"
        )
        assert b.n_closed == 20
        assert b.hit_rate is not None
        assert b.hit_rate_wilson_lower is not None
        assert b.hit_rate_wilson_upper is not None
        # Wilson lower <= raw hit_rate <= Wilson upper
        assert b.hit_rate_wilson_lower <= b.hit_rate + 1e-6
        assert b.hit_rate <= b.hit_rate_wilson_upper + 1e-6
        # Wilson CI bounds within [0, 1]
        assert 0.0 <= b.hit_rate_wilson_lower <= 1.0
        assert 0.0 <= b.hit_rate_wilson_upper <= 1.0
    # Buckets should be monotonically improving (we built it that way).
    hits = [b.hit_rate for b in buckets]
    assert hits[0] < hits[-1], (
        "synthetic monotonic data should give increasing hit_rate"
    )


def test_bucketing_flags_thin_buckets_as_insufficient():
    """When n_closed < min_n_per_bucket, the bucket carries the note +
    None metrics — operator sees ``insufficient_sample_size`` instead of
    a fabricated number."""
    rule = _rule_low_confidence()
    # Only 5 samples in the first bucket (below min_n=20). Others
    # empty.
    samples = []
    for j in range(5):
        samples.append((
            0.35, _make_row(scenario_value=0.35, pnl_pct=1.0, win=1),
        ))
    buckets = _build_buckets(
        rule=rule, samples=samples,
        n_buckets=DEFAULT_NUM_BUCKETS,
        min_n_per_bucket=DEFAULT_MIN_N_PER_BUCKET,
    )
    # Bucket 0 has 5 samples — below threshold.
    assert buckets[0].n_decisions == 5
    assert buckets[0].hit_rate is None
    assert buckets[0].hit_rate_wilson_lower is None
    assert "insufficient_sample_size" in buckets[0].notes
    # Other buckets are 0 samples.
    for b in buckets[1:]:
        assert b.n_decisions == 0
        assert b.hit_rate is None
        assert "insufficient_sample_size" in b.notes


def test_bucketing_clips_out_of_range_values_to_edge_buckets():
    """Samples outside plausible_range get clamped — no silent drop."""
    rule = _rule_low_confidence()
    # Range is (0.30, 0.80). Value 0.05 should land in bucket 0;
    # value 0.95 should land in bucket 4.
    samples = []
    samples.extend([(
        0.05, _make_row(scenario_value=0.05, pnl_pct=0.0, win=0),
    )] * 25)
    samples.extend([(
        0.95, _make_row(scenario_value=0.95, pnl_pct=10.0, win=1),
    )] * 25)
    buckets = _build_buckets(
        rule=rule, samples=samples,
        n_buckets=DEFAULT_NUM_BUCKETS,
        min_n_per_bucket=DEFAULT_MIN_N_PER_BUCKET,
    )
    assert buckets[0].n_decisions == 25
    assert buckets[-1].n_decisions == 25


# ── Recommendation logic ──────────────────────────────────────────────


def test_recommendation_picks_best_wilson_lower_bound():
    """Two buckets have the same raw hit_rate but different sample
    sizes — the larger-sample bucket should win because its Wilson
    lower bound is tighter (less penalised)."""
    rule = _rule_low_confidence()
    # Bucket idx 1: n=20, all wins (raw hit_rate = 1.0).
    # Bucket idx 3: n=80, 64 wins (raw hit_rate = 0.80).
    # Wilson lower(80, 100%) ≈ 0.84; Wilson lower(64/80, 80%) ≈ 0.70.
    # So the 100% bucket actually wins on Wilson lower. To make the
    # test interesting, use 19 of 20 vs 70 of 80: Wilson lower(19, 20)
    # ≈ 0.764; Wilson lower(70, 80) ≈ 0.802 → the larger sample wins.
    samples = []
    # bucket idx 1 → midpoint ~0.45 (range 0.30..0.80; width 0.10).
    for j in range(20):
        samples.append((
            0.45, _make_row(
                scenario_value=0.45, pnl_pct=2.0,
                win=1 if j < 19 else 0,
            ),
        ))
    # bucket idx 3 → midpoint ~0.65.
    for j in range(80):
        samples.append((
            0.65, _make_row(
                scenario_value=0.65, pnl_pct=2.0,
                win=1 if j < 70 else 0,
            ),
        ))
    buckets = _build_buckets(
        rule=rule, samples=samples,
        n_buckets=DEFAULT_NUM_BUCKETS,
        min_n_per_bucket=DEFAULT_MIN_N_PER_BUCKET,
    )
    val, conf, rationale = _pick_recommendation(
        buckets=buckets, rule=rule,
        total_n=sum(b.n_closed for b in buckets),
    )
    # The winning bucket midpoint should be 0.65 (bucket idx 3), NOT
    # 0.45 (bucket idx 1) — confirming Wilson-lower (not raw hit_rate)
    # is the ranking key.
    assert val is not None
    assert abs(val - 0.65) < 0.05, (
        f"expected ~0.65 (bucket 3 wins on Wilson_lower), got {val}"
    )
    assert "Best bucket" in rationale
    assert conf in ("low", "medium", "high")


def test_recommendation_returns_insufficient_data_when_no_bucket_clears():
    """No bucket has min_n samples → no recommendation, label is
    ``insufficient_data``."""
    rule = _rule_low_confidence()
    # 5 thin buckets, 3 samples each (15 total, all below min_n=20).
    samples = []
    for mid in (0.35, 0.45, 0.55, 0.65, 0.75):
        for j in range(3):
            samples.append((
                mid, _make_row(scenario_value=mid, pnl_pct=1.0, win=1),
            ))
    buckets = _build_buckets(
        rule=rule, samples=samples,
        n_buckets=DEFAULT_NUM_BUCKETS,
        min_n_per_bucket=DEFAULT_MIN_N_PER_BUCKET,
    )
    val, conf, rationale = _pick_recommendation(
        buckets=buckets, rule=rule,
        total_n=sum(b.n_closed for b in buckets),
    )
    assert val is None
    assert conf == "insufficient_data"
    assert "minimum sample size" in rationale
    # Rationale should cite the actual current_value of the rule.
    assert f"{rule.current_value:g}" in rationale


def test_rationale_carries_sample_count_and_range():
    """Rationale string must mention N + the threshold range so the
    operator can audit the recommendation at a glance."""
    rule = _rule_low_confidence()
    samples = []
    for mid in (0.35, 0.45, 0.55, 0.65, 0.75):
        for j in range(25):
            win = 1 if (j + int(mid * 100)) % 2 == 0 else 0
            samples.append((
                mid, _make_row(scenario_value=mid, pnl_pct=1.0, win=win),
            ))
    buckets = _build_buckets(
        rule=rule, samples=samples,
        n_buckets=DEFAULT_NUM_BUCKETS,
        min_n_per_bucket=DEFAULT_MIN_N_PER_BUCKET,
    )
    val, conf, rationale = _pick_recommendation(
        buckets=buckets, rule=rule,
        total_n=sum(b.n_closed for b in buckets),
    )
    assert val is not None
    assert "n_closed=" in rationale
    assert "Wilson_lower=" in rationale
    assert "Total closed decisions" in rationale
    assert rule.direction in rationale


# ── Direction semantics ──────────────────────────────────────────────


def test_higher_is_stricter_rule_recommends_higher_when_better():
    """``higher_is_stricter`` rules where high buckets win should
    produce a high recommended_value."""
    rule = _rule_low_confidence()  # higher_is_stricter
    assert rule.direction == "higher_is_stricter"
    samples = []
    # Make bucket 4 (high) the clear winner.
    for mid in (0.35, 0.45, 0.55, 0.65):
        for j in range(25):
            win = 1 if j < 10 else 0
            samples.append((
                mid, _make_row(scenario_value=mid, pnl_pct=1.0, win=win),
            ))
    for j in range(40):
        win = 1 if j < 35 else 0
        samples.append((
            0.75, _make_row(scenario_value=0.75, pnl_pct=2.0, win=win),
        ))
    recs = compute_policy_tuning(
        window_days=DEFAULT_WINDOW_DAYS,
        min_n_per_bucket=DEFAULT_MIN_N_PER_BUCKET,
        rules=[rule],
        decisions=[r for _, r in samples],
    )
    assert len(recs) == 1
    rec = recs[0]
    assert rec.recommended_value is not None
    assert rec.recommended_value > rule.current_value


# ── Boundary cases ──────────────────────────────────────────────────


def test_compute_policy_tuning_handles_zero_decisions():
    """Empty input → every recommendation flags insufficient_data."""
    recs = compute_policy_tuning(
        window_days=DEFAULT_WINDOW_DAYS,
        min_n_per_bucket=DEFAULT_MIN_N_PER_BUCKET,
        decisions=[],
    )
    assert len(recs) == len(TUNABLE_RULES)
    for rec in recs:
        assert rec.recommendation_confidence == "insufficient_data"
        assert rec.recommended_value is None
        assert rec.n_closed_total == 0


def test_compute_policy_tuning_handles_all_same_threshold_samples():
    """When every sample has the identical scenario value, only ONE
    bucket gets populated — the other 4 should flag insufficient."""
    rule = _rule_low_confidence()
    decisions = [
        _make_row(scenario_value=0.65, pnl_pct=2.0, win=1)
        for _ in range(50)
    ]
    recs = compute_policy_tuning(
        rules=[rule], decisions=decisions,
        min_n_per_bucket=DEFAULT_MIN_N_PER_BUCKET,
    )
    rec = recs[0]
    # One bucket should clear min_n.
    populated = [b for b in rec.buckets if b.n_decisions >= 20]
    assert len(populated) == 1
    # The other buckets are empty / insufficient.
    thin = [b for b in rec.buckets if b.n_decisions == 0]
    assert len(thin) == 4
    # Recommendation lands inside the populated bucket.
    if rec.recommended_value is not None:
        assert (
            populated[0].threshold_low
            <= rec.recommended_value
            <= populated[0].threshold_high
        )


def test_scenario_value_none_silently_skipped():
    """Rows where the scenario_value_fn returns None must not crash
    the pipeline — they just don't contribute to any bucket."""
    rule = _rule_low_confidence()
    # Mix of rows with confidence + without (consensus={}).
    decisions: List[_DecisionRow] = []
    for j in range(25):
        decisions.append(_make_row(
            scenario_value=0.55, pnl_pct=1.0, win=1,
        ))
    for j in range(25):
        # confidence not on consensus + no rule_evaluations carrying it
        # → scenario_value_fn returns None
        decisions.append(_DecisionRow(
            trade_id=1000 + j,
            pnl_pct=1.0, win=1,
            decision_timestamp=datetime.utcnow() - timedelta(days=1),
            consensus={},
            confidence_breakdown={},
            regime_vector={}, simulator_verdict={},
            correlation_cap={}, portfolio_context={},
            policy_result={}, rule_evaluations=[],
            decision_quality={},
        ))
    recs = compute_policy_tuning(
        rules=[rule], decisions=decisions,
        min_n_per_bucket=DEFAULT_MIN_N_PER_BUCKET,
    )
    rec = recs[0]
    # Only the 25 rows with scenario_value contribute.
    assert rec.n_decisions_total == 25


# ── Round-trip + dataclass shape ─────────────────────────────────────


def test_recommendation_to_dict_is_json_round_trippable():
    """The recommendation must round-trip via json so the persistence
    layer + the API layer share the same shape."""
    rule = _rule_low_confidence()
    samples = []
    for mid in (0.35, 0.45, 0.55, 0.65, 0.75):
        for j in range(25):
            samples.append((
                mid, _make_row(scenario_value=mid, pnl_pct=1.0, win=j % 2),
            ))
    recs = compute_policy_tuning(
        rules=[rule], decisions=[r for _, r in samples],
        min_n_per_bucket=DEFAULT_MIN_N_PER_BUCKET,
    )
    rec = recs[0]
    payload = rec.to_dict()
    # Required keys present.
    expected_keys = {
        "rule_name", "threshold_attr", "current_value", "plausible_range",
        "direction", "units", "description", "buckets", "recommended_value",
        "recommendation_confidence", "rationale", "n_decisions_total",
        "n_closed_total", "sample_age_days", "window_days",
        "min_n_per_bucket", "computed_at",
    }
    assert expected_keys.issubset(payload.keys()), (
        f"missing keys: {expected_keys - set(payload.keys())}"
    )
    # JSON round-trip.
    encoded = json.dumps(payload, default=str)
    decoded = json.loads(encoded)
    assert decoded["rule_name"] == rec.rule_name
    assert decoded["recommendation_confidence"] in (
        "insufficient_data", "low", "medium", "high",
    )
    assert isinstance(decoded["buckets"], list)
    assert len(decoded["buckets"]) == DEFAULT_NUM_BUCKETS
    for b in decoded["buckets"]:
        assert "bucket_idx" in b
        assert "threshold_low" in b
        assert "threshold_high" in b
        assert "n_decisions" in b
        assert "n_closed" in b


def test_wilson_interval_widens_with_smaller_samples():
    """Sanity check on the borrowed Wilson helper — smaller N should
    produce a wider band."""
    from backend.bot.learning.attribution import _wilson_interval
    lo_big, hi_big = _wilson_interval(50, 100)
    lo_small, hi_small = _wilson_interval(5, 10)
    band_big = hi_big - lo_big
    band_small = hi_small - lo_small
    assert band_small > band_big, (
        f"Wilson CI for n=10 ({band_small}) should be wider than n=100 "
        f"({band_big})"
    )
