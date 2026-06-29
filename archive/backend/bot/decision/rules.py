"""Registered rule library for the declarative decision policy.

Every gate the engine's procedural ``run_cycle`` block used to enforce
is implemented here as one ``rule_*`` function. Each function reads
:class:`PolicyContext`, performs the same check the engine used to
inline, and returns a :class:`BlockingFactor` or ``None``.

The hidden gates (silent ``try/except`` blocks in the legacy
procedural code) are now first-class rules in the
``data_quality`` category. Their evaluators wrap the same call sites
the legacy code wrapped, but every failure becomes a row in
``policy_rule_evaluations`` instead of disappearing into the debug
log.

Side-effects rules legitimately produce (e.g. building
``analytics_result``, running the council) are written into
``ctx.event`` (the engine's canonical event dict) and ``ctx.scratch``
(intermediate objects the engine consumes after evaluation). Rules do
not call ``self._emit`` — the engine still owns event emission.
"""
from __future__ import annotations

import logging
import time as _time
from typing import Any, Dict, Optional

from backend.bot.decision.policy import (
    BlockingFactor,
    DecisionPolicy,
    PolicyContext,
    PolicyRule,
)

logger = logging.getLogger(__name__)


# Engine module imports a single Action enum — keep the reference local
# so each rule can read signal.action without pulling backend.bot.engine.
from backend.bot.strategies.base import Action  # noqa: E402

# Sets that classify Action enum members.
STOCK_ACTIONS = {Action.BUY_STOCK, Action.SELL_STOCK}
SINGLE_LEG_OPTIONS = {Action.BUY_CALL, Action.BUY_PUT}
SINGLE_LEG_SHORT_OPTIONS = {Action.SELL_CSP, Action.SELL_COVERED_CALL}
SPREAD_OPTIONS = {
    Action.BULL_CALL_SPREAD, Action.BUY_STRADDLE, Action.IRON_CONDOR,
    Action.RATIO_SPREAD, Action.COLLAR,
}
COMPLEX_OPTIONS = SINGLE_LEG_SHORT_OPTIONS | SPREAD_OPTIONS


def _is_buy_action(action: Action) -> bool:
    return action.value.startswith("BUY")


def _is_option_action(action: Action) -> bool:
    av = action.value
    lower = av.lower()
    return (
        "_call" in lower
        or "_put" in lower
        or av in {"BUY_CALL", "BUY_PUT", "SELL_CALL", "SELL_PUT"}
        or action in COMPLEX_OPTIONS
    )


# ── 1. market_closed ───────────────────────────────────────────────────

