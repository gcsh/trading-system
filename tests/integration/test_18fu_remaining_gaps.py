"""MITS Phase 18-FU REMAINING-GAPS integration tests (R1, R2, R4).

End-to-end exercises for the 4 gaps closed in the 18-FU remaining-gaps
pass:

  * Gap R1 — wire the 5 remaining tunable policy rules
    (``simulator_veto``, ``catalyst_gate``, ``abstain_and_throttle_hi``,
    ``abstain_and_throttle_lo``, ``cycle_budget_overrun``) to consult
    ``resolve_threshold`` so operator-approved overrides are honored
    (where the rule body owns enforcement) AND stamped into evidence
    (always — even where the underlying callee still reads TUNABLES
    directly, so audit + replay see the threshold that was active).

  * Gap R2 — switch ``run_consensus``'s adaptive-weight apply path
    from un-logged ``get_current_weights()`` to logged
    ``apply_weights_for_cycle(cycle_id=...)`` so each consumption
    writes one ``weight_application_log`` row.

  * Gap R4 — reject ``POST /learning/approve`` (and rollback) for any
    row whose ``scope_name`` starts with a test sentinel prefix
    (``_test_`` or ``_18fu_``) — so a test-pollution row landing in
    the production DB can never be approved through the API.

Gap R3 is regression-fixed in-place by editing the 3 stale test files
(``test_thesis_health.py``, ``test_stage11_agents.py``,
``test_stage15_agent_voice.py``) — no new test needed.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from backend.bot.abstain import abstain_and_throttle
from backend.bot.decision.policy import PolicyContext
from backend.bot.decision.rules import (
    rule_abstain_and_throttle,
    rule_catalyst_gate,
    rule_cycle_budget_overrun,
    rule_simulator_veto,
)
from backend.bot.gates import catalyst_gate as _catalyst_gate
from backend.bot.learning.policy_apply import (
    apply_to_tunable_context,
    invalidate_cache,
    resolve_threshold,
)
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.learned_attribution import LearnedAttribution
from backend.models.policy_tuning import PolicyTuning


pytestmark = [pytest.mark.integration]


# ── Helpers ──────────────────────────────────────────────────────────


def _build_ctx(
    *, ticker: str = "AAPL", cycle_id: str = "test-cycle",
    use_brain: bool = False,
) -> PolicyContext:
    """Minimal PolicyContext used by the rule-body unit exercises."""
    return PolicyContext(
        ticker=ticker,
        signal=None, event={}, data={}, analytics_cfg={},
        ai_config={}, config={}, kill_active=False,
        portfolio_risk_dict=None, eod_bias_map={},
        brain_cooldown={}, use_brain=use_brain, cycle_id=cycle_id,
    )


def _seed_approved_tuning(
    *, threshold_attr: str, recommended_value: float,
    rule_name: str = "x", current_value: float = 0.0,
) -> int:
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
            operator_approved=1,
        )
        s.add(row)
        s.flush()
        return int(row.id)


# ── Gap R1.a — simulator_veto stamps threshold_evidence ──────────────


def test_R1_simulator_veto_evidence_default(temp_db, monkeypatch):
    """When auto-apply is OFF, simulator_veto's BlockingFactor.evidence
    records the TUNABLES default threshold source (replay-stable)."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", False,
    )
    invalidate_cache()
    ctx = _build_ctx()
    apply_to_tunable_context(ctx)
    # Synthetic consensus with a non-empty reject_reason.
    class _Consensus:
        simulator_verdict = {
            "reject_reason": "simulator_veto: p_max_loss=0.45 > threshold=0.30",
            "p_max_loss": 0.45,
        }
    ctx.scratch["consensus_obj"] = _Consensus()
    bf = rule_simulator_veto(ctx)
    assert bf is not None
    assert bf.evidence["threshold_source"] == "tunable_default"
    assert bf.evidence["threshold_default"] == pytest.approx(
        float(TUNABLES.simulator_max_loss_veto)
    )


def test_R1_simulator_veto_evidence_auto_applied(temp_db, monkeypatch):
    """When auto-apply is ON + an approved row exists, simulator_veto's
    evidence records the policy_tunings_id_<N> source."""
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    row_id = _seed_approved_tuning(
        threshold_attr="TUNABLES.simulator_max_loss_veto",
        recommended_value=0.20,
        rule_name="simulator_veto",
        current_value=float(TUNABLES.simulator_max_loss_veto),
    )
    ctx = _build_ctx()
    apply_to_tunable_context(ctx)
    class _Consensus:
        simulator_verdict = {
            "reject_reason": "simulator_veto: p_max_loss=0.45 > threshold=0.20",
            "p_max_loss": 0.45,
        }
    ctx.scratch["consensus_obj"] = _Consensus()
    bf = rule_simulator_veto(ctx)
    assert bf is not None
    assert bf.evidence["threshold_source"] == f"policy_tunings_id_{row_id}"
    assert bf.evidence["threshold_value_used"] == pytest.approx(0.20)


