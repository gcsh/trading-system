"""MITS Phase 0 — Knowledge Graph API.

Endpoints:
  GET  /knowledge/cells                       — filtered list of cells
  GET  /knowledge/{ticker}/{pattern}          — single cell + recent obs
  GET  /knowledge/observations/recent         — recent observations
  GET  /knowledge/corpus/status               — per-ticker corpus state
  POST /knowledge/corpus/rebuild/{ticker}     — async re-bootstrap
  GET  /knowledge/priors                      — list pattern priors
"""
from __future__ import annotations

import logging
import threading
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.corpus_status import CorpusStatus
from backend.models.knowledge_graph_cell import KnowledgeGraphCell
from backend.models.knowledge_graph_history import KnowledgeGraphHistory
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome
from backend.models.pattern_prior import PatternPrior

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("/cells")
async def list_cells(
    ticker: Optional[str] = None,
    pattern: Optional[str] = None,
    regime: Optional[str] = None,
    vol_state: Optional[str] = None,
    time_bucket: Optional[str] = None,
    horizon: Optional[str] = None,
    sample_split: Optional[str] = None,
    min_samples: int = 0,
    limit: int = 2000,
) -> List[dict]:
    """Return knowledge-graph cells, filtered. All filter args optional.

    MITS Phase 1: `sample_split` filters to one of `in_sample`,
    `out_of_sample`, or `combined`. Unfiltered queries return every
    split (default behaviour preserved).
    """
    with session_scope() as session:
        q = select(KnowledgeGraphCell)
        if ticker:
            q = q.where(KnowledgeGraphCell.ticker == ticker.upper().strip())
        if pattern:
            q = q.where(KnowledgeGraphCell.pattern == pattern)
        if regime:
            q = q.where(KnowledgeGraphCell.regime == regime)
        if vol_state:
            q = q.where(KnowledgeGraphCell.vol_state == vol_state)
        if time_bucket:
            q = q.where(KnowledgeGraphCell.time_bucket == time_bucket)
        if horizon:
            q = q.where(KnowledgeGraphCell.horizon == horizon)
        if sample_split:
            q = q.where(KnowledgeGraphCell.sample_split == sample_split)
        if min_samples > 0:
            q = q.where(KnowledgeGraphCell.sample_size >= int(min_samples))
        q = q.order_by(desc(KnowledgeGraphCell.sample_size)).limit(int(limit))
        rows = session.execute(q).scalars().all()
        return [r.to_dict() for r in rows]


@router.get("/observations/recent")
async def recent_observations(
    limit: int = 100,
    ticker: Optional[str] = None,
    pattern: Optional[str] = None,
) -> List[dict]:
    """Return the most-recent observations, optionally filtered."""
    with session_scope() as session:
        q = select(MarketObservation)
        if ticker:
            q = q.where(MarketObservation.ticker == ticker.upper().strip())
        if pattern:
            q = q.where(MarketObservation.pattern == pattern)
        q = q.order_by(desc(MarketObservation.timestamp)).limit(int(limit))
        rows = session.execute(q).scalars().all()
        return [r.to_dict() for r in rows]


@router.get("/corpus/status")
async def corpus_status() -> List[dict]:
    """Per-ticker corpus bootstrap state."""
    with session_scope() as session:
        rows = session.execute(
            select(CorpusStatus).order_by(CorpusStatus.ticker.asc())
        ).scalars().all()
        return [r.to_dict() for r in rows]


