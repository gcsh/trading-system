"""Stage-11.5 Memory Layer — regime episodes + "we've seen this before" recall.

Two surfaces:

  • ``build_episodes(limit)`` walks ``DecisionLog`` chronologically and groups
    consecutive rows with the same (regime_trend, regime_volatility,
    regime_gamma) tuple into ``RegimeEpisode`` records (start, end, trade
    count, win rate, total P&L, dominant tickers). This is the *historical
    map* — operator can scroll through their past regimes.

  • ``recall_similar(context, k)`` takes a current decision context and
    returns the top-K most-similar past closed trades with their outcomes
    (P&L, win/loss). Similarity is heuristic: matching regime trend +
    volatility + gamma counts most; numeric feature distance breaks ties.

Both helpers are READ-ONLY and lazy — no extra DB writes, no new schema.
The engine attaches the top-3 recall matches to ``detail_json["memory"]``
so lineage + Mission Control surface them for free.

Future (v2 — when corpus grows past ~10k decisions): cache episodes in a
``regime_episodes`` table; for recall, use a vector index over feature
embeddings instead of in-process distance.
"""
from __future__ import annotations

import json
import logging
import math
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.decision_log import DecisionLog
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


# ── data types ───────────────────────────────────────────────────────────


@dataclass
class RegimeEpisode:
    regime_trend: str
    regime_volatility: str
    regime_gamma: str
    regime_label: str
    start: str                     # ISO timestamp
    end: str                       # ISO timestamp
    span_minutes: int
    decisions: int                 # all DecisionLog rows in this episode
    submitted: int                 # of which actually executed
    closed: int                    # closed positions (have outcome_pnl)
    wins: int
    losses: int
    win_rate: Optional[float]
    total_pnl: float
    top_tickers: List[Tuple[str, int]] = field(default_factory=list)
    top_strategies: List[Tuple[str, int]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["top_tickers"] = [{"ticker": t, "count": c} for t, c in self.top_tickers]
        d["top_strategies"] = [{"strategy": s, "count": c} for s, c in self.top_strategies]
        return d


@dataclass
class MemoryMatch:
    trade_id: Optional[int]
    decision_id: int
    similarity: float              # 0-1
    timestamp: str
    ticker: str
    strategy: str
    regime_label: str
    grade: Optional[str]
    win_probability: Optional[float]
    outcome_pnl: Optional[float]
    outcome_status: Optional[str]
    win: Optional[bool]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── episode builder ──────────────────────────────────────────────────────


def _key(row: DecisionLog) -> Tuple[str, str, str]:
    return (
        (row.regime_trend or "unknown"),
        (row.regime_volatility or "normal"),
        (row.regime_gamma or "unknown"),
    )


def _label(trend: str, vol: str, gamma: str) -> str:
    return f"{trend} · {vol}-vol · {gamma}"


def build_episodes(limit: int = 2000) -> List[RegimeEpisode]:
    """Group consecutive DecisionLog rows with the same regime key into
    episodes. Returns newest-first.
    """
    try:
        with session_scope() as session:
            rows = list(session.execute(
                select(DecisionLog)
                # P1.2 — episode memory feeds agents at decision time;
                # synthetic-replay episodes would poison the lookup.
                .where(DecisionLog.signal_source != "historical_replay")
                .order_by(DecisionLog.timestamp.asc())
                .limit(limit)
            ).scalars().all())
            # Eager-load every field while session is open.
            snap = [(r.id, r.timestamp, _key(r), r.ticker, r.strategy, r.status,
                       r.outcome_pnl, r.outcome_status) for r in rows]
    except Exception:
        logger.debug("memory.build_episodes failed", exc_info=True)
        return []

    episodes: List[RegimeEpisode] = []
    cur_key: Optional[Tuple[str, str, str]] = None
    cur: List[Tuple[Any, ...]] = []

    def flush(group: List[Tuple[Any, ...]], key: Tuple[str, str, str]) -> None:
        if not group:
            return
        ts_start = group[0][1]
        ts_end = group[-1][1]
        tickers = Counter(g[3] for g in group if g[3])
        strategies = Counter(g[4] for g in group if g[4])
        submitted = sum(1 for g in group if g[5] == "submitted")
        closed_rows = [g for g in group if g[6] is not None]
        wins = sum(1 for g in closed_rows if g[6] > 0)
        losses = sum(1 for g in closed_rows if g[6] < 0)
        closed = len(closed_rows)
        wr = (wins / closed) if closed else None
        total_pnl = sum(float(g[6] or 0.0) for g in closed_rows)
        episodes.append(RegimeEpisode(
            regime_trend=key[0], regime_volatility=key[1], regime_gamma=key[2],
            regime_label=_label(*key),
            start=ts_start.isoformat() if ts_start else "",
            end=ts_end.isoformat() if ts_end else "",
            span_minutes=int((ts_end - ts_start).total_seconds() // 60)
                if (ts_start and ts_end) else 0,
            decisions=len(group), submitted=submitted, closed=closed,
            wins=wins, losses=losses,
            win_rate=round(wr, 3) if wr is not None else None,
            total_pnl=round(total_pnl, 2),
            top_tickers=tickers.most_common(5),
            top_strategies=strategies.most_common(5),
        ))

    for snap_row in snap:
        k = snap_row[2]
        if cur_key is None or k == cur_key:
            cur.append(snap_row)
            cur_key = k
        else:
            flush(cur, cur_key)
            cur = [snap_row]
            cur_key = k
    flush(cur, cur_key) if cur_key is not None else None
    episodes.reverse()
    return episodes


# ── similarity / recall ──────────────────────────────────────────────────


def _parse_features(s: Optional[str]) -> Dict[str, float]:
    if not s:
        return {}
    try:
        d = json.loads(s)
    except Exception:
        return {}
    out: Dict[str, float] = {}
    for k, v in (d or {}).items():
        if isinstance(v, (int, float)):
            out[k] = float(v)
    return out


# Features used in the numeric distance term. Picked because they're the
# ones the ranker / probability layer leans on most.
_NUM_FEATURES = (
    "rsi_14", "macd_hist", "trend_bias", "adx", "vix", "iv_rank",
    "composite_bias", "flow_bullishness", "pinning_probability",
    "news_sentiment", "volume_ratio",
)


def _numeric_distance(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Mean absolute distance over the shared numeric features, scaled to
    a 0-1 range by per-feature normalization.  Returns 1.0 when nothing
    overlaps (= maximally distant)."""
    diffs = []
    for f in _NUM_FEATURES:
        if f in a and f in b:
            # Most of these live in roughly [-100, 100] (rsi, vix, iv_rank)
            # or [-1, 1] (biases, sentiment). Normalize crudely by the max
            # observed magnitude so each feature contributes comparably.
            mag = max(abs(a[f]), abs(b[f]), 1.0)
            diffs.append(abs(a[f] - b[f]) / mag)
    if not diffs:
        return 1.0
    return sum(diffs) / len(diffs)


def _similarity(target_key: Tuple[str, str, str],
                  target_feats: Dict[str, float],
                  row_key: Tuple[str, str, str],
                  row_feats: Dict[str, float],
                  target_action: Optional[str],
                  row_action: Optional[str]) -> float:
    """Composite similarity score in [0, 1].

    Weighting:
      • Regime key match: 0.55 (trend 0.30 + vol 0.15 + gamma 0.10)
      • Numeric feature distance: 0.30 (inverted)
      • Same direction (long vs short): 0.15
    """
    score = 0.0
    score += 0.30 if target_key[0] == row_key[0] else 0.0
    score += 0.15 if target_key[1] == row_key[1] else 0.0
    score += 0.10 if target_key[2] == row_key[2] else 0.0
    score += 0.30 * (1.0 - _numeric_distance(target_feats, row_feats))
    if target_action and row_action:
        ta = (target_action or "").upper()
        ra = (row_action or "").upper()
        target_long = ta.startswith("BUY") and "PUT" not in ta
        row_long = ra.startswith("BUY") and "PUT" not in ra
        if target_long == row_long:
            score += 0.15
    return round(max(0.0, min(1.0, score)), 3)


def recall_similar(context: Dict[str, Any], *, k: int = 5,
                     min_similarity: float = 0.45,
                     limit: int = 2000) -> List[MemoryMatch]:
    """Find the K past CLOSED trades whose regime + features most resemble
    ``context``. Returns newest-first within tied similarity.

    ``context`` keys consumed:
      ticker, action, analytics.regime (or regime), analytics.features
      (or features). Missing fields are tolerated.
    """
    analytics = context.get("analytics") or {}
    regime = analytics.get("regime") or context.get("regime") or {}
    features = (analytics.get("features") or context.get("features") or {})
    target_key = (
        (regime.get("trend") or "unknown"),
        (regime.get("volatility") or "normal"),
        (regime.get("gamma") or "unknown"),
    )
    target_feats = {k: float(v) for k, v in features.items()
                      if isinstance(v, (int, float))}
    target_action = context.get("action")
    target_trade_id = context.get("trade_id")

    try:
        with session_scope() as session:
            rows = list(session.execute(
                select(DecisionLog)
                .where(DecisionLog.outcome_pnl.is_not(None))
                # P1.2 — similar-trades lookup feeds the live brain;
                # synthetic-replay matches would advise on backtest outcomes.
                .where(DecisionLog.signal_source != "historical_replay")
                .order_by(desc(DecisionLog.timestamp))
                .limit(limit)
            ).scalars().all())
            snap = [(r.id, r.trade_id, r.timestamp, r.ticker, r.strategy or "",
                       _key(r), _parse_features(r.features_json),
                       r.action, r.grade, r.win_probability,
                       r.outcome_pnl, r.outcome_status, _label(*_key(r)))
                      for r in rows]
    except Exception:
        logger.debug("memory.recall_similar failed", exc_info=True)
        return []

    scored: List[Tuple[float, Any]] = []
    for row in snap:
        if target_trade_id and row[1] == target_trade_id:
            continue           # skip the trade itself
        sim = _similarity(target_key, target_feats, row[5], row[6],
                            target_action, row[7])
        if sim < min_similarity:
            continue
        scored.append((sim, row))
    scored.sort(key=lambda x: (-x[0], -x[1][2].timestamp() if hasattr(x[1][2], "timestamp") else 0))
    out: List[MemoryMatch] = []
    for sim, row in scored[:k]:
        pnl = float(row[10] or 0.0)
        out.append(MemoryMatch(
            trade_id=row[1], decision_id=row[0], similarity=sim,
            timestamp=row[2].isoformat() if row[2] else "",
            ticker=row[3], strategy=row[4], regime_label=row[12],
            grade=row[8], win_probability=row[9],
            outcome_pnl=round(pnl, 2),
            outcome_status=row[11],
            win=(pnl > 0) if pnl != 0 else None,
        ))
    return out


def recall_summary(matches: List[MemoryMatch]) -> Dict[str, Any]:
    """Roll-up over a list of matches — counts + aggregate P&L. Cheap to
    surface in the engine + Mission Control."""
    if not matches:
        return {"matches": 0, "wins": 0, "losses": 0, "neutral": 0,
                  "avg_similarity": 0.0, "total_pnl": 0.0,
                  "hit_rate": None}
    wins = sum(1 for m in matches if m.win is True)
    losses = sum(1 for m in matches if m.win is False)
    neutral = sum(1 for m in matches if m.win is None)
    avg_sim = sum(m.similarity for m in matches) / len(matches)
    total_pnl = sum((m.outcome_pnl or 0.0) for m in matches)
    decided = wins + losses
    return {
        "matches": len(matches),
        "wins": wins, "losses": losses, "neutral": neutral,
        "avg_similarity": round(avg_sim, 3),
        "total_pnl": round(total_pnl, 2),
        "hit_rate": (round(wins / decided, 3) if decided else None),
    }