# ── Gap R1.b — catalyst_gate enforces override AND stamps evidence ───


def test_R1_catalyst_gate_check_accepts_override():
    """catalyst_gate.check now accepts a ``short_dte_threshold`` kwarg
    that overrides the TUNABLES default. Synthetic earnings-close +
    DTE=10: passes with default threshold 7, REJECTS with override 12.
    """
    from datetime import datetime as _dt, timedelta
    now = _dt(2026, 6, 12, 10, 0, 0)
    # Force the earnings_event path: monkeypatch _next_earnings_date.
    earnings_d = (now + timedelta(days=3)).date()
    import backend.bot.gates.catalyst_gate as cgm
    saved = cgm._next_earnings_date
    cgm._next_earnings_date = lambda t, n: earnings_d
    try:
        # Default threshold 7, DTE=10 → not blocked (10 > 7).
        r_default = _catalyst_gate.check(
            "AAPL", instrument="option", dte=10, now=now,
        )
        assert r_default.passes is True
        # Override threshold to 12, DTE=10 → BLOCKED (10 <= 12).
        r_override = _catalyst_gate.check(
            "AAPL", instrument="option", dte=10, now=now,
            short_dte_threshold=12,
        )
        assert r_override.passes is False
        assert "DTE=10" in (r_override.reason or "")
        assert "≤ 12 threshold" in (r_override.reason or "")
    finally:
        cgm._next_earnings_date = saved


# ── Gap R1.c — abstain_and_throttle accepts band overrides ───────────


def test_R1_abstain_bands_threaded_through():
    """abstain_and_throttle now accepts ``band_lo``/``band_hi`` kwargs.
    A probability of 0.55 hits the default band [0.50, 0.58] but NOT
    a narrowed override band [0.60, 0.70] — proves the override
    actually changes enforcement (not just observation)."""
    common = dict(
        action="BUY_STOCK", probability=0.55,
        expected_move_pct=0.01, total_cost_bps=200.0,
    )
    # Default band — hits.
    d_default = abstain_and_throttle(**common)
    assert d_default.monitor_only is True
    assert "no_trade_band" in d_default.triggered_rules
    # Narrowed override band that EXCLUDES 0.55 — no abstain.
    d_override = abstain_and_throttle(
        **common, band_lo=0.60, band_hi=0.70,
    )
    assert d_override.monitor_only is False
    assert "no_trade_band" not in d_override.triggered_rules


# ── Gap R1.d — cycle_budget_overrun stamps threshold_evidence ────────


def test_R1_cycle_budget_overrun_evidence_default(temp_db, monkeypatch):
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", False,
    )
    invalidate_cache()
    ctx = _build_ctx()
    apply_to_tunable_context(ctx)
    ctx.scratch["cycle_budget_overrun_seconds"] = 250.0
    bf = rule_cycle_budget_overrun(ctx)
    assert bf is not None
    assert bf.evidence["threshold_source"] == "tunable_default"
    assert bf.evidence["budget_seconds"] == pytest.approx(250.0)


def test_R1_cycle_budget_overrun_evidence_auto_applied(temp_db, monkeypatch):
    monkeypatch.setattr(
        TUNABLES, "policy_tuning_auto_apply_enabled", True,
    )
    invalidate_cache()
    row_id = _seed_approved_tuning(
        threshold_attr="TUNABLES.engine_cycle_timeout_sec",
        recommended_value=120.0,
        rule_name="cycle_budget_overrun",
        current_value=240.0,
    )
    ctx = _build_ctx()
    apply_to_tunable_context(ctx)
    ctx.scratch["cycle_budget_overrun_seconds"] = 250.0
    bf = rule_cycle_budget_overrun(ctx)
    assert bf is not None
    assert bf.evidence["threshold_source"] == f"policy_tunings_id_{row_id}"
    assert bf.evidence["threshold_value_used"] == pytest.approx(120.0)


# ── Gap R2 — apply_weights_for_cycle writes weight_application_log ───


def test_R2_apply_weights_for_cycle_writes_log_when_apply_on(
    temp_db, monkeypatch,
):
    """When apply_enabled=True AND agent_weight_history has rows, calling
    apply_weights_for_cycle writes one weight_application_log row keyed
    to the cycle_id."""
    from backend.bot.learning.weight_adaptation import (
        apply_weights_for_cycle,
    )
    from backend.models.agent_weight_history import AgentWeightHistory
    from backend.models.weight_application_log import WeightApplicationLog
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_apply_enabled", True,
    )
    # Seed one history row per agent so the apply path has something
    # to consume.
    from backend.bot.learning.attribution import KNOWN_AGENTS
    with session_scope() as s:
        ts = datetime.utcnow()
        for name in KNOWN_AGENTS:
            s.add(AgentWeightHistory(
                computed_at=ts, agent=name,
                base_weight=1.0, weight_proposed=1.1, weight_active=1.1,
                adaptive_multiplier=1.1, n_closed=50,
                confidence_level="medium", rationale="seed",
                payload_json="{}",
            ))
    out = apply_weights_for_cycle(cycle_id="gate_b_test")
    assert isinstance(out, dict)
    # All 8 agents should be in the returned set.
    assert set(out.keys()) == set(KNOWN_AGENTS)
    with session_scope() as s:
        rows = s.query(WeightApplicationLog).all()
        assert len(rows) == 1
        assert rows[0].cycle_id == "gate_b_test"


