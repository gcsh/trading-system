"""Stage-6 portfolio optimizer — clean public surface."""
from __future__ import annotations

from backend.bot.portfolio_optimizer.allocator import (
    StrategyAllocation,
    allocate_capital,
)
from backend.bot.portfolio_optimizer.correlation import (
    ClusterCapResult,
    ClusterExposure,
    check_cluster_cap,
    cluster_exposures,
)
from backend.bot.portfolio_optimizer.optimizer import (
    OptimizerDecision,
    optimize_size,
)
from backend.bot.portfolio_optimizer.sizing import (
    SizingResult,
    combine_size,
    cvar_size_fraction,
    drawdown_size_multiplier,
    kelly_fraction,
    vol_target_fraction,
)

__all__ = [
    "StrategyAllocation", "allocate_capital",
    "ClusterCapResult", "ClusterExposure", "check_cluster_cap",
    "cluster_exposures",
    "OptimizerDecision", "optimize_size",
    "SizingResult", "combine_size", "cvar_size_fraction",
    "drawdown_size_multiplier", "kelly_fraction", "vol_target_fraction",
]
