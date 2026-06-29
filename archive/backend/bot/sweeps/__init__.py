"""Stage-10 item 9 — threshold sweep frontier.

Walks the grid of ``(min_grade, prob_floor)`` over historical labelled
trades, computing realized expectancy + Sharpe + max-drawdown at each
(grade_floor, prob_floor) point. Returns:

  • The full grid (for the UI to render as a heatmap)
  • The **frontier** — the points that maximize Sharpe subject to staying
    below the operator's max-drawdown cap
  • A suggested config diff (the single best point) the user can apply

Nightly cron-friendly — pure given the input labels; deterministic.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.bot.labeling import TradeLabel
from backend.bot.metrics import expectancy, max_drawdown, sharpe_ratio, win_rate

logger = logging.getLogger(__name__)

GRADE_ORDER = ["A+", "A", "B", "C", "D"]


@dataclass
class GridPoint:
    min_grade: str
    prob_floor: float
    n_trades: int
    n_wins: int
    win_rate: Optional[float]
    expectancy: Optional[float]
    sharpe: Optional[float]
    max_dd_pct: Optional[float]
    total_pnl: float = 0.0
    accepted_under_dd_cap: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SweepFrontier:
    grid: List[Dict[str, Any]] = field(default_factory=list)
    frontier: List[Dict[str, Any]] = field(default_factory=list)
    best: Optional[Dict[str, Any]] = None
    suggested_config_diff: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── helpers ──────────────────────────────────────────────────────────────


def _grades_at_or_above(floor: str) -> set:
    try:
        idx = GRADE_ORDER.index(floor)
    except ValueError:
        return set(GRADE_ORDER)
    return set(GRADE_ORDER[:idx + 1])


def _eligible(label: TradeLabel, *, min_grade: str,
                prob_floor: float) -> bool:
    """Would the label have been TAKEN under (min_grade, prob_floor)?"""
    if label.grade and label.grade not in _grades_at_or_above(min_grade):
        return False
    if label.win_probability is not None and label.win_probability < prob_floor:
        return False
    return True


def _equity_curve_from(pnls: List[float], starting: float = 10_000) -> List[float]:
    """Build a per-trade running equity from realized P&Ls."""
    equity = [starting]
    for p in pnls:
        equity.append(equity[-1] + float(p))
    return equity


def _eval_grid_point(labels: List[TradeLabel], *, min_grade: str,
                      prob_floor: float) -> GridPoint:
    eligible = [l for l in labels
                  if l.win is not None and _eligible(
                      l, min_grade=min_grade, prob_floor=prob_floor)]
    pnls = [l.pnl for l in eligible if l.pnl is not None]
    if not pnls:
        return GridPoint(min_grade=min_grade, prob_floor=prob_floor,
                          n_trades=0, n_wins=0, win_rate=None,
                          expectancy=None, sharpe=None,
                          max_dd_pct=None, total_pnl=0.0)
    eq = _equity_curve_from(pnls)
    rets = [(eq[i] - eq[i - 1]) / eq[i - 1]
             for i in range(1, len(eq)) if eq[i - 1] > 0]
    sharpe = sharpe_ratio(rets, periods_per_year=252) if rets else None
    dd = max_drawdown(eq)["dd_pct"]
    return GridPoint(
        min_grade=min_grade, prob_floor=round(prob_floor, 2),
        n_trades=len(pnls),
        n_wins=sum(1 for p in pnls if p > 0),
        win_rate=win_rate(pnls),
        expectancy=expectancy(pnls),
        sharpe=sharpe,
        max_dd_pct=dd,
        total_pnl=round(sum(pnls), 2),
    )


# ── public API ──────────────────────────────────────────────────────────


def sweep_threshold_frontier(
    labels: List[TradeLabel],
    *,
    grades: Optional[List[str]] = None,
    prob_floors: Optional[List[float]] = None,
    max_dd_cap_pct: float = 0.15,
    min_trades: int = 10,
) -> SweepFrontier:
    """Build the (grade × prob_floor) grid + extract the frontier.

    A grid point is on the **frontier** when:
      1. It has ≥ ``min_trades`` (statistical floor)
      2. Its max drawdown ≤ ``max_dd_cap_pct``
      3. There's no other point with a higher Sharpe AND lower-or-equal DD

    Returns ``SweepFrontier`` with ``best`` being the single highest-Sharpe
    frontier point + a ``suggested_config_diff`` ready to PR into config.
    """
    grades = grades or GRADE_ORDER
    prob_floors = prob_floors or [0.45, 0.50, 0.55, 0.60, 0.65, 0.70]
    grid: List[GridPoint] = []
    for g in grades:
        for p in prob_floors:
            grid.append(_eval_grid_point(labels, min_grade=g, prob_floor=p))

    # Mark accepted points
    for pt in grid:
        pt.accepted_under_dd_cap = (
            pt.n_trades >= min_trades
            and pt.max_dd_pct is not None and pt.max_dd_pct <= max_dd_cap_pct
            and pt.sharpe is not None
        )

    accepted = [pt for pt in grid if pt.accepted_under_dd_cap]
    # Pareto frontier: maximize Sharpe; for equal Sharpe, prefer lower DD
    frontier: List[GridPoint] = []
    for pt in sorted(accepted, key=lambda x: (x.sharpe or 0), reverse=True):
        dominated = any(
            (f.sharpe or 0) >= (pt.sharpe or 0)
            and (f.max_dd_pct or 1) <= (pt.max_dd_pct or 1)
            and (f.min_grade, f.prob_floor) != (pt.min_grade, pt.prob_floor)
            for f in frontier
        )
        if not dominated:
            frontier.append(pt)

    best = max(frontier, key=lambda x: (x.sharpe or 0), default=None)
    suggested: Dict[str, Any] = {}
    notes: List[str] = []
    if best is not None:
        suggested = {
            "analytics.min_grade": best.min_grade,
            "TB_PROB_FLOOR": best.prob_floor,
            "expected_sharpe": best.sharpe,
            "expected_max_dd_pct": best.max_dd_pct,
            "expected_n_trades": best.n_trades,
        }
    else:
        notes.append(
            f"no frontier point satisfies n_trades ≥ {min_trades} "
            f"AND max_dd ≤ {max_dd_cap_pct:.0%}"
        )

    return SweepFrontier(
        grid=[pt.to_dict() for pt in grid],
        frontier=[pt.to_dict() for pt in frontier],
        best=best.to_dict() if best else None,
        suggested_config_diff=suggested,
        notes=notes,
    )