def test_R2_apply_weights_for_cycle_noop_when_apply_off(
    temp_db, monkeypatch,
):
    """When apply_enabled=False the function returns base weights and
    writes NO weight_application_log row."""
    from backend.bot.learning.weight_adaptation import (
        AGENT_BASE_WEIGHTS, apply_weights_for_cycle,
    )
    from backend.models.weight_application_log import WeightApplicationLog
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_apply_enabled", False,
    )
    out = apply_weights_for_cycle(cycle_id="gate_b_off_test")
    assert out == AGENT_BASE_WEIGHTS
    with session_scope() as s:
        rows = s.query(WeightApplicationLog).all()
        assert len(rows) == 0


# ── Gap R4 — test-sentinel guard on approve/rollback ─────────────────


def test_R4_approve_rejects_test_sentinel_scope_name(temp_db):
    """POST /learning/approve with a row whose scope_name starts with
    _test_ or _18fu_ returns HTTP 400."""
    # Seed a learned_attribution row with a test-sentinel scope_name.
    with session_scope() as s:
        row = LearnedAttribution(
            computed_at=datetime.utcnow(),
            scope_kind="agent",
            scope_name="_18fu_uxC_gateE",
            window_days=90, n_closed=99,
            hit_rate=0.6, hit_rate_wilson_lower=0.5,
            hit_rate_wilson_upper=0.7, brier_score=0.2,
            ece=0.1, mean_pnl_pct=0.5,
            payload_json="{}",
        )
        s.add(row)
        s.flush()
        row_id = int(row.id)
    from backend.main import app
    client = TestClient(app)
    resp = client.post("/learning/approve", json={
        "table": "learned_attribution",
        "row_id": row_id,
    })
    assert resp.status_code == 400
    body = resp.json()
    assert "_18fu_uxC_gateE" in body["detail"]
    assert "test sentinel" in body["detail"]


def test_R4_approve_accepts_real_scope_name(temp_db):
    """Sanity: a non-sentinel scope_name still approves cleanly."""
    with session_scope() as s:
        row = LearnedAttribution(
            computed_at=datetime.utcnow(),
            scope_kind="agent",
            scope_name="market",
            window_days=90, n_closed=99,
            hit_rate=0.6, hit_rate_wilson_lower=0.5,
            hit_rate_wilson_upper=0.7, brier_score=0.2,
            ece=0.1, mean_pnl_pct=0.5,
            payload_json="{}",
        )
        s.add(row)
        s.flush()
        row_id = int(row.id)
    from backend.main import app
    client = TestClient(app)
    resp = client.post("/learning/approve", json={
        "table": "learned_attribution",
        "row_id": row_id,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["row"]["scope_name"] == "market"


def test_R4_rollback_also_rejects_test_sentinel(temp_db):
    """Same guard applies to POST /learning/rollback."""
    with session_scope() as s:
        row = LearnedAttribution(
            computed_at=datetime.utcnow(),
            scope_kind="agent",
            scope_name="_test_pollute_1",
            window_days=90, n_closed=99,
            hit_rate=0.6, hit_rate_wilson_lower=0.5,
            hit_rate_wilson_upper=0.7, brier_score=0.2,
            ece=0.1, mean_pnl_pct=0.5,
            payload_json="{}",
        )
        s.add(row)
        s.flush()
        row_id = int(row.id)
    from backend.main import app
    client = TestClient(app)
    resp = client.post("/learning/rollback", json={
        "table": "learned_attribution",
        "row_id": row_id,
    })
    assert resp.status_code == 400
    assert "_test_pollute_1" in resp.json()["detail"]


def test_R4_allow_test_sentinel_kwarg_opt_out(temp_db):
    """Tests that legitimately need to drive the approve path on a
    sentinel row pass ``allow_test_sentinel=True`` directly to the
    helper. The HTTP route NEVER sets this flag."""
    from backend.api.routes.learning import _apply_review
    with session_scope() as s:
        row = LearnedAttribution(
            computed_at=datetime.utcnow(),
            scope_kind="agent",
            scope_name="_test_unit_x",
            window_days=90, n_closed=99,
            hit_rate=0.6, hit_rate_wilson_lower=0.5,
            hit_rate_wilson_upper=0.7, brier_score=0.2,
            ece=0.1, mean_pnl_pct=0.5,
            payload_json="{}",
        )
        s.add(row)
        s.flush()
        row_id = int(row.id)
    out = _apply_review(
        "learned_attribution", row_id,
        action="approve", notes=None,
        allow_test_sentinel=True,
    )
    assert out["ok"] is True
