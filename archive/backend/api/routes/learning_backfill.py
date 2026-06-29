"""MITS Phase 18-FU Gap 4 — Learning-layer historical-replay backfill route.

Endpoint:

  ``POST /learning/backfill``
    body: ``{days_back: int, max_rows: int, dry_run: bool}``
    response: ``BackfillResult.to_dict()``

Safety contract (mandatory):

  * Requires ``TUNABLES.learning_backfill_enabled = True`` (env
    ``TB_LEARNING_BACKFILL_ENABLED=1`` sets it) — otherwise returns
    HTTP 403 immediately.
  * ``dry_run`` defaults to True. The operator must EXPLICITLY pass
    ``dry_run=false`` to mutate state.
  * Synthetic rows are tagged ``source_kind='synthetic_backfill'``
    and EXCLUDED from default learning-layer reads (see
    ``compute_attribution_report(include_synthetic=False)``, the
    default).

This file is owned exclusively by Stream B (Gap 4). It is mounted
under the existing ``/learning`` prefix — separate file from
``routes/learning.py`` (Stream A's territory) so there are no merge
conflicts.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.bot.learning.backfill import (
    backfill_learning_from_historical_replay,
)
from backend.config import TUNABLES


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/learning", tags=["learning"])


class BackfillRequest(BaseModel):
    """Operator-supplied parameters for one backfill invocation."""

    days_back: int = Field(
        default=90, ge=1, le=3650,
        description="Walk back this many days on observation timestamp.",
    )
    max_rows: int = Field(
        default=500, ge=1, le=10_000,
        description="Upper bound on synthetic Trade+Provenance rows.",
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "When True (default), reports counts without writing. "
            "Operator MUST pass dry_run=false to mutate state."
        ),
    )
    horizon: str = Field(
        default="1d",
        description=(
            "MarketOutcome horizon to replay. Default '1d' (matches "
            "typical paper-trial trade duration)."
        ),
    )


@router.post("/backfill")
def trigger_backfill(req: BackfillRequest) -> Dict[str, Any]:
    """Synthesize learning-layer feed rows from MarketObservation
    × MarketOutcome data.

    Refuses to run unless ``TUNABLES.learning_backfill_enabled`` is
    True (which itself requires the ``TB_LEARNING_BACKFILL_ENABLED``
    env var). On enabled, runs the backfill and returns a count
    summary.

    Always-safe defaults: dry_run=True writes nothing. The operator
    flips ``dry_run=false`` only after reviewing dry-run counts.
    """
    if not bool(getattr(TUNABLES, "learning_backfill_enabled", False)):
        raise HTTPException(
            status_code=403,
            detail=(
                "learning backfill disabled — set "
                "TB_LEARNING_BACKFILL_ENABLED=1 to enable"
            ),
        )
    result = backfill_learning_from_historical_replay(
        days_back=req.days_back,
        max_synthetic_rows=req.max_rows,
        dry_run=req.dry_run,
        horizon=req.horizon,
    )
    return result.to_dict()


@router.get("/backfill/status")
def backfill_status() -> Dict[str, Any]:
    """Read-only status of the backfill kill switch + a recent row
    count. Useful for cockpit display."""
    from sqlalchemy import func, select as _select

    from backend.db import session_scope
    from backend.models.trade import Trade
    from backend.bot.learning.backfill import (
        SYNTHETIC_SIGNAL_SOURCE,
        SYNTHETIC_SOURCE_KIND,
    )

    flag_enabled = bool(
        getattr(TUNABLES, "learning_backfill_enabled", False),
    )
    n_synthetic: Optional[int] = None
    try:
        with session_scope() as s:
            n_synthetic = int(
                s.execute(
                    _select(func.count(Trade.id)).where(
                        Trade.signal_source == SYNTHETIC_SIGNAL_SOURCE,
                    ).where(
                        Trade.source_kind == SYNTHETIC_SOURCE_KIND,
                    )
                ).scalar() or 0
            )
    except Exception:
        logger.exception("backfill_status: synthetic count query failed")
        n_synthetic = None
    return {
        "flag_enabled": flag_enabled,
        "synthetic_trades_in_ledger": n_synthetic,
    }
