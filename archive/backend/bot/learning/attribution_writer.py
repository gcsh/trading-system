"""MITS Phase 18.A — LearnedAttribution persistence + read helpers.

Splits the side-effecting DB writes out of ``attribution.py`` so the
pure aggregation math stays test-friendly. Two surfaces:

  * ``persist_attribution_report`` — writes one row per scope (agent,
    axis, strategy) for a single computed report. Used by both the
    nightly scheduler job AND the on-demand
    ``POST /learning/attribution/recompute`` route.

  * ``latest_attribution_rows`` — reads back the latest computed batch
    for a given scope_kind. Used by the GET endpoints.

The persistence + read helpers are intentionally small so the route
layer (which is async + lives in api/routes) can stay thin.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from backend.bot.learning.attribution import compute_attribution_report
from backend.db import session_scope
from backend.models.learned_attribution import LearnedAttribution


logger = logging.getLogger(__name__)


SCOPE_KIND_AGENT = "agent"
SCOPE_KIND_AXIS = "axis"
SCOPE_KIND_STRATEGY = "strategy"
ALL_SCOPE_KINDS = (SCOPE_KIND_AGENT, SCOPE_KIND_AXIS, SCOPE_KIND_STRATEGY)


def _build_rows(
    *, scope_kind: str, scope_payload: Dict[str, Any],
    window_days: int, computed_at: datetime,
) -> LearnedAttribution:
    """Map one entry from the attribution report into a LearnedAttribution
    row. Pulls the numeric columns onto the row body and stashes the
    full dict as ``payload_json`` so the read endpoints can return the
    rich shape without a second compute."""
    name_key = {
        SCOPE_KIND_AGENT: "agent",
        SCOPE_KIND_AXIS: "axis",
        SCOPE_KIND_STRATEGY: "strategy_name",
    }[scope_kind]
    notes = scope_payload.get("notes") or []
    notes_str = ",".join(notes) if isinstance(notes, list) else str(notes)
    return LearnedAttribution(
        computed_at=computed_at,
        scope_kind=scope_kind,
        scope_name=str(scope_payload.get(name_key) or "_unknown"),
        window_days=int(window_days),
        n_closed=int(scope_payload.get("n_closed") or 0),
        hit_rate=scope_payload.get("hit_rate"),
        hit_rate_wilson_lower=scope_payload.get("hit_rate_wilson_lower"),
        hit_rate_wilson_upper=scope_payload.get("hit_rate_wilson_upper"),
        mean_pnl_pct=scope_payload.get("mean_pnl_pct"),
        brier_score=scope_payload.get("brier_score"),
        ece=scope_payload.get("ece"),
        spearman_corr=scope_payload.get("spearman_corr"),
        discrimination=scope_payload.get("discrimination"),
        payload_json=json.dumps(scope_payload, default=str),
        notes=notes_str or None,
    )


def persist_attribution_report(
    *, window_days: int = 90,
    report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute (or accept) one attribution report and write a row per
    scope. Returns the meta block + counts.

    The function is the ONLY write path for the table outside of tests.
    Re-runnable: each call appends a fresh batch keyed by computed_at
    (never updates existing rows in place) — operator can browse
    historical learning trajectories.
    """
    if report is None:
        report = compute_attribution_report(window_days=window_days)
    computed_at = datetime.utcnow()
    counts: Dict[str, int] = {k: 0 for k in ALL_SCOPE_KINDS}
    rows_added: List[LearnedAttribution] = []
    try:
        with session_scope() as s:
            for scope_kind, key in (
                (SCOPE_KIND_AGENT, "agents"),
                (SCOPE_KIND_AXIS, "axes"),
                (SCOPE_KIND_STRATEGY, "strategies"),
            ):
                for entry in report.get(key) or []:
                    row = _build_rows(
                        scope_kind=scope_kind,
                        scope_payload=entry,
                        window_days=window_days,
                        computed_at=computed_at,
                    )
                    s.add(row)
                    rows_added.append(row)
                    counts[scope_kind] += 1
            s.flush()
    except Exception:
        logger.exception("persist_attribution_report failed")
        return {
            "ok": False,
            "computed_at": computed_at.isoformat(),
            "window_days": window_days,
            "counts": counts,
        }
    return {
        "ok": True,
        "computed_at": computed_at.isoformat(),
        "window_days": window_days,
        "n_closed_decisions": int(report.get("n_closed_decisions") or 0),
        "counts": counts,
    }


def latest_attribution_rows(
    *, scope_kind: Optional[str] = None,
    window_days: Optional[int] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return the most recently computed batch's rows, optionally
    filtered by scope_kind + window_days.

    "Most recent batch" = rows sharing the maximum ``computed_at``
    that matches the filters. Keeps the GET endpoints simple: the UI
    always sees one cohesive snapshot, never a Frankenstein blend of
    multiple batches.
    """
    with session_scope() as s:
        q = select(LearnedAttribution)
        if scope_kind:
            q = q.where(LearnedAttribution.scope_kind == scope_kind)
        if window_days is not None:
            q = q.where(LearnedAttribution.window_days == int(window_days))
        # Find the freshest computed_at that matches.
        head = s.execute(
            q.order_by(desc(LearnedAttribution.computed_at)).limit(1)
        ).scalars().first()
        if head is None:
            return []
        rows = s.execute(
            q.where(LearnedAttribution.computed_at == head.computed_at)
            .order_by(LearnedAttribution.scope_kind, LearnedAttribution.scope_name)
            .limit(limit)
        ).scalars().all()
        return [r.to_dict() for r in rows]
