"""Stage-17 Trade Journal endpoints.

  • ``GET /journal/lessons``  — list every actionable lesson mined from closed trades
  • ``GET /journal/applicable`` — lessons matching a hypothetical live context
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from backend.bot.journal import applicable_lessons, build_lessons
from backend.bot.journal.curated import CURATED_RULES, applicable_curated_lessons

router = APIRouter(prefix="/journal", tags=["journal"])


@router.get("/curated")
async def curated_catalog() -> dict:
    """The hand-curated institutional guardrails (P2.2).

    Returns the static rule catalog so operators can see what's encoded
    without diving into source. Each entry includes pattern, citation,
    suggested action, severity and the conditions matched against."""
    return {
        "rules": [
            {
                "rule_id": r.rule_id,
                "pattern": r.pattern,
                "citation": r.citation,
                "suggested_action": r.suggested_action,
                "size_multiplier": r.size_multiplier,
                "severity": r.severity,
                "condition_keys": r.condition_keys,
            }
            for r in CURATED_RULES
        ],
        "count": len(CURATED_RULES),
    }


@router.get("/curated/applicable")
async def curated_applicable(strategy: str, regime_trend: str = "unknown",
                                volatility: str = "normal", gamma: str = "unknown",
                                earnings_days: Optional[float] = None,
                                iv_rank: Optional[float] = None,
                                vix: Optional[float] = None,
                                yield_curve_inverted: Optional[bool] = None,
                                day_of_week: Optional[str] = None) -> dict:
    """Which curated rules fire for a given context? Same kwargs as
    /journal/applicable but only returns curated matches."""
    matches = applicable_curated_lessons(
        strategy=strategy, regime_trend=regime_trend,
        volatility=volatility, gamma=gamma,
        earnings_days=earnings_days, iv_rank=iv_rank,
        vix=vix, yield_curve_inverted=yield_curve_inverted,
        day_of_week=day_of_week,
    )
    return {"matches": [l.to_dict() for l in matches]}


@router.get("/lessons")
async def lessons(limit: int = Query(5000, ge=50, le=20000),
                     min_samples: int = Query(8, ge=2, le=200),
                     delta_threshold: float = Query(0.10, ge=0.0, le=1.0),
                     top_k: int = Query(50, ge=1, le=500)) -> dict:
    report = build_lessons(limit=limit, min_samples=min_samples,
                              delta_threshold=delta_threshold)
    d = report.to_dict()
    d["lessons"] = d["lessons"][:top_k]
    return d


@router.get("/applicable")
async def applicable(strategy: str, regime_trend: str = "unknown",
                        volatility: str = "normal", gamma: str = "unknown",
                        earnings_days: Optional[float] = None,
                        iv_rank: Optional[float] = None,
                        cross_asset_equities: Optional[str] = None,
                        vix: Optional[float] = None,
                        day_of_week: Optional[str] = None) -> dict:
    matches = applicable_lessons(
        strategy=strategy, regime_trend=regime_trend,
        volatility=volatility, gamma=gamma,
        earnings_days=earnings_days, iv_rank=iv_rank,
        cross_asset_equities=cross_asset_equities,
        vix=vix, day_of_week=day_of_week,
    )
    return {"matches": [l.to_dict() for l in matches]}
