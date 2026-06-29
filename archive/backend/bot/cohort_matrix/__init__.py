"""Stage-9 cohort confusion matrix + lift vs baseline.

Cohort grid: (strategy × regime × grade) → (closed, win_rate, expectancy,
lift). The lift is win_rate divided by the baseline (global win rate)
so values > 1.0 mark cohorts the bot beats average on; < 1.0 mark
cohorts to demote.

Foundation for:
  • Auto promote/demote in adaptive.plan_day (the cohort grades feed
    rolling out-of-sample scoring)
  • Threshold sweeps that explore (min_grade × prob_floor) for the
    frontier maximizing Sharpe subject to drawdown cap
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.bot.labeling import TradeLabel, build_labels
from backend.bot.metrics import expectancy, profit_factor, win_rate
from backend.db import session_scope

logger = logging.getLogger(__name__)


@dataclass
class CohortCell:
    strategy: str
    regime: str
    grade: str
    closed: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: Optional[float] = None
    expectancy: Optional[float] = None
    profit_factor: Optional[Any] = None
    lift: Optional[float] = None      # win_rate / baseline_win_rate
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── builder ──────────────────────────────────────────────────────────────


def _load_labels(limit: int = 5000) -> List[TradeLabel]:
    from sqlalchemy import desc, select
    from backend.models.decision_log import DecisionLog
    from backend.models.trade import Trade
    with session_scope() as session:
        trade_rows = session.execute(
            select(Trade).order_by(desc(Trade.timestamp)).limit(limit)
        ).scalars().all()
        trades = [r.to_dict() for r in trade_rows]
        decision_rows = session.execute(
            select(DecisionLog).order_by(desc(DecisionLog.timestamp)).limit(limit)
        ).scalars().all()
        decisions = [r.to_dict() for r in decision_rows]
    return build_labels(trades, decisions)


def build_cohort_matrix(*, limit: int = 5000,
                          min_cohort_closed: int = 1
                          ) -> Dict[str, Any]:
    """Return ``{cells, baseline, totals}`` where ``cells`` is the list of
    every (strategy, regime, grade) cohort with ≥ ``min_cohort_closed`` closed
    trades, including a ``lift`` over the baseline global win rate."""
    labels = _load_labels(limit=limit)
    closed = [l for l in labels if l.win is not None]
    if not closed:
        return {"cells": [], "baseline": None, "totals": {
            "n_labels": len(labels), "n_closed": 0,
        }}

    baseline_pnls = [l.pnl for l in closed if l.pnl is not None]
    baseline_wr = win_rate(baseline_pnls)

    buckets: Dict[Tuple[str, str, str], List[TradeLabel]] = defaultdict(list)
    for l in closed:
        key = (l.strategy or "—", l.regime_trend or "—", l.grade or "—")
        buckets[key].append(l)

    cells: List[CohortCell] = []
    for (strategy, regime, grade), items in buckets.items():
        pnls = [l.pnl for l in items if l.pnl is not None]
        if len(pnls) < min_cohort_closed:
            continue
        wr = win_rate(pnls)
        lift = round(wr / baseline_wr, 3) if (wr is not None and baseline_wr) else None
        pf = profit_factor(pnls)
        cells.append(CohortCell(
            strategy=strategy, regime=regime, grade=grade,
            closed=len(pnls),
            wins=sum(1 for p in pnls if p > 0),
            losses=sum(1 for p in pnls if p < 0),
            win_rate=wr, expectancy=expectancy(pnls),
            profit_factor=("inf" if pf == float("inf") else pf),
            lift=lift,
        ))
    cells.sort(key=lambda c: (c.closed, c.win_rate or 0.0), reverse=True)
    cell_dicts = [c.to_dict() for c in cells]
    # Blend research priors (P2.4) so each cell carries a posterior
    # win-rate the agents can read even before the live sample is large.
    try:
        from backend.bot.cohort_matrix.priors import apply_priors_to_cells
        cell_dicts = apply_priors_to_cells(cell_dicts, baseline_wr=baseline_wr)
    except Exception:
        logger.debug("cohort priors blend failed", exc_info=True)

    return {
        "cells": cell_dicts,
        "baseline": {"win_rate": baseline_wr, "n_closed": len(closed)},
        "totals": {"n_labels": len(labels), "n_closed": len(closed),
                     "n_cohorts": len(cells)},
    }


def cohort_win_rate(strategy: str, regime: str,
                      *, limit: int = 5000, recent_n: int = 30
                      ) -> Tuple[Optional[float], int]:
    """Rolling-window cohort win rate for the abstain engine. Returns
    ``(win_rate, n_closed)`` using the most recent ``recent_n`` closed trades
    in the (strategy, regime) cohort."""
    labels = _load_labels(limit=limit)
    matching = [l for l in labels if (
        (l.strategy or "—") == strategy
        and (l.regime_trend or "—") == regime
        and l.win is not None
    )]
    matching = matching[:recent_n]    # labels are loaded newest first
    pnls = [l.pnl for l in matching if l.pnl is not None]
    return win_rate(pnls), len(pnls)
