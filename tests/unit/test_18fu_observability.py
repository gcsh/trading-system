"""MITS Phase 18-FU Stream D — unit tests for Gap 6 / 9 / 10 / 12 fixes.

Covers:

  * Gap 6 — WeightApplicationLog write when ``apply_weights_for_cycle``
    fires + apply_enabled is True. No row when apply is OFF, no row
    from the advisor's bare ``get_current_weights`` (log_application
    defaults to False).
  * Gap 6 — TTL prune removes rows older than ``ttl_days``, leaves
    fresh ones.
  * Gap 9 — stability check: 3 consecutive matching prior batches →
    stable → confidence stays 'high'. With a single outlier prior the
    grade is demoted to 'medium'.
  * Gap 10 — compute_impact correctly derives delta + sets is_significant
    False when n_closed < MIN_N_FOR_SIGNIFICANCE (insufficient_sample
    note). With a synthetic 25+25 sample the flag flips True when the
    composite delta clears the noise floor.
  * Gap 10 — learning_impact persistence round-trip + latest_impact_rows
    returns the just-written rows.
  * Gap 12 — counterfactual results carry ``code_version`` field on
    every variation. cache_version_status reports mismatch when cached
    payload predates the current version.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

import pytest
from sqlalchemy import delete, func, select

from backend.bot.learning.attribution import KNOWN_AGENTS
from backend.bot.learning.counterfactual import (
    COUNTERFACTUAL_CODE_VERSION,
    SizingCounterfactual,
    cache_version_status,
    get_code_version,
)
from backend.bot.learning.impact_measurement import (
    EVENT_TYPE_WEIGHT_APPLY,
    LearningImpactReport,
    MIN_N_FOR_SIGNIFICANCE,
    NOISE_FLOOR_COMPOSITE_DELTA,
    WindowMetrics,
    _delta_dict,
    _is_significant,
    compute_impact,
    latest_impact_rows,
    persist_impact_reports,
)
from backend.bot.learning.policy_tuning import (
    STABILITY_N_CONSECUTIVE_REQUIRED,
    STABILITY_TOLERANCE_PCT,
    _stability_check,
)
from backend.bot.learning.weight_adaptation import (
    AGENT_BASE_WEIGHTS,
    WEIGHT_APPLICATION_LOG_TTL_DAYS,
    apply_weights_for_cycle,
    get_current_weights,
    latest_weight_application_rows,
    prune_weight_application_log,
)
from backend.config import TUNABLES
from backend.models.learning_impact import LearningImpact
from backend.models.policy_tuning import PolicyTuning
from backend.models.weight_application_log import WeightApplicationLog


pytestmark = [pytest.mark.unit]


def _session():
    from backend.db import session_scope
    return session_scope()


# ── Gap 6 — per-cycle weight application log ──────────────────────────


def test_gap6_apply_weights_for_cycle_writes_log_when_apply_enabled(
    temp_db, monkeypatch,
):
    """apply_weights_for_cycle with apply_enabled=True + at least one
    history row ⇒ one WeightApplicationLog row written carrying the
    cycle context + the weight set the engine consumed."""
    # Seed one agent_weight_history row so get_current_weights returns
    # an adaptive (non-empty seen) set.
    from backend.bot.learning.weight_adaptation import (
        compute_weight_proposals,
        persist_weight_proposals,
    )

    @dataclass
    class _Cal:
        agent: str
        n_closed: int
        hit_rate: float
        brier_score: float
        ece: Optional[float] = None

    cals: List[_Cal] = [
        _Cal(agent="market", n_closed=200, hit_rate=0.70,
             brier_score=0.0, ece=0.05),
    ]
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_advisory_enabled", True, raising=False,
    )
    report = compute_weight_proposals(
        agent_calibrations=cals, axis_calibrations=[],
    )
    persist_weight_proposals(report)

    # Flip apply on so get_current_weights actually returns adaptive.
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_apply_enabled", True, raising=False,
    )

    out = apply_weights_for_cycle(
        cycle_id="cycle-test-1",
        decision_provenance_id=None,
        composite_quality_at_apply=72.5,
    )
    # Sanity — engine got back an adaptive set, not just base.
    assert isinstance(out, dict) and out
    assert "market" in out

    with _session() as s:
        rows = s.execute(select(WeightApplicationLog)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        cycle = row.cycle_id
        comp = row.composite_quality_at_apply
        blob = row.weight_set_json
        hist_id = row.agent_weight_history_id
    assert cycle == "cycle-test-1"
    assert comp == 72.5
    assert blob is not None
    parsed = json.loads(blob)
    assert "market" in parsed
    assert hist_id is not None and hist_id > 0


def test_gap6_no_log_row_when_apply_disabled(temp_db, monkeypatch):
    """apply_weights_for_cycle with apply_enabled=False ⇒ NO log row
    written and the engine sees the base weights."""
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_apply_enabled", False, raising=False,
    )
    out = apply_weights_for_cycle(cycle_id="cycle-test-OFF")
    assert out == AGENT_BASE_WEIGHTS
    with _session() as s:
        n = s.execute(
            select(func.count()).select_from(WeightApplicationLog)
        ).scalar() or 0
    assert n == 0


def test_gap6_bare_get_current_weights_does_not_log(
    temp_db, monkeypatch,
):
    """get_current_weights() without log_application=True must NOT
    write a row — the advisor's read path uses this and would flood
    the log if it logged on every call."""
    # Seed one history row so an adaptive set is available.
    from backend.bot.learning.weight_adaptation import (
        compute_weight_proposals,
        persist_weight_proposals,
    )

    @dataclass
    class _Cal:
        agent: str
        n_closed: int
        hit_rate: float
        brier_score: float
        ece: Optional[float] = None

    cals = [_Cal(agent="market", n_closed=200,
                 hit_rate=0.70, brier_score=0.0, ece=0.05)]
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_advisory_enabled", True, raising=False,
    )
    persist_weight_proposals(compute_weight_proposals(
        agent_calibrations=cals, axis_calibrations=[],
    ))
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_apply_enabled", True, raising=False,
    )

    _ = get_current_weights()  # no log_application kwarg

    with _session() as s:
        n = s.execute(
            select(func.count()).select_from(WeightApplicationLog)
        ).scalar() or 0
    assert n == 0


def test_gap6_prune_removes_old_rows_keeps_recent(temp_db):
    """Insert 1 old (40d ago) + 2 fresh rows; prune with default TTL
    (30d) should delete only the old one."""
    with _session() as s:
        now = datetime.utcnow()
        s.add(WeightApplicationLog(
            applied_at=now - timedelta(days=40),
            cycle_id="old", weight_set_json="{}",
        ))
        s.add(WeightApplicationLog(
            applied_at=now - timedelta(days=2),
            cycle_id="fresh-1", weight_set_json="{}",
        ))
        s.add(WeightApplicationLog(
            applied_at=now - timedelta(hours=1),
            cycle_id="fresh-2", weight_set_json="{}",
        ))
        s.flush()

    deleted = prune_weight_application_log()
    assert deleted == 1

    with _session() as s:
        rows = list(s.execute(
            select(WeightApplicationLog.cycle_id)
            .order_by(WeightApplicationLog.applied_at)
        ).scalars().all())
    assert set(rows) == {"fresh-1", "fresh-2"}


def test_gap6_latest_weight_application_rows_filters_by_agent(temp_db):
    """The agent filter is a substring match on weight_set_json."""
    with _session() as s:
        s.add(WeightApplicationLog(
            applied_at=datetime.utcnow(),
            cycle_id="c1",
            weight_set_json=json.dumps({"market": 1.1, "macro": 1.0}),
        ))
        s.add(WeightApplicationLog(
            applied_at=datetime.utcnow(),
            cycle_id="c2",
            weight_set_json=json.dumps({"simulator": 0.9}),
        ))
        s.flush()

    rows = latest_weight_application_rows(limit=10, agent="market")
    assert len(rows) == 1
    assert rows[0]["cycle_id"] == "c1"


# ── Gap 9 — stability check ───────────────────────────────────────────


def test_gap9_stability_with_three_matching_priors_is_stable(temp_db):
    """3 prior rows within tolerance ⇒ is_stable True. The
    n_consecutive_required default is 3 so we need 2 priors plus the
    current value to qualify."""
    rule = "low_confidence"
    cur = 0.50
    now = datetime.utcnow()
    with _session() as s:
        # priors at 0.50 and 0.51 → both within 5%.
        s.add(PolicyTuning(
            computed_at=now - timedelta(days=1),
            rule_name=rule, threshold_attr="x", current_value=0.4,
            recommended_value=0.50,
            recommendation_confidence="high", rationale="",
        ))
        s.add(PolicyTuning(
            computed_at=now - timedelta(days=2),
            rule_name=rule, threshold_attr="x", current_value=0.4,
            recommended_value=0.51,
            recommendation_confidence="high", rationale="",
        ))
        s.flush()

    out = _stability_check(
        rule_name=rule,
        current_recommendation=cur,
        n_consecutive_required=3,
        tolerance_pct=STABILITY_TOLERANCE_PCT,
    )
    assert out["is_stable"] is True
    assert out["n_consecutive_matching"] == 2  # the 2 priors we wrote


def test_gap9_stability_with_outlier_prior_is_not_stable(temp_db):
    """A single outlier prior (>5% drift) fails the stability check."""
    rule = "low_confidence"
    cur = 0.50
    now = datetime.utcnow()
    with _session() as s:
        s.add(PolicyTuning(
            computed_at=now - timedelta(days=1),
            rule_name=rule, threshold_attr="x", current_value=0.4,
            recommended_value=0.50,
            recommendation_confidence="high", rationale="",
        ))
        # Outlier: 0.80 vs current 0.50 → +60% delta, way outside 5%.
        s.add(PolicyTuning(
            computed_at=now - timedelta(days=2),
            rule_name=rule, threshold_attr="x", current_value=0.4,
            recommended_value=0.80,
            recommendation_confidence="high", rationale="",
        ))
        s.flush()

    out = _stability_check(
        rule_name=rule,
        current_recommendation=cur,
        n_consecutive_required=3,
        tolerance_pct=STABILITY_TOLERANCE_PCT,
    )
    assert out["is_stable"] is False
    assert out["n_consecutive_matching"] == 1


def test_gap9_stability_no_priors_is_not_stable(temp_db):
    """No prior rows at all ⇒ can't be stable — first nightly run
    always demotes to medium."""
    out = _stability_check(
        rule_name="never_seen",
        current_recommendation=0.50,
    )
    assert out["is_stable"] is False
    assert out["n_consecutive_matching"] == 0
    assert out["priors_consulted"] == 0


# ── Gap 10 — impact measurement ───────────────────────────────────────


def test_gap10_delta_dict_returns_none_when_either_side_is_none():
    before = WindowMetrics(
        n_decisions=10, n_closed=5, submission_rate=0.5,
        composite_mean=60.0, hit_rate=None, mean_pnl_pct=None,
    )
    after = WindowMetrics(
        n_decisions=8, n_closed=6, submission_rate=0.6,
        composite_mean=70.0, hit_rate=0.5, mean_pnl_pct=2.0,
    )
    delta = _delta_dict(before, after)
    assert delta["submission_rate"] == pytest.approx(0.1)
    assert delta["composite_mean"] == pytest.approx(10.0)
    # hit_rate and mean_pnl_pct must be None because the before window
    # had None for both.
    assert delta["hit_rate"] is None
    assert delta["mean_pnl_pct"] is None


def test_gap10_significance_below_min_n_returns_false():
    before = WindowMetrics(
        n_decisions=20, n_closed=5,  # below MIN_N
        submission_rate=0.5, composite_mean=60.0,
        hit_rate=0.4, mean_pnl_pct=1.0,
    )
    after = WindowMetrics(
        n_decisions=20, n_closed=5,
        submission_rate=0.6, composite_mean=70.0,
        hit_rate=0.5, mean_pnl_pct=2.0,
    )
    delta = _delta_dict(before, after)
    sig, note = _is_significant(delta, before, after)
    assert sig is False
    assert note == "insufficient_sample_size_for_significance"


def test_gap10_significance_above_min_n_clears_noise_floor():
    before = WindowMetrics(
        n_decisions=40, n_closed=MIN_N_FOR_SIGNIFICANCE + 5,
        submission_rate=0.5, composite_mean=60.0,
        hit_rate=0.4, mean_pnl_pct=1.0,
    )
    after = WindowMetrics(
        n_decisions=40, n_closed=MIN_N_FOR_SIGNIFICANCE + 5,
        submission_rate=0.6,
        composite_mean=60.0 + NOISE_FLOOR_COMPOSITE_DELTA + 1.0,
        hit_rate=0.5, mean_pnl_pct=2.0,
    )
    delta = _delta_dict(before, after)
    sig, note = _is_significant(delta, before, after)
    assert sig is True
    assert note == "windows_computed"


def test_gap10_compute_impact_unknown_event_type_returns_safe_shape(
    temp_db,
):
    rpt = compute_impact("not_an_event", 1)
    assert rpt.note == "unknown_event_type"
    assert rpt.is_significant is False
    assert rpt.metrics_before is None
    assert rpt.metrics_after is None


def test_gap10_compute_impact_event_not_found_returns_note(temp_db):
    rpt = compute_impact(EVENT_TYPE_WEIGHT_APPLY, 99999)
    assert rpt.note == "event_row_not_found"
    assert rpt.is_significant is False


def test_gap10_compute_impact_with_event_timestamp_override(temp_db):
    """Pass event_timestamp explicitly to bypass the source-table lookup.
    With no decision_provenance rows in either window we expect the
    insufficient_sample_size note + is_significant=False."""
    ts = datetime.utcnow() - timedelta(days=3)
    rpt = compute_impact(
        EVENT_TYPE_WEIGHT_APPLY, event_id=1,
        before_window_days=7, after_window_days=7,
        event_timestamp=ts,
    )
    assert rpt.is_significant is False
    assert rpt.metrics_before is not None
    assert rpt.metrics_after is not None
    # With zero rows, every metric is None.
    assert rpt.metrics_before.n_closed == 0
    assert rpt.metrics_after.n_closed == 0
    assert rpt.note == "insufficient_sample_size_for_significance"


def test_gap10_persist_impact_reports_roundtrips(temp_db):
    rpt = LearningImpactReport(
        learning_event_type=EVENT_TYPE_WEIGHT_APPLY,
        event_id=42,
        event_timestamp=datetime.utcnow() - timedelta(days=1),
        before_window_days=7,
        after_window_days=7,
        metrics_before=WindowMetrics(
            n_decisions=10, n_closed=5,
            submission_rate=0.5, composite_mean=60.0,
            hit_rate=0.5, mean_pnl_pct=1.0,
        ),
        metrics_after=WindowMetrics(
            n_decisions=10, n_closed=5,
            submission_rate=0.6, composite_mean=65.0,
            hit_rate=0.6, mean_pnl_pct=1.5,
        ),
        delta={"submission_rate": 0.1, "composite_mean": 5.0,
               "hit_rate": 0.1, "mean_pnl_pct": 0.5},
        is_significant=False,
        note="insufficient_sample_size_for_significance",
    )
    meta = persist_impact_reports([rpt])
    assert meta["ok"] is True
    assert meta["written"] == 1
    rows = latest_impact_rows(limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["learning_event_type"] == EVENT_TYPE_WEIGHT_APPLY
    assert row["event_id"] == 42
    assert row["is_significant"] == 0
    # Delta JSON round-trips.
    assert row["delta_json"] is not None
    parsed = json.loads(row["delta_json"])
    assert parsed["composite_mean"] == 5.0


def test_gap10_latest_impact_rows_filters_by_event_type(temp_db):
    rpt = LearningImpactReport(
        learning_event_type=EVENT_TYPE_WEIGHT_APPLY,
        event_id=1,
        event_timestamp=datetime.utcnow(),
        delta={},
    )
    persist_impact_reports([rpt])
    out_weight = latest_impact_rows(event_type=EVENT_TYPE_WEIGHT_APPLY)
    out_policy = latest_impact_rows(event_type="policy_apply")
    assert len(out_weight) == 1
    assert len(out_policy) == 0


# ── Gap 12 — counterfactual code version ──────────────────────────────


def test_gap12_sizing_to_dict_carries_code_version():
    cf = SizingCounterfactual(
        factors=[1.0],
        original_pnl=10.0,
        original_factor=1.0,
        pnl_curve=[(1.0, 10.0)],
    )
    payload = cf.to_dict()
    assert payload["code_version"] == COUNTERFACTUAL_CODE_VERSION


def test_gap12_get_code_version_matches_constant():
    assert get_code_version() == COUNTERFACTUAL_CODE_VERSION


def test_gap12_cache_version_status_matches_when_payload_is_current():
    payload = {"code_version": COUNTERFACTUAL_CODE_VERSION, "x": 1}
    out = cache_version_status(payload)
    assert out["cache_version_mismatch"] is False
    assert out["cached_code_version"] == COUNTERFACTUAL_CODE_VERSION
    assert out["current_code_version"] == COUNTERFACTUAL_CODE_VERSION


def test_gap12_cache_version_status_flags_legacy_payload_without_field():
    payload = {"x": 1}  # legacy — no code_version key
    out = cache_version_status(payload)
    assert out["cache_version_mismatch"] is True
    assert out["cached_code_version"] is None


def test_gap12_cache_version_status_flags_stale_payload():
    payload = {"code_version": "v0.0.1"}  # pretend a prior version
    out = cache_version_status(payload)
    # Only mismatch when the current constant has been bumped past v0.0.1.
    if COUNTERFACTUAL_CODE_VERSION != "v0.0.1":
        assert out["cache_version_mismatch"] is True
    assert out["cached_code_version"] == "v0.0.1"


def test_gap12_cache_version_status_handles_none_payload():
    out = cache_version_status(None)
    assert out["cache_version_mismatch"] is True
    assert out["cached_code_version"] is None
