"""Stage-9 Loss Autopsy — counterfactual per-trade analysis.

For every losing trade, produce a structured bundle answering:

  • What was the regime + features + grade + execution cost at signal time?
  • Why did it lose — exit reason (stop, expiry, ...) + holding window
  • **What single change would have flipped this from a loss to a pass?**
    e.g. "event-hold was inactive but triggered 30 min later",
         "spread was 80 bps — over the abstain threshold",
         "Kelly fraction would have capped at $200, but $1000 was sent"
  • Avoidable vs variance tag — heuristic classifier

The autopsy is built from data already persisted by earlier stages
(Trade + DecisionLog + ExecutionLog) so it stays additive — no schema bump.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.db import session_scope
from backend.models.decision_log import DecisionLog
from backend.models.execution_log import ExecutionLog
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


@dataclass
class FlipHypothesis:
    """One counterfactual — "if X had been Y, would the trade have been
    blocked or sized differently?"."""
    name: str
    triggered: bool
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LossAutopsy:
    trade_id: int
    ticker: str
    strategy: str
    action: str
    pnl: float
    pnl_pct: Optional[float]
    holding_minutes: Optional[int]
    exit_reason: Optional[str]
    grade: Optional[str]
    win_probability: Optional[float]
    regime_label: Optional[str]
    execution_quality: Dict[str, Any] = field(default_factory=dict)
    flip_hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    avoidable_tag: str = "variance"          # "avoidable" | "variance" | "mixed"
    avoidable_score: float = 0.0             # 0–1 confidence in tag
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── counterfactual hypotheses ────────────────────────────────────────────


def _flip_event_hold(decision: Dict[str, Any], trade: Dict[str, Any]
                       ) -> FlipHypothesis:
    """Was an event-risk window active for the trade's ticker around entry
    time? If yes, event-hold should have blocked it."""
    # Lazy lookup — heavy import, only when called.
    try:
        from backend.bot.event_risk import can_trade
    except Exception:
        return FlipHypothesis("event_hold",
                                  triggered=False,
                                  detail="event_risk module unavailable")
    try:
        ts = trade.get("timestamp") or ""
        when = datetime.fromisoformat(ts) if ts else None
    except Exception:
        when = None
    if not when:
        return FlipHypothesis("event_hold", False, "no timestamp on trade")
    perm = can_trade(trade.get("ticker") or "", now=when)
    if not perm.can_trade:
        return FlipHypothesis(
            "event_hold", triggered=True,
            detail=f"event-hold WOULD have blocked at entry: {perm.reason[:120]}",
        )
    return FlipHypothesis("event_hold", False, "no event window at entry")


def _flip_abstain_band(decision: Dict[str, Any]) -> FlipHypothesis:
    """Was calibrated probability in the no-trade band?"""
    from backend.bot.abstain import in_no_trade_band
    p = decision.get("win_probability")
    if p is None:
        return FlipHypothesis("abstain_band", False,
                                  "no win probability on decision")
    if in_no_trade_band(float(p)):
        return FlipHypothesis(
            "abstain_band", triggered=True,
            detail=f"p={float(p):.2f} fell in no-trade band [0.50, 0.58] — "
                     f"Stage-9 abstain would have rejected",
        )
    return FlipHypothesis("abstain_band", False,
                              f"p={float(p):.2f} outside no-trade band")


def _flip_spread_too_wide(execution: Dict[str, Any]) -> FlipHypothesis:
    """Was the realized slippage > threshold? — a too-wide spread / illiquid
    fill is a frequent loss driver."""
    bps = execution.get("slippage_bps")
    if bps is None:
        return FlipHypothesis("spread_too_wide", False,
                                  "no execution telemetry recorded")
    if abs(float(bps)) > 50.0:        # 50 bps — institutional rule of thumb
        return FlipHypothesis(
            "spread_too_wide", triggered=True,
            detail=f"realized slippage {bps:.1f} bps > 50 bps threshold",
        )
    return FlipHypothesis("spread_too_wide", False,
                              f"slippage {bps:.1f} bps within band")


def _flip_low_grade(decision: Dict[str, Any]) -> FlipHypothesis:
    grade = decision.get("grade") or ""
    if grade in ("C", "D", "Reject"):
        return FlipHypothesis(
            "low_grade", triggered=True,
            detail=f"ranker grade '{grade}' — raising min_grade to B+ would have rejected",
        )
    return FlipHypothesis("low_grade", False, f"grade '{grade}' clear")


def _flip_kelly_oversize(trade: Dict[str, Any]) -> FlipHypothesis:
    """Was the position size clearly larger than a Kelly cap would have
    allowed?  Without per-trade Kelly state we approximate from notional."""
    qty = float(trade.get("quantity") or 0)
    price = float(trade.get("price") or 0)
    notional = qty * price
    # heuristic — if a single trade exceeded $1000 in a $5000 trial, Kelly
    # would have capped it
    if notional > 1000:
        return FlipHypothesis(
            "kelly_oversize", triggered=True,
            detail=f"notional ${notional:.0f} > $1000; "
                     f"Stage-6 Kelly cap would have shrunk to ~$200-$300",
        )
    return FlipHypothesis("kelly_oversize", False,
                              f"notional ${notional:.0f} within sensible band")


