"""Feedback Learning Loop.

Persists every actionable engine decision (with its analytical context) and
matches realized outcomes back to the decision when positions close. The
``insights`` aggregator turns those rows into the questions a quant actually
cares about: which strategies make money in which regimes, at which grade.

Never raises into the engine — telemetry must not break the trader.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.decision_log import DecisionLog

logger = logging.getLogger(__name__)


def log_decision(event: Dict[str, Any]) -> Optional[int]:
    """Persist one engine event as a DecisionLog row. Returns the row id or None."""
    try:
        analytics = event.get("analytics") or {}
        regime = (analytics.get("regime") or {})
        prob = (analytics.get("probability") or {})
        rank = (analytics.get("rank") or {})
        feats = analytics.get("features") or {}
        with session_scope() as session:
            # P1.1 — derive signal_source from the event's strategy/source
            # so downstream analytics can cleanly filter synthetic vs live.
            strategy_name = str(event.get("strategy") or "")
            if strategy_name == "ai_brain":
                src = "ai_brain"
            elif strategy_name == "exit_manager":
                src = "exit_manager"
            else:
                src = "live_engine"
            row = DecisionLog(
                ticker=(event.get("ticker") or "").upper(),
                action=str(event.get("action") or ""),
                strategy=strategy_name,
                confidence=float(event.get("confidence") or 0.0),
                status=str(event.get("status") or "signal_only"),
                regime_trend=str(regime.get("trend") or "unknown"),
                regime_volatility=str(regime.get("volatility") or "normal"),
                regime_gamma=str(regime.get("gamma") or "unknown"),
                regime_label=str(regime.get("label") or ""),
                grade=str(rank.get("grade") or ""),
                win_probability=prob.get("probability"),
                trade_id=event.get("trade_id"),
                features_json=json.dumps(feats) if feats else None,
                signal_source=src,
            )
            session.add(row)
            session.flush()
            return int(row.id)
    except Exception:
        logger.debug("log_decision failed", exc_info=True)
        return None


def record_outcome(ticker: str, pnl: float, outcome_status: str = "closed") -> bool:
    """Match the most recent open submitted decision for ``ticker`` and write its
    realized P&L. Best-effort — silently does nothing if no candidate is found."""
    try:
        with session_scope() as session:
            row = session.execute(
                select(DecisionLog)
                .where(
                    DecisionLog.ticker == ticker.upper(),
                    DecisionLog.outcome_status.is_(None),
                    DecisionLog.status == "submitted",
                    # P1.2 — never match a synthetic decision to a live outcome.
                    DecisionLog.signal_source != "historical_replay",
                )
                .order_by(desc(DecisionLog.timestamp))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return False
            row.outcome_pnl = float(pnl)
            row.outcome_status = outcome_status
            return True
    except Exception:
        logger.debug("record_outcome failed for %s", ticker, exc_info=True)
        return False


def _bucket(rows: List[DecisionLog], key) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, float]] = defaultdict(lambda: {
        "count": 0, "submitted": 0, "closed": 0,
        "wins": 0, "losses": 0, "total_pnl": 0.0, "avg_prob": 0.0, "_prob_sum": 0.0,
    })
    for r in rows:
        k = str(key(r) or "—")
        b = out[k]
        b["count"] += 1
        if r.status == "submitted":
            b["submitted"] += 1
        if r.outcome_pnl is not None:
            b["closed"] += 1
            b["total_pnl"] += float(r.outcome_pnl)
            if r.outcome_pnl > 0:
                b["wins"] += 1
            elif r.outcome_pnl < 0:
                b["losses"] += 1
        if r.win_probability is not None:
            b["_prob_sum"] += float(r.win_probability)
    summary: Dict[str, Dict[str, Any]] = {}
    for k, b in out.items():
        closed = b["closed"] or 1
        win_rate = b["wins"] / closed if b["closed"] else 0.0
        avg_prob = b["_prob_sum"] / b["count"] if b["count"] else 0.0
        summary[k] = {
            "count": b["count"], "submitted": b["submitted"], "closed": b["closed"],
            "wins": b["wins"], "losses": b["losses"],
            "win_rate": round(win_rate, 3),
            "total_pnl": round(b["total_pnl"], 2),
            "avg_predicted_probability": round(avg_prob, 3),
        }
    return summary


def insights(limit: int = 1000) -> Dict[str, Any]:
    """Aggregate the last ``limit`` decisions into per-strategy / per-regime /
    per-grade buckets, and surface any combos that look weak.

    P1.2 — live insights surface excludes synthetic decisions so the
    "failing combos" callout reflects real trading, not backfill."""
    try:
        with session_scope() as session:
            rows = list(session.execute(
                select(DecisionLog)
                .where(DecisionLog.signal_source != "historical_replay")
                .order_by(desc(DecisionLog.timestamp))
                .limit(limit)
            ).scalars().all())
            by_strategy = _bucket(rows, lambda r: r.strategy or "unknown")
            by_regime   = _bucket(rows, lambda r: r.regime_trend or "unknown")
            by_grade    = _bucket(rows, lambda r: r.grade or "—")
            by_combo    = _bucket(rows, lambda r: f"{r.strategy or '?'}::{r.regime_trend or '?'}")

        # Failing combos: ≥5 closed trades and win-rate < 40%.
        failing = [
            {"combo": combo, **stats}
            for combo, stats in by_combo.items()
            if stats["closed"] >= 5 and stats["win_rate"] < 0.40
        ]
        failing.sort(key=lambda x: x["total_pnl"])

        return {
            "decisions_analyzed": len(rows),
            "by_strategy": by_strategy,
            "by_regime": by_regime,
            "by_grade": by_grade,
            "failing_combos": failing,
        }
    except Exception:
        logger.debug("insights failed", exc_info=True)
        return {
            "decisions_analyzed": 0, "by_strategy": {}, "by_regime": {},
            "by_grade": {}, "failing_combos": [],
        }
