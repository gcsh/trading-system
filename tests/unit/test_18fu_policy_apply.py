"""MITS Phase 18-FU Gap 1 — Policy auto-tuning APPLY path unit tests.

Covers:

  * ``get_applied_thresholds`` returns ``{}`` when
    ``TUNABLES.policy_tuning_auto_apply_enabled`` is False (the kill
    switch is honored).
  * ``get_applied_thresholds`` returns ``{}`` when no policy_tunings
    row is operator_approved.
  * ``apply_to_tunable_context`` injects scratch fields.
  * Unknown threshold_attr values in the DB are dropped (allow-list).
  * ``resolve_threshold`` returns ``(default, default_evidence)`` when
    no override exists.
  * ``resolve_threshold`` returns ``(override, override_evidence)``
    with ``policy_tunings_id_<N>`` source string when override exists.
  * Replay invariant: the override + its source are deterministic — a
    second call with the same DB state yields the same evidence dict.
  * Cache hit / refresh: a second call within TTL returns cached
    value without DB query.
  * ``mark_thresholds_applied`` stamps applied_at on consulted rows.
  * ``invalidate_cache`` forces re-query.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, Optional

import pytest

from backend.bot.decision.policy import PolicyContext
from backend.bot.learning.policy_apply import (
    _allowed_threshold_attrs,
    apply_to_tunable_context,
    applied_threshold_ids,
    get_applied_thresholds,
    invalidate_cache,
    mark_thresholds_applied,
    resolve_threshold,
)
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.policy_tuning import PolicyTuning


pytestmark = [pytest.mark.unit]


# ── Helpers ──────────────────────────────────────────────────────────


def _empty_ctx() -> PolicyContext:
    """Build a minimal PolicyContext for scratch-injection tests."""
    return PolicyContext(
        ticker="AAPL",
        signal=None,
        event={},
        data={},
        analytics_cfg={},
        ai_config={},
        config={},
        kill_active=False,
        portfolio_risk_dict=None,
        eod_bias_map={},
        brain_cooldown={},
        use_brain=False,
        cycle_id="test",
    )


def _write_tuning(
    *, rule_name: str, threshold_attr: str,
    recommended_value: float, operator_approved: int = 1,
    current_value: float = 0.5,
) -> int:
    """Persist one policy_tunings row + return its id."""
    with session_scope() as s:
        row = PolicyTuning(
            computed_at=datetime.utcnow(),
            rule_name=rule_name,
            threshold_attr=threshold_attr,
            current_value=current_value,
            recommended_value=recommended_value,
            recommendation_confidence="high",
            rationale="test",
            payload_json="{}",
            operator_reviewed=1,
            operator_approved=int(operator_approved),
        )
        s.add(row)
        s.flush()
        return int(row.id)


# ── Kill switch ──────────────────────────────────────────────────────


def test_kill_switch_off_returns_empty(temp_db, monkeypatch):
    """When auto_apply_enabled is False, the helper never reads DB."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", False,
    )
    invalidate_cache()
    # Even with an approved row sitting in DB, returns {}.
    _write_tuning(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        recommended_value=0.30,
    )
    assert get_applied_thresholds() == {}


def test_kill_switch_on_no_approved_rows_returns_empty(temp_db, monkeypatch):
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    assert get_applied_thresholds() == {}


# ── Approved-row read ─────────────────────────────────────────────────


def test_approved_row_appears_in_overrides(temp_db, monkeypatch):
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    _write_tuning(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        recommended_value=0.30,
    )
    overrides = get_applied_thresholds(force_refresh=True)
    assert overrides.get("config.min_confidence") == pytest.approx(0.30)


def test_unapproved_row_is_ignored(temp_db, monkeypatch):
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    _write_tuning(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        recommended_value=0.30,
        operator_approved=0,
    )
    overrides = get_applied_thresholds(force_refresh=True)
    assert overrides == {}


def test_most_recent_approved_row_wins(temp_db, monkeypatch):
    """If multiple approved rows exist for the same threshold_attr,
    the most-recently-computed wins."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    _write_tuning(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        recommended_value=0.40,
    )
    time.sleep(0.01)  # ensure distinct computed_at timestamps
    _write_tuning(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        recommended_value=0.55,
    )
    overrides = get_applied_thresholds(force_refresh=True)
    assert overrides.get("config.min_confidence") == pytest.approx(0.55)


def test_unknown_threshold_attr_is_dropped(temp_db, monkeypatch):
    """A row with a threshold_attr not in TUNABLE_RULES is rejected."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    _write_tuning(
        rule_name="suspicious_rule",
        threshold_attr="arbitrary_attribute_not_in_registry",
        recommended_value=99.0,
    )
    overrides = get_applied_thresholds(force_refresh=True)
    assert "arbitrary_attribute_not_in_registry" not in overrides


# ── Context injection ────────────────────────────────────────────────