# ── classifier ───────────────────────────────────────────────────────────


def _classify(hypotheses: List[FlipHypothesis]) -> tuple[str, float, str]:
    """Pick avoidable vs variance based on how many hypotheses fired.

    Returns (tag, confidence, summary_text).
    """
    triggers = [h for h in hypotheses if h.triggered]
    n = len(triggers)
    if n == 0:
        return ("variance", 0.0,
                "no Stage-9 gate would have flipped this; outcome is variance")
    if n == 1:
        return ("mixed", 0.5,
                f"single counterfactual fired: {triggers[0].name}")
    return ("avoidable", min(1.0, 0.5 + 0.15 * n),
            f"{n} counterfactual gates would have caught this — "
            f"{', '.join(t.name for t in triggers)}")


# ── public API ───────────────────────────────────────────────────────────


def autopsy_trade(trade_id: int) -> Optional[LossAutopsy]:
    """Compose the autopsy bundle. Returns None when the trade isn't a loss
    or the row doesn't exist — callers translate to 404 or 200{loss=False}."""
    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            return None
        trade_dict = trade.to_dict()
        decision_row = session.execute(
            select(DecisionLog).where(DecisionLog.trade_id == trade_id)
        ).scalar_one_or_none()
        decision = decision_row.to_dict() if decision_row else {}
        features = {}
        if decision_row:
            try:
                features = json.loads(decision_row.features_json or "{}") or {}
            except Exception:
                features = {}
        exec_row = session.execute(
            select(ExecutionLog).where(ExecutionLog.trade_id == trade_id)
        ).scalar_one_or_none()
        execution = ({
            "expected_price": exec_row.expected_price,
            "fill_price": exec_row.fill_price,
            "slippage_bps": exec_row.slippage_bps,
            "is_adverse": exec_row.is_adverse,
            "side": exec_row.side,
        } if exec_row else {})

    pnl = trade_dict.get("pnl")
    if pnl is None or float(pnl) >= 0:
        return None        # not a loss — autopsy not applicable

    hypotheses = [
        _flip_event_hold(decision, trade_dict),
        _flip_abstain_band({**decision, **{"features": features}}),
        _flip_spread_too_wide(execution),
        _flip_low_grade(decision),
        _flip_kelly_oversize(trade_dict),
    ]
    tag, conf, summary = _classify(hypotheses)

    return LossAutopsy(
        trade_id=int(trade_dict.get("id") or 0),
        ticker=str(trade_dict.get("ticker") or ""),
        strategy=str(trade_dict.get("strategy") or ""),
        action=str(trade_dict.get("action") or ""),
        pnl=float(pnl),
        pnl_pct=None,        # filled in by labeling if needed
        holding_minutes=None,
        exit_reason=_exit_reason_from_reason(trade_dict.get("reason") or ""),
        grade=decision.get("grade"),
        win_probability=decision.get("win_probability"),
        regime_label=decision.get("regime_label") or decision.get("regime_trend"),
        execution_quality=execution,
        flip_hypotheses=[h.to_dict() for h in hypotheses],
        avoidable_tag=tag,
        avoidable_score=conf,
        summary=summary,
    )


def _exit_reason_from_reason(reason: str) -> Optional[str]:
    r = (reason or "").lower()
    if "take" in r and "profit" in r: return "take_profit"
    if "stop" in r and "loss" in r:   return "stop_loss"
    if "expiry" in r:                  return "expiry"
    return "manual" if reason else None


# ── batch autopsy (for the Trades-table summary card) ────────────────────


def autopsy_recent_losses(limit: int = 50) -> Dict[str, Any]:
    """Run autopsy over the most recent N losing closed trades. Aggregates
    avoidable vs variance counts so the Cockpit can show "you had 6 losses;
    4 were avoidable" style headlines."""
    with session_scope() as session:
        rows = session.execute(
            select(Trade).where(Trade.pnl < 0, Trade.status == "closed")
            .order_by(Trade.timestamp.desc()).limit(limit)
        ).scalars().all()
        ids = [int(r.id) for r in rows]
    autopsies = []
    for tid in ids:
        a = autopsy_trade(tid)
        if a is not None:
            autopsies.append(a.to_dict())
    counts = {"avoidable": 0, "mixed": 0, "variance": 0}
    for a in autopsies:
        counts[a.get("avoidable_tag", "variance")] += 1
    return {
        "n_losses_analyzed": len(autopsies),
        "by_tag": counts,
        "autopsies": autopsies,
    }
