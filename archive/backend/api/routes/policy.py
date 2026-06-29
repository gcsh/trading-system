"""MITS Phase 16.A — declarative policy engine surface.

Two endpoints:

* ``GET /policy/rules``        — every registered PolicyRule plus its
                                  category / severity / enabled state.
* ``GET /policy/veto-budget``  — per-rule block-rate over the requested
                                  window (default 7d). Operator-facing
                                  metric: which rules vetoed which
                                  fraction of decisions?
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from backend.bot.decision.policy import DecisionPolicy
from backend.bot.decision.rules import _register_all
from backend.db import session_scope
from backend.models.policy_rule_evaluation import PolicyRuleEvaluation


router = APIRouter(prefix="/policy", tags=["policy"])


# Build the rule registry once at module import so /policy/rules is
# cheap and never depends on engine startup. The engine constructs its
# own DecisionPolicy in BotEngine.__init__; the registry here is a
# read-only mirror that exposes the same rules.
_REGISTRY = DecisionPolicy()
_register_all(_REGISTRY)


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
async def list_rules() -> List[Dict[str, Any]]:
    """Return every registered rule with its category / severity /
    enabled state. Operator uses this to confirm a deploy actually
    shipped the expected rule set."""
    return [
        {
            "name": r.name,
            "category": r.category,
            "severity": r.severity,
            "enabled": r.enabled,
        }
        for r in _REGISTRY.all_rules()
    ]


@router.get("/veto-budget")
async def get_veto_budget(
    window: str = Query("7d", description="Time window e.g. '24h' or '7d'."),
) -> Dict[str, Any]:
    """Per-rule rejection-rate telemetry.

    For each rule in the registered registry, returns:
    - ``evaluated``: number of times the rule was evaluated in the
      window.
    - ``blocked``: number of times the rule produced a BlockingFactor.
    - ``block_rate``: blocked / evaluated, rounded to 4 decimals.
    - ``category`` / ``severity``: copied from the registry so the UI
      can group / colour by axis without a second request.
    """
    delta = _parse_window(window)
    cutoff = datetime.utcnow() - delta

    rule_meta = {
        r.name: {"category": r.category, "severity": r.severity}
        for r in _REGISTRY.all_rules()
    }

    with session_scope() as session:
        # Total evaluated rows in window — denominator for "total_decisions".
        total_rows = int(session.execute(
            select(func.count()).select_from(PolicyRuleEvaluation)
            .where(PolicyRuleEvaluation.evaluated_at >= cutoff)
        ).scalar_one() or 0)

        rows = session.execute(
            select(
                PolicyRuleEvaluation.rule_name,
                PolicyRuleEvaluation.blocked,
                func.count().label("n"),
            )
            .where(PolicyRuleEvaluation.evaluated_at >= cutoff)
            .group_by(
                PolicyRuleEvaluation.rule_name,
                PolicyRuleEvaluation.blocked,
            )
        ).all()

    agg: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"evaluated": 0, "blocked": 0}
    )
    for rule_name, blocked, n in rows:
        agg[rule_name]["evaluated"] += int(n)
        if blocked:
            agg[rule_name]["blocked"] += int(n)

    payload: List[Dict[str, Any]] = []
    for name, counts in sorted(agg.items()):
        meta = rule_meta.get(name) or {
            "category": "unknown", "severity": "unknown",
        }
        evaluated = counts["evaluated"]
        blocked = counts["blocked"]
        block_rate = round(blocked / evaluated, 4) if evaluated else 0.0
        payload.append({
            "rule": name,
            "category": meta["category"],
            "severity": meta["severity"],
            "evaluated": evaluated,
            "blocked": blocked,
            "block_rate": block_rate,
        })

    return {
        "window": window,
        "cutoff": cutoff.isoformat(),
        "total_decisions": total_rows,
        "rules": payload,
    }
