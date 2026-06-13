"""MITS Phase 0 — convenience wrapper for the watchlist-add bootstrap.

Single function: ``run_full_bootstrap(ticker)``. Walks through:

  1. ``bootstrap_ticker(ticker)``  — fetch bars + persist observations.
  2. ``link_outcomes_batch(ticker)`` — compute forward returns.
  3. ``recompute_cells(ticker)``     — fold into knowledge-graph cells.
  4. Mark `corpus_status="ready"` (or "error" on exception).

Designed to be called from a daemon thread so the watchlist HTTP POST
returns immediately. Exceptions are caught + logged + recorded into the
`corpus_status` row so the operator can see them in the UI.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import func, select

from backend.bot.corpus.historical_replay import bootstrap_ticker
from backend.bot.corpus.knowledge_aggregator import recompute_cells
from backend.bot.corpus.outcome_linker import link_outcomes_batch
from backend.db import session_scope
from backend.models.corpus_status import CorpusStatus
from backend.models.knowledge_graph_cell import KnowledgeGraphCell
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome

logger = logging.getLogger(__name__)


def run_full_bootstrap(ticker: str, *,
                              daily_lookback_years: int = 10,
                              intraday_lookback_days: int = 180) -> Dict[str, Any]:
    """Run the full Phase 0 pipeline for one ticker. Idempotent."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return {"status": "error", "error": "missing ticker"}
    out: Dict[str, Any] = {"ticker": ticker, "stages": {}}
    try:
        out["stages"]["bootstrap"] = bootstrap_ticker(
            ticker,
            daily_lookback_years=daily_lookback_years,
            intraday_lookback_days=intraday_lookback_days,
        )
        out["stages"]["outcomes"] = link_outcomes_batch(ticker)
        out["stages"]["cells"] = recompute_cells(ticker)
        _finalize_status(ticker, status="ready")
        out["status"] = "ready"
    except Exception as e:
        logger.exception("run_full_bootstrap failed for %s", ticker)
        _finalize_status(ticker, status="error", error=str(e))
        out["status"] = "error"
        out["error"] = str(e)
    return out


def _finalize_status(ticker: str, *, status: str,
                          error: str | None = None) -> None:
    """Refresh corpus_status counts after the pipeline finishes."""
    try:
        with session_scope() as s:
            row = s.execute(
                select(CorpusStatus).where(CorpusStatus.ticker == ticker)
            ).scalar_one_or_none()
            if row is None:
                row = CorpusStatus(ticker=ticker)
                s.add(row)
                s.flush()
            row.observation_count = int(s.execute(
                select(func.count(MarketObservation.id))
                .where(MarketObservation.ticker == ticker)
            ).scalar_one() or 0)
            row.outcome_count = int(s.execute(
                select(func.count(MarketOutcome.id))
                .join(MarketObservation,
                          MarketObservation.id == MarketOutcome.observation_id)
                .where(MarketObservation.ticker == ticker)
            ).scalar_one() or 0)
            row.cell_count = int(s.execute(
                select(func.count(KnowledgeGraphCell.id))
                .where(KnowledgeGraphCell.ticker == ticker)
            ).scalar_one() or 0)
            if status == "ready" and row.observation_count == 0:
                row.status = "insufficient"
            else:
                row.status = status
            if status == "ready":
                row.last_built_at = datetime.utcnow()
                row.error = None
            if error is not None:
                row.error = error[:500]
    except Exception:
        logger.debug("status finalize failed for %s", ticker, exc_info=True)
