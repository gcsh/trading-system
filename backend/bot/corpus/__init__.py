"""MITS Phase 0 — corpus bootstrap package.

Submodules:
  * historical_replay — fetch yfinance bars + run detectors against history.
  * outcome_linker   — link forward returns to each observation.
  * knowledge_aggregator — fold (obs + outcomes) into knowledge_graph cells.
  * priors_loader    — seed pattern_priors with academic / TA-Lib priors.
  * auto_bootstrap   — convenience wrapper invoked by the watchlist add hook.
"""

from backend.bot.corpus.historical_replay import bootstrap_ticker
from backend.bot.corpus.knowledge_aggregator import (
    recompute_cells,
    snapshot_cells_to_history,
)
from backend.bot.corpus.outcome_linker import link_outcomes_batch
from backend.bot.corpus.priors_loader import load_default_priors

__all__ = [
    "bootstrap_ticker",
    "link_outcomes_batch",
    "load_default_priors",
    "recompute_cells",
    "snapshot_cells_to_history",
]
