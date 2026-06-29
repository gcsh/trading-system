"""Stage-11.2 Decision Lineage — reconstruct the full decision chain.

For any trade we can rebuild what the bot was thinking at every gate:

  signal → snapshot → regime → features → confluence → probability → rank
        → abstain → min_grade_tightened → meta_ai → risk → audit
        → execution → outcome → autopsy → cohort → memo

The lineage payload is a flat dict keyed by stage; each stage is either a
sub-dict of fields or ``None`` when that gate did not run / not persisted.

This module is **read-only** — it never writes. It pulls from:
  • ``Trade.detail_json`` (signal, snapshot, analytics, abstain, meta,
    portfolio_risk, min_grade_tightened, risk_decision, audit_violations,
    memo, legs, metadata, ai_components — persisted by ``_persist_trade``)
  • ``DecisionLog`` (regime/grade/probability/features for legacy rows
    whose detail_json predates Stage-11.2 widening)
  • ``ExecutionLog`` (fill slippage, expected vs. realized price)
  • ``autopsy_trade(id)`` (counterfactual analysis when the trade is closed)
  • ``cohort_win_rate(strategy, regime)`` (cohort baseline for context)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.decision_log import DecisionLog
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


def _load_detail(detail_json: Optional[str]) -> Dict[str, Any]:
    if not detail_json:
        return {}
    try:
        d = json.loads(detail_json)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _load_decision_log_row(trade_id: int) -> Optional[DecisionLog]:
    try:
        with session_scope() as session:
            row = session.execute(
                select(DecisionLog)
                .where(DecisionLog.trade_id == trade_id)
                .order_by(desc(DecisionLog.timestamp))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            # Detach the row from the session so we can return it safely.
            session.expunge(row)
            return row
    except Exception:
        logger.debug("decision_log lookup failed for %s", trade_id, exc_info=True)
        return None


def _execution_log_row(trade_id: int) -> Optional[Dict[str, Any]]:
    """Best-effort: ExecutionLog rows aren't strictly required for lineage."""
    try:
        from backend.models.execution_log import ExecutionLog  # type: ignore

        with session_scope() as session:
            row = session.execute(
                select(ExecutionLog)
                .where(ExecutionLog.trade_id == trade_id)
                .order_by(desc(ExecutionLog.timestamp))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "expected_price": getattr(row, "expected_price", None),
                "fill_price": getattr(row, "fill_price", None),
                "slippage_bps": getattr(row, "slippage_bps", None),
                "spread_bps": getattr(row, "spread_bps", None),
                "side": getattr(row, "side", None),
                "timestamp": (row.timestamp.isoformat()
                                if getattr(row, "timestamp", None) else None),
            }
    except Exception:
        logger.debug("execution_log lookup failed for %s", trade_id, exc_info=True)
        return None


def _autopsy_dict(trade_id: int) -> Optional[Dict[str, Any]]:
    try:
        from backend.bot.autopsy import autopsy_trade

        out = autopsy_trade(trade_id)
        if out is None:
            return None
        return out.to_dict() if hasattr(out, "to_dict") else dict(out)
    except Exception:
        logger.debug("autopsy lookup failed for %s", trade_id, exc_info=True)
        return None


def _cohort_context(strategy: Optional[str], regime: Optional[str]) -> Optional[Dict[str, Any]]:
    if not strategy or not regime:
        return None
    try:
        from backend.bot.cohort_matrix import cohort_win_rate

        win_rate, closed = cohort_win_rate(strategy, regime)
        return {
            "strategy": strategy,
            "regime": regime,
            "win_rate": win_rate,
            "closed_count": closed,
        }
    except Exception:
        logger.debug("cohort lookup failed for %s/%s", strategy, regime, exc_info=True)
        return None


