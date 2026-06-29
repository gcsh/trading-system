"""MITS Phase 5 (P5.1 + P5.2) — EOD-bias loader + outcome reconciler.

Two responsibilities, kept in one module so they share data shape:

  * ``load_eod_bias(date)`` — pull today's top-N EOD analysis rows and
    return them as a ``{ticker: EodBiasRow}`` map keyed by ticker. The
    engine consults this map at cycle start to:
      - promote high-conviction tickers into the priority candidate
        list (even if not in the strategy's default universe);
      - tag any resulting trade with ``signal_source='eod_bias'``;
      - know which suggested action to use as the primary hypothesis.

  * ``reconcile_outcomes(date)`` — nightly 17:00 ET job. For each
    EodAnalysis row, look at the realized trades that day. Write /
    update an EodPredictionOutcome row with status ``traded_matched``,
    ``traded_diverged``, ``not_traded`` (+ skip_reason from DecisionLog),
    or ``pending`` (trade still open).

Both functions are pure-ish — they read from SQLite and write only the
prediction-outcomes table. Idempotent on repeated calls.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date as _date, datetime, time as _time, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.decision_log import DecisionLog
from backend.models.eod_analysis import EodAnalysis
from backend.models.eod_prediction_outcome import (
    EodPredictionOutcome,
    OUTCOME_NOT_TRADED,
    OUTCOME_PENDING,
    OUTCOME_TRADED_DIVERGED,
    OUTCOME_TRADED_MATCHED,
)
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


# Direction → action canon. Mirrors the EodAnalysis.suggested_action
# direction strings so the lookup is symmetric.
LONG_DIRECTIONS = {"long_call", "long_stock"}
SHORT_DIRECTIONS = {"long_put", "short_stock"}


@dataclass
class EodBiasRow:
    """Lightweight projection of an EodAnalysis row, plus the rank
    position. The engine consumes this rather than the ORM row so we
    don't drag a session into the cycle hot-path."""

    ticker: str
    rank: int
    top_pattern: Optional[str] = None
    posterior: Optional[float] = None
    sample_size: Optional[int] = None
    suggested_action: Optional[Dict[str, Any]] = None
    eod_analysis_id: Optional[int] = None
    rank_score: float = 0.0
    headline: Optional[str] = None

    def is_high_conviction(self) -> bool:
        post = float(self.posterior or 0.0)
        n = int(self.sample_size or 0)
        return (
            post >= float(getattr(
                TUNABLES, "eod_high_conviction_posterior", 0.70))
            and n >= int(getattr(
                TUNABLES, "eod_high_conviction_min_samples", 50))
        )

    def is_info_only(self) -> bool:
        post = float(self.posterior or 0.0)
        n = int(self.sample_size or 0)
        return (
            post >= float(getattr(
                TUNABLES, "eod_info_only_posterior", 0.55))
            and n >= int(getattr(
                TUNABLES, "eod_info_only_min_samples", 30))
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "rank": self.rank,
            "top_pattern": self.top_pattern,
            "posterior": self.posterior,
            "sample_size": self.sample_size,
            "suggested_action": self.suggested_action,
            "eod_analysis_id": self.eod_analysis_id,
            "rank_score": self.rank_score,
            "headline": self.headline,
            "high_conviction": self.is_high_conviction(),
            "info_only": self.is_info_only(),
        }


def load_eod_bias(target: Optional[_date] = None,
                    limit: Optional[int] = None) -> Dict[str, EodBiasRow]:
    """Return ``{ticker_upper: EodBiasRow}`` for the EOD pass on ``target``
    (defaults to today UTC). ``limit`` defaults to
    ``TUNABLES.eod_bias_top_n``.

    Always returns a dict (never raises). On empty corpus / missing pass,
    returns ``{}``.
    """
    target = target or datetime.utcnow().date()
    limit = int(limit if limit is not None
                 else getattr(TUNABLES, "eod_bias_top_n", 20))
    out: Dict[str, EodBiasRow] = {}
    try:
        with session_scope() as s:
            rows = s.execute(
                select(EodAnalysis)
                .where(EodAnalysis.analysis_date == target)
                .order_by(desc(EodAnalysis.rank_score))
                .limit(limit)
            ).scalars().all()
            for idx, row in enumerate(rows, start=1):
                try:
                    suggested = (
                        json.loads(row.suggested_action_json)
                        if row.suggested_action_json else None
                    )
                except Exception:
                    suggested = None
                ticker = (row.ticker or "").upper().strip()
                if not ticker:
                    continue
                out[ticker] = EodBiasRow(
                    ticker=ticker, rank=idx,
                    top_pattern=row.top_pattern,
                    posterior=row.top_posterior,
                    sample_size=row.top_sample_size,
                    suggested_action=suggested,
                    eod_analysis_id=row.id,
                    rank_score=float(row.rank_score or 0.0),
                    headline=row.headline,
                )
    except Exception:
        logger.debug("load_eod_bias failed for %s", target, exc_info=True)
    return out


