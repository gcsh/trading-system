"""Stage-6 per-strategy capital allocator.

Strategies with proven edge get more capital; strategies that bleed get
ratcheted down. Uses the Stage-1 metrics (expectancy + Sharpe + win rate)
as the input signal.

Allocation respects two caps:
  • ``strategy_min_allocation`` (default 5%)  — every active strategy gets
    at least this much so we keep sampling thin strategies' edge
  • ``strategy_max_allocation`` (default 40%) — no single strategy ever
    exceeds this share so concentration risk stays bounded

The output is a dict of ``strategy → share`` summing to 1.0 (modulo cash
reserve).
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


@dataclass
class StrategyAllocation:
    strategy: str
    share: float = 0.0
    score: float = 0.0
    reason: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _score_strategy(metrics: Dict[str, Any]) -> float:
    """Composite scoring — combines expectancy + Sharpe + win rate.

    Strategies with no closed trades get a neutral score of 1.0 so they
    still get the floor allocation and start accumulating data.
    """
    closed = int(metrics.get("closed") or 0)
    if closed < 5:
        return 1.0
    expectancy = float(metrics.get("expectancy") or 0.0)
    win_rate = float(metrics.get("win_rate") or 0.0)
    pf_raw = metrics.get("profit_factor")
    profit_factor = float(pf_raw) if isinstance(pf_raw, (int, float)) else 1.0

    # Multiplicative scoring — every component must be > 0 for the strategy
    # to score above 1.0; a negative expectancy zeros the strategy out.
    if expectancy <= 0:
        return 0.05      # floor — keep it alive but starve it
    base = math.log1p(expectancy) * max(0.1, win_rate) * max(0.5, min(3.0, profit_factor))
    # Tiny-sample correction — confidence grows with closed count up to ~30.
    confidence = min(1.0, closed / 30.0)
    return round(max(0.05, base * confidence), 4)


def allocate_capital(by_strategy_metrics: Dict[str, Dict[str, Any]],
                       *, allow_strategies: Optional[List[str]] = None,
                       ) -> List[StrategyAllocation]:
    """Build per-strategy allocations from ``/metrics/by-strategy`` data.

    Args:
        by_strategy_metrics: ``{strategy_name: {win_rate, expectancy, ...}}``
        allow_strategies: optional whitelist; strategies not in this list
            are excluded.

    Returns a list of StrategyAllocation sorted by share descending. Shares
    sum to ≤ 1.0 (cash reserve = 1 - sum(shares)).
    """
    min_share = float(getattr(TUNABLES, "strategy_min_allocation", 0.05))
    max_share = float(getattr(TUNABLES, "strategy_max_allocation", 0.40))

    candidates: List[StrategyAllocation] = []
    for name, m in (by_strategy_metrics or {}).items():
        if allow_strategies is not None and name not in allow_strategies:
            continue
        score = _score_strategy(m or {})
        candidates.append(StrategyAllocation(
            strategy=name, score=score, metrics=m or {},
            reason="initial",
        ))

    if not candidates:
        return []

    total_score = sum(c.score for c in candidates)
    if total_score <= 0:
        return [StrategyAllocation(strategy=c.strategy, share=min_share,
                                       score=0.0, reason="zero-score floor",
                                       metrics=c.metrics)
                  for c in candidates]

    # Initial proportional allocation
    for c in candidates:
        raw = c.score / total_score
        c.share = max(min_share, min(max_share, raw))
        c.reason = f"score {c.score:.3f} / {total_score:.3f} = {raw:.2%}"

    # Renormalize so total ≤ 1.0, preserving caps + floors as much as possible
    total_share = sum(c.share for c in candidates)
    if total_share > 1.0:
        scale = 1.0 / total_share
        for c in candidates:
            c.share = round(max(min_share, c.share * scale), 4)
            c.reason += f"; scaled by {scale:.2%}"

    candidates.sort(key=lambda c: c.share, reverse=True)
    return candidates
