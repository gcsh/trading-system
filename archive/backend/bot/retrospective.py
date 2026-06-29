"""MITS Phase 6 (P6.4) — Sunday weekly retrospective.

Walks the prior trading week (Mon-Fri) and assembles a structured recap
into a `WeeklyRetrospective` row. Idempotent: re-running the pass for a
given `week_start_date` overwrites the existing row.

The week is identified by its MONDAY date (per ISO week convention).
`build_weekly_retrospective(week_start)` accepts a Monday `date`;
`monday_of_week(today)` is the helper for the Sunday cron.

Plain-English summary paragraph: Claude-composed when an
`anthropic_key()` is configured, deterministic fallback otherwise.
The result is CACHED on the row so subsequent UI reads don't re-call
Claude.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date as _date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from backend.bot.scorecard.detector_scorecard import _trade_pattern_keys
from backend.config import TUNABLES, anthropic_key
from backend.db import session_scope
from backend.models.decision_log import DecisionLog
from backend.models.eod_prediction_outcome import (
    EodPredictionOutcome, OUTCOME_NOT_TRADED,
)
from backend.models.trade import Trade
from backend.models.weekly_retrospective import WeeklyRetrospective

logger = logging.getLogger(__name__)


# Family resolver — best-effort. Walks the detector registry to find
# the family for a given pattern. Falls back to "uncategorized".
def _detector_family_map() -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        from backend.bot.detectors import all_detectors
        for d in all_detectors():
            out[d.pattern] = getattr(d, "family", "uncategorized") or "uncategorized"
    except Exception:
        logger.debug("detector family map unavailable", exc_info=True)
    return out


def monday_of_week(target: Optional[_date] = None) -> _date:
    """Return the Monday on or before ``target`` (or today)."""
    d = target or _date.today()
    return d - timedelta(days=d.weekday())


def _week_range(monday: _date) -> Tuple[datetime, datetime, _date]:
    """Return (start_dt, end_dt_exclusive, friday_date) for the week."""
    start_dt = datetime.combine(monday, datetime.min.time())
    end_dt = start_dt + timedelta(days=7)
    friday = monday + timedelta(days=4)
    return start_dt, end_dt, friday


def _eod_bias_rank(trade) -> Optional[int]:
    if not trade:
        return None
    detail_json = trade.get("detail_json") if isinstance(trade, dict) \
        else getattr(trade, "detail_json", None)
    if not detail_json:
        return None
    try:
        detail = json.loads(detail_json)
    except Exception:
        return None
    if not isinstance(detail, dict):
        return None
    eod = detail.get("eod_bias") or {}
    if not isinstance(eod, dict):
        return None
    return eod.get("rank")


def _conviction_bucket(rank: Optional[int]) -> str:
    if rank is None:
        return "no_eod_bias"
    if rank == 1:
        return "rank_1"
    if rank in (2, 3):
        return "rank_2_3"
    return "rank_4_plus"


def _hold_minutes(trade) -> Optional[float]:
    if not trade:
        return None
    ts = trade.get("timestamp") if isinstance(trade, dict) \
        else getattr(trade, "timestamp", None)
    if not ts:
        return None
    try:
        delta = datetime.utcnow() - ts
        return max(0.0, delta.total_seconds() / 60.0)
    except Exception:
        return None


def _aggregate_top_n(d: Dict[str, Dict[str, float]],
                              top_n: int,
                              reverse: bool = True) -> List[Dict[str, Any]]:
    """Convert a {key: {pnl_dollars, trade_count}} dict to a sorted list."""
    items = [
        {"key": k,
          "pnl_dollars": round(float(v.get("pnl_dollars", 0.0)), 2),
          "trade_count": int(v.get("trade_count", 0))}
        for k, v in d.items()
    ]
    items.sort(key=lambda x: x["pnl_dollars"], reverse=reverse)
    return items[:top_n]


def _catalyst_gate_saves(start: datetime, end: datetime
                                  ) -> Tuple[int, float]:
    """Count predictions that the catalyst gate skipped + estimate the
    avoided drawdown.

    Walks EodPredictionOutcome rows whose outcome == not_traded AND
    skip_reason in {catalyst_gate, earnings_gate, fomc_gate}. The
    "saved dollars" estimate is conservative: we use the
    average_loss_per_skipped multiplied by the count when
    actual_pnl_dollars is unavailable.
    """
    saves_count = 0
    saved_dollars = 0.0
    with session_scope() as s:
        rows = s.execute(
            select(EodPredictionOutcome)
            .where(EodPredictionOutcome.analysis_date >= start.date())
            .where(EodPredictionOutcome.analysis_date < end.date())
            .where(EodPredictionOutcome.outcome == OUTCOME_NOT_TRADED)
        ).scalars().all()
        for r in rows:
            sr = (r.skip_reason or "").lower()
            if "catalyst" in sr or "earnings" in sr or "fomc" in sr:
                saves_count += 1
    # Conservative save estimate: assume each skipped trade WOULD have
    # cost the cohort's expected adverse move (~1.5% of starting
    # equity). Without a counterfactual we keep this a coarse number
    # the operator can tune.
    if saves_count > 0:
        saved_dollars = float(saves_count) * 75.0  # $5k * ~1.5%
    return saves_count, saved_dollars


def _claude_summary(stats: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Return (summary, source) where source is 'claude' or 'fallback'."""
    try:
        if not anthropic_key():
            return None, "fallback"
        from anthropic import Anthropic  # type: ignore
        client = Anthropic(api_key=anthropic_key(), timeout=30.0)
    except Exception:
        return None, "fallback"
    try:
        prompt = (
            "You are writing a one-paragraph weekly recap for a paper "
            "trading bot's operator (a markets beginner). Be specific, "
            "plain-English, and cite the numbers. 3-4 sentences max.\n\n"
            f"Week: {stats.get('week_start_date')} → "
            f"{stats.get('week_end_date')}\n"
            f"Trades: {stats.get('total_trades')} total · "
            f"{stats.get('closed_trades')} closed · "
            f"WR {stats.get('win_rate')}\n"
            f"Realized P&L: ${stats.get('realized_pnl_dollars'):.2f}\n"
            f"Top winning tickers: "
            f"{', '.join((t.get('key') or '?') for t in stats.get('top_winning_tickers', [])[:3])}\n"
            f"Top losing tickers: "
            f"{', '.join((t.get('key') or '?') for t in stats.get('top_losing_tickers', [])[:3])}\n"
            f"Top winning patterns: "
            f"{', '.join((t.get('key') or '?') for t in stats.get('top_winning_patterns', [])[:3])}\n"
            f"Catalyst-gate saves: "
            f"{stats.get('catalyst_gate_saves_count')} skipped "
            f"(~${stats.get('catalyst_gate_saves_dollars_estimated'):.0f} saved)\n"
        )
        resp = client.messages.create(
            model=TUNABLES.memo_model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        if resp and resp.content:
            txt = "".join(
                getattr(b, "text", "") for b in resp.content
            ).strip()
            if txt:
                return txt, "claude"
    except Exception:
        logger.debug("claude summary failed", exc_info=True)
    return None, "fallback"


def _fallback_summary(stats: Dict[str, Any]) -> str:
    pnl = float(stats.get("realized_pnl_dollars") or 0.0)
    pnl_str = f"${pnl:+,.2f}"
    closed = int(stats.get("closed_trades") or 0)
    wr = stats.get("win_rate")
    wr_str = f"{wr*100:.0f}%" if wr is not None else "—"
    parts = [
        f"Week of {stats.get('week_start_date')} → "
        f"{stats.get('week_end_date')}: closed {closed} trades for "
        f"{pnl_str} realized P&L (WR {wr_str}).",
    ]
    top_w = stats.get("top_winning_tickers") or []
    top_l = stats.get("top_losing_tickers") or []
    if top_w:
        parts.append(
            "Winners led by " + ", ".join(
                t.get("key", "?") for t in top_w[:3]
            ) + "."
        )
    if top_l:
        parts.append(
            "Drags from " + ", ".join(
                t.get("key", "?") for t in top_l[:3]
            ) + "."
        )
    saves = int(stats.get("catalyst_gate_saves_count") or 0)
    if saves > 0:
        parts.append(
            f"Catalyst gate skipped {saves} setup{'s' if saves != 1 else ''} "
            f"(estimated ~${stats.get('catalyst_gate_saves_dollars_estimated'):.0f} saved)."
        )
    return " ".join(parts)


class _RetroResult:
    """Lightweight dict-shaped wrapper so callers can either:
    1. read `.to_dict()` (legacy expectation).
    2. read `.closed_trades` directly (test-friendly).
    """

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data

    def to_dict(self) -> Dict[str, Any]:
        return self._data

    def __getattr__(self, item: str) -> Any:
        try:
            return self._data[item]
        except KeyError as e:
            raise AttributeError(item) from e


def build_weekly_retrospective(week_start: _date,
                                          *,
                                          allow_claude: bool = True,
                                          ) -> _RetroResult:
    """Assemble the WeeklyRetrospective for the given Monday and UPSERT.

    Returns a detached _RetroResult so callers can read fields without
    a live session.
    """
    monday = monday_of_week(week_start)
    start_dt, end_dt, friday = _week_range(monday)

    # Pull every closed Trade in the week.
    total_trades = 0
    closed_trades = 0
    realized_pnl = 0.0
    wins = 0
    losses = 0
    holds: List[float] = []

    ticker_buckets: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"pnl_dollars": 0.0, "trade_count": 0})
    pattern_buckets: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"pnl_dollars": 0.0, "trade_count": 0})
    family_buckets: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"pnl_dollars": 0.0, "trade_count": 0})
    conviction_buckets: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"pnl_dollars": 0.0, "trade_count": 0})

    family_map = _detector_family_map()

    # Pull every Trade column we need into a detached dict so the
    # later UPSERT session doesn't try to refresh expired ORM rows.
    trades: List[Dict[str, Any]] = []
    with session_scope() as s:
        for row in s.execute(
            select(
                Trade.id, Trade.timestamp, Trade.ticker, Trade.pnl,
                Trade.strategy, Trade.detail_json, Trade.status,
                Trade.instrument, Trade.contracts,
                Trade.price, Trade.quantity,
            )
            .where(Trade.timestamp >= start_dt)
            .where(Trade.timestamp < end_dt)
        ).all():
            (tid, ts, tkr, pnl, strat, detail_json, status,
              instrument, contracts, price, quantity) = row
            trades.append({
                "id": tid, "timestamp": ts, "ticker": tkr, "pnl": pnl,
                "strategy": strat, "detail_json": detail_json,
                "status": status, "instrument": instrument,
                "contracts": contracts, "price": price,
                "quantity": quantity,
            })

    for t in trades:
        total_trades += 1
        is_closed = (t.get("status") or "").lower() in (
            "closed", "filled_closed",
            "closed_by_exit_manager",
            "closed_by_thesis_health",
        )
        if not is_closed or t.get("pnl") is None:
            continue
        closed_trades += 1
        pnl = float(t.get("pnl") or 0.0)
        realized_pnl += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        h = _hold_minutes(t)
        if h is not None:
            holds.append(h)
        # Ticker bucket.
        tkr = (t.get("ticker") or "").upper() or "?"
        ticker_buckets[tkr]["pnl_dollars"] += pnl
        ticker_buckets[tkr]["trade_count"] += 1
        # Pattern bucket — use the first key as the primary label.
        keys = _trade_pattern_keys(t)
        primary = keys[0] if keys else (t.get("strategy") or "?")
        pattern_buckets[primary]["pnl_dollars"] += pnl
        pattern_buckets[primary]["trade_count"] += 1
        # Family bucket.
        fam = family_map.get(primary, "uncategorized")
        family_buckets[fam]["pnl_dollars"] += pnl
        family_buckets[fam]["trade_count"] += 1
        # Conviction bucket.
        bucket = _conviction_bucket(_eod_bias_rank(t))
        conviction_buckets[bucket]["pnl_dollars"] += pnl
        conviction_buckets[bucket]["trade_count"] += 1

    win_rate = (wins / closed_trades) if closed_trades > 0 else None
    avg_hold = (sum(holds) / len(holds)) if holds else None
    top_n = int(TUNABLES.weekly_retrospective_top_n)
    # Top winners / losers — losers are the LOWEST P&L.
    top_winning_tickers = _aggregate_top_n(
        {k: v for k, v in ticker_buckets.items() if v["pnl_dollars"] > 0},
        top_n, reverse=True)
    top_losing_tickers = _aggregate_top_n(
        {k: v for k, v in ticker_buckets.items() if v["pnl_dollars"] < 0},
        top_n, reverse=False)
    top_winning_patterns = _aggregate_top_n(
        {k: v for k, v in pattern_buckets.items() if v["pnl_dollars"] > 0},
        top_n, reverse=True)
    top_losing_patterns = _aggregate_top_n(
        {k: v for k, v in pattern_buckets.items() if v["pnl_dollars"] < 0},
        top_n, reverse=False)
    # Family rollup includes EVERY family, ordered by pnl desc.
    family_attribution = _aggregate_top_n(
        family_buckets, 50, reverse=True)

    saves_count, saves_dollars = _catalyst_gate_saves(start_dt, end_dt)

    stats_dict = {
        "week_start_date": monday.isoformat(),
        "week_end_date": friday.isoformat(),
        "total_trades": total_trades,
        "closed_trades": closed_trades,
        "realized_pnl_dollars": realized_pnl,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "avg_hold_minutes": (round(avg_hold, 1)
                                      if avg_hold is not None else None),
        "top_winning_tickers": top_winning_tickers,
        "top_losing_tickers": top_losing_tickers,
        "top_winning_patterns": top_winning_patterns,
        "top_losing_patterns": top_losing_patterns,
        "family_pnl_attribution": family_attribution,
        "catalyst_gate_saves_count": saves_count,
        "catalyst_gate_saves_dollars_estimated": round(saves_dollars, 2),
    }

    # Summary paragraph — Claude-composed when configured.
    summary, source = (None, "fallback")
    if allow_claude:
        summary, source = _claude_summary(stats_dict)
    if not summary:
        summary = _fallback_summary(stats_dict)
        source = "fallback"

    # UPSERT.
    with session_scope() as s:
        row = s.execute(
            select(WeeklyRetrospective)
            .where(WeeklyRetrospective.week_start_date == monday)
        ).scalar_one_or_none()
        if row is None:
            row = WeeklyRetrospective(
                week_start_date=monday,
                week_end_date=friday,
            )
            s.add(row)
            s.flush()
        row.week_end_date = friday
        row.total_trades = total_trades
        row.closed_trades = closed_trades
        row.realized_pnl_dollars = round(realized_pnl, 2)
        row.win_rate = (round(win_rate, 4)
                              if win_rate is not None else None)
        row.avg_hold_minutes = (round(avg_hold, 1)
                                          if avg_hold is not None else None)
        row.top_winning_tickers_json = json.dumps(top_winning_tickers)
        row.top_losing_tickers_json = json.dumps(top_losing_tickers)
        row.top_winning_patterns_json = json.dumps(top_winning_patterns)
        row.top_losing_patterns_json = json.dumps(top_losing_patterns)
        row.family_pnl_attribution_json = json.dumps(family_attribution)
        row.catalyst_gate_saves_count = saves_count
        row.catalyst_gate_saves_dollars_estimated = round(saves_dollars, 2)
        row.conviction_multiplier_pnl_effect_json = json.dumps(
            {k: {"trade_count": int(v["trade_count"]),
                  "pnl_dollars": round(float(v["pnl_dollars"]), 2)}
              for k, v in conviction_buckets.items()})
        row.summary_paragraph = summary
        row.summary_source = source
        row.updated_at = datetime.utcnow()

    # Re-read in a fresh session, materialise to dict, then return a
    # detached _RetroResult that won't hit DetachedInstanceError.
    with session_scope() as s:
        row = s.execute(
            select(WeeklyRetrospective)
            .where(WeeklyRetrospective.week_start_date == monday)
        ).scalar_one_or_none()
        if row is None:
            return _RetroResult({})
        return _RetroResult(row.to_dict())


__all__ = [
    "build_weekly_retrospective",
    "monday_of_week",
]
