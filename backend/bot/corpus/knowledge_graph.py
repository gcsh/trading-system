"""MITS Phase 12.1 Fix 6 + Phase 12.2 — consumer-side hierarchical
fallback for the knowledge graph.

The aggregator in ``knowledge_aggregator.py`` already shrinks thin
cells toward a (pattern, regime) or (pattern) parent during the upsert.
But consumers (agent_context, EOD analysis, theory engine,
Opportunity Brain, /analysis, /knowledge, /theories) read individual
``knowledge_graph`` cells directly and still see N=5 cells driven by
the academic prior.

This module exposes a small read-side helper:

    get_posterior_with_fallback(
        ticker, pattern, regime, vol_state, horizon='5d',
        sample_split='combined',
    ) -> {posterior, win_rate, n, level, source, ticker, pattern, regime, vol_state}

Fallback chain:
  1. Cell at the exact 4-axis cohort (ticker, pattern, regime, vol_state).
     Used directly when N >= MIN_N_LOCAL (30).
  2. (pattern, regime) parent across all tickers/vol_states.
  3. (pattern) global parent across all cohorts.
  4. None — caller decides whether to fall back to academic prior.

Each returned dict carries `source` so the UI/operator can see whether
the posterior is grounded in the specific ticker or borrowed.

Phase 12.2 adds:
  * In-process counters of source distribution
    (cell / pattern_regime / pattern / local_thin / none) so the engine
    can surface "how often consumers fell back to parents" in its
    cycle metrics. Reset on demand via reset_fallback_stats(). Atomic
    via a single lock; safe under the scheduler thread pool.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from backend.db import session_scope
from backend.models.knowledge_graph_cell import KnowledgeGraphCell

logger = logging.getLogger(__name__)


# When the specific cell has N below this threshold, the parent
# distribution is preferred.
MIN_N_LOCAL = 30
MIN_N_PARENT = 30


# ── Phase 12.2 — fallback observability counters ─────────────────────
_STATS_LOCK = threading.Lock()
_FALLBACK_STATS: Dict[str, int] = {
    "calls": 0,
    "cell": 0,
    "pattern_regime": 0,
    "pattern": 0,
    "local_thin": 0,
    "none": 0,
}


def _record_source(source: str) -> None:
    with _STATS_LOCK:
        _FALLBACK_STATS["calls"] += 1
        _FALLBACK_STATS[source] = _FALLBACK_STATS.get(source, 0) + 1


def get_fallback_stats() -> Dict[str, Any]:
    """Snapshot of (source, count) for this process. Used by the
    engine cycle metrics + ops endpoint."""
    with _STATS_LOCK:
        snap = dict(_FALLBACK_STATS)
    calls = max(int(snap.get("calls", 0)), 1)
    snap["fallback_rate"] = round(
        1.0 - (snap.get("cell", 0) / calls), 4,
    )
    return snap


def reset_fallback_stats() -> None:
    with _STATS_LOCK:
        for k in _FALLBACK_STATS:
            _FALLBACK_STATS[k] = 0


def _cell_to_dict(row: KnowledgeGraphCell, source: str) -> Dict[str, Any]:
    return {
        "ticker": row.ticker,
        "pattern": row.pattern,
        "regime": row.regime,
        "vol_state": row.vol_state,
        "horizon": row.horizon,
        "sample_split": row.sample_split,
        "n": row.sample_size,
        "win_rate": row.win_rate,
        "posterior": row.posterior_win_rate,
        "avg_return_pct": row.avg_return_pct,
        "confidence_level": row.confidence_level,
        "confidence_lower": row.confidence_lower,
        "confidence_upper": row.confidence_upper,
        # MITS Phase 13 Fix 7 — direction-aware bounds (NULL when the
        # cell has uniform direction).
        "confidence_lower_long": getattr(row, "confidence_lower_long", None),
        "confidence_upper_long": getattr(row, "confidence_upper_long", None),
        "confidence_lower_short": getattr(row, "confidence_lower_short", None),
        "confidence_upper_short": getattr(row, "confidence_upper_short", None),
        "parent_type": getattr(row, "parent_type", "cell"),
        "source": source,
    }


def _pooled(rows: List[KnowledgeGraphCell], source: str) -> Optional[Dict[str, Any]]:
    """Pool a list of cells (sample-size weighted) into a single
    posterior_win_rate + total N. Used for parent fallback rows."""
    if not rows:
        return None
    total_n = 0
    weighted_post = 0.0
    weighted_wr = 0.0
    weighted_ret = 0.0
    for r in rows:
        n = int(r.sample_size or 0)
        if n <= 0 or r.posterior_win_rate is None:
            continue
        total_n += n
        weighted_post += n * float(r.posterior_win_rate)
        if r.win_rate is not None:
            weighted_wr += n * float(r.win_rate)
        if r.avg_return_pct is not None:
            weighted_ret += n * float(r.avg_return_pct)
    if total_n == 0:
        return None
    return {
        "posterior": round(weighted_post / total_n, 4),
        "win_rate": round(weighted_wr / total_n, 4),
        "avg_return_pct": round(weighted_ret / total_n, 6),
        "n": total_n,
        "confidence_level": ("high" if total_n >= 100
                                  else "medium" if total_n >= 30
                                  else "low" if total_n >= 10 else "thin"),
        "source": source,
    }


# MITS Phase 13 Fix 4 — sentinel ticker/regime values for persisted
# parent rows. Mirror the aggregator constants here to avoid a circular
# import; both files keep the source-of-truth definition.
SENTINEL_TICKER_ALL = "__ALL__"
SENTINEL_REGIME_ALL = "__ALL__"


def _ci_width(row: KnowledgeGraphCell) -> Optional[float]:
    """Helper: posterior CI width when both bounds exist."""
    try:
        lo = row.confidence_lower
        hi = row.confidence_upper
        if lo is None or hi is None:
            return None
        return round(float(hi) - float(lo), 4)
    except Exception:
        return None


def get_posterior_with_fallback(ticker: str, pattern: str,
                                              regime: str = "unknown",
                                              vol_state: str = "normal",
                                              time_bucket: str = "rth",
                                              horizon: str = "5d",
                                              sample_split: str = "combined",
                                              ) -> Optional[Dict[str, Any]]:
    """Return the most-specific posterior available, with explicit
    provenance.

    Tries the exact 6-axis cell first; falls back to the PERSISTED
    (pattern, regime) parent row (ticker=__ALL__) then to the PERSISTED
    (pattern) global parent row (ticker=__ALL__, regime=__ALL__).

    MITS Phase 13 Fix 4 — parent rows are persisted by the aggregator
    (parent_type ∈ {'pattern_regime_parent', 'pattern_parent'}) so
    fallback is a single indexed lookup, not a pool computation. When
    a persisted parent row is missing (cold cache, fresh DB) we fall
    through to the legacy on-the-fly pool so the fallback is still
    safe.

    The 'source' field tells callers what level the answer came from:
      - 'cell'              — local 6-axis cell with N >= MIN_N_LOCAL
      - 'pattern_regime'    — persisted (pattern, regime) parent
      - 'pattern'           — persisted (pattern) global parent
      - 'pattern_regime_pool' — on-the-fly pool (legacy fallback)
      - 'pattern_pool'      — on-the-fly pool (legacy fallback)
      - 'local_thin'        — local cell exists but N < MIN_N_LOCAL
                              and no parent had enough samples.
    Returns None when nothing is found at all.
    """
    if not pattern:
        _record_source("none")
        return None
    tkr = (ticker or "").upper().strip()
    try:
        with session_scope() as s:
            # 1. Exact cell.
            local = s.execute(
                select(KnowledgeGraphCell)
                .where(KnowledgeGraphCell.ticker == tkr)
                .where(KnowledgeGraphCell.pattern == pattern)
                .where(KnowledgeGraphCell.regime == regime)
                .where(KnowledgeGraphCell.vol_state == vol_state)
                .where(KnowledgeGraphCell.horizon == horizon)
                .where(KnowledgeGraphCell.sample_split == sample_split)
            ).scalars().first()
            if local is not None and (local.sample_size or 0) >= MIN_N_LOCAL:
                _record_source("cell")
                d = _cell_to_dict(local, "cell")
                d["ci_width"] = _ci_width(local)
                return d
            # 2. Persisted (pattern, regime) parent row.
            pr_parent = s.execute(
                select(KnowledgeGraphCell)
                .where(KnowledgeGraphCell.ticker == SENTINEL_TICKER_ALL)
                .where(KnowledgeGraphCell.pattern == pattern)
                .where(KnowledgeGraphCell.regime == regime)
                .where(KnowledgeGraphCell.horizon == horizon)
                .where(KnowledgeGraphCell.sample_split == sample_split)
                .where(KnowledgeGraphCell.parent_type
                       == "pattern_regime_parent")
            ).scalars().first()
            if pr_parent is not None and (pr_parent.sample_size or 0) >= MIN_N_PARENT:
                _record_source("pattern_regime")
                d = _cell_to_dict(pr_parent, "pattern_regime")
                d["ticker"] = tkr  # surface to the caller, not the sentinel
                d["ci_width"] = _ci_width(pr_parent)
                return d
            # 3. Persisted (pattern) global parent row.
            p_parent = s.execute(
                select(KnowledgeGraphCell)
                .where(KnowledgeGraphCell.ticker == SENTINEL_TICKER_ALL)
                .where(KnowledgeGraphCell.pattern == pattern)
                .where(KnowledgeGraphCell.regime == SENTINEL_REGIME_ALL)
                .where(KnowledgeGraphCell.horizon == horizon)
                .where(KnowledgeGraphCell.sample_split == sample_split)
                .where(KnowledgeGraphCell.parent_type == "pattern_parent")
            ).scalars().first()
            if p_parent is not None and (p_parent.sample_size or 0) >= MIN_N_PARENT:
                _record_source("pattern")
                d = _cell_to_dict(p_parent, "pattern")
                d["ticker"] = tkr
                d["regime"] = regime
                d["ci_width"] = _ci_width(p_parent)
                return d
            # 4. Legacy fallback — on-the-fly pool from cell rows.
            # Used when persisted parents are missing (e.g. fresh DB,
            # pre-Phase-13 migration). Excludes the parent sentinel
            # rows so we don't double-count them.
            pr_rows = s.execute(
                select(KnowledgeGraphCell)
                .where(KnowledgeGraphCell.ticker != SENTINEL_TICKER_ALL)
                .where(KnowledgeGraphCell.pattern == pattern)
                .where(KnowledgeGraphCell.regime == regime)
                .where(KnowledgeGraphCell.horizon == horizon)
                .where(KnowledgeGraphCell.sample_split == sample_split)
            ).scalars().all()
            pooled = _pooled(list(pr_rows), "pattern_regime_pool")
            if pooled and pooled["n"] >= MIN_N_PARENT:
                pooled.update({
                    "ticker": tkr, "pattern": pattern, "regime": regime,
                    "vol_state": vol_state, "horizon": horizon,
                    "sample_split": sample_split,
                })
                _record_source("pattern_regime")
                return pooled
            p_rows = s.execute(
                select(KnowledgeGraphCell)
                .where(KnowledgeGraphCell.ticker != SENTINEL_TICKER_ALL)
                .where(KnowledgeGraphCell.pattern == pattern)
                .where(KnowledgeGraphCell.horizon == horizon)
                .where(KnowledgeGraphCell.sample_split == sample_split)
            ).scalars().all()
            pooled_p = _pooled(list(p_rows), "pattern_pool")
            if pooled_p:
                pooled_p.update({
                    "ticker": tkr, "pattern": pattern, "regime": regime,
                    "vol_state": vol_state, "horizon": horizon,
                    "sample_split": sample_split,
                })
                _record_source("pattern")
                return pooled_p
            # 5. Fall through to the local thin cell if we had one.
            if local is not None:
                _record_source("local_thin")
                d = _cell_to_dict(local, "local_thin")
                d["ci_width"] = _ci_width(local)
                return d
    except Exception:
        logger.debug("knowledge_graph fallback fetch failed", exc_info=True)
    _record_source("none")
    return None


__all__ = [
    "get_posterior_with_fallback",
    "get_fallback_stats",
    "reset_fallback_stats",
    "MIN_N_LOCAL",
    "MIN_N_PARENT",
]
