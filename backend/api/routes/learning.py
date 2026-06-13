"""Learning-feedback endpoints — surface what the bot is learning from outcomes.

Two layers live here:

  * Legacy ``/learning/insights`` — DecisionLog-driven aggregate
    surfaces written by Phase 2's learning loop. Untouched by 18.A.

  * MITS Phase 18.A ``/learning/attribution`` — per-agent / per-axis /
    per-strategy calibration scoreboard computed from closed Trades +
    decision_provenance rows. The foundation of the Phase 18 Learning
    Layer. Min-N guardrails surface ``insufficient_sample_size`` so
    the operator never sees a misleading number on a thin sample.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from sqlalchemy import desc, select

from backend.bot.learning import insights
from backend.bot.learning.attribution import (
    DEFAULT_MIN_N_AGENT,
    DEFAULT_MIN_N_AXIS,
    DEFAULT_MIN_N_STRATEGY,
    DEFAULT_WINDOW_DAYS,
    compute_attribution_report,
)
from backend.bot.learning.attribution_writer import (
    SCOPE_KIND_AGENT,
    SCOPE_KIND_AXIS,
    SCOPE_KIND_STRATEGY,
    latest_attribution_rows,
    persist_attribution_report,
)
from backend.bot.learning.counterfactual import (
    ALLOWED_STANCES,
    DEFAULT_SIZING_FACTORS,
    compute_all_counterfactuals,
    consensus_counterfactual,
    policy_counterfactual,
    sizing_counterfactual,
)
from backend.bot.learning.funnel import (
    COUNTERFACTUAL_SAMPLE_SIZE as FUNNEL_COUNTERFACTUAL_SAMPLE_SIZE,
    compute_funnel_report,
    funnel_history,
    is_anomalous_drop,
    latest_funnel_row,
    persist_funnel_report,
)
from backend.bot.learning.policy_tuning import (
    DEFAULT_MIN_N_PER_BUCKET,
    DEFAULT_WINDOW_DAYS as POLICY_TUNING_WINDOW_DAYS,
    TUNABLE_RULES,
    compute_policy_tuning,
    latest_policy_tuning_rows,
    persist_policy_tuning,
)
from backend.bot.learning.weight_adaptation import (
    AGENT_BASE_WEIGHTS,
    DEFAULT_MIN_N as WEIGHT_DEFAULT_MIN_N,
    DEFAULT_WINDOW_DAYS as WEIGHT_DEFAULT_WINDOW_DAYS,
    compute_weight_proposals,
    history_for_agent,
    latest_weight_rows,
    persist_weight_proposals,
)
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.agent_weight_history import AgentWeightHistory
from backend.models.counterfactual_replay import CounterfactualReplay
from backend.models.learned_attribution import LearnedAttribution
from backend.models.learning_rollback_log import (
    ACTION_APPROVE,
    ACTION_ROLLBACK,
    ALLOWED_ACTIONS,
    ALLOWED_TABLES,
    LearningRollbackLog,
    TABLE_AGENT_WEIGHT_HISTORY,
    TABLE_LEARNED_ATTRIBUTION,
    TABLE_POLICY_TUNINGS,
)
from backend.models.policy_tuning import PolicyTuning


# Map a table-name string from the request body to the concrete ORM
# model. Centralized so every endpoint that takes a ``table`` arg
# rejects unknown tables with the same 400 shape, and so the
# learning-table → model relationship lives in exactly one place.
_TABLE_TO_MODEL = {
    TABLE_LEARNED_ATTRIBUTION: LearnedAttribution,
    TABLE_POLICY_TUNINGS: PolicyTuning,
    TABLE_AGENT_WEIGHT_HISTORY: AgentWeightHistory,
}


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/learning", tags=["learning"])


# 5-minute TTL cache for the funnel ON-DEMAND FALLBACK path only. When
# ``decision_funnel_daily`` has a fresh row we always serve that (it's
# an O(1) read); the fallback ``compute_funnel_report`` scans every
# provenance row in the window and is expensive enough that rapid
# cockpit polls would otherwise stack. Keyed by ``window`` so different
# window sizes cache independently.
_FUNNEL_FALLBACK_CACHE_TTL_SEC = 300.0
_FUNNEL_FALLBACK_CACHE: Dict[int, Dict[str, Any]] = {}
_FUNNEL_FALLBACK_CACHE_LOCK = threading.Lock()


@router.get("/insights")
async def learning_insights(limit: int = Query(1000, ge=10, le=10000)) -> dict:
    """Per-strategy / per-regime / per-grade win-rate + P&L aggregates from the
    decision log, plus failing combinations worth pruning."""
    return insights(limit=limit)


# ── MITS Phase 18.A — Learned Hypothesis Attribution ─────────────────


def _decode_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """Inline-decode payload_json so the API consumer doesn't have to
    parse it a second time. Returns the row dict with ``payload``
    populated (full dataclass-projection) and ``payload_json`` dropped."""
    out = dict(row)
    raw = out.pop("payload_json", None)
    if raw:
        try:
            out["payload"] = json.loads(raw)
        except (TypeError, ValueError):
            out["payload"] = None
    else:
        out["payload"] = None
    return out


@router.get("/attribution")
async def get_attribution(
    window: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=730),
    scope: Optional[str] = Query(
        None,
        description="Filter to one of: agent, axis, strategy. Omit to return all.",
    ),
) -> Dict[str, Any]:
    """Return the latest computed calibration batch.

    The response is a single cohesive snapshot — the most recent
    ``computed_at`` whose rows match the ``window`` + ``scope`` filter.
    Operator never sees a Frankenstein blend of multiple batches.
    """
    rows = latest_attribution_rows(
        scope_kind=scope, window_days=window, limit=500,
    )
    decoded: List[Dict[str, Any]] = [_decode_payload(r) for r in rows]
    return {
        "window_days": window,
        "scope": scope,
        "count": len(decoded),
        "rows": decoded,
        "computed_at": (decoded[0]["computed_at"] if decoded else None),
    }


@router.get("/attribution/agents")
async def get_attribution_agents(
    window: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=730),
) -> Dict[str, Any]:
    """Per-agent calibration scoreboard — Brier + ECE + hit-rate (Wilson CI).

    Returns one entry per agent in ``KNOWN_AGENTS`` even when the
    closed-sample count falls below ``min_n``. The
    ``notes=["insufficient_sample_size_n_lt_<N>"]`` flag tells the UI to
    render "not enough data" instead of fabricating a number.
    """
    rows = latest_attribution_rows(
        scope_kind=SCOPE_KIND_AGENT, window_days=window, limit=200,
    )
    return {
        "window_days": window,
        "min_n": DEFAULT_MIN_N_AGENT,
        "count": len(rows),
        "agents": [_decode_payload(r) for r in rows],
        "computed_at": (rows[0]["computed_at"] if rows else None),
    }


@router.get("/attribution/axes")
async def get_attribution_axes(
    window: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=730),
) -> Dict[str, Any]:
    """Per-axis calibration scoreboard — Spearman ρ + high/low
    discrimination on the 6 ConfidenceBreakdown axes."""
    rows = latest_attribution_rows(
        scope_kind=SCOPE_KIND_AXIS, window_days=window, limit=200,
    )
    return {
        "window_days": window,
        "min_n": DEFAULT_MIN_N_AXIS,
        "count": len(rows),
        "axes": [_decode_payload(r) for r in rows],
        "computed_at": (rows[0]["computed_at"] if rows else None),
    }


@router.get("/attribution/strategies")
async def get_attribution_strategies(
    window: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=730),
) -> Dict[str, Any]:
    """Per-strategy calibration scoreboard, stratified by RegimeVector.trend."""
    rows = latest_attribution_rows(
        scope_kind=SCOPE_KIND_STRATEGY, window_days=window, limit=200,
    )
    return {
        "window_days": window,
        "min_n": DEFAULT_MIN_N_STRATEGY,
        "count": len(rows),
        "strategies": [_decode_payload(r) for r in rows],
        "computed_at": (rows[0]["computed_at"] if rows else None),
    }


@router.post("/attribution/recompute")
async def post_attribution_recompute(
    window: int = Query(DEFAULT_WINDOW_DAYS, ge=1, le=730),
    persist: bool = Query(
        True,
        description="If True, write the result rows to learned_attribution.",
    ),
) -> Dict[str, Any]:
    """Force a fresh calibration pass over the closed-decision window.

    Used by the cockpit "refresh now" affordance and by the nightly
    scheduler hook. When ``persist=True`` (the default), one row per
    scope is appended to ``learned_attribution``; the GET endpoints
    will pick the new snapshot up immediately.
    """
    report = compute_attribution_report(window_days=window)
    if not persist:
        return {
            "ok": True,
            "persisted": False,
            "window_days": window,
            "n_closed_decisions": report.get("n_closed_decisions"),
            "report": report,
        }
    persistence_meta = persist_attribution_report(
        window_days=window, report=report,
    )
    return {
        "ok": bool(persistence_meta.get("ok")),
        "persisted": True,
        "window_days": window,
        "n_closed_decisions": report.get("n_closed_decisions"),
        "counts": persistence_meta.get("counts"),
        "computed_at": persistence_meta.get("computed_at"),
    }


# ── MITS Phase 18.B — Counterfactual Replayer ────────────────────────


_CFR_KIND_SIZING = "sizing"
_CFR_KIND_POLICY = "policy"
_CFR_KIND_CONSENSUS = "consensus"
_CFR_KIND_BUNDLE = "bundle"


def _cache_get(
    provenance_id: int, variation_kind: str, variation_key: str,
) -> Optional[Dict[str, Any]]:
    """Return the cached result_json (decoded) for the latest row
    matching the key, or None when nothing is cached. We pick the
    LATEST computed_at when multiple rows exist — keeps the cockpit
    honest about which compute is being shown."""
    with session_scope() as s:
        row = s.execute(
            select(CounterfactualReplay)
            .where(CounterfactualReplay.provenance_id == int(provenance_id))
            .where(CounterfactualReplay.variation_kind == variation_kind)
            .where(CounterfactualReplay.variation_key == variation_key)
            .order_by(desc(CounterfactualReplay.computed_at))
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        cached_id = int(row.id)
        cached_computed_at = (
            row.computed_at.isoformat() if row.computed_at else None
        )
        raw = row.result_json or ""
    try:
        decoded = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return None
    decoded["_cache_id"] = cached_id
    decoded["_cache_computed_at"] = cached_computed_at
    return decoded


def _cache_put(
    provenance_id: int, variation_kind: str, variation_key: str,
    result: Dict[str, Any],
) -> int:
    """Write a row to the cache. Returns the new row's id. Never
    mutates an existing row — appending preserves an audit trail of
    every What-if ask the operator made."""
    payload = json.dumps(result, default=str)
    with session_scope() as s:
        row = CounterfactualReplay(
            computed_at=datetime.utcnow(),
            provenance_id=int(provenance_id),
            variation_kind=variation_kind,
            variation_key=variation_key,
            result_json=payload,
        )
        s.add(row)
        s.flush()
        return int(row.id)


def _factors_key(factors: List[float]) -> str:
    """Stable cache key for a sizing factor list."""
    rounded = [round(float(f), 4) for f in factors]
    return "factors=" + ",".join(f"{f}" for f in rounded)


@router.get("/counterfactual/{provenance_id}")
async def get_counterfactual_bundle(provenance_id: int) -> Dict[str, Any]:
    """Return the all-in-one bundle for the cockpit's What-if panel.

    Always returns a CounterfactualResult shape (with ``notes``
    explaining any None variation). Cached by provenance_id so the
    cockpit re-open is instant. The cache row carries the result
    payload as JSON; first-call computes and inserts, subsequent
    calls read from cache.
    """
    if provenance_id <= 0:
        raise HTTPException(status_code=400, detail="provenance_id must be > 0")
    cached = _cache_get(provenance_id, _CFR_KIND_BUNDLE, "all")
    if cached is not None:
        return cached
    result = compute_all_counterfactuals(provenance_id).to_dict()
    new_id = _cache_put(
        provenance_id, _CFR_KIND_BUNDLE, "all", result,
    )
    result["_cache_id"] = new_id
    result["_cache_computed_at"] = result.get("computed_at")
    return result


@router.post("/counterfactual/{provenance_id}/sizing")
async def post_sizing_counterfactual(
    provenance_id: int,
    body: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    """Linear sizing CF over the requested factor list (defaults to
    [0.5, 1.0, 1.5, 2.0]). Returns ``{counterfactual: ...}`` or 404
    when the trade isn't eligible."""
    if provenance_id <= 0:
        raise HTTPException(status_code=400, detail="provenance_id must be > 0")
    raw_factors = body.get("factors") if isinstance(body, dict) else None
    if raw_factors is None:
        factors = list(DEFAULT_SIZING_FACTORS)
    else:
        try:
            factors = [float(f) for f in raw_factors]
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="factors must be a list of numbers",
            )
        if not factors:
            factors = list(DEFAULT_SIZING_FACTORS)
    key = _factors_key(factors)
    cached = _cache_get(provenance_id, _CFR_KIND_SIZING, key)
    if cached is not None:
        return {"counterfactual": cached}
    cf = sizing_counterfactual(provenance_id, factors=factors)
    if cf is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "sizing counterfactual not available — "
                "linked trade is not closed or sizing_chain_json is missing"
            ),
        )
    payload = cf.to_dict()
    new_id = _cache_put(provenance_id, _CFR_KIND_SIZING, key, payload)
    payload["_cache_id"] = new_id
    return {"counterfactual": payload}


