"""MITS Phase 17.E — declarative exit policy surface.

Mirrors ``backend/api/routes/policy.py`` for the exit side:

* ``GET /exit/rules``        — every registered ExitRule plus its
                                severity + enabled state. Operator uses
                                this to confirm a deploy shipped the
                                expected rule set (Gate F).
* ``GET /exit/veto-budget``  — per-rule fire-rate over the requested
                                window (default 7d). "Which exit
                                triggers fire most often?"
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from backend.bot.decision.exit_rules import build_default_policy
from backend.db import session_scope
from backend.models.exit_rule_evaluation import ExitRuleEvaluation


router = APIRouter(prefix="/exit", tags=["exit_policy"])


# Build the rule registry once at module import so /exit/rules is cheap
# and does not depend on engine startup. The engine reaches for its own
# ExitPolicy via ``decide_exit_with_policy``; this is the read-only
# mirror exposed to the operator surface.
_REGISTRY = build_default_policy()


_WINDOW_RE = re.compile(r"^(\d+)([hd])$")


def _parse_window(spec: str) -> timedelta:
    m = _WINDOW_RE.match(spec.strip().lower())
    if not m:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid window '{spec}' — use formats like '24h' or '7d'"
            ),
        )
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "h":
        return timedelta(hours=n)
    return timedelta(days=n)


@router.get("/rules")
async def list_exit_rules() -> List[Dict[str, Any]]:
    """Return every registered exit rule with its severity + enabled
    state. Operator uses this on Gate F to confirm the deploy actually
    shipped the cataloged 3-rule set."""
    return [
        {
            "name": r.name,
            "severity": r.severity,
            "enabled": r.enabled,
        }
        for r in _REGISTRY.all_rules()
    ]


@router.get("/veto-budget")
async def get_exit_veto_budget(
    window: str = Query(
        "7d", description="Time window e.g. '24h' or '7d'.",
    ),
) -> Dict[str, Any]:
    """Per-rule fire-rate telemetry.

    For each rule in the registered registry, returns:

    * ``evaluated``: number of times the rule was evaluated in the
      window.
    * ``fired``: number of times the rule produced an ExitTrigger.
    * ``fire_rate``: fired / evaluated, rounded to 4 decimals.
    * ``severity``: copied from the registry so the UI can group /
      colour by axis without a second request.
    """
    delta = _parse_window(window)
    cutoff = datetime.utcnow() - delta

    rule_meta = {
        r.name: {"severity": r.severity, "enabled": r.enabled}
        for r in _REGISTRY.all_rules()
    }

    with session_scope() as session:
        # Total evaluated rows in window — denominator for
        # "total_evaluations". Mirrors the policy/veto-budget counter.
        total_rows = int(session.execute(
            select(func.count()).select_from(ExitRuleEvaluation)
            .where(ExitRuleEvaluation.evaluated_at >= cutoff)
        ).scalar_one() or 0)

        rows = session.execute(
            select(
                ExitRuleEvaluation.rule_name,
                ExitRuleEvaluation.fired,
                func.count().label("n"),
            )
            .where(ExitRuleEvaluation.evaluated_at >= cutoff)
            .group_by(
                ExitRuleEvaluation.rule_name,
                ExitRuleEvaluation.fired,
            )
        ).all()

    agg: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"evaluated": 0, "fired": 0}
    )
    for rule_name, fired, n in rows:
        agg[rule_name]["evaluated"] += int(n)
        if fired:
            agg[rule_name]["fired"] += int(n)

    # Include every registered rule even if it has zero evaluations in
    # the window — operator wants to see "rule registered but never
    # evaluated" as a green zero, not an empty row.
    for name in rule_meta:
        agg.setdefault(name, {"evaluated": 0, "fired": 0})

    payload: List[Dict[str, Any]] = []
    for name, counts in sorted(agg.items()):
        meta = rule_meta.get(name) or {
            "severity": "unknown", "enabled": True,
        }
        evaluated = counts["evaluated"]
        fired = counts["fired"]
        fire_rate = round(fired / evaluated, 4) if evaluated else 0.0
        payload.append({
            "rule": name,
            "severity": meta["severity"],
            "enabled": meta["enabled"],
            "evaluated": evaluated,
            "fired": fired,
            "fire_rate": fire_rate,
        })

    return {
        "window": window,
        "cutoff": cutoff.isoformat(),
        "total_evaluations": total_rows,
        "rules": payload,
    }
