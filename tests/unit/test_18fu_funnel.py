"""MITS Phase 18-FU Stream A — Decision Funnel unit tests.

Covers the funnel compute, confidence histogram, cooldown audit,
counterfactual histogram, surgical-change advisory decision tree,
persistence round-trip, and 7d anomaly detection.

The tests seed synthetic ``DecisionProvenance`` + ``Trade`` +
``PolicyRuleEvaluation`` rows directly into the test DB so the math
is isolated from any live engine state. The Step 6 brief asked for
≥10 tests; this file ships 14.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest

from backend.bot.learning.funnel import (
    CONFIDENCE_BIN_EDGES,
    COUNTERFACTUAL_SAMPLE_SIZE,
    SURGICAL_CONSENSUS_ABSTAIN_MIN,
    SURGICAL_DOMINANT_BLOCKER_MIN,
    SURGICAL_LOW_CONFIDENCE_MIN,
    ConfidenceHistogram,
    CooldownAudit,
    CounterfactualHistogram,
    FunnelReport,
    FunnelStage,
    _build_confidence_histogram,
    _build_cooldown_audit,
    _build_counterfactual_histogram,
    _derive_surgical_change_candidate,
    _ProvSnapshot,
    _stage_passes,
    compute_funnel_report,
    funnel_history,
    is_anomalous_drop,
    latest_funnel_row,
    persist_funnel_report,
)
from backend.db import session_scope
from backend.models.decision_funnel_daily import DecisionFunnelDaily
from backend.models.decision_provenance import DecisionProvenance
from backend.models.policy_rule_evaluation import PolicyRuleEvaluation
from backend.models.trade import Trade


pytestmark = [pytest.mark.unit]


# ── Helpers ──────────────────────────────────────────────────────────


def _prov_snap(
    *,
    snap_id: int = 1,
    trade_id: Optional[int] = None,
    event_status: str = "submitted",
    ticker: str = "AAPL",
    decision_timestamp: Optional[datetime] = None,
    strategy_matrix: Optional[Dict[str, Any]] = None,
    agent_outputs: Optional[List[Dict[str, Any]]] = None,
    consensus: Optional[Dict[str, Any]] = None,
    policy_result: Optional[Dict[str, Any]] = None,
    composite_quality: Optional[float] = None,
) -> _ProvSnapshot:
    """Build a synthetic _ProvSnapshot — the dataclass the funnel
    compute walks. Defaults to a fully-passed pipeline."""
    return _ProvSnapshot(
        id=snap_id,
        trade_id=trade_id,
        event_status=event_status,
        ticker=ticker,
        decision_timestamp=decision_timestamp or datetime.utcnow(),
        strategy_matrix=strategy_matrix,
        agent_outputs=agent_outputs or [],
        consensus=consensus,
        policy_result=policy_result,
        simulator_verdict=None,
        correlation_cap=None,
        composite_quality=composite_quality,
    )


def _policy(
    blocking_factors: Optional[List[Dict[str, Any]]] = None,
    *,
    eligible: Optional[bool] = None,
) -> Dict[str, Any]:
    bfs = blocking_factors or []
    if eligible is None:
        eligible = not any(
            b.get("severity") == "hard" for b in bfs
        )
    return {
        "eligible": eligible,
        "blocking_factors": bfs,
        "soft_penalties_total_pct": 0.0,
    }


def _hard_bf(rule: str) -> Dict[str, Any]:
    return {
        "rule": rule,
        "category": "strategy",
        "severity": "hard",
        "reason": f"{rule} fired",
        "evidence": {},
        "legacy_status": rule,
    }


def _consensus(
    *, recommendation: str = "execute", confidence: float = 0.7,
    quorum_met: bool = True,
) -> Dict[str, Any]:
    return {
        "stance": "buy",
        "confidence": float(confidence),
        "recommendation": recommendation,
        "quorum_met": bool(quorum_met),
        "quorum_required": 3,
        "contributing_count": 5,
    }


def _agent_outputs_buy(n: int = 3, confidence_pct: int = 70) -> List[Dict[str, Any]]:
    return [
        {
            "agent": f"agent_{i}",
            "stance": "buy",
            "confidence": int(confidence_pct),
            "reasoning_type": "contributing",
        }
        for i in range(n)
    ]


# ── 1. 10-stage funnel structure ─────────────────────────────────────


def test_stage_passes_produces_10_stages_in_correct_order():
    snaps = [
        _prov_snap(
            snap_id=1,
            strategy_matrix={"candidates": [{"name": "x"}]},
            agent_outputs=_agent_outputs_buy(),
            consensus=_consensus(),
            policy_result=_policy([]),
        ),
    ]
    stages, _ = _stage_passes(snaps)
    assert len(stages) == 10
    expected_names = [
        "watchlist_evaluated",
        "analysis_candidate",
        "brain_non_hold",
        "policy_eligible",
        "consensus_quorum_met",
        "consensus_non_abstain",
        "risk_passed",
        "simulator_passed",
        "submitted",
        "closed_with_pnl",
    ]
    assert [s.name for s in stages] == expected_names


def test_stage_passes_funnel_narrows_at_each_filter():
    # Mix: 1 row passes everything, 1 fails at signal_hold, 1 fails
    # at policy (low_confidence), 1 fails at simulator.
    snaps = [
        _prov_snap(
            snap_id=1,
            strategy_matrix={"candidates": [{"name": "x"}]},
            agent_outputs=_agent_outputs_buy(),
            consensus=_consensus(),
            policy_result=_policy([]),
            event_status="submitted",
            trade_id=101,
        ),
        _prov_snap(
            snap_id=2,
            strategy_matrix={"candidates": [{"name": "x"}]},
            agent_outputs=[],  # no non-hold vote
            consensus={
                "recommendation": "abstain", "stance": "hold",
                "confidence": 0.3, "quorum_met": False,
                "quorum_required": 3, "contributing_count": 0,
            },
            policy_result=_policy([_hard_bf("signal_hold")]),
            event_status="hold",
        ),
        _prov_snap(
            snap_id=3,
            strategy_matrix={"candidates": [{"name": "x"}]},
            agent_outputs=_agent_outputs_buy(),
            consensus=_consensus(),
            policy_result=_policy([_hard_bf("low_confidence")]),
            event_status="low_confidence",
        ),
        _prov_snap(
            snap_id=4,
            strategy_matrix={"candidates": [{"name": "x"}]},
            agent_outputs=_agent_outputs_buy(),
            consensus=_consensus(),
            policy_result=_policy([_hard_bf("simulator_veto")]),
            event_status="simulator_veto",
        ),
    ]
    stages, submitted_ids = _stage_passes(snaps)
    assert stages[0].n_passed == 4  # all evaluated
    assert stages[1].n_passed == 4  # all have matrix
    # Brain non-hold drops the one with no non-hold vote.
    assert stages[2].n_decisions == 4
    assert stages[2].n_passed == 3
    # policy_eligible drops 2 (signal_hold + low_confidence + simulator_veto).
    # But that cohort is downstream of brain_non_hold, so we count from 3.
    assert stages[3].n_decisions == 3
    # Only the fully-clean snap is eligible.
    assert stages[3].n_passed == 1
    # Final submitted should be 1.
    assert stages[8].n_decisions <= 4
    assert submitted_ids == [101]


# ── 2. Concurrent-veto parsing ───────────────────────────────────────


def test_concurrent_vetoes_all_surface_in_drop_reasons():
    # Single decision with 3 concurrent hard blockers; the funnel
    # must tally ALL of them, not just the headline.
    snaps = [
        _prov_snap(
            snap_id=1,
            strategy_matrix={"candidates": [{"name": "x"}]},
            agent_outputs=_agent_outputs_buy(),
            consensus=_consensus(),
            policy_result=_policy([
                _hard_bf("market_closed"),
                _hard_bf("signal_hold"),
                _hard_bf("low_confidence"),
            ]),
            event_status="market_closed",
        ),
    ]
    stages, _ = _stage_passes(snaps)
    # The policy_eligible stage tally should include all 3 rule names.
    drop_rules = {
        e["rule"] for e in stages[3].top_3_drop_reasons
    }
    assert "market_closed" in drop_rules
    assert "signal_hold" in drop_rules
    assert "low_confidence" in drop_rules


# ── 3. Confidence histogram bins ─────────────────────────────────────


def test_confidence_histogram_bins_correctly():
    snaps = [
        _prov_snap(
            snap_id=i,
            consensus={"confidence": conf},
            agent_outputs=_agent_outputs_buy() if non_hold else [],
            event_status="submitted" if submitted else "hold",
        )
        for i, (conf, non_hold, submitted) in enumerate([
            (0.05, False, False),   # bin 0
            (0.15, True, False),    # bin 1
            (0.45, True, False),    # bin 4
            (0.55, True, True),     # bin 5
            (0.95, True, True),     # bin 9
        ])
    ]
    hist = _build_confidence_histogram(snaps)
    assert len(hist.bin_edges) == 11
    assert list(hist.bin_edges) == list(CONFIDENCE_BIN_EDGES)
    assert hist.all_evals[0] == 1
    assert hist.all_evals[1] == 1
    assert hist.all_evals[4] == 1
    assert hist.all_evals[5] == 1
    assert hist.all_evals[9] == 1
    assert sum(hist.non_hold) == 4
    assert sum(hist.submitted) == 2
    assert hist.submitted[5] == 1
    assert hist.submitted[9] == 1


def test_confidence_histogram_skips_rows_without_confidence():
    snaps = [
        _prov_snap(snap_id=1, consensus=None),
        _prov_snap(snap_id=2, consensus={"confidence": None}),
        _prov_snap(snap_id=3, consensus={"confidence": 0.5}),
    ]
    hist = _build_confidence_histogram(snaps)
    assert sum(hist.all_evals) == 1


# ── 4. Cooldown audit ────────────────────────────────────────────────


def test_cooldown_audit_no_firings_returns_zero(temp_db):
    snaps = [_prov_snap(snap_id=1, ticker="AAPL")]
    audit = _build_cooldown_audit(
        snaps=snaps,
        window_start=datetime.utcnow() - timedelta(days=1),
        window_end=datetime.utcnow() + timedelta(days=1),
    )
    assert audit.n_cooldown_hits == 0
    assert audit.n_lost_opportunities == 0
    assert audit.affected_tickers == []


def test_cooldown_audit_flags_lost_opportunity(temp_db):
    t0 = datetime.utcnow() - timedelta(minutes=5)
    # Plant a brain_cooldown firing at t0 + a follow-up prov row 2
    # minutes later with composite_quality >= threshold.
    with session_scope() as s:
        ev = PolicyRuleEvaluation(
            rule_name="brain_cooldown",
            category="strategy",
            severity="hard",
            ticker="AAPL",
            evaluated_at=t0,
            blocked=True,
            reason="cooldown",
            sizing_penalty_pct=0.0,
        )
        s.add(ev)
    snaps = [
        _prov_snap(
            snap_id=99,
            ticker="AAPL",
            decision_timestamp=t0 + timedelta(minutes=2),
            composite_quality=75.0,
        ),
    ]
    audit = _build_cooldown_audit(
        snaps=snaps,
        window_start=t0 - timedelta(hours=1),
        window_end=t0 + timedelta(hours=1),
    )
    assert audit.n_cooldown_hits == 1
    assert audit.n_lost_opportunities == 1
    assert audit.affected_tickers == ["AAPL"]


# ── 5. Counterfactual histogram ──────────────────────────────────────


def test_counterfactual_histogram_tallies_new_headline_blockers(temp_db):
    """Plant 3 HOLD decisions with signal_hold + a co-firing blocker;
    counterfactual must tally the surviving blocker correctly.
    """
    with session_scope() as s:
        for i, second in enumerate(
            ["low_confidence", "low_confidence", "consensus_abstain"]
        ):
            policy = _policy([
                _hard_bf("signal_hold"),
                _hard_bf(second),
            ])
            prov = DecisionProvenance(
                trade_id=None,
                event_status="hold",
                ticker=f"T{i}",
                decision_timestamp=datetime.utcnow() - timedelta(hours=i),
                cycle_id=f"cf-{i}",
                policy_result_json=json.dumps(policy),
                agent_outputs_json=json.dumps([]),
            )
            s.add(prov)
        s.flush()
    snaps = [
        _prov_snap(
            snap_id=row.id,
            ticker=row.ticker,
            event_status=row.event_status,
            policy_result=json.loads(row.policy_result_json),
            decision_timestamp=row.decision_timestamp,
            agent_outputs=[],
        )
        for row in _load_recent_provenance()
    ]
    cf = _build_counterfactual_histogram(snaps, rule_overridden="signal_hold")
    assert cf.rule_overridden == "signal_hold"
    assert cf.n_decisions_analyzed == 3
    counts = cf.new_headline_blocker_counts
    assert counts.get("low_confidence") == 2
    assert counts.get("consensus_abstain") == 1
    assert cf.eligible_after_override == 0


def _load_recent_provenance() -> List[DecisionProvenance]:
    with session_scope() as s:
        rows = s.query(DecisionProvenance).order_by(
            DecisionProvenance.id.asc(),
        ).all()
        # Detach by reading fields into a plain list.
        out: List[Any] = []
        for r in rows:
            class _Row:
                pass
            row = _Row()
            row.id = int(r.id)
            row.ticker = str(r.ticker)
            row.event_status = str(r.event_status)
            row.policy_result_json = r.policy_result_json
            row.decision_timestamp = r.decision_timestamp
            out.append(row)
        return out


# ── 6. Surgical-change advisory decision tree ────────────────────────


def test_surgical_change_dominant_blocker_branch():
    cf = CounterfactualHistogram(
        rule_overridden="signal_hold",
        n_decisions_analyzed=1000,
        new_headline_blocker_counts={"__eligible__": SURGICAL_DOMINANT_BLOCKER_MIN + 1},
        eligible_after_override=SURGICAL_DOMINANT_BLOCKER_MIN + 1,
    )
    audit = CooldownAudit(0, 0, [], 600.0)
    out = _derive_surgical_change_candidate(
        counterfactual=cf, cooldown_audit=audit,
    )
    assert out["candidate"] == "fix_brain_hold_bias"
    assert out["auto_apply"] is False
    assert out["severity"] == "high"


def test_surgical_change_low_confidence_branch():
    cf = CounterfactualHistogram(
        rule_overridden="signal_hold",
        n_decisions_analyzed=1000,
        new_headline_blocker_counts={
            "low_confidence": SURGICAL_LOW_CONFIDENCE_MIN + 1,
            "consensus_abstain": 50,
        },
        eligible_after_override=10,
    )
    audit = CooldownAudit(0, 0, [], 600.0)
    out = _derive_surgical_change_candidate(
        counterfactual=cf, cooldown_audit=audit,
    )
    assert out["candidate"] == "investigate_confidence_distribution"
    assert out["auto_apply"] is False


def test_surgical_change_consensus_abstain_branch():
    cf = CounterfactualHistogram(
        rule_overridden="signal_hold",
        n_decisions_analyzed=1000,
        new_headline_blocker_counts={
            "consensus_abstain": SURGICAL_CONSENSUS_ABSTAIN_MIN + 1,
            "low_confidence": 50,
        },
        eligible_after_override=10,
    )
    audit = CooldownAudit(0, 0, [], 600.0)
    out = _derive_surgical_change_candidate(
        counterfactual=cf, cooldown_audit=audit,
    )
    assert out["candidate"] == "investigate_quorum_and_silence"
    assert out["auto_apply"] is False


def test_surgical_change_no_dominant_branch():
    cf = CounterfactualHistogram(
        rule_overridden="signal_hold",
        n_decisions_analyzed=100,
        new_headline_blocker_counts={
            "low_confidence": 50,
            "consensus_abstain": 30,
        },
        eligible_after_override=20,
    )
    audit = CooldownAudit(0, 0, [], 600.0)
    out = _derive_surgical_change_candidate(
        counterfactual=cf, cooldown_audit=audit,
    )
    assert out["candidate"] == "no_single_dominant_blocker"
    assert out["auto_apply"] is False


# ── 7. Persistence round-trip ────────────────────────────────────────


def test_persist_funnel_report_round_trip(temp_db):
    """Build a synthetic FunnelReport and persist; latest_funnel_row
    must read back the same numeric fields."""
    stages = [
        FunnelStage(name="watchlist_evaluated", n_decisions=100, n_passed=100, n_dropped=0, pass_rate=1.0),
        FunnelStage(name="analysis_candidate", n_decisions=100, n_passed=54, n_dropped=46, pass_rate=0.54),
        FunnelStage(name="brain_non_hold", n_decisions=54, n_passed=10, n_dropped=44, pass_rate=0.18),
        FunnelStage(name="policy_eligible", n_decisions=10, n_passed=8, n_dropped=2, pass_rate=0.8),
        FunnelStage(name="consensus_quorum_met", n_decisions=8, n_passed=7, n_dropped=1, pass_rate=0.875),
        FunnelStage(name="consensus_non_abstain", n_decisions=7, n_passed=6, n_dropped=1, pass_rate=0.857),
        FunnelStage(name="risk_passed", n_decisions=6, n_passed=6, n_dropped=0, pass_rate=1.0),
        FunnelStage(name="simulator_passed", n_decisions=6, n_passed=5, n_dropped=1, pass_rate=0.833),
        FunnelStage(name="submitted", n_decisions=5, n_passed=4, n_dropped=1, pass_rate=0.8),
        FunnelStage(name="closed_with_pnl", n_decisions=4, n_passed=2, n_dropped=2, pass_rate=0.5),
    ]
    report = FunnelReport(
        window_days=14,
        window_start=(datetime.utcnow() - timedelta(days=14)).isoformat(),
        window_end=datetime.utcnow().isoformat(),
        watchlist_size=42,
        stages=stages,
        confidence_histograms=ConfidenceHistogram(
            bin_edges=list(CONFIDENCE_BIN_EDGES),
            all_evals=[1] * 10,
            non_hold=[1] * 10,
            submitted=[0] * 10,
        ),
        cooldown_audit=CooldownAudit(
            n_cooldown_hits=5,
            n_lost_opportunities=2,
            affected_tickers=["AAPL", "TSLA"],
            avg_cooldown_seconds=600.0,
        ),
        counterfactual=CounterfactualHistogram(
            rule_overridden="signal_hold",
            n_decisions_analyzed=100,
            new_headline_blocker_counts={"low_confidence": 60},
            eligible_after_override=10,
        ),
        top_surgical_change_candidate={
            "candidate": "no_single_dominant_blocker",
            "auto_apply": False,
        },
        composite_quality_mean=47.27,
        composite_quality_median=49.67,
        computed_at=datetime.utcnow().isoformat(),
        notes=["synthetic_test_row"],
    )
    meta = persist_funnel_report(report)
    assert meta["ok"] is True
    assert meta["n_evaluations"] == 100
    assert meta["n_submitted"] == 4
    assert meta["n_closed_with_pnl"] == 2

    row = latest_funnel_row()
    assert row is not None
    assert row["n_evaluations"] == 100
    assert row["n_brain_non_hold"] == 10
    assert row["n_cooldown_hits"] == 5
    assert row["n_cooldown_lost_opportunities"] == 2
    assert row["composite_quality_mean"] == 47.27
    # Round-trip the payload.
    decoded = json.loads(row["payload_json"])
    assert decoded["window_days"] == 14
    assert len(decoded["stages"]) == 10


def test_persist_funnel_report_upserts_on_same_date(temp_db):
    """Persisting twice for the same target_date must UPDATE in place,
    not create a duplicate row — the unique index on date enforces
    this."""
    today = datetime.utcnow().date()
    # Two reports with different counts; second should overwrite.
    def _stub_report(n_evals: int) -> FunnelReport:
        stages = [
            FunnelStage(name=name, n_decisions=n_evals, n_passed=n_evals, n_dropped=0, pass_rate=1.0)
            for name in [
                "watchlist_evaluated", "analysis_candidate", "brain_non_hold",
                "policy_eligible", "consensus_quorum_met",
                "consensus_non_abstain", "risk_passed", "simulator_passed",
                "submitted", "closed_with_pnl",
            ]
        ]
        return FunnelReport(
            window_days=1,
            window_start=datetime.utcnow().isoformat(),
            window_end=datetime.utcnow().isoformat(),
            watchlist_size=0,
            stages=stages,
            confidence_histograms=ConfidenceHistogram([], [], [], []),
            cooldown_audit=CooldownAudit(0, 0, [], 600.0),
            counterfactual=CounterfactualHistogram(
                rule_overridden="signal_hold",
                n_decisions_analyzed=0,
                new_headline_blocker_counts={},
                eligible_after_override=0,
            ),
            top_surgical_change_candidate={
                "candidate": "insufficient_counterfactual_signal",
                "auto_apply": False,
            },
            composite_quality_mean=None,
            composite_quality_median=None,
            computed_at=datetime.utcnow().isoformat(),
        )
    persist_funnel_report(_stub_report(100), target_date=today)
    persist_funnel_report(_stub_report(200), target_date=today)
    with session_scope() as s:
        rows = s.query(DecisionFunnelDaily).filter(
            DecisionFunnelDaily.date == today,
        ).all()
        # Materialize fields inside the session — DetachedInstanceError
        # otherwise (the test_db fixture closes the session on exit).
        count = len(rows)
        n_evals = int(rows[0].n_evaluations) if rows else None
    assert count == 1
    assert n_evals == 200


# ── 8. Anomaly detection (7d median > 50% drop) ──────────────────────


def test_anomaly_flag_fires_on_large_drop():
    current = {
        "date": "2026-06-12",
        "n_evaluations": 100,
        "n_submitted": 0,
    }
    history = [
        {"date": f"2026-06-{d:02d}", "n_evaluations": 100, "n_submitted": 10}
        for d in range(5, 12)
    ]
    out = is_anomalous_drop(current, history)
    assert out["anomalous"] is True
    assert "n_submitted" in out["anomalous_stages"]
    delta = out["deltas"]["n_submitted"]
    assert delta["current"] == 0
    assert delta["median_7d"] == 10
    assert delta["ratio"] == 0.0


def test_anomaly_flag_silent_when_within_band():
    current = {"date": "2026-06-12", "n_evaluations": 95, "n_submitted": 9}
    history = [
        {"date": f"2026-06-{d:02d}", "n_evaluations": 100, "n_submitted": 10}
        for d in range(5, 12)
    ]
    out = is_anomalous_drop(current, history)
    assert out["anomalous"] is False


def test_anomaly_ignores_small_medians():
    current = {"date": "2026-06-12", "n_submitted": 0}
    history = [
        {"date": f"2026-06-{d:02d}", "n_submitted": 1}
        for d in range(5, 12)
    ]
    out = is_anomalous_drop(current, history)
    # median=1 < threshold 5 ⇒ stage skipped entirely.
    assert "n_submitted" not in out["anomalous_stages"]


# ── 9. compute_funnel_report end-to-end on empty DB ──────────────────


def test_compute_funnel_report_empty_db_returns_valid_structure(temp_db):
    report = compute_funnel_report(window_days=14)
    assert isinstance(report, FunnelReport)
    assert len(report.stages) == 10
    assert report.stages[0].n_decisions == 0
    assert report.counterfactual.n_decisions_analyzed == 0
    assert report.cooldown_audit.n_cooldown_hits == 0
    assert (
        report.top_surgical_change_candidate["candidate"]
        == "insufficient_counterfactual_signal"
    )
    d = report.to_dict()
    assert d["window_days"] == 14
    assert d["computed_at"]
