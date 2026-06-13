"""Stage-13.C5 Regime Similarity Engine — find historical regimes like today.

Given a current ``MarketState``, scan the ``regime_episode_snapshots``
table and return the K most-similar historical fingerprints. For each
match, surface:

  • the snapshot itself
  • forward 1d / 5d return (when backfilled)
  • aggregate win-rate of trades fired during that window
  • the strategy that worked best at the time

This is the *bigger* sibling of Stage-11.5's ``memory.recall_similar()``,
which returns per-trade analogues. This one returns *macro-regime*
analogues — the right question to ask at portfolio-level decision time.

Two helpers:

  • ``snapshot_current(state)`` — write one row from a ``MarketState`` (also
    fills any numeric fields the caller supplies in ``extra``).
  • ``find_similar(target_state, k)`` — return top-K matches with similarity
    score + forward-outcome stats.

Similarity is composite:
  • Categorical match (trend / vol_phase / gamma / equities / yields): 0.45
  • Numeric distance over (vix, iv_rank, breadth, sentiment, sector_strength,
    rates_10y, dollar_dxy): 0.55  (per-feature L1 normalized)
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from backend.bot.state import MarketState
from backend.db import session_scope
from backend.models.regime_episode import RegimeEpisodeSnapshot

logger = logging.getLogger(__name__)


# Categorical axes used in the similarity score. Match score: 0.45 / N
# per axis matched.
_CAT_AXES = ("trend", "vol_phase", "gamma", "equities", "yields")

# Numeric axes used in the L1 distance term. Magnitudes vary wildly so each
# is normalized by the per-axis magnitude before averaging.
_NUM_AXES = (
    "vix", "iv_rank", "breadth_score", "sentiment_score",
    "sector_strength", "rates_10y", "dollar_dxy",
)


@dataclass
class RegimeMatch:
    snapshot_id: int
    timestamp: str
    similarity: float                    # 0-1
    label: str
    snapshot: Dict[str, Any] = field(default_factory=dict)
    fwd_1d_return: Optional[float] = None
    fwd_5d_return: Optional[float] = None
    fwd_trades_count: int = 0
    fwd_trades_win_rate: Optional[float] = None
    fwd_trades_pnl: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── snapshot writer ─────────────────────────────────────────────────────


def snapshot_current(state: MarketState,
                        *,
                        breadth_score: float = 0.0,
                        sentiment_score: float = 0.0,
                        sector_strength: float = 0.0,
                        rates_10y: Optional[float] = None,
                        dollar_dxy: Optional[float] = None) -> Optional[int]:
    """Persist a snapshot from the current MarketState. Returns the row id."""
    try:
        with session_scope() as session:
            row = RegimeEpisodeSnapshot(
                trend=state.trend, trend_phase=state.trend_phase,
                volatility=state.volatility, vol_phase=state.vol_phase,
                gamma=state.gamma, risk=state.risk,
                equities=state.equities, yields=state.yields,
                dollar=state.dollar, label=state.label,
                vix=float(state.vix or 0.0),
                iv_rank=float(state.iv_rank or 0.0),
                breadth_score=float(breadth_score or 0.0),
                sentiment_score=float(sentiment_score or 0.0),
                sector_strength=float(sector_strength or 0.0),
                rates_10y=rates_10y, dollar_dxy=dollar_dxy,
            )
            session.add(row); session.flush()
            return int(row.id)
    except Exception:
        logger.debug("snapshot_current failed", exc_info=True)
        return None


# ── similarity scoring ──────────────────────────────────────────────────


def _cat_score(target: Dict[str, Any], row: Dict[str, Any]) -> float:
    matches = sum(1 for ax in _CAT_AXES if target.get(ax) == row.get(ax))
    return 0.45 * (matches / len(_CAT_AXES))


def _num_score(target: Dict[str, Any], row: Dict[str, Any]) -> float:
    diffs: List[float] = []
    for ax in _NUM_AXES:
        a = target.get(ax)
        b = row.get(ax)
        if a is None or b is None:
            continue
        try:
            a = float(a); b = float(b)
        except Exception:
            continue
        mag = max(abs(a), abs(b), 1.0)
        diffs.append(abs(a - b) / mag)
    if not diffs:
        return 0.0
    return 0.55 * (1.0 - sum(diffs) / len(diffs))


# Stage-15 — switch to the NumPy vectorized scan once the corpus crosses
# this many rows. The per-row Python loop is fine for small corpora but
# becomes a hot spot when snapshots accumulate every 15 minutes for weeks.
_VECTORIZE_THRESHOLD = 200


def _vectorized_scores(target: Dict[str, Any],
                          snap: List[Dict[str, Any]]) -> "Any":
    """Compute similarity for every row in ``snap`` as a single NumPy
    operation. Returns a 1-D array of scores aligned with ``snap``."""
    import numpy as np
    n = len(snap)

    # Categorical match — 1 per axis matched, summed.
    cat_acc = np.zeros(n, dtype=np.float32)
    for ax in _CAT_AXES:
        t = target.get(ax)
        col = np.array([r.get(ax) for r in snap], dtype=object)
        cat_acc += (col == t).astype(np.float32)
    cat_score = (0.45 / len(_CAT_AXES)) * cat_acc

    # Numeric L1 — average normalized distance over the shared axes.
    num_acc = np.zeros(n, dtype=np.float32)
    num_n = np.zeros(n, dtype=np.float32)
    for ax in _NUM_AXES:
        t = target.get(ax)
        if t is None:
            continue
        try:
            tv = float(t)
        except Exception:
            continue
        col = np.array(
            [(float(r[ax]) if r.get(ax) is not None else np.nan) for r in snap],
            dtype=np.float32,
        )
        mask = ~np.isnan(col)
        diff = np.zeros(n, dtype=np.float32)
        mag = np.maximum.reduce([np.abs(col), np.full(n, abs(tv), dtype=np.float32),
                                    np.ones(n, dtype=np.float32)])
        diff[mask] = np.abs(col[mask] - tv) / mag[mask]
        num_acc[mask] += diff[mask]
        num_n[mask] += 1
    num_score = np.zeros(n, dtype=np.float32)
    has_any = num_n > 0
    num_score[has_any] = 0.55 * (1.0 - num_acc[has_any] / num_n[has_any])
    return (cat_score + num_score).tolist()


def find_similar(target: Dict[str, Any],
                    *,
                    k: int = 20,
                    min_similarity: float = 0.50,
                    limit: int = 5000) -> List[RegimeMatch]:
    """Find the K most-similar historical regime snapshots. ``target`` is a
    dict of (categorical + numeric) regime fields — call ``to_dict()`` on
    a ``MarketState`` and pass that. Numeric extras (breadth_score, etc.)
    are tolerated as additional keys.

    Stage-15: auto-switches to a NumPy vectorized scan once the candidate
    set exceeds ``_VECTORIZE_THRESHOLD`` rows. Identical scores to the
    Python loop (verified by paired tests)."""
    try:
        with session_scope() as session:
            rows = list(session.execute(
                select(RegimeEpisodeSnapshot)
                .order_by(desc(RegimeEpisodeSnapshot.timestamp))
                .limit(limit)
            ).scalars().all())
            snap = [r.to_dict() for r in rows]
    except Exception:
        logger.debug("find_similar load failed", exc_info=True)
        return []

    use_vectorized = len(snap) >= _VECTORIZE_THRESHOLD
    if use_vectorized:
        try:
            sims = _vectorized_scores(target, snap)
        except Exception:
            logger.debug("vectorized scoring failed; falling back to loop",
                          exc_info=True)
            use_vectorized = False

    scored: List[RegimeMatch] = []
    for i, row in enumerate(snap):
        if use_vectorized:
            sim = float(sims[i])
        else:
            sim = _cat_score(target, row) + _num_score(target, row)
        if sim < min_similarity:
            continue
        wr = None
        if (row.get("fwd_trades_count") or 0) > 0:
            wr = round((row["fwd_trades_wins"] or 0)
                        / row["fwd_trades_count"], 3)
        scored.append(RegimeMatch(
            snapshot_id=row["id"], timestamp=row["timestamp"],
            similarity=round(sim, 3), label=row["label"],
            snapshot=row,
            fwd_1d_return=row.get("fwd_1d_return"),
            fwd_5d_return=row.get("fwd_5d_return"),
            fwd_trades_count=int(row.get("fwd_trades_count") or 0),
            fwd_trades_win_rate=wr,
            fwd_trades_pnl=float(row.get("fwd_trades_pnl") or 0.0),
        ))
    scored.sort(key=lambda m: -m.similarity)
    return scored[:k]


def aggregate_outcomes(matches: List[RegimeMatch]) -> Dict[str, Any]:
    """Roll-up across matches — what historically happened in regimes like
    this? Returns mean forward return, win-rate, sample size."""
    n = len(matches)
    if n == 0:
        return {"matches": 0, "mean_fwd_1d": None, "mean_fwd_5d": None,
                  "trades_count": 0, "win_rate": None, "total_pnl": 0.0}
    fwd1 = [m.fwd_1d_return for m in matches if m.fwd_1d_return is not None]
    fwd5 = [m.fwd_5d_return for m in matches if m.fwd_5d_return is not None]
    trades = sum(m.fwd_trades_count for m in matches)
    wins = sum(int(round((m.fwd_trades_win_rate or 0) * m.fwd_trades_count))
                  for m in matches)
    pnl = sum(m.fwd_trades_pnl for m in matches)
    return {
        "matches": n,
        "mean_fwd_1d": (round(sum(fwd1) / len(fwd1), 4) if fwd1 else None),
        "mean_fwd_5d": (round(sum(fwd5) / len(fwd5), 4) if fwd5 else None),
        "trades_count": trades,
        "win_rate": (round(wins / trades, 3) if trades else None),
        "total_pnl": round(pnl, 2),
    }