@router.post("/corpus/rebuild/{ticker}")
async def rebuild_corpus(ticker: str, response: Response) -> dict:
    """Trigger an async re-bootstrap. Returns 202 immediately."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")

    # Mark as building synchronously so the UI sees the state flip.
    try:
        with session_scope() as session:
            row = session.execute(
                select(CorpusStatus).where(CorpusStatus.ticker == ticker)
            ).scalar_one_or_none()
            if row is None:
                row = CorpusStatus(ticker=ticker, status="building")
                session.add(row)
            else:
                row.status = "building"
                row.error = None
    except Exception:
        logger.debug("corpus_status pre-mark failed", exc_info=True)

    def _run() -> None:
        try:
            from backend.bot.corpus.auto_bootstrap import run_full_bootstrap
            run_full_bootstrap(ticker)
        except Exception:
            logger.exception("rebuild_corpus thread for %s failed", ticker)

    threading.Thread(target=_run, name=f"corpus-rebuild-{ticker}",
                          daemon=True).start()
    response.status_code = 202
    return {"ticker": ticker, "status": "building"}


@router.get("/priors")
async def list_priors() -> List[dict]:
    """Return all encoded pattern priors."""
    with session_scope() as session:
        rows = session.execute(
            select(PatternPrior).order_by(PatternPrior.pattern.asc())
        ).scalars().all()
        return [r.to_dict() for r in rows]


@router.get("/{ticker}/{pattern}")
async def cell_detail(ticker: str, pattern: str,
                              history_days: int = 0) -> dict:
    """Single most-populated cell for (ticker, pattern) + the 20 most-recent
    matching observations with their outcomes attached.

    When `history_days > 0`, also returns the last N days of
    `KnowledgeGraphHistory` rows for the primary cell so the UI can
    render a posterior sparkline.
    """
    from datetime import date, timedelta
    ticker = ticker.upper().strip()
    with session_scope() as session:
        cells = session.execute(
            select(KnowledgeGraphCell)
            .where(KnowledgeGraphCell.ticker == ticker)
            .where(KnowledgeGraphCell.pattern == pattern)
            .order_by(desc(KnowledgeGraphCell.sample_size))
        ).scalars().all()
        if not cells:
            raise HTTPException(status_code=404,
                                       detail=f"no cell for {ticker}/{pattern}")
        primary = cells[0].to_dict()
        siblings = [c.to_dict() for c in cells[1:]]

        # Recent observations for this (ticker, pattern), with outcomes.
        obs_rows = session.execute(
            select(MarketObservation)
            .where(MarketObservation.ticker == ticker)
            .where(MarketObservation.pattern == pattern)
            .order_by(desc(MarketObservation.timestamp))
            .limit(20)
        ).scalars().all()
        obs_ids = [r.id for r in obs_rows]
        outcomes_by_obs: dict = {}
        if obs_ids:
            outcome_rows = session.execute(
                select(MarketOutcome).where(MarketOutcome.observation_id.in_(obs_ids))
            ).scalars().all()
            for o in outcome_rows:
                outcomes_by_obs.setdefault(o.observation_id, []).append(o.to_dict())
        observations = []
        for r in obs_rows:
            d = r.to_dict()
            d["outcomes"] = outcomes_by_obs.get(r.id, [])
            observations.append(d)

        out = {
            "primary_cell": primary,
            "siblings": siblings,
            "recent_observations": observations,
        }

        # MITS Phase 1 — optional posterior history sparkline.
        # MITS Phase 2 (P2.4) — auto-density: when history spans more
        # than `TUNABLES.knowledge_sparkline_daily_cap_days` (default 180),
        # the rows are bucketed into weekly aggregates so the rendered
        # sparkline stays readable. Resolution is surfaced via the
        # `resolution` field of the response.
        if history_days and history_days > 0:
            try:
                from backend.config import TUNABLES as _TUN
                daily_cap = int(getattr(_TUN, "knowledge_sparkline_daily_cap_days",
                                              180))
                cutoff = date.today() - timedelta(days=int(history_days))
                hist_q = (
                    select(KnowledgeGraphHistory)
                    .where(KnowledgeGraphHistory.ticker == ticker)
                    .where(KnowledgeGraphHistory.pattern == pattern)
                    .where(KnowledgeGraphHistory.regime == primary["regime"])
                    .where(KnowledgeGraphHistory.vol_state == primary["vol_state"])
                    .where(KnowledgeGraphHistory.time_bucket == primary["time_bucket"])
                    .where(KnowledgeGraphHistory.horizon == primary["horizon"])
                    .where(KnowledgeGraphHistory.sample_split == (
                        primary.get("sample_split") or "combined"))
                    .where(KnowledgeGraphHistory.snapshot_date >= cutoff)
                    .order_by(KnowledgeGraphHistory.snapshot_date.asc())
                )
                hist_rows = session.execute(hist_q).scalars().all()
                history_dicts = [r.to_dict() for r in hist_rows]
                resolution = "daily"
                if (int(history_days) > daily_cap
                        or len(history_dicts) > daily_cap):
                    history_dicts = _bucket_history_weekly(history_dicts)
                    resolution = "weekly"
                out["history"] = history_dicts
                out["resolution"] = resolution
            except Exception:
                logger.debug("history fetch failed", exc_info=True)
                out["history"] = []
                out["resolution"] = "daily"

        return out


def _bucket_history_weekly(rows: list) -> list:
    """Aggregate daily history rows into Mon-Sun weekly buckets.

    - `snapshot_date` becomes the Monday of each bucket.
    - `posterior_win_rate` / `win_rate` are weighted by sample_size
      across the rows in the bucket.
    - `confidence_lower` / `confidence_upper` are recomputed as a
      sample-size-weighted Wilson 95% CI over the aggregated wins / N
      (more accurate than averaging the per-day CIs).
    - `sample_size` is the SUM across the bucket — the operator-spec'd
      "weighted average by sample size" semantics.

    Returns the bucketed rows in chronological order. Empty list input
    returns empty list.
    """
    if not rows:
        return []
    from datetime import datetime as _dt, timedelta as _td
    import math as _math

    def _parse_date(d: object):
        if isinstance(d, str):
            try:
                return _dt.strptime(d[:10], "%Y-%m-%d").date()
            except Exception:
                return None
        if hasattr(d, "isoformat"):
            return d
        return None

    buckets: dict = {}
    for r in rows:
        d = _parse_date(r.get("snapshot_date"))
        if d is None:
            continue
        # Monday-of-week (ISO calendar — weekday 0 = Monday).
        try:
            monday = d - _td(days=d.weekday())
        except Exception:
            continue
        b = buckets.setdefault(monday, {
            "snapshot_date": monday.isoformat(),
            "_total_n": 0,
            "_weighted_post": 0.0,
            "_weighted_wr": 0.0,
            "_weighted_avg_ret": 0.0,
            "_avg_ret_denom": 0,
            "ticker": r.get("ticker"),
            "pattern": r.get("pattern"),
            "regime": r.get("regime"),
            "vol_state": r.get("vol_state"),
            "time_bucket": r.get("time_bucket"),
            "horizon": r.get("horizon"),
            "sample_split": r.get("sample_split"),
        })
        n = int(r.get("sample_size") or 0)
        if n <= 0:
            continue
        b["_total_n"] += n
        post = r.get("posterior_win_rate")
        if post is not None:
            b["_weighted_post"] += float(post) * n
        wr = r.get("win_rate")
        if wr is not None:
            b["_weighted_wr"] += float(wr) * n
        avg_ret = r.get("avg_return_pct")
        if avg_ret is not None:
            b["_weighted_avg_ret"] += float(avg_ret) * n
            b["_avg_ret_denom"] += n

    def _wilson_from_wr(wr: float, n: int):
        if n <= 0:
            return None, None
        wins = wr * n
        z = 1.96
        denom = 1.0 + z * z / n
        center = (wr + z * z / (2.0 * n)) / denom
        margin = (z * _math.sqrt((wr * (1.0 - wr) / n)
                                          + (z * z / (4.0 * n * n)))) / denom
        return max(0.0, center - margin), min(1.0, center + margin)

    out: list = []
    for monday in sorted(buckets.keys()):
        b = buckets[monday]
        n = int(b["_total_n"])
        if n <= 0:
            continue
        agg_post = (b["_weighted_post"] / n) if n else None
        agg_wr = (b["_weighted_wr"] / n) if n else None
        avg_ret_d = b["_avg_ret_denom"]
        avg_ret = (b["_weighted_avg_ret"] / avg_ret_d) if avg_ret_d else None
        if agg_wr is not None:
            lo, hi = _wilson_from_wr(agg_wr, n)
        else:
            lo, hi = None, None
        out.append({
            "snapshot_date": b["snapshot_date"],
            "ticker": b["ticker"],
            "pattern": b["pattern"],
            "regime": b["regime"],
            "vol_state": b["vol_state"],
            "time_bucket": b["time_bucket"],
            "horizon": b["horizon"],
            "sample_split": b["sample_split"],
            "sample_size": n,
            "win_rate": (round(agg_wr, 4) if agg_wr is not None else None),
            "posterior_win_rate": (round(agg_post, 4)
                                    if agg_post is not None else None),
            "avg_return_pct": (round(avg_ret, 6)
                                if avg_ret is not None else None),
            "confidence_lower": (round(lo, 4) if lo is not None else None),
            "confidence_upper": (round(hi, 4) if hi is not None else None),
        })
    return out
