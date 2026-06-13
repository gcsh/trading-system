"""Stage-7 P&L attribution + explainability.

Two surfaces:

  • ``attribution_by_*`` — slice realized P&L by strategy / regime / grade
    so the user can see WHERE the edge (or loss) is coming from. The numbers
    are computed from the same labels Stage 1 uses, so Sharpe / win-rate
    cohort numbers tie out exactly.

  • ``explain_trade(trade_id)`` — the *full why* for a single trade,
    composed from existing data: trade row + decision row + execution
    telemetry + analytics components. Returns a structured rationale dict
    that the UI renders as a clean human-readable card.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from backend.bot.labeling import TradeLabel, build_labels
from backend.bot.metrics import expectancy, profit_factor, win_rate
from backend.db import session_scope
from backend.models.decision_log import DecisionLog
from backend.models.execution_log import ExecutionLog
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


@dataclass
class AttributionBucket:
    key: str
    closed: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    win_rate: Optional[float] = None
    expectancy: Optional[float] = None
    profit_factor: Optional[Any] = None       # "inf" sentinel or float
    pnl_contribution_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _load_labels(limit: int = 5000) -> List[TradeLabel]:
    """P1.2 — strategy/source attribution feeds capital weighting; both
    Trade + DecisionLog halves must exclude the synthetic corpus."""
    with session_scope() as session:
        trade_rows = session.execute(
            select(Trade)
            .where(Trade.status != "closed_by_reset")
            .where(Trade.signal_source != "historical_replay")
            .order_by(desc(Trade.timestamp)).limit(limit)
        ).scalars().all()
        trades = [r.to_dict() for r in trade_rows]
        decision_rows = session.execute(
            select(DecisionLog)
            .where(DecisionLog.signal_source != "historical_replay")
            .order_by(desc(DecisionLog.timestamp)).limit(limit)
        ).scalars().all()
        decisions = [r.to_dict() for r in decision_rows]
    return build_labels(trades, decisions)


def _bucket(labels: List[TradeLabel], key_fn) -> List[AttributionBucket]:
    buckets: Dict[str, List[TradeLabel]] = defaultdict(list)
    for l in labels:
        buckets[str(key_fn(l) or "—")].append(l)

    total_pnl = sum(l.pnl for l in labels if l.pnl is not None)

    out: List[AttributionBucket] = []
    for key, items in buckets.items():
        pnls = [l.pnl for l in items if l.pnl is not None]
        if not pnls:
            out.append(AttributionBucket(key=key))
            continue
        pf = profit_factor(pnls)
        bucket_total = round(sum(pnls), 2)
        out.append(AttributionBucket(
            key=key,
            closed=len(pnls),
            wins=sum(1 for p in pnls if p > 0),
            losses=sum(1 for p in pnls if p < 0),
            total_pnl=bucket_total,
            win_rate=win_rate(pnls),
            expectancy=expectancy(pnls),
            profit_factor=("inf" if pf == float("inf") else pf),
            pnl_contribution_pct=(round(bucket_total / total_pnl, 4)
                                    if total_pnl else None),
        ))
    out.sort(key=lambda b: abs(b.total_pnl), reverse=True)
    return out


def attribution_by_strategy(limit: int = 5000) -> List[Dict[str, Any]]:
    return [b.to_dict() for b in _bucket(_load_labels(limit), lambda l: l.strategy)]


def attribution_by_regime(limit: int = 5000) -> List[Dict[str, Any]]:
    return [b.to_dict() for b in _bucket(_load_labels(limit), lambda l: l.regime_trend)]


def attribution_by_grade(limit: int = 5000) -> List[Dict[str, Any]]:
    return [b.to_dict() for b in _bucket(_load_labels(limit), lambda l: l.grade)]


# ── per-trade explanation ────────────────────────────────────────────────


@dataclass
class Explanation:
    trade_id: int
    headline: str                       # one-sentence summary
    why: List[str] = field(default_factory=list)   # bullet rationale
    decision_context: Dict[str, Any] = field(default_factory=dict)
    execution_quality: Dict[str, Any] = field(default_factory=dict)
    outcome: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _grade_label(grade: str) -> str:
    table = {"A+": "exceptional setup", "A": "high-quality setup",
              "B": "decent setup", "C": "marginal setup", "D": "weak setup"}
    return table.get(grade, "unranked setup")


def explain_trade(trade_id: int) -> Optional[Explanation]:
    """Compose the explanation from existing artefacts. Returns None when
    the trade ID isn't found — caller maps to 404."""
    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            return None
        trade_dict = trade.to_dict()
        decision = session.execute(
            select(DecisionLog).where(DecisionLog.trade_id == trade_id)
        ).scalar_one_or_none()
        decision_dict = decision.to_dict() if decision else {}
        features = {}
        if decision:
            try:
                features = json.loads(decision.features_json or "{}")
            except Exception:
                features = {}
        execution = session.execute(
            select(ExecutionLog).where(ExecutionLog.trade_id == trade_id)
        ).scalar_one_or_none()
        execution_dict = ({
            "expected_price": execution.expected_price,
            "fill_price": execution.fill_price,
            "slippage_bps": execution.slippage_bps,
            "is_adverse": execution.is_adverse,
            "side": execution.side,
        } if execution else {})

    why: List[str] = []
    if trade_dict.get("reason"):
        why.append(f"signal reason: {trade_dict['reason']}")
    if decision_dict.get("regime_label"):
        why.append(f"regime at signal time: {decision_dict['regime_label']}")
    if decision_dict.get("grade"):
        why.append(f"ranker grade: {decision_dict['grade']} "
                    f"({_grade_label(decision_dict['grade'])})")
    if decision_dict.get("win_probability") is not None:
        why.append(f"calibrated win probability: "
                    f"{float(decision_dict['win_probability']) * 100:.0f}%")
    for fname, fval in features.items():
        if fname in ("composite_bias", "pinning_probability",
                       "hedging_pressure", "dominant_wall") and fval is not None:
            why.append(f"feature {fname} = {fval}")

    grade = decision_dict.get("grade") or "—"
    headline = (f"{trade_dict.get('action','')} {trade_dict.get('ticker','')} "
                  f"({_grade_label(grade)})")

    outcome = {
        "status": trade_dict.get("status"),
        "pnl": trade_dict.get("pnl"),
        "instrument": trade_dict.get("instrument"),
    }

    return Explanation(
        trade_id=int(trade_dict.get("id") or 0),
        headline=headline,
        why=why,
        decision_context={
            "regime_trend": decision_dict.get("regime_trend"),
            "regime_volatility": decision_dict.get("regime_volatility"),
            "regime_gamma": decision_dict.get("regime_gamma"),
            "grade": decision_dict.get("grade"),
            "win_probability": decision_dict.get("win_probability"),
            "features": features,
        },
        execution_quality=execution_dict,
        outcome=outcome,
    )