def test_apply_to_tunable_context_injects_scratch(
    temp_db, monkeypatch,
):
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    _write_tuning(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        recommended_value=0.30,
    )
    ctx = _empty_ctx()
    apply_to_tunable_context(ctx)
    assert "applied_thresholds" in ctx.scratch
    assert ctx.scratch["applied_thresholds"].get(
        "config.min_confidence"
    ) == pytest.approx(0.30)
    assert "applied_threshold_ids" in ctx.scratch


def test_apply_with_kill_switch_off_writes_empty_dict(monkeypatch):
    """Operator MUST be able to flip auto-apply OFF and immediately
    see overrides disappear from scratch on next cycle."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", False,
    )
    invalidate_cache()
    ctx = _empty_ctx()
    apply_to_tunable_context(ctx)
    assert ctx.scratch["applied_thresholds"] == {}
    assert ctx.scratch["applied_threshold_ids"] == []


# ── resolve_threshold helper ─────────────────────────────────────────


def test_resolve_threshold_returns_default_when_no_override(monkeypatch):
    """Without an override, returns the TUNABLE default and tags
    the source as 'tunable_default'."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", False,
    )
    invalidate_cache()
    ctx = _empty_ctx()
    apply_to_tunable_context(ctx)
    value, evidence = resolve_threshold(
        ctx,
        threshold_attr="config.min_confidence",
        tunable_default=0.60,
    )
    assert value == pytest.approx(0.60)
    assert evidence["threshold_source"] == "tunable_default"
    assert evidence["threshold_value_used"] == pytest.approx(0.60)


def test_resolve_threshold_returns_override(temp_db, monkeypatch):
    """With an override, returns the override + tags the source with
    the policy_tunings row id so replay traces it back."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    row_id = _write_tuning(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        recommended_value=0.30,
    )
    ctx = _empty_ctx()
    apply_to_tunable_context(ctx)
    value, evidence = resolve_threshold(
        ctx,
        threshold_attr="config.min_confidence",
        tunable_default=0.60,
    )
    assert value == pytest.approx(0.30)
    assert evidence["threshold_source"] == f"policy_tunings_id_{row_id}"
    assert evidence["threshold_value_used"] == pytest.approx(0.30)
    assert evidence["threshold_default"] == pytest.approx(0.60)


def test_resolve_threshold_replay_determinism(temp_db, monkeypatch):
    """Replay invariant: identical scratch state produces identical
    evidence on every call. The threshold value AND the source id
    must round-trip bit-identical."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    _write_tuning(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        recommended_value=0.42,
    )
    ctx = _empty_ctx()
    apply_to_tunable_context(ctx)
    v1, e1 = resolve_threshold(
        ctx,
        threshold_attr="config.min_confidence",
        tunable_default=0.60,
    )
    v2, e2 = resolve_threshold(
        ctx,
        threshold_attr="config.min_confidence",
        tunable_default=0.60,
    )
    assert v1 == v2
    assert e1 == e2


# ── Cache + invalidation ─────────────────────────────────────────────


def test_invalidate_cache_forces_refresh(temp_db, monkeypatch):
    """``invalidate_cache`` must drop the entry so the next call
    re-reads the DB. Operator UIs rely on this for instant-apply."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    _write_tuning(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        recommended_value=0.30,
    )
    first = get_applied_thresholds()
    # Add a newer approved row.
    time.sleep(0.01)
    _write_tuning(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        recommended_value=0.45,
    )
    # Without invalidation, cache returns the first value.
    cached = get_applied_thresholds()
    assert cached == first
    # After invalidation, the new value surfaces.
    invalidate_cache()
    fresh = get_applied_thresholds()
    assert fresh.get("config.min_confidence") == pytest.approx(0.45)


# ── mark_thresholds_applied ──────────────────────────────────────────


def test_mark_thresholds_applied_sets_applied_at(temp_db, monkeypatch):
    """Stamps applied_at on the row when the engine consults it."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    row_id = _write_tuning(
        rule_name="low_confidence",
        threshold_attr="config.min_confidence",
        recommended_value=0.30,
    )
    n = mark_thresholds_applied([row_id])
    assert n == 1
    with session_scope() as s:
        row = s.get(PolicyTuning, row_id)
        assert row.applied_at is not None


def test_mark_thresholds_applied_empty_list_is_noop(temp_db):
    n = mark_thresholds_applied([])
    assert n == 0


# ── Allow-list sanity ────────────────────────────────────────────────


def test_allowed_threshold_attrs_contains_8_rules():
    """The allow-list mirrors the 18.C TUNABLE_RULES registry."""
    allowed = _allowed_threshold_attrs()
    # 8 rules in 18.C catalog.
    assert len(allowed) == 8
    # Spot-check a few of the documented threshold_attr strings.
    assert "config.min_confidence" in allowed
    assert "hardcoded_iv_rank_ceiling" in allowed
    assert "TUNABLES.correlation_cap_rho" in allowed