def priority_tickers_from_bias(bias: Dict[str, EodBiasRow]) -> List[str]:
    """Return the subset of biased tickers that clear the high-conviction
    floor — these are promoted into the engine's scan universe even when
    they aren't in the operator's watchlist."""
    return [t for t, row in bias.items() if row.is_high_conviction()]


# ── outcome reconciler ────────────────────────────────────────────────


def _action_to_direction(action: str, instrument: Optional[str] = None
                            ) -> Optional[str]:
    action = (action or "").upper()
    if action == "BUY_CALL":
        return "long_call"
    if action == "BUY_PUT":
        return "long_put"
    if action == "BUY_STOCK":
        return "long_stock"
    if action == "SELL_STOCK":
        return "short_stock"
    return None


def _latest_skip_reason(session, ticker: str,
                          target: _date) -> Optional[str]:
    """Look up the most recent DecisionLog row for ``ticker`` on
    ``target`` with a non-trade outcome and return its reason / status."""
    start = datetime.combine(target, _time.min)
    end = datetime.combine(target, _time.max)
    try:
        row = session.execute(
            select(DecisionLog)
            .where(DecisionLog.ticker == ticker)
            .where(DecisionLog.timestamp >= start)
            .where(DecisionLog.timestamp <= end)
            .where(DecisionLog.signal_source != "historical_replay")
            .order_by(desc(DecisionLog.timestamp))
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        status = row.status or ""
        reason = row.action or ""
        # Status carries the gate name (catalyst_gate, abstain, low_grade,
        # event_hold, etc); fall back to action when status is blank.
        return (status or reason or "").strip()[:240] or None
    except Exception:
        return None


def _find_matching_trade(session, ticker: str, target: _date,
                            predicted_direction: Optional[str]
                            ) -> Optional[Trade]:
    """Return the FIRST trade on ``target`` for ``ticker`` whose direction
    matches the predicted one, falling back to ANY trade if direction
    isn't known. Same-day window."""
    start = datetime.combine(target, _time.min)
    end = datetime.combine(target, _time.max)
    try:
        trades = session.execute(
            select(Trade)
            .where(Trade.ticker == ticker)
            .where(Trade.timestamp >= start)
            .where(Trade.timestamp <= end)
            .order_by(Trade.timestamp.asc())
        ).scalars().all()
    except Exception:
        return None
    if not trades:
        return None
    if predicted_direction is None:
        return trades[0]
    for t in trades:
        d = _action_to_direction(t.action, t.instrument)
        if d == predicted_direction:
            return t
    return trades[0]


def _outcome_for(predicted_direction: Optional[str],
                    trade: Trade) -> str:
    """Map the (predicted direction, actual trade) pair into an outcome."""
    actual_dir = _action_to_direction(trade.action, trade.instrument)
    if trade.status == "open":
        return OUTCOME_PENDING
    if predicted_direction is None or actual_dir is None:
        return OUTCOME_TRADED_MATCHED
    if predicted_direction == actual_dir:
        return OUTCOME_TRADED_MATCHED
    return OUTCOME_TRADED_DIVERGED


def reconcile_outcomes(target: Optional[_date] = None) -> Dict[str, Any]:
    """Walk today's EodAnalysis rows and persist / refresh the matching
    EodPredictionOutcome row. Idempotent — re-running the same day
    refreshes rows in place.

    Returns counts so the scheduler log can report what happened.
    """
    target = target or datetime.utcnow().date()
    stats = {
        "analysis_rows": 0,
        "traded_matched": 0,
        "traded_diverged": 0,
        "not_traded": 0,
        "pending": 0,
        "errors": 0,
    }
    try:
        with session_scope() as s:
            analyses = s.execute(
                select(EodAnalysis)
                .where(EodAnalysis.analysis_date == target)
            ).scalars().all()
            stats["analysis_rows"] = len(analyses)
            for a in analyses:
                try:
                    suggested = (
                        json.loads(a.suggested_action_json)
                        if a.suggested_action_json else None
                    )
                except Exception:
                    suggested = None
                predicted_direction = None
                predicted_strike = None
                predicted_dte = None
                if isinstance(suggested, dict):
                    predicted_direction = suggested.get("direction") or None
                    if not predicted_direction:
                        predicted_direction = _action_to_direction(
                            suggested.get("action") or ""
                        )
                    try:
                        predicted_strike = (
                            float(suggested["strike"])
                            if suggested.get("strike") is not None else None
                        )
                    except (TypeError, ValueError):
                        predicted_strike = None
                    try:
                        predicted_dte = (
                            int(suggested["dte"])
                            if suggested.get("dte") is not None else None
                        )
                    except (TypeError, ValueError):
                        predicted_dte = None

                # Locate / create the outcome row.
                row = s.execute(
                    select(EodPredictionOutcome)
                    .where(EodPredictionOutcome.eod_analysis_id == a.id)
                    .where(EodPredictionOutcome.ticker == a.ticker)
                ).scalar_one_or_none()
                if row is None:
                    row = EodPredictionOutcome(
                        eod_analysis_id=a.id,
                        ticker=a.ticker,
                        analysis_date=a.analysis_date,
                    )
                    s.add(row)
                row.predicted_direction = predicted_direction
                row.predicted_strike = predicted_strike
                row.predicted_dte = predicted_dte
                row.posterior = a.top_posterior
                row.sample_size = a.top_sample_size

                # Find matching trade and classify.
                trade = _find_matching_trade(
                    s, a.ticker, target, predicted_direction,
                )
                if trade is None:
                    row.traded = 0
                    row.trade_id = None
                    row.actual_direction = None
                    row.actual_strike = None
                    row.actual_pnl_pct = None
                    row.actual_pnl_dollars = None
                    row.outcome = OUTCOME_NOT_TRADED
                    row.skip_reason = _latest_skip_reason(
                        s, a.ticker, target,
                    )
                    row.resolved_at = datetime.utcnow()
                    stats["not_traded"] += 1
                    continue
                row.traded = 1
                row.trade_id = trade.id
                row.actual_direction = _action_to_direction(
                    trade.action, trade.instrument,
                )
                row.actual_strike = trade.strike
                if trade.pnl is not None and trade.price:
                    try:
                        row.actual_pnl_dollars = float(trade.pnl)
                        denom = float(trade.price) * float(trade.quantity or 1)
                        row.actual_pnl_pct = (
                            (float(trade.pnl) / denom)
                            if denom else None
                        )
                    except Exception:
                        row.actual_pnl_pct = None
                        row.actual_pnl_dollars = None
                row.outcome = _outcome_for(predicted_direction, trade)
                row.skip_reason = None
                if row.outcome == OUTCOME_PENDING:
                    stats["pending"] += 1
                    row.resolved_at = None
                elif row.outcome == OUTCOME_TRADED_DIVERGED:
                    stats["traded_diverged"] += 1
                    row.resolved_at = datetime.utcnow()
                else:
                    stats["traded_matched"] += 1
                    row.resolved_at = datetime.utcnow()
    except Exception:
        stats["errors"] += 1
        logger.exception("reconcile_outcomes failed for %s", target)
    return stats


# ── lightweight summary used by the /prediction-outcomes/accuracy route ──


def accuracy_window(window: str = "30") -> Dict[str, Any]:
    """Aggregate prediction-vs-outcome stats over a window.

    ``window`` is "7", "30", "all" (or any positive int as string).
    Returns a dict suitable for direct JSON serialization.
    """
    today = datetime.utcnow().date()
    if window == "all":
        start = None
    else:
        try:
            days = int(window)
        except (TypeError, ValueError):
            days = 30
        start = today - timedelta(days=max(1, days))
    counts: Dict[str, int] = {
        OUTCOME_TRADED_MATCHED: 0,
        OUTCOME_TRADED_DIVERGED: 0,
        OUTCOME_NOT_TRADED: 0,
        OUTCOME_PENDING: 0,
    }
    wins = 0
    losses = 0
    total_pnl = 0.0
    high_conviction_total = 0
    high_conviction_traded = 0
    try:
        post_floor = float(getattr(
            TUNABLES, "eod_high_conviction_posterior", 0.70))
        min_n = int(getattr(
            TUNABLES, "eod_high_conviction_min_samples", 50))
        with session_scope() as s:
            stmt = select(EodPredictionOutcome)
            if start is not None:
                stmt = stmt.where(EodPredictionOutcome.analysis_date >= start)
            for r in s.execute(stmt).scalars():
                counts[r.outcome] = counts.get(r.outcome, 0) + 1
                if (r.posterior or 0) >= post_floor and (
                        r.sample_size or 0) >= min_n:
                    high_conviction_total += 1
                    if r.traded:
                        high_conviction_traded += 1
                if r.outcome in (
                    OUTCOME_TRADED_MATCHED, OUTCOME_TRADED_DIVERGED,
                ):
                    if r.actual_pnl_dollars is not None:
                        total_pnl += float(r.actual_pnl_dollars)
                        if r.actual_pnl_dollars > 0:
                            wins += 1
                        elif r.actual_pnl_dollars < 0:
                            losses += 1
    except Exception:
        logger.debug("accuracy_window failed", exc_info=True)
    closed = wins + losses
    win_rate = (wins / closed) if closed else None
    conviction_act_rate = (
        (high_conviction_traded / high_conviction_total)
        if high_conviction_total else None
    )
    return {
        "window": window,
        "as_of": today.isoformat(),
        "counts": counts,
        "high_conviction_total": high_conviction_total,
        "high_conviction_traded": high_conviction_traded,
        "high_conviction_act_rate": conviction_act_rate,
        "closed_wins": wins,
        "closed_losses": losses,
        "closed_win_rate": win_rate,
        "realized_pnl_dollars": round(total_pnl, 2),
    }


__all__ = [
    "EodBiasRow",
    "load_eod_bias",
    "priority_tickers_from_bias",
    "reconcile_outcomes",
    "accuracy_window",
]