@router.post("/counterfactual/{provenance_id}/policy")
async def post_policy_counterfactual(
    provenance_id: int,
    body: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    """Policy CF — override one hard BlockingFactor by ``rule_name``.
    Returns 404 when the rule didn't actually block on the original
    decision."""
    if provenance_id <= 0:
        raise HTTPException(status_code=400, detail="provenance_id must be > 0")
    rule_name = str(body.get("rule_name") or "").strip()
    if not rule_name:
        raise HTTPException(status_code=400, detail="rule_name is required")
    key = f"rule={rule_name}"
    cached = _cache_get(provenance_id, _CFR_KIND_POLICY, key)
    if cached is not None:
        return {"counterfactual": cached}
    cf = policy_counterfactual(provenance_id, rule_name)
    if cf is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "policy counterfactual not available — "
                "rule_did_not_block_original_decision or policy_result missing"
            ),
        )
    payload = cf.to_dict()
    new_id = _cache_put(provenance_id, _CFR_KIND_POLICY, key, payload)
    payload["_cache_id"] = new_id
    return {"counterfactual": payload}


@router.post("/counterfactual/{provenance_id}/consensus")
async def post_consensus_counterfactual(
    provenance_id: int,
    body: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    """Consensus CF — flip one agent's vote and re-aggregate.

    Body shape: ``{agent: str, new_stance: str, new_confidence: int}``.
    new_stance must be one of ``ALLOWED_STANCES``. new_confidence is
    the AgentOutput-style int 0..100 (matches the persisted shape).
    """
    if provenance_id <= 0:
        raise HTTPException(status_code=400, detail="provenance_id must be > 0")
    agent = str(body.get("agent") or "").strip()
    new_stance = str(body.get("new_stance") or "").strip().lower()
    try:
        new_confidence = int(body.get("new_confidence") or 0)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="new_confidence must be an int 0..100",
        )
    if not agent:
        raise HTTPException(status_code=400, detail="agent is required")
    if new_stance not in ALLOWED_STANCES:
        raise HTTPException(
            status_code=400,
            detail=f"new_stance must be one of {list(ALLOWED_STANCES)}",
        )
    new_confidence = max(0, min(100, new_confidence))
    key = f"agent={agent}->{new_stance}@{new_confidence}"
    cached = _cache_get(provenance_id, _CFR_KIND_CONSENSUS, key)
    if cached is not None:
        return {"counterfactual": cached}
    cf = consensus_counterfactual(
        provenance_id,
        agent=agent,
        new_stance=new_stance,
        new_confidence=new_confidence,
    )
    if cf is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "consensus counterfactual not available — "
                "agent_outputs missing or agent name not present in provenance row"
            ),
        )
    payload = cf.to_dict()
    new_id = _cache_put(provenance_id, _CFR_KIND_CONSENSUS, key, payload)
    payload["_cache_id"] = new_id
    return {"counterfactual": payload}


