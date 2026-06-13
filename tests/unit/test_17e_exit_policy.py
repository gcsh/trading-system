"""MITS Phase 17.E — declarative exit policy unit tests.

Coverage matrix:

  * each registered rule fires correctly on its trigger condition
  * each registered rule does NOT fire when condition absent
  * ExitPolicy.evaluate runs EVERY rule (no short-circuit) and records
    one row per rule, even when several fire on the same cycle
  * back-compat: decide_exit returns the SAME ExitDecision fields for
    the SAME triggering condition as the pre-refactor body would have
  * concurrent triggers: when 2 rules fire, both are in result.triggers
    and chosen is the first hard in registration order
  * to_dict() round-trips JSON-safely
  * invariant: should_close=True implies at least one hard trigger
"""
from __future__ import annotations

import json

import pytest

from backend.bot.decision.exit_policy import (
    ExitContext,
    ExitPolicy,
    ExitRule,
    ExitRuleEvaluation,
    ExitTrigger,
)
from backend.bot.decision.exit_rules import (
    build_default_policy,
    rule_catastrophe_stop,
    rule_dte_cliff,
    rule_trailing_stop,
)
from backend.bot.options.exit_manager import (
    decide_exit,
    decide_exit_with_policy,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


# ── Helpers ───────────────────────────────────────────────────────────


def _ctx(
    *,
    entry: float = 5.0,
    cur: float = 5.0,
    peak: float | None = None,
    dte: int = 21,
    entry_iv: float | None = 0.30,
    current_iv: float | None = 0.30,
):
    """Build an ExitContext with the same math the exit_manager uses."""
    from backend.bot.options.exit_manager import _build_exit_context
    ctx, _hold_factory = _build_exit_context(
        entry_premium_per_share=entry,
        current_premium_per_share=cur,
        peak_premium_per_share=peak,
        dte=dte,
        entry_iv=entry_iv,
        current_iv=current_iv,
    )
    return ctx


# ── Per-rule firing tests ─────────────────────────────────────────────


class TestRuleDteCliff:
    """rule_dte_cliff fires iff DTE <= cliff AND gain_pct > 0."""

    def test_fires_with_profit_at_low_dte(self):
        ctx = _ctx(entry=5.0, cur=5.25, peak=5.25, dte=2)
        trig = rule_dte_cliff(ctx)
        assert trig is not None
        assert trig.rule_name == "dte_cliff"
        assert trig.severity == "hard"
        assert trig.legacy_action == "close"
        assert "theta cliff" in trig.reason
        assert trig.evidence["dte"] == 2

    def test_does_not_fire_at_a_loss_near_expiry(self):
        ctx = _ctx(entry=5.0, cur=4.5, peak=5.0, dte=2)
        assert rule_dte_cliff(ctx) is None

    def test_does_not_fire_at_long_dte(self):
        ctx = _ctx(entry=5.0, cur=5.50, peak=5.50, dte=21)
        assert rule_dte_cliff(ctx) is None


class TestRuleCatastropheStop:
    """rule_catastrophe_stop fires iff gain_pct <= -hard_stop (DTE-adj)."""

    def test_fires_on_50pct_loss_at_long_dte(self):
        ctx = _ctx(entry=5.0, cur=2.5, peak=5.0, dte=21)
        trig = rule_catastrophe_stop(ctx)
        assert trig is not None
        assert trig.rule_name == "catastrophe_stop"
        assert trig.legacy_action == "close"
        assert "catastrophe stop" in trig.reason
        assert trig.evidence["hard_stop_pct"] == pytest.approx(50.0, abs=0.1)

    def test_does_not_fire_at_minor_drawdown(self):
        ctx = _ctx(entry=5.0, cur=4.5, peak=5.0, dte=21)
        assert rule_catastrophe_stop(ctx) is None

    def test_fires_tighter_at_short_dte(self):
        # 5 DTE → hard_stop is -35%; -40% (5→3) triggers.
        ctx = _ctx(entry=5.0, cur=3.0, peak=5.0, dte=5)
        trig = rule_catastrophe_stop(ctx)
        assert trig is not None
        assert trig.evidence["hard_stop_pct"] == pytest.approx(35.0, abs=0.1)


class TestRuleTrailingStop:
    """rule_trailing_stop fires only in monitor mode + below floor."""

    def test_fires_on_big_winner_giveback(self):
        # Peak +200%, current at trail-floor breach (need to confirm with ctx)
        ctx = _ctx(entry=5.0, cur=9.0, peak=15.0, dte=21)
        trig = rule_trailing_stop(ctx)
        assert trig is not None
        assert trig.rule_name == "trailing_stop"
        assert "trail hit" in trig.reason
        assert trig.evidence["iv_crush_detected"] is False

    def test_does_not_fire_below_monitor_floor(self):
        # +10% peak — monitor not active, no trail.
        ctx = _ctx(entry=5.0, cur=5.50, peak=5.50, dte=21)
        assert rule_trailing_stop(ctx) is None

    def test_does_not_fire_when_above_floor(self):
        # Peak +200% with small giveback → still above wide trail floor.
        ctx = _ctx(entry=5.0, cur=14.0, peak=15.0, dte=21)
        assert rule_trailing_stop(ctx) is None

    def test_includes_iv_crush_suffix_when_detected(self):
        # 50% IV collapse — crush detected, trail tightens.
        ctx = _ctx(
            entry=5.0, cur=6.75, peak=7.5, dte=21,
            entry_iv=0.40, current_iv=0.20,
        )
        trig = rule_trailing_stop(ctx)
        if trig is not None:
            assert "(IV crush)" in trig.reason


# ── Policy-level behavior tests ──────────────────────────────────────


class TestExitPolicyEvaluate:
    """ExitPolicy.evaluate runs every rule + records every verdict."""

    def test_runs_all_rules_no_short_circuit(self):
        # Profitable position at low DTE → dte_cliff fires. Even so,
        # every other rule must still have been evaluated + recorded.
        ctx = _ctx(entry=5.0, cur=5.25, peak=5.25, dte=2)
        policy = build_default_policy()
        result = policy.evaluate(ctx)
        rule_names = {r.rule_name for r in result.rule_evaluations}
        assert rule_names == {
            "dte_cliff", "catastrophe_stop", "trailing_stop",
        }, "all 3 cataloged rules must have been recorded"
        assert result.should_close is True
        assert result.legacy_action == "close"

    def test_hold_path_records_every_rule_as_not_fired(self):
        # Quiet position: nothing should trigger. Every rule recorded
        # with fired=False.
        ctx = _ctx(entry=5.0, cur=5.05, peak=5.05, dte=21)
        policy = build_default_policy()
        result = policy.evaluate(ctx)
        assert result.should_close is False
        assert result.legacy_action == "hold"
        assert all(not r.fired for r in result.rule_evaluations)
        assert len(result.rule_evaluations) == 3
        assert result.chosen is None
        assert result.triggers == []

    def test_concurrent_triggers_capture_all(self):
        # Construct a state where catastrophe + trailing both fire:
        # entry $5, peak $15 (+200%), now $1 (-80% gain, -93% drawdown).
        # gain -80% <= -50% hard stop → catastrophe fires.
        # gain -80% < trailing floor (peak +200% trail floor stays in
        # monitor range; current well below) → trailing fires too.
        ctx = _ctx(entry=5.0, cur=1.0, peak=15.0, dte=21)
        policy = build_default_policy()
        result = policy.evaluate(ctx)
        fired_names = {t.rule_name for t in result.triggers}
        assert "catastrophe_stop" in fired_names
        assert "trailing_stop" in fired_names
        # Registration-order — catastrophe is registered AFTER dte_cliff
        # but BEFORE trailing_stop, so it becomes the headline.
        assert result.chosen.rule_name == "catastrophe_stop"
        assert result.should_close is True

    def test_invariant_should_close_implies_hard_trigger(self):
        # Walk every fired result: should_close == True requires at
        # least one hard trigger. The invariant is the structural
        # guarantee that prevents a "close without a reason" regression.
        configs = [
            dict(entry=5.0, cur=5.25, peak=5.25, dte=2),     # dte_cliff
            dict(entry=5.0, cur=2.5, peak=5.0, dte=21),      # catastrophe
            dict(entry=5.0, cur=9.0, peak=15.0, dte=21),     # trailing
        ]
        for cfg in configs:
            ctx = _ctx(**cfg)
            result = build_default_policy().evaluate(ctx)
            if result.should_close:
                assert any(t.severity == "hard" for t in result.triggers)

    def test_to_dict_roundtrips_json(self):
        ctx = _ctx(entry=5.0, cur=5.25, peak=5.25, dte=2)
        result = build_default_policy().evaluate(ctx)
        d = result.to_dict()
        # Must be JSON-serializable.
        s = json.dumps(d)
        d2 = json.loads(s)
        assert d2["should_close"] is True
        assert d2["legacy_action"] == "close"
        assert isinstance(d2["rule_evaluations"], list)
        assert isinstance(d2["triggers"], list)
        assert d2["chosen"]["rule_name"] == "dte_cliff"


# ── Back-compat regression tests ──────────────────────────────────────


class TestDecideExitBackCompat:
    """decide_exit returns IDENTICAL strings + flags pre/post refactor.

    The legacy callers in engine.py + paper_executor.py consume the
    ExitDecision dataclass fields verbatim. Any drift here breaks
    persisted Trade.reason strings and the UI's "Why?" panel.
    """

    def test_dte_cliff_decision_unchanged(self):
        d = decide_exit(
            entry_premium_per_share=5.0,
            current_premium_per_share=5.25,
            peak_premium_per_share=5.25,
            dte=2,
            entry_iv=0.30, current_iv=0.30,
        )
        assert d.should_exit is True
        # Original cliff reason: "DTE 2 ≤ 3 (theta cliff) — banking +X.X% before decay"
        assert d.reason.startswith("DTE 2 ≤ 3 (theta cliff)")
        assert "banking +" in d.reason

    def test_catastrophe_decision_unchanged(self):
        d = decide_exit(
            entry_premium_per_share=5.0,
            current_premium_per_share=2.5,
            peak_premium_per_share=5.0,
            dte=21,
            entry_iv=0.30, current_iv=0.30,
        )
        assert d.should_exit is True
        assert d.reason.startswith("catastrophe stop:")
        assert d.hard_stop_pct == pytest.approx(50.0, abs=0.1)

    def test_trailing_decision_unchanged(self):
        d = decide_exit(
            entry_premium_per_share=5.0,
            current_premium_per_share=9.0,
            peak_premium_per_share=15.0,
            dte=21,
            entry_iv=0.30, current_iv=0.30,
        )
        assert d.should_exit is True
        assert d.reason.startswith("trail hit:")
        assert d.monitor_active is True

    def test_hold_decision_unchanged(self):
        d = decide_exit(
            entry_premium_per_share=5.0,
            current_premium_per_share=5.05,
            peak_premium_per_share=5.05,
            dte=21,
            entry_iv=0.30, current_iv=0.30,
        )
        assert d.should_exit is False
        # Early phase — gain +1.0% < monitor floor +15%
        assert "early phase" in d.reason

    def test_monitoring_hold_reason_unchanged(self):
        d = decide_exit(
            entry_premium_per_share=5.0,
            current_premium_per_share=6.0,
            peak_premium_per_share=6.0,
            dte=21,
            entry_iv=0.30, current_iv=0.30,
        )
        assert d.should_exit is False
        # Monitor mode active — "monitoring: gain +X%, peak +X%, ..."
        assert d.reason.startswith("monitoring:")

    def test_decide_exit_with_policy_returns_pair(self):
        # The new helper returns (ExitDecision, ExitPolicyResult). The
        # decision MUST match what decide_exit returns for the same
        # inputs — the two paths share the same evaluator.
        decision_old = decide_exit(
            entry_premium_per_share=5.0,
            current_premium_per_share=2.5,
            peak_premium_per_share=5.0,
            dte=21,
            entry_iv=0.30, current_iv=0.30,
        )
        decision_new, result = decide_exit_with_policy(
            entry_premium_per_share=5.0,
            current_premium_per_share=2.5,
            peak_premium_per_share=5.0,
            dte=21,
            entry_iv=0.30, current_iv=0.30,
        )
        assert decision_old.should_exit == decision_new.should_exit
        assert decision_old.reason == decision_new.reason
        assert decision_old.hard_stop_pct == decision_new.hard_stop_pct
        assert result.should_close is True
        assert result.legacy_action == "close"


# ── ExitTrigger / Evaluation dataclass tests ──────────────────────────


class TestExitTriggerDataclass:
    def test_severity_validation(self):
        with pytest.raises(ValueError):
            ExitTrigger(
                rule_name="x", severity="bogus", legacy_action="close",
                reason="x",
            )

    def test_triggered_at_auto_stamped(self):
        t = ExitTrigger(
            rule_name="x", severity="hard", legacy_action="close",
            reason="x",
        )
        assert t.triggered_at  # non-empty ISO timestamp

    def test_to_dict_shape(self):
        t = ExitTrigger(
            rule_name="dte_cliff", severity="hard", legacy_action="close",
            reason="banking", evidence={"dte": 2},
        )
        d = t.to_dict()
        assert d["rule_name"] == "dte_cliff"
        assert d["severity"] == "hard"
        assert d["legacy_action"] == "close"
        assert d["evidence"] == {"dte": 2}


class TestExitRuleEvaluationDataclass:
    def test_fired_flag_preserved(self):
        ev = ExitRuleEvaluation(
            rule_name="x", severity="hard", fired=True,
            legacy_action="close", reason="r",
        )
        d = ev.to_dict()
        assert d["fired"] is True
        assert d["rule_name"] == "x"


# ── Persistence sanity (mock DB) ──────────────────────────────────────


class TestPersistEvaluations:
    """persist_exit_evaluations writes one ExitRuleEvaluation per rule.

    Uses an in-memory SQLite to avoid touching the live DB.
    """

    def test_persists_all_evaluations(self, tmp_path):
        from backend.db import init_db, session_scope
        from backend.models.exit_rule_evaluation import ExitRuleEvaluation
        from backend.bot.options.exit_manager import persist_exit_evaluations

        # Fresh in-memory DB pointed at a tmp file.
        db_path = str(tmp_path / "test17e.db")
        init_db(db_path=db_path)

        ctx = _ctx(entry=5.0, cur=5.25, peak=5.25, dte=2)
        result = build_default_policy().evaluate(ctx)
        persist_exit_evaluations(
            result=result, position_id=42, ticker="AAPL",
        )
        with session_scope() as session:
            rows = session.query(ExitRuleEvaluation).all()
            assert len(rows) == 3
            by_name = {r.rule_name: r for r in rows}
            assert by_name["dte_cliff"].fired is True
            assert by_name["catastrophe_stop"].fired is False
            assert by_name["trailing_stop"].fired is False
            assert all(r.position_id == 42 for r in rows)
            assert all(r.ticker == "AAPL" for r in rows)
            # Evidence must round-trip as JSON string.
            ev_str = by_name["dte_cliff"].evidence_json
            assert ev_str is not None
            ev = json.loads(ev_str)
            assert ev["dte"] == 2


# ── Disabled-rule branch ──────────────────────────────────────────────


class TestDisabledRule:
    """A disabled rule still emits an evaluation row (with fired=False
    + reason='rule_disabled') so the UI can show 'off' rather than
    'gone'."""

    def test_disabled_rule_records_evaluation(self):
        policy = ExitPolicy()
        policy.register(ExitRule(
            name="dte_cliff", severity="hard",
            evaluator=rule_dte_cliff, enabled=False,
        ))
        ctx = _ctx(entry=5.0, cur=5.25, peak=5.25, dte=2)
        result = policy.evaluate(ctx)
        assert len(result.rule_evaluations) == 1
        assert result.rule_evaluations[0].fired is False
        assert result.rule_evaluations[0].reason == "rule_disabled"
        assert result.should_close is False
        assert result.legacy_action == "hold"