def build_lineage(trade_id: int) -> Optional[Dict[str, Any]]:
    """Reconstruct the full decision chain for ``trade_id``.

    Returns ``None`` if the trade does not exist. Each stage in the payload
    is either a dict of fields or ``None`` (when that gate did not fire,
    has no data, or the trade predates Stage-11 widening).
    """
    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            return None
        detail = _load_detail(trade.detail_json)
        trade_row = {
            "id": trade.id,
            "ticker": trade.ticker,
            "action": trade.action,
            "strategy": trade.strategy,
            "timestamp": trade.timestamp.isoformat() if trade.timestamp else None,
            "quantity": trade.quantity,
            "price": trade.price,
            "pnl": trade.pnl,
            "status": trade.status,
            "instrument": trade.instrument,
            "option_type": trade.option_type,
            "strike": trade.strike,
            "expiration": trade.expiration,
            "stop_loss_price": trade.stop_loss_price,
            "take_profit_price": trade.take_profit_price,
        }

    decision_row = _load_decision_log_row(trade_id)
    decision_features: Optional[Dict[str, Any]] = None
    if decision_row and decision_row.features_json:
        try:
            decision_features = json.loads(decision_row.features_json)
        except Exception:
            decision_features = None

    analytics = detail.get("analytics") or {}
    regime = analytics.get("regime") or (
        {
            "trend": decision_row.regime_trend,
            "volatility": decision_row.regime_volatility,
            "gamma": decision_row.regime_gamma,
            "label": decision_row.regime_label,
        } if decision_row else None
    )
    probability = analytics.get("probability") or (
        {"probability": decision_row.win_probability,
          "direction": "LONG" if (trade_row["action"] or "").startswith("BUY") else "SHORT"}
        if decision_row else None
    )
    rank = analytics.get("rank") or (
        {"grade": decision_row.grade} if decision_row and decision_row.grade else None
    )
    features = analytics.get("features") or decision_features
    confluence = analytics.get("confluence")

    signal_stage = {
        "strategy": trade_row["strategy"],
        "action": trade_row["action"],
        "reason": detail.get("signal_reason"),
        "confidence_num": detail.get("confidence"),
        "stop_loss_pct": detail.get("stop_loss_pct"),
        "take_profit_pct": detail.get("take_profit_pct"),
        "dte": detail.get("dte"),
        "ai_components": detail.get("ai_components") or (
            detail.get("metadata") or {}).get("ai_components"),
    }

    execution = _execution_log_row(trade_id)
    # Even when ExecutionLog has no row, surface what the Trade row knows.
    execution_summary = {
        "fill_price": trade_row["price"],
        "quantity": trade_row["quantity"],
        "instrument": trade_row["instrument"],
        "option_type": trade_row["option_type"],
        "strike": trade_row["strike"],
        "expiration": trade_row["expiration"],
        "stop_loss_price": trade_row["stop_loss_price"],
        "take_profit_price": trade_row["take_profit_price"],
        "legs": detail.get("legs"),
    }
    if execution:
        execution_summary.update(
            {k: v for k, v in execution.items() if v is not None}
        )

    outcome = None
    if trade_row["pnl"] is not None or (trade_row["status"] or "") != "open":
        outcome = {
            "status": trade_row["status"],
            "pnl": trade_row["pnl"],
        }
        if decision_row is not None:
            outcome["outcome_status"] = decision_row.outcome_status
            if decision_row.outcome_pnl is not None and outcome["pnl"] is None:
                outcome["pnl"] = decision_row.outcome_pnl

    payload = {
        "trade_id": trade_id,
        "ticker": trade_row["ticker"],
        "action": trade_row["action"],
        "timestamp": trade_row["timestamp"],
        "stages": {
            "signal": signal_stage,
            "snapshot": detail.get("snapshot") or None,
            "regime": regime,
            "features": features,
            "confluence": confluence,
            "probability": probability,
            "rank": rank,
            "abstain": detail.get("abstain"),
            "min_grade_tightened": detail.get("min_grade_tightened"),
            "meta_ai": detail.get("meta"),
            "portfolio_risk": detail.get("portfolio_risk"),
            "risk": ({"reason": detail.get("risk_decision")}
                       if detail.get("risk_decision") else None),
            "audit": ({"violations": detail.get("audit_violations")}
                        if detail.get("audit_violations") else None),
            "market_state": detail.get("market_state"),
            "consensus": detail.get("consensus"),
            "memory": detail.get("memory"),
            "execution": execution_summary,
            "outcome": outcome,
            "autopsy": _autopsy_dict(trade_id),
            "cohort": _cohort_context(trade_row["strategy"],
                                          (regime or {}).get("trend")),
            "memo": detail.get("memo"),
        },
    }
    return payload