def rule_market_closed(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """NYSE closed (off-hours / weekend / holiday).

    The engine has a calendar-gate check earlier that returns a single
    "—" event when the whole cycle is closed. This per-ticker rule
    catches the unusual force_run_when_closed=True path where the
    cycle ran anyway.
    """
    from backend.bot.calendar import is_us_market_open
    if is_us_market_open():
        return None
    if ctx.config.get("force_run_when_closed"):
        return None
    return BlockingFactor(
        category="market", rule="market_closed", severity="hard",
        reason="NYSE closed (off-hours, weekend, or holiday)",
        evidence={"ticker": ctx.ticker},
        legacy_status="market_closed",
    )


# ── 2. kill_switch ──────────────────────────────────────────────────────

def rule_kill_switch_active(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Stage-8 global kill-switch: blocks every new entry."""
    if not ctx.kill_active:
        return None
    if not _is_buy_action(ctx.signal.action):
        return None
    return BlockingFactor(
        category="risk", rule="kill_switch_active", severity="hard",
        reason="kill-switch active — operator hold",
        evidence={},
        legacy_status="kill_switch",
    )


# ── 3. options_disabled ─────────────────────────────────────────────────

def rule_options_disabled(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Operator config flag pauses every option order."""
    if not ctx.config.get("options_disabled"):
        return None
    if (ctx.signal.action not in SINGLE_LEG_OPTIONS
            and ctx.signal.action not in COMPLEX_OPTIONS):
        return None
    return BlockingFactor(
        category="strategy", rule="options_disabled", severity="hard",
        reason=(
            f"options trading paused (config.options_disabled). "
            f"Original action: {ctx.signal.action.value} — converted to HOLD."
        ),
        evidence={"original_action": ctx.signal.action.value},
        legacy_status="options_disabled",
    )


# ── 4. abstain (Stage-9) ────────────────────────────────────────────────

def rule_abstain_and_throttle(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Selective-abstention soft gate. Hard-blocks when the
    ``AbstainDecision`` is full abstain OR monitor-only; otherwise
    records the size multiplier on the event for the sizing layer.

    18-FU Gap R1 — consults operator-approved overrides for
    ``TUNABLES.abstain_band_lo`` and ``TUNABLES.abstain_band_hi`` via
    ``resolve_threshold`` and THREADS them into ``abstain_and_throttle``
    via the new ``band_lo`` / ``band_hi`` kwargs. The threshold sources
    are stamped into ``BlockingFactor.evidence`` so replay sees the
    exact band that was active at decision time.
    """
    from backend.bot.abstain import abstain_and_throttle
    from backend.bot.cohort_matrix import cohort_win_rate
    from backend.bot.learning.policy_apply import resolve_threshold
    from backend.config import TUNABLES as _T

    signal = ctx.signal
    analytics_result = ctx.scratch.get("analytics_result")
    cohort_wr, cohort_n = (None, 0)
    try:
        strategy_name = (
            signal.strategy
            or (ctx.scratch.get("active_strategy") or "ai_brain")
        )
        cohort_wr, cohort_n = cohort_win_rate(
            strategy_name,
            (analytics_result.regime.trend if analytics_result else "—"),
            recent_n=30,
        )
    except Exception:
        cohort_wr, cohort_n = (None, 0)
    prob = None
    if analytics_result and getattr(analytics_result, "probability", None):
        prob = analytics_result.probability.probability
    # 18-FU Gap R1 — resolve both bands through the operator-approved
    # override layer. Threaded into abstain_and_throttle so enforcement
    # actually uses the override value (not just observed in evidence).
    band_lo, band_lo_evidence = resolve_threshold(
        ctx,
        threshold_attr="TUNABLES.abstain_band_lo",
        tunable_default=float(getattr(_T, "abstain_band_lo", 0.50)),
    )
    band_hi, band_hi_evidence = resolve_threshold(
        ctx,
        threshold_attr="TUNABLES.abstain_band_hi",
        tunable_default=float(getattr(_T, "abstain_band_hi", 0.58)),
    )
    abstain_dec = abstain_and_throttle(
        action=signal.action.value, probability=prob,
        expected_move_pct=(
            analytics_result.probability.expected_move
            if analytics_result and analytics_result.probability else None
        ),
        total_cost_bps=0.0,
        regime_label=(
            analytics_result.regime.label if analytics_result else None
        ),
        snapshot=ctx.data,
        cohort_win_rate=cohort_wr, cohort_closed=cohort_n,
        band_lo=band_lo, band_hi=band_hi,
    )
    if abstain_dec.abstain or abstain_dec.monitor_only:
        ctx.event["abstain"] = abstain_dec.to_dict()
        return BlockingFactor(
            category="strategy", rule="abstain_and_throttle", severity="hard",
            reason=("; ".join(abstain_dec.reasons))[:240],
            evidence={
                "size_multiplier": abstain_dec.size_multiplier,
                "monitor_only": abstain_dec.monitor_only,
                "rules": list(abstain_dec.triggered_rules),
                "band_lo_used": float(band_lo),
                "band_hi_used": float(band_hi),
                # Separate sub-keys so audits can answer "did lo OR hi
                # come from operator-approved override?" independently.
                "threshold_source_lo": band_lo_evidence["threshold_source"],
                "threshold_source_hi": band_hi_evidence["threshold_source"],
                # Headline threshold_source aggregates the two bands for
                # the per-rule veto-budget panel (one row per rule needs
                # one source string). 'auto_applied' when EITHER band
                # was overridden; 'default' only when BOTH are default.
                "threshold_source": (
                    "auto_applied"
                    if (band_lo_evidence["threshold_source"] != "tunable_default"
                        or band_hi_evidence["threshold_source"]
                        != "tunable_default")
                    else "default"
                ),
            },
            legacy_status="abstain",
        )
    if abstain_dec.size_multiplier != 1.0:
        ctx.event["abstain"] = abstain_dec.to_dict()
    return None


# ── 5. event_risk window ────────────────────────────────────────────────

def rule_event_risk_window(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Block opening orders during macro prints / earnings."""
    event_cfg = ctx.config.get("event_risk") or {}
    if not event_cfg.get("enabled", True):
        return None
    if not _is_buy_action(ctx.signal.action):
        return None
    from backend.bot.event_risk import can_trade as _can_trade
    perm = _can_trade(ctx.ticker)
    if perm.can_trade:
        return None
    ctx.event["next_window"] = perm.next_window
    return BlockingFactor(
        category="market", rule="event_risk_window", severity="hard",
        reason=f"event-risk hold: {perm.reason[:200]}",
        evidence={"next_window": perm.next_window},
        legacy_status="event_hold",
    )


# ── 6. catalyst_gate ────────────────────────────────────────────────────

def rule_catalyst_gate(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Earnings / FOMC proximity gate. Default conviction multiplier
    stamps onto the event even on pass so the sizing layer can use it.

    18-FU Gap R1 — when the operator has approved a tuning for
    ``TUNABLES.catalyst_short_dte_threshold``, we resolve the override
    via ``policy_apply.resolve_threshold`` and thread it into
    ``_cg.check`` via the new ``short_dte_threshold`` kwarg so
    enforcement uses the override (not just observes it). The threshold
    source is stamped into the BlockingFactor.evidence so replay sees
    the exact threshold that was active.
    """
    from backend.bot.learning.policy_apply import resolve_threshold
    from backend.config import TUNABLES as _T

    ctx.event.setdefault("catalyst_multiplier", 1.0)
    signal = ctx.signal
    if not _is_buy_action(signal.action):
        return None
    from backend.bot.gates import catalyst_gate as _cg
    action_str = signal.action.value
    action_lower = action_str.lower()
    if action_str in {"BUY_CALL", "BUY_PUT"}:
        instrument_for_gate = "option"
    elif "_call" in action_lower or "_put" in action_lower:
        instrument_for_gate = "spread"
    else:
        instrument_for_gate = "stock"
    dte_for_gate = None
    meta_for_gate = signal.metadata or {}
    if meta_for_gate.get("dte") is not None:
        try:
            dte_for_gate = int(meta_for_gate["dte"])
        except (TypeError, ValueError):
            dte_for_gate = None
    short_dte_threshold, threshold_evidence = resolve_threshold(
        ctx,
        threshold_attr="TUNABLES.catalyst_short_dte_threshold",
        tunable_default=float(
            getattr(_T, "catalyst_short_dte_threshold", 7)
        ),
    )
    cgate = _cg.check(
        ctx.ticker, instrument=instrument_for_gate, dte=dte_for_gate,
        short_dte_threshold=int(short_dte_threshold),
    )
    ctx.event["catalyst_gate"] = cgate.to_dict()
    if not cgate.passes:
        _record_brain_cooldown(ctx)
        return BlockingFactor(
            category="market", rule="catalyst_gate", severity="hard",
            reason=(cgate.reason or "catalyst_gate: abstain")[:240],
            evidence={
                "instrument": instrument_for_gate, "dte": dte_for_gate,
                **threshold_evidence,
            },
            legacy_status="catalyst_gate",
        )
    ctx.event["catalyst_multiplier"] = float(cgate.conviction_multiplier)
    return None


# ── 7. analytics_build_failed (hidden gate → explicit) ──────────────────

def rule_analytics_build_failed(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Wraps the engine's analytics.evaluate() call. Populates
    ``ctx.scratch['analytics_result']`` and ``event['analytics']`` on
    success; on failure emits an explicit BlockingFactor."""
    if not ctx.analytics_cfg.get("enabled", True):
        return None
    if ctx.signal.action == Action.HOLD:
        return None
    predictive_cfg = ctx.config.get("predictive") or {}
    ml_weight = (
        float(predictive_cfg.get("weight", 0.0) or 0.0)
        if predictive_cfg.get("enabled") else 0.0
    )
    try:
        analytics_result = ctx.analytics_engine.evaluate(
            ctx.ticker, ctx.data, ctx.signal, ml_weight=ml_weight,
        )
    except Exception as exc:
        logger.warning(
            "analytics eval failed for %s — using fallback grade",
            ctx.ticker, exc_info=True,
        )
        return BlockingFactor(
            category="data_quality", rule="analytics_build_failed",
            severity="hard",
            reason=f"analytics evaluate raised: {type(exc).__name__}",
            evidence={"exception": str(exc)[:240]},
            legacy_status="analytics_failed",
        )
    ctx.scratch["analytics_result"] = analytics_result
    ctx.event["analytics"] = analytics_result.to_dict()
    if ctx.portfolio_risk_dict:
        ctx.event["portfolio_risk"] = ctx.portfolio_risk_dict
    return None


# ── 8. signal_hold ──────────────────────────────────────────────────────

def rule_signal_hold(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Strategy returned HOLD — no actionable signal."""
    if ctx.signal.action != Action.HOLD:
        return None
    # Preserve the engine's legacy behavior: HOLD events carried
    # signal.reason verbatim. ``override_event_reason=False`` tells the
    # engine NOT to clobber event["reason"] (which is pre-populated
    # from signal.reason when the per-ticker event dict is built).
    return BlockingFactor(
        category="strategy", rule="signal_hold", severity="hard",
        reason=ctx.signal.reason or "",
        evidence={"action": "HOLD"},
        legacy_status="hold",
        override_event_reason=False,
    )


# ── 9. low_confidence ───────────────────────────────────────────────────

def rule_low_confidence(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Signal confidence below ``min_confidence`` threshold.

    18-FU Gap 1 — consults
    ``ctx.scratch['applied_thresholds']['config.min_confidence']``
    when the operator has auto-applied a recommendation; falls back
    to ``ctx.config['min_confidence']`` otherwise. The threshold
    source is stamped into the BlockingFactor.evidence so replay
    deterministically reads the same value off
    ``policy_result_json``.
    """
    from backend.bot.learning.policy_apply import resolve_threshold

    tunable_default = float(ctx.config.get("min_confidence", 0.6))
    threshold, threshold_evidence = resolve_threshold(
        ctx,
        threshold_attr="config.min_confidence",
        tunable_default=tunable_default,
    )
    if ctx.signal.is_actionable(threshold=threshold):
        return None
    _record_brain_cooldown(ctx)
    return BlockingFactor(
        category="strategy", rule="low_confidence", severity="hard",
        reason=(
            f"confidence {ctx.signal.confidence:.2f} below threshold "
            f"{threshold:.2f}"
        ),
        evidence={
            "confidence": float(ctx.signal.confidence),
            "threshold": threshold,
            **threshold_evidence,
        },
        legacy_status="low_confidence",
        # Legacy gate never wrote event["reason"]; keep signal.reason
        # so UI consumers see the original explanation.
        override_event_reason=False,
    )


# ── 10. drift_check_failed (hidden) + drift_halt ────────────────────────

def rule_drift_check_failed(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Wraps the drift-auto-halt check so vendor exceptions become
    explicit BlockingFactors instead of silently bypassing the halt
    gate. Writes ``ctx.scratch['drift_halt_strategies']`` on success
    so :func:`rule_drift_halt` can consult it."""
    if not _is_buy_action(ctx.signal.action):
        return None
    try:
        from backend.bot.drift.auto_halt import is_halted
        s_name = (
            ctx.signal.strategy
            or ctx.scratch.get("active_strategy")
            or "ai_brain"
        )
        ctx.scratch["drift_check"] = {
            "strategy": s_name,
            "halted": bool(is_halted(s_name)),
        }
        return None
    except Exception as exc:
        logger.warning(
            "drift halt check failed for %s — halt gate bypassed",
            ctx.ticker, exc_info=True,
        )
        return BlockingFactor(
            category="data_quality", rule="drift_check_failed",
            severity="hard",
            reason=f"drift halt probe raised: {type(exc).__name__}",
            evidence={"exception": str(exc)[:240]},
            legacy_status="drift_check_failed",
        )


def rule_drift_halt(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Block entries when the signal's strategy is auto-halted."""
    drift = ctx.scratch.get("drift_check") or {}
    if not drift.get("halted"):
        return None
    s_name = drift.get("strategy", "")
    return BlockingFactor(
        category="data_quality", rule="drift_halt", severity="hard",
        reason=f"strategy '{s_name}' is auto-halted by drift detector",
        evidence={"strategy": s_name},
        legacy_status="drift_halt",
    )


# ── 11. low_grade ───────────────────────────────────────────────────────

def rule_low_grade(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Analytics grade below configured / adaptive ``min_grade``.

    Replicates the engine's adaptive-grade tightening: AI Brain trades
    require at least grade B regardless of operator config, and
    calibration drift bumps the threshold further."""
    from backend.bot.analytics import gate_by_grade

    analytics_result = ctx.scratch.get("analytics_result")
    if analytics_result is None:
        return None
    min_grade = ctx.analytics_cfg.get("min_grade")
    if ctx.use_brain and (min_grade is None or min_grade < "B"):
        min_grade = "B"
    try:
        from backend.bot.gates.adaptive import adaptive_min_grade
        if analytics_result.probability:
            from backend.api.routes.metrics import build_summary
            summary = build_summary()
            metrics_data = summary.get("data") or {}
            effective = adaptive_min_grade(
                configured_min_grade=min_grade,
                calibration_error=metrics_data.get("calibration_error"),
                brier=metrics_data.get("brier"),
            )
            if effective != min_grade and effective is not None:
                ctx.event["min_grade_tightened"] = {
                    "configured": min_grade, "effective": effective,
                    "reason": "calibration drift",
                }
                min_grade = effective
    except Exception:
        logger.debug(
            "adaptive min_grade failed for %s", ctx.ticker, exc_info=True,
        )

    if gate_by_grade(analytics_result.rank, min_grade):
        return None
    _record_brain_cooldown(ctx)
    return BlockingFactor(
        category="strategy", rule="low_grade", severity="hard",
        reason=(
            f"{ctx.signal.reason} · grade "
            f"{analytics_result.rank.grade} below {min_grade}"
        ),
        evidence={
            "grade": analytics_result.rank.grade,
            "min_grade": min_grade,
        },
        legacy_status="low_grade",
    )


# ── 12. iv_too_rich ─────────────────────────────────────────────────────

def rule_iv_too_rich(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """AI Brain options sanity: refuse long premium when IV rank > 70.

    18-FU Gap 1 — operator-approved overrides via the 18.C tunable
    ``hardcoded_iv_rank_ceiling`` apply here. Default ceiling 70.
    """
    from backend.bot.learning.policy_apply import resolve_threshold

    if not ctx.use_brain:
        return None
    if ctx.signal.action.value not in ("BUY_CALL", "BUY_PUT"):
        return None
    iv_rank = float((ctx.data or {}).get("iv_rank") or 0.0)
    ceiling, threshold_evidence = resolve_threshold(
        ctx,
        threshold_attr="hardcoded_iv_rank_ceiling",
        tunable_default=70.0,
    )
    if iv_rank <= ceiling:
        return None
    _record_brain_cooldown(ctx)
    return BlockingFactor(
        category="strategy", rule="iv_too_rich", severity="hard",
        reason=(
            f"ai_brain {ctx.signal.action.value} blocked: IV rank "
            f"{iv_rank:.0f} > {ceiling:.0f} — buying premium when IV "
            f"is expensive. Consider sell-premium structure."
        ),
        evidence={
            "iv_rank": iv_rank,
            "threshold": ceiling,
            **threshold_evidence,
        },
        legacy_status="iv_too_rich",
    )


# ── 13. meta_rejected (runs the audit) + meta_ai_offline (soft, sees
#       the failure stashed by the hard rule) ─────────────────────────────

def rule_meta_rejected(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Run the Meta-AI audit and act on its verdict.

    The legacy code wrapped audit() in a broad try/except that silently
    swallowed authentication / quota errors — so an offline meta-AI
    appeared to approve every trade. We split the failure into a soft
    BlockingFactor (registered separately as ``meta_ai_offline``) and
    keep the verdict-based veto here as the hard gate.
    """
    if not ctx.ai_config.get("meta_enabled"):
        return None
    analytics_result = ctx.scratch.get("analytics_result")
    if analytics_result is None:
        return None
    if not ctx.meta_engine.available:
        return None
    try:
        signal = ctx.signal
        meta = ctx.meta_engine.audit(
            ctx.ticker,
            {
                "action": signal.action.value, "strategy": signal.strategy,
                "confidence": signal.confidence,
                "reason": (signal.reason or "")[:300],
            },
            ctx.event.get("analytics") or {},
            ctx.portfolio_risk_dict,
        )
    except Exception as exc:
        logger.warning(
            "meta-AI audit failed for %s — strategist veto offline",
            ctx.ticker, exc_info=True,
        )
        # Stash so the soft ``meta_ai_offline`` rule (registered later
        # for sizing-penalty telemetry) sees the failure.
        ctx.scratch["meta_audit_failure"] = (
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )
        return None
    meta_dict = meta.to_dict()
    ctx.event["meta"] = meta_dict
    ctx.scratch["meta_dict"] = meta_dict
    ctx.scratch["meta_obj"] = meta
    if meta.approve:
        return None
    _record_brain_cooldown(ctx)
    reason = f"meta veto: {'; '.join(meta.reasoning)[:240]}"
    return BlockingFactor(
        category="strategy", rule="meta_rejected", severity="hard",
        reason=reason,
        evidence={"reasoning": list(meta.reasoning)[:5]},
        legacy_status="meta_rejected",
    )


def rule_meta_ai_offline(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Soft telemetry: did the meta-AI audit raise this cycle?
    The hard ``meta_rejected`` rule stashes the exception string when
    audit fails; we surface it as a 5% sizing penalty so an extended
    meta outage is visible in the veto-budget panel."""
    failure = ctx.scratch.get("meta_audit_failure")
    if not failure:
        return None
    return BlockingFactor(
        category="data_quality", rule="meta_ai_offline", severity="soft",
        reason=f"meta-AI audit raised: {failure}",
        evidence={"exception": failure},
        sizing_penalty_pct=5.0,
        legacy_status="",
    )


# ── 14. consensus (Stage-15) — exception wrap + simulator + correlation
#       + chairman/legacy abstain ──────────────────────────────────────────

def rule_consensus_exception(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Run the multi-agent consensus + per-ticker regime vector. On
    success, writes ``ctx.scratch['consensus_obj']`` and the relevant
    event keys. On failure, returns a hard BlockingFactor instead of
    swallowing the exception."""
    from backend.bot.agents import run_consensus
    from backend.bot.regime.vector import build_regime_vector

    signal = ctx.signal
    macro_for_agents: Dict[str, Any] = {}
    breadth_for_agents: Dict[str, Any] = {}
    try:
        from backend.bot.data.fred import macro_snapshot
        macro_for_agents = macro_snapshot() or {}
    except Exception:
        pass
    try:
        from backend.bot.breadth import regime_health
        breadth_for_agents = regime_health() or {}
    except Exception:
        pass
    earnings_intel: Dict[str, Any] = {}
    try:
        from backend.bot.earnings_intel import latest_for as _ei_latest
        earnings_intel = _ei_latest(signal.ticker) or {}
    except Exception:
        pass
    knowledge_evidence: Dict[str, Any] = {
        "cells": [], "summary": "", "most_similar_outcomes": [],
    }
    try:
        from backend.bot.agent_context import (
            load_knowledge_evidence as _load_ke,
        )
        analytics_regime = (
            (ctx.event.get("analytics") or {}).get("regime") or {}
        )
        knowledge_evidence = _load_ke(
            ticker=signal.ticker,
            regime=(analytics_regime.get("trend") or "unknown"),
            vol_state=(analytics_regime.get("volatility") or "normal"),
            snapshot=ctx.data,
            strategy=signal.strategy,
        )
    except Exception:
        pass

    agents_ctx = {
        "ticker": signal.ticker,
        "action": signal.action.value,
        "strategy": signal.strategy,
        "analytics": ctx.event.get("analytics"),
        "features": (ctx.event.get("analytics") or {}).get("features"),
        "snapshot": ctx.data,
        "portfolio_risk": ctx.event.get("portfolio_risk"),
        "optimizer": ctx.event.get("optimizer"),
        "cross_asset": ctx.event.get("cross_asset"),
        "macro": macro_for_agents,
        "breadth": breadth_for_agents,
        "earnings_intel": earnings_intel,
        "knowledge_evidence": knowledge_evidence,
        # 18-FU Gap R2 — thread the engine cycle_id so
        # ``run_consensus`` can route adaptive-weight reads through
        # ``apply_weights_for_cycle`` and write one
        # ``weight_application_log`` row per consensus run. ``cycle_id``
        # is the engine event timestamp string (engine.py:856).
        "cycle_id": ctx.cycle_id,
    }
    try:
        consensus_obj = run_consensus(
            agents_ctx, use_dynamic_weights=True,
            enrich_with_claude=bool(
                ctx.ai_config.get("agents_claude_enrich")
            ),
        )
    except Exception as exc:
        logger.warning(
            "consensus gate failed for %s — Council pillar will read Unknown",
            ctx.ticker, exc_info=True,
        )
        return BlockingFactor(
            category="data_quality", rule="consensus_exception",
            severity="hard",
            reason=f"consensus raised: {type(exc).__name__}",
            evidence={"exception": str(exc)[:240]},
            legacy_status="consensus_failed",
        )

    ctx.event["consensus"] = consensus_obj.to_dict()
    ctx.scratch["consensus_obj"] = consensus_obj
    # MITS Phase 16.B — lift the typed envelope + per-vote projections
    # onto the event so ``_persist_trade`` (executed path) and
    # ``_sweep_block_brain_predictions`` (rejected-post-consensus path)
    # both find the same shape when writing decision_provenance.
    # ``getattr`` with default tolerates test fixtures that mock the
    # consensus object as a SimpleNamespace without the 16.B fields.
    agent_input_blob = getattr(consensus_obj, "agent_input", None)
    if agent_input_blob:
        ctx.event["agent_input"] = agent_input_blob
    agent_outputs_blob = getattr(consensus_obj, "agent_outputs", None)
    if agent_outputs_blob:
        ctx.event["agent_outputs"] = agent_outputs_blob
    # MITS Phase 19 — lift the simulator verdict onto the event
    # unconditionally so the decision_provenance row carries it on
    # HOLDs / non-veto blocks (the legacy lift in ``rule_simulator_veto``
    # only fires when ``reject_reason`` is set, leaving every
    # non-vetoed path with simulator_verdict_json=NULL). Observational
    # only — never changes the policy outcome.
    sv_blob = getattr(consensus_obj, "simulator_verdict", None)
    if isinstance(sv_blob, dict) and sv_blob:
        ctx.event["simulator_verdict"] = sv_blob

    # Per-ticker regime vector — fail-open, no rule emit.
    rv = None
    try:
        rv = build_regime_vector(
            ticker=signal.ticker,
            snapshot=ctx.data,
            intraday_classifier=ctx.intraday_classifier,
        )
        ctx.event["regime_vector"] = rv.to_dict()
        ctx.scratch["regime_vector_obj"] = rv
    except Exception:
        logger.debug(
            "per-ticker regime_vector build failed for %s",
            ctx.ticker, exc_info=True,
        )

    # MITS Phase 16 followup O3 + Phase 18-FU Gap R3 — engine-cycle
    # StrategyMatrix is now pre-built upstream in ``engine.run_cycle``
    # (via the TTL cache in ``strategy_matrix_cache``) so every
    # evaluation gets matrix coverage, not just the ones that reach
    # this rule. We skip the build entirely when ``event["strategy_matrix"]``
    # is already populated. If a caller invoked the consensus rule
    # without the upstream lift (the legacy entry path used by some
    # tests + the /analysis route), we fall back to the cache here.
    # Fail-open in every branch — builder exceptions never block.
    from backend.config import TUNABLES as _TUNABLES
    if (
        rv is not None
        and ctx.event.get("strategy_matrix") is None
        and bool(getattr(_TUNABLES, "engine_strategy_matrix_enabled", True))
    ):
        try:
            from backend.bot.analysis.strategy_matrix_cache import (
                get_or_build as _sm_get_or_build,
            )
            sm_dict, top_strategy_dict = _sm_get_or_build(
                ticker=signal.ticker,
                regime_vector=rv,
                signal=signal,
                analytics=ctx.event.get("analytics"),
            )
            if sm_dict is not None:
                ctx.event["strategy_matrix"] = sm_dict
            if (
                top_strategy_dict is not None
                and ctx.event.get("top_strategy") is None
            ):
                ctx.event["top_strategy"] = top_strategy_dict
        except Exception:
            logger.debug(
                "engine strategy_matrix build failed for %s",
                ctx.ticker, exc_info=True,
            )

    ctx.event["_signal_for_brain"] = {
        "action": signal.action.value,
        "confidence": signal.confidence,
        "reason": signal.reason,
        "invalidation": (signal.metadata or {}).get("invalidation"),
    }
    return None


def _build_engine_strategy_matrix(
    *,
    ticker: str,
    regime_vector,
    signal,
    analytics: Optional[Dict[str, Any]],
):
    """MITS Phase 16 followup O3 — assemble the StrategyMatrix inputs
    the engine cycle has at this point and call build_strategy_matrix.

    Returns ``(matrix_dict, top_strategy_dict)``. Either may be None
    when the matcher returns no eligible candidate (empty `candidates`)
    or when the builder declined to populate ``top_candidate``.

    The /analysis route already does the same dance against richer
    inputs (rendered bars + detector observations + full IV report).
    The engine cycle has a snapshot but not the bar series, so:

      * ``pattern_hits`` is derived from ``signal.strategy`` (primary
        engine pattern), with ``signal.metadata.pattern`` as a fallback.
      * ``analogs`` are retrieved off the (regime_vector, pattern,
        5d horizon) tuple — the same horizon the analysis route uses.
      * ``iv_state`` comes from ``analytics.features.iv_rank`` plus an
        optional ``iv_regime.classify_ticker`` call. The IV classifier
        is fail-open here too — a missing report doesn't abort the
        matrix build.
    """
    from backend.bot.analysis.strategy_matrix import build_strategy_matrix
    from backend.bot.corpus.analog_retrieval import retrieve_analogs

    pattern_label = None
    md = signal.metadata or {}
    if md.get("pattern"):
        pattern_label = str(md.get("pattern"))
    elif signal.strategy:
        pattern_label = str(signal.strategy)
    pattern_hits = [{"pattern": pattern_label}] if pattern_label else []

    analogs = retrieve_analogs(
        ticker=ticker, regime_vector=regime_vector,
        pattern=pattern_label or "unknown",
        horizon="5d", k=50, sector_fallback=True,
    )

    feats = (analytics or {}).get("features") or {}
    iv_rank = feats.get("iv_rank")
    iv_regime_label: Optional[str] = None
    current_iv: Optional[float] = None
    try:
        from backend.bot.iv_regime import classify_ticker
        report = classify_ticker(ticker)
        iv_regime_label = report.regime
        current_iv = report.current_iv
    except Exception:
        pass
    iv_state = {
        "iv_rank": iv_rank,
        "iv_regime": iv_regime_label,
        "current_iv": current_iv,
    }

    sm = build_strategy_matrix(
        ticker=ticker,
        regime_vector=regime_vector,
        pattern_hits=pattern_hits,
        analogs=analogs,
        iv_state=iv_state,
        greeks=None,
    )
    sm_dict = sm.to_dict()
    top = sm_dict.get("top_strategy") if isinstance(sm_dict, dict) else None
    return sm_dict, top


def rule_simulator_veto(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Phase 14.C simulator veto. Reads the consensus' simulator
    verdict; a non-empty reject_reason hard-blocks the trade.

    18-FU Gap R1 — when the operator has approved a tuning for
    ``TUNABLES.simulator_max_loss_veto`` and auto-apply is on, the
    override is recorded in evidence so replay sees the threshold that
    was active at decision time. The underlying SimulatorEngine still
    reads TUNABLES directly when building its veto string (out of scope
    here), so the evidence currently records observation, not
    enforcement — that matches the gate's actual behavior, which is
    the honest signal.
    """
    from backend.bot.learning.policy_apply import resolve_threshold
    from backend.config import TUNABLES as _T

    consensus_obj = ctx.scratch.get("consensus_obj")
    if consensus_obj is None:
        return None
    sv = consensus_obj.simulator_verdict or {}
    if not sv.get("reject_reason"):
        return None
    _, threshold_evidence = resolve_threshold(
        ctx,
        threshold_attr="TUNABLES.simulator_max_loss_veto",
        tunable_default=float(getattr(_T, "simulator_max_loss_veto", 0.30)),
    )
    ctx.event["simulator_verdict"] = sv
    return BlockingFactor(
        category="risk", rule="simulator_veto", severity="hard",
        reason=sv["reject_reason"],
        evidence={"verdict": dict(sv), **threshold_evidence},
        legacy_status="simulator_veto",
    )


def rule_portfolio_context_failed(
    ctx: PolicyContext,
) -> Optional[BlockingFactor]:
    """Build portfolio context for the correlation-cap gate. On
    exception, surface as a hard BlockingFactor (legacy code only
    warned)."""
    if ctx.scratch.get("consensus_obj") is None:
        return None
    try:
        from backend.bot.portfolio_intel.portfolio_context import (
            build_portfolio_context,
        )
    except Exception as exc:
        return BlockingFactor(
            category="data_quality", rule="portfolio_context_failed",
            severity="hard",
            reason=f"portfolio_context import raised: {type(exc).__name__}",
            evidence={"exception": str(exc)[:240]},
            legacy_status="portfolio_context_failed",
        )
    signal = ctx.signal
    positions = (
        ctx.executor.positions() or []
        if hasattr(ctx.executor, "positions") else []
    )
    equity = float(getattr(ctx.account, "portfolio_value", 0.0) or 0.0)
    cand_dir = "LONG"
    action_str = signal.action.value.upper()
    if (action_str.startswith("SELL") or action_str == "BUY_PUT"
            or "SHORT" in action_str):
        cand_dir = "SHORT"
    try:
        pctx = build_portfolio_context(
            positions=positions, equity=equity,
            candidate_ticker=signal.ticker,
            candidate_direction=cand_dir,
        )
    except Exception as exc:
        logger.warning(
            "portfolio context build failed for %s", ctx.ticker, exc_info=True,
        )
        return BlockingFactor(
            category="data_quality", rule="portfolio_context_failed",
            severity="hard",
            reason=f"build_portfolio_context raised: {type(exc).__name__}",
            evidence={"exception": str(exc)[:240]},
            legacy_status="portfolio_context_failed",
        )
    ctx.event["portfolio_context"] = pctx.to_dict()
    ctx.scratch["portfolio_context"] = pctx
    ctx.scratch["positions_snapshot"] = positions
    ctx.scratch["candidate_direction"] = cand_dir
    return None


def rule_market_data_integrity(
    ctx: PolicyContext,
) -> Optional[BlockingFactor]:
    """2026-06-15 — refuse trades when the live quote can't be trusted.

    Catches three failure modes:
      * `quote.source` not in the approved live-feed whitelist — HARD
        (anything ending in ``_stale`` / ``_previous`` is rejected —
        these are yfinance fallbacks from `quote_source.get_quote`)
      * `quote.age_seconds` > MAX_AGE_SEC — feed is stale, HARD block
      * Engine-computed snapshot price diverges from the live quote by
        more than MAX_PRICE_DRIFT_PCT — two layers disagree, HARD

    This is the gate that would have blocked Monday-open trading on
    stale weekend yfinance data. Lives ahead of `rule_naked_short_block`
    so a bad-feed cycle never gets near sizing or collateral checks.

    2026-06-15 calibration update: original 30s age + 0.5% drift was
    blocking 7,932 of ~10,000 evals during normal market hours because
    the engine cycle takes 60-240s (brain timeouts compounding), so the
    quote used for sizing is naturally 1-4 min older than the integrity
    re-check. Raised age threshold to 300s (catches the 58-hour
    yfinance regression we built this for, still blocks anything truly
    stale) and drift to 2% (the original 0.5% was tighter than typical
    minute-bar slip).
    """
    APPROVED_SOURCES = {"alpaca", "thetadata"}
    MAX_AGE_SEC = 300.0
    MAX_PRICE_DRIFT_PCT = 2.0

    # Fetch fresh quote and compare with the snapshot price the engine
    # already has (signal.metadata snapshot.price OR data.price).
    try:
        from backend.bot.data.quote_source import get_quote
        q = get_quote(ctx.ticker.upper())
    except Exception as exc:   # noqa: BLE001
        # Quote resolver itself is broken — block hard.
        return BlockingFactor(
            category="data_quality", rule="market_data_integrity",
            severity="hard",
            reason=f"quote_source.get_quote raised: {str(exc)[:120]}",
            evidence={"failure_mode": "quote_resolver_exception"},
            legacy_status="market_data_integrity",
        )

    src = (q.source or "").lower()
    age = q.age_seconds if q.age_seconds is not None else 1e9

    if src not in APPROVED_SOURCES:
        return BlockingFactor(
            category="data_quality", rule="market_data_integrity",
            severity="hard",
            reason=(
                f"feed source '{q.source}' not in approved "
                f"{sorted(APPROVED_SOURCES)} — refusing to act on "
                f"non-realtime data"
            ),
            evidence={
                "failure_mode": "non_approved_source",
                "source": q.source,
                "age_seconds": q.age_seconds,
                "approved": sorted(APPROVED_SOURCES),
            },
            legacy_status="market_data_integrity",
        )

    if age > MAX_AGE_SEC:
        return BlockingFactor(
            category="data_quality", rule="market_data_integrity",
            severity="hard",
            reason=(
                f"last tick age {age:.0f}s exceeds max {MAX_AGE_SEC:.0f}s "
                f"— feed is stale, refusing to act"
            ),
            evidence={
                "failure_mode": "stale_age",
                "source": q.source,
                "age_seconds": age,
                "max_age_seconds": MAX_AGE_SEC,
            },
            legacy_status="market_data_integrity",
        )

    # Cross-check the engine's snapshot price against the fresh quote.
    # The engine writes `data["price"]` during the cycle; if it diverges
    # from the live tape by > MAX_PRICE_DRIFT_PCT we refuse.
    snap_price = float(ctx.data.get("price") or 0.0)
    if snap_price > 0 and q.price > 0:
        drift_pct = abs(snap_price - q.price) / q.price * 100.0
        if drift_pct > MAX_PRICE_DRIFT_PCT:
            return BlockingFactor(
                category="data_quality", rule="market_data_integrity",
                severity="hard",
                reason=(
                    f"engine snapshot ${snap_price:.2f} differs from "
                    f"live tape ${q.price:.2f} by {drift_pct:.2f}% — "
                    f"refusing (max {MAX_PRICE_DRIFT_PCT}%)"
                ),
                evidence={
                    "failure_mode": "price_divergence",
                    "snapshot_price": snap_price,
                    "live_price": q.price,
                    "drift_pct": round(drift_pct, 4),
                    "max_drift_pct": MAX_PRICE_DRIFT_PCT,
                    "source": q.source,
                    "age_seconds": age,
                },
                legacy_status="market_data_integrity",
            )

    return None


def rule_naked_short_block(
    ctx: PolicyContext,
) -> Optional[BlockingFactor]:
    """Fix N=4 — refuse naked option writes and unbacked covered
    structures.

    Three branches:

    * ``SELL_CALL`` / ``SELL_PUT`` — naked shorts. Engine has no
      facility for matching cash + share margin requirements, so we
      block outright. The 2026-06-13 trial blew up writing 14 naked
      short calls on a $5k account because the executor (Fix N=1)
      was mis-routing CSPs into this bucket. Even with the routing
      fixed, naked shorts are off-limits.
    * ``SELL_COVERED_CALL`` — requires ``100 × contracts`` long shares
      of the underlying. Reads ``ctx.executor.positions()`` and sums
      the open long-stock quantity for the ticker.
    * ``SELL_CSP`` — requires ``strike × 100 × contracts`` cash.
      Reads ``ctx.account.cash``.

    Anything else passes through.
    """
    signal = ctx.signal
    action = signal.action.value if hasattr(signal.action, "value") else str(
        signal.action
    )
    action = action.upper()

    if action not in (
        "SELL_CALL", "SELL_PUT", "SELL_COVERED_CALL", "SELL_CSP",
    ):
        return None

    # Number of contracts the signal wants to write. We don't have the
    # sized quantity yet (RiskManager runs later), so use signal.metadata
    # or default to 1 — the SAFER assumption when we don't know.
    md = signal.metadata or {}
    try:
        contracts = int(md.get("contracts") or md.get("quantity") or 1)
    except (TypeError, ValueError):
        contracts = 1
    if contracts < 1:
        contracts = 1

    # Strike for CSP cash collateral — use signal.strike or metadata.
    try:
        strike = float(signal.strike) if signal.strike else float(
            md.get("strike") or 0.0
        )
    except (TypeError, ValueError):
        strike = 0.0

    if action in ("SELL_CALL", "SELL_PUT"):
        return BlockingFactor(
            category="risk", rule="naked_short_block", severity="hard",
            reason=(
                f"naked {action} blocked — no facility for "
                f"unlimited-loss positions"
            ),
            evidence={
                "action": action,
                "cash_required": None,
                "cash_have": float(getattr(ctx.account, "cash", 0.0) or 0.0),
                "shares_required": None,
                "shares_have": None,
            },
            legacy_status="naked_short_block",
        )

    if action == "SELL_COVERED_CALL":
        shares_required = 100 * contracts
        # Sum long-stock quantity across executor positions for the
        # ticker. Short stock doesn't cover a short call.
        shares_have = 0
        try:
            positions = (
                ctx.executor.positions() if ctx.executor is not None else []
            ) or []
        except Exception:
            positions = []
        for p in positions:
            try:
                if (p.get("kind") == "stock"
                        and (p.get("ticker") or "").upper()
                        == ctx.ticker.upper()):
                    qty = float(p.get("quantity") or 0)
                    if qty > 0:
                        shares_have += int(qty)
            except Exception:
                continue
        if shares_have >= shares_required:
            return None
        return BlockingFactor(
            category="risk", rule="naked_short_block", severity="hard",
            reason=(
                f"SELL_COVERED_CALL needs {shares_required} shares of "
                f"{ctx.ticker}; have {shares_have}"
            ),
            evidence={
                "action": action,
                "contracts": contracts,
                "cash_required": None,
                "cash_have": float(getattr(ctx.account, "cash", 0.0) or 0.0),
                "shares_required": shares_required,
                "shares_have": shares_have,
            },
            legacy_status="naked_short_block",
        )

    # action == "SELL_CSP"
    cash_required = float(strike) * 100.0 * float(contracts)
    cash_have = float(getattr(ctx.account, "cash", 0.0) or 0.0)
    if cash_have >= cash_required and cash_required > 0:
        return None
    return BlockingFactor(
        category="risk", rule="naked_short_block", severity="hard",
        reason=(
            f"SELL_CSP needs ${cash_required:,.2f} cash collateral; "
            f"have ${cash_have:,.2f}"
        ),
        evidence={
            "action": action,
            "contracts": contracts,
            "strike": strike,
            "cash_required": cash_required,
            "cash_have": cash_have,
            "shares_required": None,
            "shares_have": None,
        },
        legacy_status="naked_short_block",
    )


def rule_correlation_cap_block(
    ctx: PolicyContext,
) -> Optional[BlockingFactor]:
    """Phase 14.B correlation-cap gate.

    18-FU Gap 1 — when the operator has approved a tuning for
    ``TUNABLES.correlation_cap_rho`` and auto-apply is on, the override
    is recorded in evidence so replay sees the threshold that was
    active at decision time. The underlying ``check_correlation_cap``
    gate still reads TUNABLES directly (out of scope here), so the
    evidence currently records observation, not enforcement — that
    matches the gate's actual behavior, which is the honest signal.
    """
    from backend.bot.learning.policy_apply import resolve_threshold
    from backend.config import TUNABLES as _T

    pctx = ctx.scratch.get("portfolio_context")
    if pctx is None:
        return None
    _, threshold_evidence = resolve_threshold(
        ctx,
        threshold_attr="TUNABLES.correlation_cap_rho",
        tunable_default=float(_T.correlation_cap_rho),
    )
    from backend.bot.gates.correlation_cap_gate import check_correlation_cap
    corr_result = check_correlation_cap(
        candidate_ticker=ctx.signal.ticker,
        candidate_direction=ctx.scratch["candidate_direction"],
        portfolio_context=pctx,
        positions=ctx.scratch.get("positions_snapshot") or [],
    )
    ctx.event["correlation_cap"] = corr_result.to_dict()
    if not corr_result.blocked:
        return None
    return BlockingFactor(
        category="portfolio", rule="correlation_cap_block", severity="hard",
        reason=corr_result.reason,
        evidence={"result": corr_result.to_dict(), **threshold_evidence},
        legacy_status="correlation_cap_block",
    )


def rule_consensus_abstain(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Council / Chairman abstain or monitor-only blocks the trade.

    Three legacy_status branches the engine emitted:
    - chairman_abstain  (Chairman authoritative, decision == ABSTAIN)
    - chairman_monitor  (Chairman authoritative, decision == MONITOR)
    - consensus_abstain (legacy path, consensus.recommendation == abstain)
    """
    from backend.config import TUNABLES

    consensus_obj = ctx.scratch.get("consensus_obj")
    if consensus_obj is None:
        return None
    use_chairman = bool(
        getattr(TUNABLES, "chairman_authoritative", False)
    ) or ctx.use_brain
    chairman_report = (
        consensus_obj.to_dict().get("chairman_report") or {}
    )
    chairman_decision = (
        chairman_report.get("decision") if use_chairman else None
    )
    ctx.event["consensus_authority"] = (
        "chairman" if use_chairman else "legacy"
    )
    consensus_gate_on = (
        bool(ctx.ai_config.get("consensus_abstain_enabled"))
        or ctx.use_brain
    )
    if not consensus_gate_on:
        return None

    if use_chairman:
        if chairman_decision in ("ABSTAIN", "MONITOR"):
            legacy = (
                "chairman_monitor"
                if chairman_decision == "MONITOR"
                else "chairman_abstain"
            )
            _record_brain_cooldown(ctx)
            return BlockingFactor(
                category="strategy", rule="consensus_abstain",
                severity="hard",
                reason=(
                    f"chairman: {chairman_decision} · "
                    f"{chairman_report.get('decision_reason') or 'no reason'}"
                ),
                evidence={
                    "decision": chairman_decision,
                    "decision_reason": chairman_report.get("decision_reason"),
                },
                legacy_status=legacy,
            )
        if chairman_decision == "SIZE_DOWN":
            psm = float(
                chairman_report.get("position_size_modifier") or 1.0
            )
            ctx.event["chairman_size_modifier"] = psm
        return None

    # Legacy (non-chairman) path: refuse on consensus.recommendation==abstain
    if consensus_obj.recommendation == "abstain":
        _record_brain_cooldown(ctx)
        return BlockingFactor(
            category="strategy", rule="consensus_abstain", severity="hard",
            reason=(
                f"agent consensus says abstain "
                f"({consensus_obj.abstain_count} of "
                f"{len(consensus_obj.votes)} agents)"
            ),
            evidence={
                "abstain_count": consensus_obj.abstain_count,
                "n_votes": len(consensus_obj.votes),
            },
            legacy_status="consensus_abstain",
        )
    return None


# ── 15. already_held ────────────────────────────────────────────────────

def rule_already_held(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Block pyramiding into a ticker we already hold. Splits between
    stock-level and option-level dedup keys."""
    signal = ctx.signal
    if not _is_buy_action(signal.action):
        return None
    action_str = signal.action.value
    action_lower = action_str.lower()
    is_option = (
        "_call" in action_lower or "_put" in action_lower
        or action_str in {"BUY_CALL", "BUY_PUT", "SELL_CALL", "SELL_PUT"}
        or signal.action in COMPLEX_OPTIONS
    )
    if not is_option:
        if ctx.ticker.upper() in ctx.held_tickers:
            return BlockingFactor(
                category="portfolio", rule="already_held", severity="hard",
                reason=f"already holding {ctx.ticker}; managed by exit logic",
                evidence={"ticker": ctx.ticker.upper()},
                legacy_status="already_held",
            )
        return None
    # Option-level dedup.
    meta = signal.metadata or {}
    proposed_strike = meta.get("strike")
    proposed_expiry = meta.get("expiration") or meta.get("expiry")
    kind = "call" if "call" in action_lower else (
        "put" if "put" in action_lower else "complex"
    )
    try:
        proposed_strike_f = (
            round(float(proposed_strike), 2)
            if proposed_strike is not None else None
        )
    except (TypeError, ValueError):
        proposed_strike_f = None
    key = (
        ctx.ticker.upper(), kind, proposed_strike_f,
        str(proposed_expiry) if proposed_expiry else None,
    )
    if key in ctx.held_option_keys:
        return BlockingFactor(
            category="portfolio", rule="already_held", severity="hard",
            reason=(
                f"already holding option {kind} {ctx.ticker} "
                f"{proposed_strike}/{proposed_expiry}"
            ),
            evidence={
                "kind": kind, "strike": proposed_strike,
                "expiry": str(proposed_expiry) if proposed_expiry else None,
            },
            legacy_status="already_held",
        )
    return None


# ── 16. risk_manager_rejected ───────────────────────────────────────────

def rule_risk_manager_rejected(
    ctx: PolicyContext,
) -> Optional[BlockingFactor]:
    """RiskManager.evaluate() returned approved=False. Writes the
    decision onto ``ctx.scratch['risk_decision']`` for the dust rule +
    downstream sizing."""
    signal = ctx.signal
    price = float((ctx.data or {}).get("price", 0.0))
    trade_style = (ctx.config.get("trade_styles") or ["intraday"])[0]
    is_paper = (
        ctx.config.get("paper_mode", True)
        or (ctx.config.get("broker") or "").startswith("local_paper")
        or (ctx.config.get("broker") or "").startswith("alpaca_paper")
    )
    side = (
        "BUY"
        if signal.action.value.startswith("BUY")
        or signal.action in COMPLEX_OPTIONS
        else "SELL"
    )
    decision = ctx.risk_manager.evaluate(
        side, price, ctx.account,
        trade_style=trade_style, is_paper=is_paper,
    )
    ctx.event["risk"] = decision.reason
    ctx.scratch["risk_decision"] = decision
    ctx.scratch["price"] = price
    if decision.approved:
        return None
    return BlockingFactor(
        category="risk", rule="risk_manager_rejected", severity="hard",
        reason=decision.reason or "risk manager rejected",
        evidence={"side": side, "price": price},
        legacy_status="rejected",
        # Legacy gate set event["risk"] (already done above) but never
        # event["reason"]. Preserve that contract for UI parity.
        override_event_reason=False,
    )


# ── 17. dust_order (too_small) ──────────────────────────────────────────

def rule_dust_order(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Reject sub-minimum-notional stock orders.

    Registered as ``deferred`` so the policy's main ``evaluate()`` pass
    skips it — the engine invokes this evaluator directly AFTER eod
    sizing applies its multiplier, since sizing can shrink a stock
    order below MIN_NOTIONAL.
    """
    if ctx.signal.action not in STOCK_ACTIONS:
        return None
    decision = ctx.scratch.get("risk_decision")
    if decision is None:
        return None
    min_notional = float(ctx.scratch.get("min_notional", 25.0))
    notional = float(decision.quantity) * float(ctx.scratch.get("price", 0.0))
    if notional >= min_notional:
        return None
    return BlockingFactor(
        category="execution", rule="dust_order", severity="hard",
        reason=(
            f"order ${notional:.2f} below ${min_notional:.0f} minimum "
            f"(low buying power)"
        ),
        evidence={
            "notional": notional, "min_notional": min_notional,
            "quantity": float(decision.quantity),
        },
        legacy_status="too_small",
    )


# ── 18. brain_cooldown ──────────────────────────────────────────────────

def rule_brain_cooldown(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Explicit cooldown enforcement. The engine already produces a
    cooldown HOLD signal before policy evaluation; this rule labels it
    so the veto-budget telemetry tracks cooldown rejections distinctly
    from generic HOLDs.

    ``legacy_status`` stays ``hold`` to preserve the contract with
    Mission Control / gate_diagnostics that read event["status"] —
    the new tagging surfaces only via ``rule_name`` in
    BlockingFactor + ``/policy/veto-budget``.
    """
    if not ctx.use_brain:
        return None
    if ctx.signal.action != Action.HOLD:
        return None
    reason = ctx.signal.reason or ""
    if "cooldown" not in reason.lower():
        return None
    return BlockingFactor(
        category="strategy", rule="brain_cooldown", severity="hard",
        reason=reason,
        evidence={"strategy": "ai_brain"},
        legacy_status="hold",
    )


# ── 19. memory_bias_failed (soft, 0%) ───────────────────────────────────

def rule_memory_bias_failed(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """Placeholder for the future memory-bias re-implementation. The
    legacy dead block (agents.aggregate → apply_memory_bias(votes,
    context)) called ``context`` which was undefined — NameError every
    cycle. 16.A deletes the dead block; if a future caller re-introduces
    apply_memory_bias and it raises here, this rule surfaces the
    failure as a soft (0%) BlockingFactor instead of a silent debug
    log."""
    failure = ctx.scratch.get("memory_bias_failure")
    if not failure:
        return None
    return BlockingFactor(
        category="data_quality", rule="memory_bias_failed", severity="soft",
        reason=f"memory bias raised: {failure[:200]}",
        evidence={"exception": failure},
        sizing_penalty_pct=0.0,
        legacy_status="",
    )


# ── 20. source_scores_unavailable (soft, 0%) ────────────────────────────

def rule_source_scores_unavailable(
    ctx: PolicyContext,
) -> Optional[BlockingFactor]:
    """The engine's _stage19_source_scores wrap silently swallows
    failures. Tag them as a soft BlockingFactor so the operator sees
    when source-attribution telemetry is starved.

    Triggered only when the caller has stamped
    ``scratch['source_scores_failure']`` with the exception string."""
    failure = ctx.scratch.get("source_scores_failure")
    if not failure:
        return None
    return BlockingFactor(
        category="data_quality", rule="source_scores_unavailable",
        severity="soft",
        reason=f"source-score snapshot raised: {failure[:200]}",
        evidence={"exception": failure},
        sizing_penalty_pct=0.0,
        legacy_status="",
    )


# ── 21. cycle_budget_overrun ────────────────────────────────────────────

def rule_cycle_budget_overrun(
    ctx: PolicyContext,
) -> Optional[BlockingFactor]:
    """Hard-block when the upstream watchdog has flagged the cycle as
    over its wall-time budget. Engine's ``_live_loop`` writes
    ``scratch['cycle_budget_overrun_seconds']`` when the timeout
    fires.

    18-FU Gap R1 — operator-approved overrides for
    ``TUNABLES.engine_cycle_timeout_sec`` are recorded in evidence
    so replay sees the threshold that was active at decision time.
    The watchdog at ``backend/bot/engine.py:200`` still reads
    TUNABLES directly (out of scope here), so the evidence currently
    records observation, not enforcement — that matches the gate's
    actual behavior, which is the honest signal.
    """
    from backend.bot.learning.policy_apply import resolve_threshold
    from backend.config import TUNABLES as _T

    seconds = ctx.scratch.get("cycle_budget_overrun_seconds")
    if not seconds:
        return None
    _, threshold_evidence = resolve_threshold(
        ctx,
        threshold_attr="TUNABLES.engine_cycle_timeout_sec",
        tunable_default=float(
            getattr(_T, "engine_cycle_timeout_sec", 240.0)
        ),
    )
    return BlockingFactor(
        category="data_quality", rule="cycle_budget_overrun", severity="hard",
        reason=(
            f"engine cycle exceeded {float(seconds):.0f}s wall-time budget"
        ),
        evidence={"budget_seconds": float(seconds), **threshold_evidence},
        legacy_status="cycle_budget_overrun",
    )


# ── 22. analytics_result_undefined (latent bug) ─────────────────────────

def rule_analytics_result_undefined(
    ctx: PolicyContext,
) -> Optional[BlockingFactor]:
    """Legacy bug: the abstain gate could reference an undefined
    ``analytics_result`` if the analytics block raised before it ran.
    Surface explicitly so the operator sees the precondition violation
    instead of a NameError swallowed by the broad try/except."""
    if ctx.signal.action == Action.HOLD:
        return None
    if not ctx.analytics_cfg.get("enabled", True):
        return None
    if "analytics_result" in ctx.scratch:
        return None
    # analytics_build_failed already fired — that rule is responsible
    # for the headline. We only fire when the analytics build was
    # skipped without a corresponding failure.
    return BlockingFactor(
        category="data_quality", rule="analytics_result_undefined",
        severity="hard",
        reason=(
            "analytics_result missing — abstain gate would NameError "
            "without this rule"
        ),
        evidence={"analytics_enabled": True},
        legacy_status="analytics_result_undefined",
    )


# ── 23. memory_bias_dead_code (latent, soft 0%) ─────────────────────────

# One-shot warning per process: fires on first cycle after deploy so the
# operator sees that 16.A removed the dead apply_memory_bias block.
_MEMORY_BIAS_DEAD_CODE_SEEN = {"flag": False}


def rule_decision_stale(ctx: PolicyContext) -> Optional[BlockingFactor]:
    """MITS Phase 16.E sentinel — never fires during normal evaluate().

    Registered ``deferred=True`` so the main policy pass skips it; the
    real stale check runs in ``BotEngine._revalidate_decision_pre_fill``
    just before the executor submit. The engine writes its own
    ``policy_rule_evaluations`` row when the rollback fires so the
    ``/policy/veto-budget`` panel still aggregates the decision_stale
    abort rate alongside the rest of the policy library.
    """
    return None


def rule_memory_bias_dead_code(
    ctx: PolicyContext,
) -> Optional[BlockingFactor]:
    """One-shot operator notice: the legacy ``apply_memory_bias(votes,
    context)`` block at agents/__init__.py:1883-1888 referenced an
    undefined ``context`` symbol — NameError every cycle that the
    broad try/except swallowed. 16.A deletes the block; this rule
    emits a single soft BlockingFactor on the first policy evaluation
    after process boot to make the deletion observable."""
    if _MEMORY_BIAS_DEAD_CODE_SEEN["flag"]:
        return None
    _MEMORY_BIAS_DEAD_CODE_SEEN["flag"] = True
    return BlockingFactor(
        category="data_quality", rule="memory_bias_dead_code", severity="soft",
        reason=(
            "Phase 16.A removed the dead apply_memory_bias(votes, "
            "context) block at agents/__init__.py:1883-1888 "
            "(context was undefined → NameError every cycle)."
        ),
        evidence={"one_shot": True},
        sizing_penalty_pct=0.0,
        legacy_status="",
    )


# ── helper: brain cooldown stamping ─────────────────────────────────────

def _record_brain_cooldown(ctx: PolicyContext) -> None:
    """Replicates the engine's ``_record_brain_cooldown`` helper.
    A ticker rejected by any hard gate goes into the cooldown so the
    AI Brain stops re-proposing it for ``_brain_cooldown_seconds``."""
    if not ctx.use_brain:
        return
    if ctx.signal.action == Action.HOLD:
        return
    seconds = float(ctx.scratch.get("brain_cooldown_seconds", 600.0))
    ctx.brain_cooldown[ctx.ticker.upper()] = _time.time() + seconds


# ── registration ────────────────────────────────────────────────────────

def _register_all(policy: DecisionPolicy) -> None:
    """Register every rule in the order ``DecisionPolicy.evaluate``
    must walk them. Order matters: it determines which hard rule
    becomes the headline blocker when several fire simultaneously.

    Ordering rationale:
    - market_closed > kill_switch > options_disabled run before any
      data-quality probe so a closed market doesn't trigger a fake
      analytics_failed.
    - analytics_build_failed runs early because abstain + low_grade
      depend on its output.
    - signal_hold runs before low_confidence so HOLD signals carry
      the right legacy_status.
    - consensus_exception precedes simulator_veto / correlation_cap /
      consensus_abstain — they all read the consensus_obj scratch.
    - risk_manager_rejected precedes dust_order which reads the
      decision.
    - cycle_budget_overrun is registered last among hard rules so it
      surfaces only when nothing else explains the abort.
    - All soft rules (meta_ai_offline, memory_bias_failed,
      source_scores_unavailable, memory_bias_dead_code) run only after
      every hard rule passed.
    """
    # Hard, market / kill / config gates.
    policy.register(PolicyRule(
        "market_closed", "market", "hard", rule_market_closed,
    ))
    policy.register(PolicyRule(
        "kill_switch_active", "risk", "hard", rule_kill_switch_active,
    ))
    policy.register(PolicyRule(
        "options_disabled", "strategy", "hard", rule_options_disabled,
    ))
    # Build analytics + run abstain / event-risk / catalyst.
    policy.register(PolicyRule(
        "analytics_build_failed", "data_quality", "hard",
        rule_analytics_build_failed,
    ))
    policy.register(PolicyRule(
        "analytics_result_undefined", "data_quality", "hard",
        rule_analytics_result_undefined,
    ))
    policy.register(PolicyRule(
        "abstain_and_throttle", "strategy", "hard",
        rule_abstain_and_throttle,
    ))
    policy.register(PolicyRule(
        "event_risk_window", "market", "hard", rule_event_risk_window,
    ))
    policy.register(PolicyRule(
        "catalyst_gate", "market", "hard", rule_catalyst_gate,
    ))
    # Strategy-shape gates.
    policy.register(PolicyRule(
        "brain_cooldown", "strategy", "hard", rule_brain_cooldown,
    ))
    policy.register(PolicyRule(
        "signal_hold", "strategy", "hard", rule_signal_hold,
    ))
    policy.register(PolicyRule(
        "low_confidence", "strategy", "hard", rule_low_confidence,
    ))
    # Drift halt + low_grade.
    policy.register(PolicyRule(
        "drift_check_failed", "data_quality", "hard",
        rule_drift_check_failed,
    ))
    policy.register(PolicyRule(
        "drift_halt", "data_quality", "hard", rule_drift_halt,
    ))
    policy.register(PolicyRule(
        "low_grade", "strategy", "hard", rule_low_grade,
    ))
    # IV richness + meta-AI veto.
    policy.register(PolicyRule(
        "iv_too_rich", "strategy", "hard", rule_iv_too_rich,
    ))
    # meta_ai_offline is SOFT — registered later in the soft block.
    policy.register(PolicyRule(
        "meta_rejected", "strategy", "hard", rule_meta_rejected,
    ))
    # Council.
    policy.register(PolicyRule(
        "consensus_exception", "data_quality", "hard",
        rule_consensus_exception,
    ))
    policy.register(PolicyRule(
        "simulator_veto", "risk", "hard", rule_simulator_veto,
    ))
    policy.register(PolicyRule(
        "portfolio_context_failed", "data_quality", "hard",
        rule_portfolio_context_failed,
    ))
    # 2026-06-15 — market_data_integrity registered BEFORE naked_short
    # so a stale-feed cycle never reaches collateral/risk checks. This
    # is the gate the operator demanded after catching the UI label
    # "LIVE $741" on Friday-close yfinance data.
    policy.register(PolicyRule(
        "market_data_integrity", "data_quality", "hard",
        rule_market_data_integrity,
    ))
    # Fix N=4 — naked-short refusal + covered-call shares check + CSP
    # cash collateral. Registered BEFORE correlation_cap_block so the
    # safety veto fires early in the chain.
    policy.register(PolicyRule(
        "naked_short_block", "risk", "hard", rule_naked_short_block,
    ))
    policy.register(PolicyRule(
        "correlation_cap_block", "portfolio", "hard",
        rule_correlation_cap_block,
    ))
    policy.register(PolicyRule(
        "consensus_abstain", "strategy", "hard", rule_consensus_abstain,
    ))
    # Held / risk / dust.
    policy.register(PolicyRule(
        "already_held", "portfolio", "hard", rule_already_held,
    ))
    policy.register(PolicyRule(
        "risk_manager_rejected", "risk", "hard", rule_risk_manager_rejected,
    ))
    # dust_order intentionally lives AFTER eod sizing in the engine's
    # run_cycle (sizing can shrink a stock quantity below MIN_NOTIONAL).
    # ``deferred=True`` excludes it from the main policy.evaluate() pass
    # — the engine invokes the evaluator inline post-sizing and writes
    # one ``policy_rule_evaluations`` row directly.
    policy.register(PolicyRule(
        "dust_order", "execution", "hard", rule_dust_order, deferred=True,
    ))
    # Cycle-level guard.
    policy.register(PolicyRule(
        "cycle_budget_overrun", "data_quality", "hard",
        rule_cycle_budget_overrun,
    ))
    # Soft (sizing-penalty) rules.
    policy.register(PolicyRule(
        "meta_ai_offline", "data_quality", "soft", rule_meta_ai_offline,
    ))
    policy.register(PolicyRule(
        "memory_bias_failed", "data_quality", "soft",
        rule_memory_bias_failed,
    ))
    policy.register(PolicyRule(
        "source_scores_unavailable", "data_quality", "soft",
        rule_source_scores_unavailable,
    ))
    policy.register(PolicyRule(
        "memory_bias_dead_code", "data_quality", "soft",
        rule_memory_bias_dead_code,
    ))
    # MITS Phase 16.E — sentinel for the pre-fill rollback hook. Deferred
    # so it never runs during evaluate(); the engine writes a
    # policy_rule_evaluations row directly when the hook aborts a trade.
    policy.register(PolicyRule(
        "decision_stale", "data_quality", "hard", rule_decision_stale,
        deferred=True,
    ))