# ── MITS Phase 18.C — Policy Auto-Tuning (Advisory) ──────────────────


def _decode_policy_tuning_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Inline-decode payload_json — same pattern as the attribution
    rows so the consumer never re-parses JSON on the wire."""
    out = dict(row)
    raw = out.pop("payload_json", None)
    if raw:
        try:
            out["payload"] = json.loads(raw)
        except (TypeError, ValueError):
            out["payload"] = None
    else:
        out["payload"] = None
    return out


@router.get("/policy-tuning")
async def get_policy_tuning(
    window: int = Query(POLICY_TUNING_WINDOW_DAYS, ge=1, le=730),
) -> Dict[str, Any]:
    """Return the latest advisory recommendations for all tunable
    PolicyRules. Operator-facing snapshot — every row carries the
    full ``payload`` (buckets + rationale + recommendation_confidence)
    so the cockpit can render the comparison panel without a second
    round-trip.

    NOTE: this reads the persisted ``policy_tunings`` table; if the
    nightly job hasn't run yet (advisory flag off, or fresh deploy),
    the response is an empty rows list with ``advisory_enabled``
    flagged so the UI can show "advisory pass not yet run".
    """
    rows = latest_policy_tuning_rows(limit=200)
    decoded: List[Dict[str, Any]] = [_decode_policy_tuning_row(r) for r in rows]
    return {
        "window_days": window,
        "advisory_enabled": bool(
            TUNABLES.policy_tuning_advisory_enabled
        ),
        "auto_apply_enabled": bool(
            TUNABLES.policy_tuning_auto_apply_enabled
        ),
        "tunable_rules": [
            {
                "rule_name": r.rule_name,
                "threshold_attr": r.threshold_attr,
                "current_value": r.current_value,
                "plausible_range": list(r.plausible_range),
                "direction": r.direction,
                "units": r.units,
                "description": r.description,
            }
            for r in TUNABLE_RULES
        ],
        "min_n_per_bucket": DEFAULT_MIN_N_PER_BUCKET,
        "count": len(decoded),
        "rows": decoded,
        "computed_at": (decoded[0]["computed_at"] if decoded else None),
    }


@router.get("/policy-tuning/{rule_name}")
async def get_policy_tuning_rule(
    rule_name: str,
    window: int = Query(POLICY_TUNING_WINDOW_DAYS, ge=1, le=730),
) -> Dict[str, Any]:
    """Single-rule deep-dive. Returns the latest recommendation for
    ``rule_name`` plus the tunable-rule metadata.

    Returns 404 when no recommendation exists yet for the rule (e.g.
    fresh deploy or rule_name not in TUNABLE_RULES).
    """
    rule_meta = next(
        (r for r in TUNABLE_RULES if r.rule_name == rule_name), None,
    )
    if rule_meta is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"rule_name={rule_name!r} is not a registered tunable "
                "rule (see TUNABLE_RULES)"
            ),
        )
    rows = latest_policy_tuning_rows(rule_name=rule_name, limit=1)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no advisory recommendation written yet for "
                f"rule_name={rule_name!r} — either the nightly "
                "advisory pass hasn't run, or TUNABLES."
                "policy_tuning_advisory_enabled is False"
            ),
        )
    decoded = _decode_policy_tuning_row(rows[0])
    return {
        "window_days": window,
        "advisory_enabled": bool(
            TUNABLES.policy_tuning_advisory_enabled
        ),
        "rule": {
            "rule_name": rule_meta.rule_name,
            "threshold_attr": rule_meta.threshold_attr,
            "current_value": rule_meta.current_value,
            "plausible_range": list(rule_meta.plausible_range),
            "direction": rule_meta.direction,
            "units": rule_meta.units,
            "description": rule_meta.description,
        },
        "recommendation": decoded,
    }


@router.post("/policy-tuning/recompute")
async def post_policy_tuning_recompute(
    window: int = Query(POLICY_TUNING_WINDOW_DAYS, ge=1, le=730),
    persist: bool = Query(
        True,
        description=(
            "If True, write recommendation rows to policy_tunings. "
            "ONLY honored when TUNABLES.policy_tuning_advisory_enabled "
            "is True — when the advisory flag is OFF, persist is "
            "forced False so the operator's opt-in gate is preserved."
        ),
    ),
) -> Dict[str, Any]:
    """Force a fresh advisory pass on demand. Useful for the operator
    to see "what would the advisor recommend NOW?" without waiting
    for the 22:30 ET cron.

    Honors the advisory flag: if it's OFF, recompute STILL returns
    the computed report (so the operator can preview), but the
    persistence side-effect is skipped — no rows enter
    ``policy_tunings`` until the flag is flipped ON. This preserves
    the "operator opts in to advisory" contract.
    """
    advisory_on = bool(TUNABLES.policy_tuning_advisory_enabled)
    effective_persist = bool(persist) and advisory_on
    recommendations = compute_policy_tuning(window_days=window)
    report = [r.to_dict() for r in recommendations]
    n_decisions = (
        report[0]["n_decisions_total"] if report else 0
    )
    if not effective_persist:
        return {
            "ok": True,
            "persisted": False,
            "advisory_enabled": advisory_on,
            "window_days": window,
            "n_decisions_total": n_decisions,
            "report": report,
            "note": (
                "advisory_enabled=False — no_op (telemetry only). "
                "Set TB_POLICY_TUNING_ENABLED=1 to allow persistence."
            ) if not advisory_on else (
                "persist=False — caller-requested preview only"
            ),
        }
    persistence = persist_policy_tuning(recommendations)
    return {
        "ok": bool(persistence.get("ok")),
        "persisted": True,
        "advisory_enabled": advisory_on,
        "window_days": window,
        "n_decisions_total": n_decisions,
        "written": persistence.get("written", 0),
        "computed_at": persistence.get("computed_at"),
        "report": report,
    }


# ── MITS Phase 18.D — Online Agent Weight Adaptation (Advisory) ──────


@router.get("/weights")
async def get_weight_adaptation(
    window: int = Query(WEIGHT_DEFAULT_WINDOW_DAYS, ge=1, le=730),
) -> Dict[str, Any]:
    """Return the latest WeightAdaptationReport.

    Reads the most recently persisted batch from ``agent_weight_history``
    (one row per agent at the same ``computed_at``). If the advisory
    pass hasn't run yet, the response is an empty rows list with
    ``advisory_enabled`` flagged so the UI can show "advisory pass not
    yet run".

    The endpoint NEVER writes — pure read.
    """
    rows = latest_weight_rows(limit=64)
    advisory_on = bool(getattr(
        TUNABLES, "adaptive_weights_advisory_enabled", False,
    ))
    apply_on = bool(getattr(
        TUNABLES, "adaptive_weights_apply_enabled", False,
    ))
    return {
        "window_days": window,
        "min_n": WEIGHT_DEFAULT_MIN_N,
        "advisory_enabled": advisory_on,
        "apply_enabled": apply_on,
        "known_agents": list(AGENT_BASE_WEIGHTS.keys()),
        "base_weights": dict(AGENT_BASE_WEIGHTS),
        "count": len(rows),
        "rows": rows,
        "computed_at": (rows[0].get("computed_at") if rows else None),
    }


@router.get("/weights/history")
async def get_weight_history(
    agent: str = Query(..., description="One of the 8 council agent names"),
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    """Rolling history per agent — most recent first.

    Returns 404 when ``agent`` is not a registered council agent.
    Returns an empty list when no history exists yet (fresh deploy or
    advisory not enabled).
    """
    if agent not in AGENT_BASE_WEIGHTS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"agent={agent!r} is not a registered council agent "
                f"(known: {list(AGENT_BASE_WEIGHTS.keys())})"
            ),
        )
    rows = history_for_agent(agent, limit=limit)
    return {
        "agent": agent,
        "base_weight": float(AGENT_BASE_WEIGHTS[agent]),
        "limit": limit,
        "count": len(rows),
        "rows": rows,
    }


@router.post("/weights/recompute")
async def post_weight_recompute(
    window: int = Query(WEIGHT_DEFAULT_WINDOW_DAYS, ge=1, le=730),
    persist: bool = Query(
        True,
        description=(
            "If True, write proposal rows to agent_weight_history. "
            "ONLY honored when TUNABLES.adaptive_weights_advisory_enabled "
            "is True — when the advisory flag is OFF, persist is forced "
            "False so the operator's opt-in gate is preserved."
        ),
    ),
) -> Dict[str, Any]:
    """Force a fresh advisory pass on demand. Useful for the operator
    to see "what would the advisor recommend NOW?" without waiting for
    the 22:45 ET cron.

    Honors the advisory flag: if it's OFF, recompute STILL returns the
    computed report (so the operator can preview), but the persistence
    side-effect is skipped — no rows enter ``agent_weight_history``
    until the flag is flipped ON.
    """
    advisory_on = bool(getattr(
        TUNABLES, "adaptive_weights_advisory_enabled", False,
    ))
    apply_on = bool(getattr(
        TUNABLES, "adaptive_weights_apply_enabled", False,
    ))
    effective_persist = bool(persist) and advisory_on
    report = compute_weight_proposals(window_days=window)
    report_dict = report.to_dict()
    if not effective_persist:
        return {
            "ok": True,
            "persisted": False,
            "advisory_enabled": advisory_on,
            "apply_enabled": apply_on,
            "window_days": window,
            "report": report_dict,
            "note": (
                "advisory_enabled=False — no_op (telemetry only). "
                "Set TB_ADAPTIVE_WEIGHTS_ENABLED=1 to allow persistence."
            ) if not advisory_on else (
                "persist=False — caller-requested preview only"
            ),
        }
    written = persist_weight_proposals(report)
    return {
        "ok": True,
        "persisted": True,
        "advisory_enabled": advisory_on,
        "apply_enabled": apply_on,
        "window_days": window,
        "written": written,
        "computed_at": report.computed_at,
        "report": report_dict,
    }


# ── MITS Phase 18.E — Hypothesis Studio + Guardrails ─────────────────


def _resolve_table_model(table: str):
    """Map the incoming ``table`` string to its ORM model.

    Raises 400 when ``table`` is empty or not one of the 3 learning
    tables. Centralized so approve / rollback / audit-log all reject
    bad input identically.
    """
    name = (table or "").strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail="table is required",
        )
    if name not in ALLOWED_TABLES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"table={name!r} is not a learning table. "
                f"Allowed: {list(ALLOWED_TABLES)}"
            ),
        )
    return name, _TABLE_TO_MODEL[name]


# 18-FU Gap R4 — test-sentinel scope_name prefixes that should NEVER
# reach the production approve/rollback path. Stream C's 18.E verification
# accidentally landed one such row (``_18fu_uxC_gateE``) in
# learned_attribution; this guard makes that class of mistake impossible
# from the API surface. Tests can opt out via ``allow_test_sentinel=True``
# on direct ``_apply_review`` calls (the HTTP routes never set it).
TEST_SENTINEL_PREFIXES: tuple = ("_test_", "_18fu_")


def _is_test_sentinel_row(row: Any) -> Optional[str]:
    """Return the row's ``scope_name`` when it starts with a test
    sentinel prefix, else None. Only LearnedAttribution carries
    ``scope_name`` today (policy_tunings / agent_weight_history have
    different identifier columns), so the check is no-op for the other
    two tables."""
    scope_name = getattr(row, "scope_name", None)
    if scope_name is None:
        return None
    sn = str(scope_name)
    for prefix in TEST_SENTINEL_PREFIXES:
        if sn.startswith(prefix):
            return sn
    return None


def _apply_review(
    table: str,
    row_id: int,
    *,
    action: str,
    notes: Optional[str],
    allow_test_sentinel: bool = False,
) -> Dict[str, Any]:
    """Shared core for approve / rollback: load the target row, flip
    operator_reviewed=1 + operator_approved={1 if approve else 0},
    snapshot the post-flip state, and append an audit row.

    Idempotent: calling approve twice on the same row leaves the
    target row in the same state and writes a second audit entry
    capturing the repeat action — the audit ledger preserves history
    while the learning row converges. Rollback after approve flips
    operator_approved back to 0 and writes its own audit entry.

    18-FU Gap R4 — rejects any row whose ``scope_name`` starts with a
    test sentinel prefix (``_test_`` or ``_18fu_``) with HTTP 400. Tests
    that legitimately want to exercise the approve path on a sentinel
    row pass ``allow_test_sentinel=True``. The HTTP routes
    (``POST /learning/approve`` and ``POST /learning/rollback``) NEVER
    set this flag — so a test pollution row landing in the production
    DB can never be approved through the API.

    Raises:
      * 400 — unknown table, bad action, or test-sentinel scope_name
      * 404 — row_id not found in the target table
    """
    if action not in ALLOWED_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"action={action!r} not allowed. "
                f"Allowed: {list(ALLOWED_ACTIONS)}"
            ),
        )
    table_name, model = _resolve_table_model(table)
    approved_flag = 1 if action == ACTION_APPROVE else 0
    notes_clean = None
    if notes is not None:
        notes_clean = str(notes).strip() or None

    with session_scope() as s:
        row = s.get(model, int(row_id))
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"row_id={int(row_id)} not found in {table_name!r}"
                ),
            )
        sentinel = _is_test_sentinel_row(row)
        if sentinel is not None and not allow_test_sentinel:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"scope_name={sentinel!r} is a test sentinel "
                    f"(prefix in {list(TEST_SENTINEL_PREFIXES)}) and "
                    f"cannot be approved/rolled back via the API. "
                    f"Test rows must be cleaned up at the DB level."
                ),
            )
        # Flip the operator-review flags. We never mutate the advisor's
        # numeric fields (current_value, recommended_value, hit_rate,
        # etc.) — only the review state. Approve/rollback is metadata.
        row.operator_reviewed = 1
        row.operator_approved = approved_flag
        # Snapshot the post-flip state so the audit ledger always
        # answers "what did this row look like at the moment of the
        # action?" without joining back to the learning table.
        snapshot = row.to_dict()
        snapshot_json = json.dumps(snapshot, default=str)
        # Write the audit row.
        audit = LearningRollbackLog(
            created_at=datetime.utcnow(),
            table_name=table_name,
            row_id=int(row_id),
            action=action,
            notes=notes_clean,
            operator="operator",
            snapshot_json=snapshot_json,
        )
        s.add(audit)
        s.flush()
        audit_id = int(audit.id)
        audit_created_at = (
            audit.created_at.isoformat() if audit.created_at else None
        )
    return {
        "ok": True,
        "table": table_name,
        "row_id": int(row_id),
        "action": action,
        "audit_id": audit_id,
        "audit_created_at": audit_created_at,
        "row": snapshot,
    }


@router.post("/approve")
async def post_learning_approve(
    body: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    """Operator approves a learning advisory row.

    Body shape: ``{table, row_id, notes?}``. Sets
    ``operator_reviewed=1, operator_approved=1`` on the target row and
    appends one ``learning_rollback_log`` entry recording the action +
    snapshotting the row's post-flip state.

    APPROVAL DOES NOT AUTO-APPLY — it only marks the row as
    operator-approved for the future apply pipeline. The engine still
    respects the 5 safety flags and will not begin applying
    recommendations until both the advisory flag AND the apply flag
    for that category are flipped on via env var.
    """
    table = str(body.get("table") or "").strip()
    row_id_raw = body.get("row_id")
    try:
        row_id = int(row_id_raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="row_id is required and must be an int",
        )
    notes = body.get("notes")
    return _apply_review(
        table, row_id, action=ACTION_APPROVE, notes=notes,
    )


@router.post("/rollback")
async def post_learning_rollback(
    body: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    """Operator rolls back (rejects) a learning advisory row.

    Body shape: ``{table, row_id, notes?}``. Sets
    ``operator_reviewed=1, operator_approved=0`` on the target row and
    appends one ``learning_rollback_log`` entry recording the action.

    Rollback after an approve will flip operator_approved back to 0;
    the audit ledger retains BOTH the prior approve entry AND this
    rollback entry, so the full operator decision history is
    preserved.
    """
    table = str(body.get("table") or "").strip()
    row_id_raw = body.get("row_id")
    try:
        row_id = int(row_id_raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="row_id is required and must be an int",
        )
    notes = body.get("notes")
    return _apply_review(
        table, row_id, action=ACTION_ROLLBACK, notes=notes,
    )


@router.get("/audit-log")
async def get_learning_audit_log(
    limit: int = Query(100, ge=1, le=1000),
    table: Optional[str] = Query(
        None,
        description=(
            "Filter to one of: learned_attribution, policy_tunings, "
            "agent_weight_history. Omit to return all."
        ),
    ),
) -> Dict[str, Any]:
    """Rolling history of approve/rollback actions, newest first.

    Optionally filtered by ``table``. Unknown ``table`` values return
    400 so the studio can't quietly request a non-existent slice.
    """
    if table is not None:
        # Validate but allow the resolve to raise 400 with the same
        # consistent message used by approve / rollback.
        _resolve_table_model(table)
    with session_scope() as s:
        q = select(LearningRollbackLog)
        if table:
            q = q.where(LearningRollbackLog.table_name == table)
        q = q.order_by(desc(LearningRollbackLog.created_at)).limit(limit)
        rows = s.execute(q).scalars().all()
        out = [r.to_dict() for r in rows]
    return {
        "limit": limit,
        "table": table,
        "count": len(out),
        "rows": out,
    }


# ── MITS Phase 18-FU Stream A — Decision Funnel diagnostic ───────────


@router.get("/funnel")
async def get_learning_funnel(
    window: int = Query(14, ge=1, le=365),
    recompute: bool = Query(
        False,
        description=(
            "If True, recompute the FunnelReport on-the-fly over the "
            "requested window (does NOT persist). If False, return the "
            "most recently persisted decision_funnel_daily row's payload."
        ),
    ),
) -> Dict[str, Any]:
    """Return the operator-facing Decision Funnel diagnostic.

    Two read modes:

      * ``recompute=False`` (default) — return the most recent
        persisted ``decision_funnel_daily`` row. O(1) read; preferred
        for the cockpit panel where the operator wants the same
        snapshot the nightly job produced.
      * ``recompute=True`` — recompute the FunnelReport on-the-fly
        over the trailing ``window`` days. NOT persisted; useful for
        an interactive "what does the funnel look like RIGHT NOW?"
        view. Heavier (scans every provenance row in the window).

    Both modes carry the same payload shape: stages + confidence
    histograms + cooldown audit + counterfactual histogram + top
    surgical change candidate + honesty notes.

    No persistence side-effect from this GET. Use the POST
    ``/recompute`` route to write a snapshot.
    """
    if recompute:
        report = compute_funnel_report(window_days=window)
        return {
            "source": "on_demand",
            "window_days": window,
            "persisted": False,
            "report": report.to_dict(),
        }
    row = latest_funnel_row()
    if row is None:
        # No persisted row yet — fall back to an on-demand compute so
        # the cockpit never shows a blank panel. This fallback path is
        # cached for 5 minutes per ``window`` so a polling cockpit
        # doesn't re-scan every provenance row in the window.
        now = time.monotonic()
        with _FUNNEL_FALLBACK_CACHE_LOCK:
            cached = _FUNNEL_FALLBACK_CACHE.get(int(window))
            if cached is not None and cached.get("expires_at", 0.0) > now:
                return cached["value"]
        report = compute_funnel_report(window_days=window)
        payload = {
            "source": "on_demand_fallback",
            "window_days": window,
            "persisted": False,
            "note": (
                "no decision_funnel_daily row yet; computed on-demand"
            ),
            "report": report.to_dict(),
        }
        with _FUNNEL_FALLBACK_CACHE_LOCK:
            _FUNNEL_FALLBACK_CACHE[int(window)] = {
                "value": payload,
                "expires_at": time.monotonic() + _FUNNEL_FALLBACK_CACHE_TTL_SEC,
            }
        return payload
    payload = row.get("payload_json")
    decoded_payload: Optional[Dict[str, Any]] = None
    if payload:
        try:
            decoded_payload = json.loads(payload)
        except (TypeError, ValueError):
            decoded_payload = None
    return {
        "source": "decision_funnel_daily",
        "window_days": window,
        "persisted": True,
        "row": {
            k: v for k, v in row.items()
            if k not in {"payload_json"}
        },
        "report": decoded_payload,
    }


@router.get("/funnel/history")
async def get_learning_funnel_history(
    days: int = Query(30, ge=1, le=365),
) -> Dict[str, Any]:
    """Rolling history of ``decision_funnel_daily`` rows, newest first.

    Each row is the to_dict projection from the model; ``payload_json``
    stays string-encoded so the wire payload doesn't balloon. The
    cockpit decodes on-demand only when the operator drills into a
    specific date.
    """
    rows = funnel_history(days=int(days))
    return {
        "days": int(days),
        "count": len(rows),
        "rows": rows,
    }


@router.post("/funnel/recompute")
async def post_learning_funnel_recompute(
    window: int = Query(14, ge=1, le=365),
    target_date: Optional[str] = Query(
        None,
        description=(
            "ISO date (yyyy-mm-dd) to persist the row under. Defaults "
            "to the date portion of window_end (typically today)."
        ),
    ),
) -> Dict[str, Any]:
    """Force a fresh funnel compute + persist one row in
    ``decision_funnel_daily``.

    Always persists when called — there is no advisory flag gate on
    this surface. The funnel diagnostic is investigation, not auto-
    apply; the operator needs the row regardless of any TUNABLES
    state.
    """
    report = compute_funnel_report(window_days=int(window))
    target = None
    if target_date:
        try:
            from datetime import date as _date
            target = _date.fromisoformat(target_date)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"target_date={target_date!r} must be yyyy-mm-dd "
                    "(ISO date format)"
                ),
            )
    meta = persist_funnel_report(report, target_date=target)
    return {
        "ok": True,
        "window_days": int(window),
        "persisted": True,
        "meta": meta,
        # Surface the surgical-change advisory directly in the response
        # so the operator can read it without a second GET.
        "top_surgical_change_candidate": (
            report.top_surgical_change_candidate
        ),
    }


@router.get("/flags")
async def get_learning_flags() -> Dict[str, Any]:
    """Return the 5 learning-related safety flags as a single object.

    The studio renders these so the operator can see at a glance
    whether each adaptive layer is in "advisory" mode (computed +
    visible but not applied) or "apply" mode (engine actually consumes
    the recommendation). All 5 default OFF; the operator flips them
    via env vars on the box.
    """
    return {
        "decision_rollback_enabled": bool(getattr(
            TUNABLES, "decision_rollback_enabled", False,
        )),
        "policy_tuning_advisory_enabled": bool(getattr(
            TUNABLES, "policy_tuning_advisory_enabled", False,
        )),
        "policy_tuning_auto_apply_enabled": bool(getattr(
            TUNABLES, "policy_tuning_auto_apply_enabled", False,
        )),
        "adaptive_weights_advisory_enabled": bool(getattr(
            TUNABLES, "adaptive_weights_advisory_enabled", False,
        )),
        "adaptive_weights_apply_enabled": bool(getattr(
            TUNABLES, "adaptive_weights_apply_enabled", False,
        )),
    }
