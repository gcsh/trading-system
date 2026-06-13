"""MITS Phase 18-FU Stream D — Learning observability endpoints.

Mounted at ``/learning/observability/*`` so Stream A's ``routes/learning.py``
stays the authoritative learning-feedback surface and we get a clean
namespace for forensic / observability reads (per-cycle weight log,
impact reports, subsystem health). Each route is paginated + safe to
poll from a cockpit panel.

Four endpoints:
  * ``GET /learning/observability/weight-applications`` — recent per-cycle
    weight application log rows (Gap 6 forensic trail).
  * ``GET /learning/observability/impact`` — recent learning-impact
    reports (Gap 10 before/after comparison).
  * ``POST /learning/observability/impact/recompute`` — on-demand recompute
    of impact reports for any learning events in the last N days. Used
    when the operator wants a refresh between nightly runs.
  * ``GET /learning/observability/health`` — subsystem snapshot:
    table row counts + Gap-6/9/10/12 status pings.

All routes return shallow JSON dicts; never raise on empty data — the
cockpit surfaces "no events yet" gracefully when the apply flags are
OFF (the default). Same observability discipline as Stream A's funnel
endpoints.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from sqlalchemy import desc, func, select

from backend.bot.learning.counterfactual import (
    COUNTERFACTUAL_CODE_VERSION,
    cache_version_status,
    get_code_version as get_cf_code_version,
)
from backend.bot.learning.impact_measurement import (
    DEFAULT_AFTER_WINDOW_DAYS,
    DEFAULT_BEFORE_WINDOW_DAYS,
    MIN_N_FOR_SIGNIFICANCE,
    compute_all_recent_impacts,
    latest_impact_rows,
    persist_impact_reports,
)
from backend.bot.learning.policy_tuning import (
    STABILITY_N_CONSECUTIVE_REQUIRED,
    STABILITY_TOLERANCE_PCT,
)
from backend.bot.learning.weight_adaptation import (
    WEIGHT_APPLICATION_LOG_TTL_DAYS,
    latest_weight_application_rows,
    prune_weight_application_log,
)
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.counterfactual_replay import CounterfactualReplay
from backend.models.learning_impact import ALLOWED_EVENT_TYPES, LearningImpact
from backend.models.policy_tuning import PolicyTuning
from backend.models.weight_application_log import WeightApplicationLog


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/learning/observability",
    tags=["learning-observability"],
)


# ── Gap 6 — weight application log endpoint ───────────────────────────


@router.get("/weight-applications")
async def get_weight_applications(
    limit: int = Query(100, ge=1, le=500),
    agent: Optional[str] = Query(
        None,
        description=(
            "Optional agent name filter; matches rows whose persisted "
            "weight_set_json mentions the agent."
        ),
    ),
) -> Dict[str, Any]:
    """Return the rolling per-cycle weight application log.

    With ``TUNABLES.adaptive_weights_apply_enabled`` OFF (default) no
    rows are ever written, so this returns an empty ``rows`` list and
    the cockpit can render "no apply events yet". When apply is ON,
    each row carries ``(cycle_id, agent_weight_history_id,
    weight_set_json, composite_quality_at_apply)`` — the forensic
    trail the operator needs to debug consensus drift.

    Decodes ``weight_set_json`` server-side into a ``weight_set`` dict
    so the cockpit doesn't need a second JSON parse per row.
    """
    rows = latest_weight_application_rows(limit=int(limit), agent=agent)
    decoded: List[Dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        blob = out.get("weight_set_json")
        out["weight_set"] = None
        if blob:
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    out["weight_set"] = {
                        k: float(v) for k, v in parsed.items()
                    }
            except (TypeError, ValueError):
                out["weight_set"] = None
        decoded.append(out)
    return {
        "apply_enabled": bool(getattr(
            TUNABLES, "adaptive_weights_apply_enabled", False,
        )),
        "advisory_enabled": bool(getattr(
            TUNABLES, "adaptive_weights_advisory_enabled", False,
        )),
        "ttl_days": int(WEIGHT_APPLICATION_LOG_TTL_DAYS),
        "n_rows": len(decoded),
        "agent_filter": agent,
        "rows": decoded,
    }


@router.post("/weight-applications/prune")
async def post_weight_applications_prune(
    body: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    """Force a TTL prune of ``weight_application_log`` rows older than
    ``ttl_days`` (defaults to ``WEIGHT_APPLICATION_LOG_TTL_DAYS``).

    Designed for the nightly scheduler hook; also callable on-demand
    from the cockpit when the log grew faster than expected.
    """
    raw_ttl = body.get("ttl_days") if isinstance(body, dict) else None
    try:
        ttl = (
            int(raw_ttl)
            if raw_ttl is not None
            else WEIGHT_APPLICATION_LOG_TTL_DAYS
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="ttl_days must be an integer",
        )
    if ttl <= 0:
        raise HTTPException(
            status_code=400, detail="ttl_days must be > 0",
        )
    deleted = prune_weight_application_log(ttl_days=ttl)
    return {
        "ok": True,
        "deleted": int(deleted),
        "ttl_days": int(ttl),
        "computed_at": datetime.utcnow().isoformat(),
    }


# ── Gap 10 — learning impact endpoints ────────────────────────────────


@router.get("/impact")
async def get_learning_impact(
    event_type: Optional[str] = Query(
        None,
        description=(
            "Filter by learning_event_type "
            f"({', '.join(ALLOWED_EVENT_TYPES)})."
        ),
    ),
    limit: int = Query(20, ge=1, le=200),
) -> Dict[str, Any]:
    """Return the most recent learning-impact reports.

    With all apply flags OFF (default) the underlying event ledgers are
    empty and this returns an empty ``rows`` list. The shape is stable
    so the cockpit can render an empty state without conditional
    branching.
    """
    if event_type is not None and event_type not in ALLOWED_EVENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "unknown event_type; allowed: "
                f"{', '.join(ALLOWED_EVENT_TYPES)}"
            ),
        )
    rows = latest_impact_rows(event_type=event_type, limit=int(limit))
    return {
        "n_rows": len(rows),
        "event_type_filter": event_type,
        "min_n_for_significance": int(MIN_N_FOR_SIGNIFICANCE),
        "rows": rows,
    }


@router.post("/impact/recompute")
async def post_learning_impact_recompute(
    body: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    """On-demand impact recompute for any learning events in the last
    ``days_back`` days (default 30). Persists one ``learning_impact``
    row per event computed.

    Body keys:
      * ``days_back`` — lookback window (1..90, default 30).
      * ``before_window_days`` — pre-event window (default 7).
      * ``after_window_days`` — post-event window (default 7).
    """
    body = body or {}
    try:
        days_back = int(body.get("days_back", 30))
        before_w = int(body.get(
            "before_window_days", DEFAULT_BEFORE_WINDOW_DAYS,
        ))
        after_w = int(body.get(
            "after_window_days", DEFAULT_AFTER_WINDOW_DAYS,
        ))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="days_back / window args must be integers",
        )
    if not 1 <= days_back <= 90:
        raise HTTPException(
            status_code=400, detail="days_back must be in [1, 90]",
        )
    if before_w <= 0 or after_w <= 0:
        raise HTTPException(
            status_code=400,
            detail="before_window_days / after_window_days must be > 0",
        )
    reports = compute_all_recent_impacts(
        days_back=days_back,
        before_window_days=before_w,
        after_window_days=after_w,
    )
    meta = persist_impact_reports(reports)
    return {
        "ok": bool(meta.get("ok")),
        "n_events_seen": len(reports),
        "written": int(meta.get("written") or 0),
        "days_back": int(days_back),
        "before_window_days": int(before_w),
        "after_window_days": int(after_w),
        "computed_at": meta.get("computed_at")
            or datetime.utcnow().isoformat(),
    }


# ── Subsystem health endpoint ─────────────────────────────────────────


def _table_row_count(session, model) -> int:
    """Cheap COUNT(*) helper — returns 0 on any failure rather than
    raising so /health stays a "tells you what it sees" surface."""
    try:
        return int(
            session.execute(select(func.count()).select_from(model))
            .scalar() or 0
        )
    except Exception:
        return 0


def _latest_timestamp(session, model, column) -> Optional[str]:
    try:
        ts = session.execute(
            select(column).order_by(desc(column)).limit(1)
        ).scalar()
        return ts.isoformat() if ts is not None else None
    except Exception:
        return None


@router.get("/health")
async def get_observability_health() -> Dict[str, Any]:
    """Subsystem snapshot — row counts, latest timestamps, flag state.

    Designed so the cockpit can render a single-glance card:
      * Gap 6  — apply_enabled flag + weight_application_log row count
                  + ttl_days.
      * Gap 9  — policy_tuning stability gate config + #high-grade
                  recommendations in the latest batch.
      * Gap 10 — learning_impact row count + latest computed_at.
      * Gap 12 — counterfactual code version + (cache_rows,
                  cache_rows_at_current_version) — the operator sees
                  immediately how many cache rows are stale.

    Pure read; never raises (every count defaults to 0 on failure).
    """
    out: Dict[str, Any] = {
        "computed_at": datetime.utcnow().isoformat(),
    }
    with session_scope() as s:
        # Gap 6 — weight application log + flag state.
        wal_count = _table_row_count(s, WeightApplicationLog)
        wal_latest = _latest_timestamp(
            s, WeightApplicationLog, WeightApplicationLog.applied_at,
        )
        out["gap6_weight_applications"] = {
            "apply_enabled": bool(getattr(
                TUNABLES, "adaptive_weights_apply_enabled", False,
            )),
            "advisory_enabled": bool(getattr(
                TUNABLES, "adaptive_weights_advisory_enabled", False,
            )),
            "row_count": wal_count,
            "latest_applied_at": wal_latest,
            "ttl_days": int(WEIGHT_APPLICATION_LOG_TTL_DAYS),
        }

        # Gap 9 — stability gate + count of HIGH recommendations in the
        # most recent policy_tuning batch.
        pt_count = _table_row_count(s, PolicyTuning)
        latest_batch_high = 0
        try:
            head = s.execute(
                select(PolicyTuning.computed_at)
                .order_by(desc(PolicyTuning.computed_at))
                .limit(1)
            ).scalar()
            if head is not None:
                latest_batch_high = int(s.execute(
                    select(func.count()).select_from(PolicyTuning)
                    .where(PolicyTuning.computed_at == head)
                    .where(
                        PolicyTuning.recommendation_confidence == "high",
                    )
                ).scalar() or 0)
        except Exception:
            latest_batch_high = 0
        out["gap9_policy_tuning_stability"] = {
            "row_count": pt_count,
            "n_consecutive_required": int(
                STABILITY_N_CONSECUTIVE_REQUIRED,
            ),
            "tolerance_pct": float(STABILITY_TOLERANCE_PCT),
            "high_recommendations_in_latest_batch": latest_batch_high,
        }

        # Gap 10 — learning_impact summary.
        li_count = _table_row_count(s, LearningImpact)
        li_latest = _latest_timestamp(
            s, LearningImpact, LearningImpact.computed_at,
        )
        out["gap10_learning_impact"] = {
            "row_count": li_count,
            "latest_computed_at": li_latest,
            "min_n_for_significance": int(MIN_N_FOR_SIGNIFICANCE),
        }

        # Gap 12 — counterfactual cache version snapshot.
        cf_count = _table_row_count(s, CounterfactualReplay)
        cf_at_version = 0
        cf_legacy = 0
        try:
            current = get_cf_code_version()
            rows = s.execute(
                select(CounterfactualReplay.result_json)
            ).scalars().all()
            for blob in rows:
                if not blob:
                    cf_legacy += 1
                    continue
                try:
                    parsed = json.loads(blob)
                except (TypeError, ValueError):
                    cf_legacy += 1
                    continue
                status = cache_version_status(parsed)
                if status.get("cache_version_mismatch"):
                    cf_legacy += 1
                else:
                    cf_at_version += 1
        except Exception:
            logger.debug(
                "Gap 12 cache audit failed; falling back to 0",
                exc_info=True,
            )
        out["gap12_counterfactual_cache"] = {
            "current_code_version": COUNTERFACTUAL_CODE_VERSION,
            "cache_rows": cf_count,
            "cache_rows_at_current_version": cf_at_version,
            "cache_rows_stale_or_legacy": cf_legacy,
        }

    return out
