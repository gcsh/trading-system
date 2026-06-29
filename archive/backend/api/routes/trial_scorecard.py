"""MITS Phase 6 (P6.5) — $5k paper trial scorecard.

THE single page the operator can point at to prove the bot works.
Aggregates over PortfolioSnapshot + Trade + EodPredictionOutcome to
produce a single response that powers the TrialScorecard UI.

Key outputs:
  * starting_equity, current_equity, total_return
  * trading_days_elapsed / days_total
  * trial_start_date / trial_end_date
  * weekly_pnl_predicted_vs_realized
  * high_conviction_setups_total / taken / won
  * hit_rate (taken & won / taken)
  * max_drawdown
  * sharpe_ratio_estimate (daily-return basis)
  * projection: on_track | off_track | breached
  * narrative paragraph (Claude when key present, deterministic fallback)
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date as _date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from backend.config import TUNABLES, anthropic_key
from backend.db import session_scope
from backend.models.eod_analysis import EodAnalysis
from backend.models.eod_prediction_outcome import (
    EodPredictionOutcome, OUTCOME_TRADED_MATCHED, OUTCOME_TRADED_DIVERGED,
)
from backend.models.paper import PaperAccount
from backend.models.snapshot import PortfolioSnapshot
from backend.models.trade import Trade

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trial_scorecard"])


# ── helpers ────────────────────────────────────────────────────────


def _compute_data_health() -> Dict[str, Any]:
    """MITS Phase 11.I — roll the per-source health rows into one badge.

    Reads the latest ``data_source_health`` snapshot for each Phase-11
    source and returns a single status (green / yellow / red) so the
    Trial Scorecard UI can display a banner. Idempotent on empty
    table (returns 'unknown'). Never raises into the scorecard payload.
    """
    try:
        from backend.models.data_source_health import DataSourceHealth
        from sqlalchemy import desc

        counts = {"green": 0, "yellow": 0, "red": 0, "unknown": 0}
        sources_seen = {}
        with session_scope() as s:
            rows = s.execute(
                select(DataSourceHealth)
                .order_by(desc(DataSourceHealth.snapshot_date))
            ).scalars().all()
            for r in rows:
                if r.source in sources_seen:
                    continue
                sources_seen[r.source] = r.status or "unknown"
                counts[r.status or "unknown"] = (
                    counts.get(r.status or "unknown", 0) + 1)
        if not sources_seen:
            return {
                "status": "unknown",
                "count_by_status": counts,
                "tooltip": "No source-health rows yet. The 00:01 ET "
                              "aggregator has not run since the table was "
                              "added.",
            }
        if counts["red"] > 0:
            status = "red"
            tip = (f"{counts['red']} source(s) red — corpus "
                   "may be missing or stale data.")
        elif counts["yellow"] >= 1:
            status = "yellow"
            tip = (f"{counts['yellow']} source(s) yellow — degraded "
                   "but operational.")
        else:
            status = "green"
            tip = f"All {counts['green']} sources healthy."
        return {
            "status": status,
            "count_by_status": counts,
            "tooltip": tip,
            "per_source": sources_seen,
        }
    except Exception:
        logger.debug("data_health rollup failed", exc_info=True)
        return {"status": "unknown",
                "count_by_status": {"green": 0, "yellow": 0,
                                       "red": 0, "unknown": 0},
                "tooltip": "Health computation failed"}


def _trial_start_date() -> _date:
    raw = (TUNABLES.trial_start_date or "").strip()
    try:
        return _date.fromisoformat(raw)
    except Exception:
        return _date(2026, 5, 28)


def _trial_end_date(start: _date) -> _date:
    return start + timedelta(days=int(TUNABLES.trial_duration_days))


def _is_trading_day(d: _date) -> bool:
    # Weekdays only. We don't filter holidays here — the scorecard is
    # a rough operator-facing snapshot, not a settlement system.
    return d.weekday() < 5


def _trading_days_elapsed(start: _date, today: _date) -> int:
    if today < start:
        return 0
    days = 0
    cur = start
    while cur <= today:
        if _is_trading_day(cur):
            days += 1
        cur += timedelta(days=1)
    return days


def _equity_snapshots(start_dt: datetime) -> List[Dict[str, Any]]:
    """Return detached dicts so we don't drag a closed session around."""
    out: List[Dict[str, Any]] = []
    with session_scope() as s:
        for ts, value, cash in s.execute(
            select(
                PortfolioSnapshot.timestamp,
                PortfolioSnapshot.portfolio_value,
                PortfolioSnapshot.cash,
            )
            .where(PortfolioSnapshot.timestamp >= start_dt)
            .order_by(PortfolioSnapshot.timestamp.asc())
        ).all():
            out.append({
                "timestamp": ts,
                "portfolio_value": value,
                "cash": cash,
            })
    return out


def _max_drawdown(values: List[float]) -> Dict[str, float]:
    """Return {pct, dollars} for the deepest peak-to-trough drawdown."""
    if not values:
        return {"pct": 0.0, "dollars": 0.0}
    peak = values[0]
    worst_pct = 0.0
    worst_dollars = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd_pct = max(0.0, (peak - v) / peak)
            dd_dollars = peak - v
            if dd_pct > worst_pct:
                worst_pct = dd_pct
                worst_dollars = dd_dollars
    return {"pct": round(worst_pct, 4), "dollars": round(worst_dollars, 2)}


def _daily_returns(snapshots: List[Dict[str, Any]]) -> List[float]:
    """One return per calendar day — the EOD snapshot's value vs the
    previous day's EOD value. Returns are SIMPLE returns.
    """
    by_day: Dict[_date, float] = {}
    for s in snapshots:
        ts = s.get("timestamp") if isinstance(s, dict) \
            else getattr(s, "timestamp", None)
        val = s.get("portfolio_value") if isinstance(s, dict) \
            else getattr(s, "portfolio_value", None)
        if not ts:
            continue
        by_day[ts.date()] = float(val or 0.0)
    days = sorted(by_day.keys())
    out: List[float] = []
    prev = None
    for d in days:
        cur = by_day[d]
        if prev is not None and prev > 0:
            out.append((cur - prev) / prev)
        prev = cur
    return out


def _sharpe(daily_rets: List[float]) -> Optional[float]:
    if len(daily_rets) < 2:
        return None
    n = len(daily_rets)
    mean = sum(daily_rets) / n
    var = sum((r - mean) ** 2 for r in daily_rets) / (n - 1)
    sd = math.sqrt(var) if var > 0 else 0.0
    if sd == 0:
        return None
    # Annualize using 252 trading days.
    return round((mean * 252.0) / (sd * math.sqrt(252.0)), 3)


def _classify_projection(current: float, starting: float,
                                  days_elapsed: int, days_total: int,
                                  target_growth_pct: float,
                                  breach_floor_pct: float) -> str:
    if current < (breach_floor_pct * starting):
        return "breached"
    if days_total <= 0:
        return "on_track" if current >= starting else "off_track"
    progress = max(0, days_elapsed) / max(1, days_total)
    target_equity = starting + (progress * target_growth_pct * starting)
    if current >= target_equity:
        return "on_track"
    return "off_track"


def _weekly_predicted_vs_realized(start: _date, end: _date
                                              ) -> List[Dict[str, Any]]:
    """For each week in the trial, sum:
      * predicted_pnl: sum of (suggested_action's target premium effect
        * trade count) — coarse proxy when no per-prediction dollar
        forecast exists. We use posterior * sample_size / 100 as a
        normalized predicted-edge metric so the chart shape is
        comparable, not literal dollars.
      * realized_pnl: sum of Trade.pnl for closed trades that week.
    """
    weeks: List[Dict[str, Any]] = []
    cur = start - timedelta(days=start.weekday())  # Monday
    while cur < end:
        week_end = cur + timedelta(days=7)
        start_dt = datetime.combine(cur, datetime.min.time())
        end_dt = datetime.combine(week_end, datetime.min.time())
        realized = 0.0
        predicted = 0.0
        with session_scope() as s:
            trades = s.execute(
                select(Trade)
                .where(Trade.timestamp >= start_dt)
                .where(Trade.timestamp < end_dt)
                .where(Trade.pnl.is_not(None))
            ).scalars().all()
            for t in trades:
                realized += float(t.pnl or 0.0)
            # Predicted side: sum of (posterior - 0.5) * sample_size *
            # 100 over EodPredictionOutcome rows whose analysis_date
            # falls in the week. This gives a coarse "expected edge"
            # signal in dollars-ish units (operator-readable, not a
            # forecast).
            outcomes = s.execute(
                select(EodPredictionOutcome)
                .where(EodPredictionOutcome.analysis_date >= cur)
                .where(EodPredictionOutcome.analysis_date < week_end)
            ).scalars().all()
            for o in outcomes:
                post = float(o.posterior or 0.0)
                ss = int(o.sample_size or 0)
                if post > 0 and ss > 0:
                    predicted += (post - 0.5) * min(ss, 200) * 0.5
        weeks.append({
            "week_start": cur.isoformat(),
            "predicted_pnl": round(predicted, 2),
            "realized_pnl": round(realized, 2),
        })
        cur = week_end
    return weeks


def _high_conviction_aggregate(start_dt: datetime,
                                          end_dt: datetime
                                          ) -> Dict[str, Any]:
    """Count high-conviction predictions + matched-trade outcomes."""
    total = 0
    taken = 0
    won = 0
    post_floor = float(TUNABLES.eod_high_conviction_posterior)
    n_floor = int(TUNABLES.eod_high_conviction_min_samples)
    with session_scope() as s:
        rows = s.execute(
            select(EodPredictionOutcome)
            .where(EodPredictionOutcome.analysis_date >= start_dt.date())
            .where(EodPredictionOutcome.analysis_date < end_dt.date())
        ).scalars().all()
        for r in rows:
            post = float(r.posterior or 0.0)
            ss = int(r.sample_size or 0)
            if post < post_floor or ss < n_floor:
                continue
            total += 1
            if r.outcome in (OUTCOME_TRADED_MATCHED,
                                 OUTCOME_TRADED_DIVERGED):
                taken += 1
                if r.actual_pnl_dollars is not None and \
                      r.actual_pnl_dollars > 0:
                    won += 1
    return {
        "total": total,
        "taken": taken,
        "won": won,
        "hit_rate": (round(won / taken, 4) if taken > 0 else None),
    }


def _layer_pnl_split(start_dt: datetime, end_dt: datetime
                          ) -> Dict[str, Any]:
    """MITS Phase 7 finishing pass — split realized P&L + win rate by
    layer so the operator can attribute crisis-day discretionary
    performance separately from the statistical Bayesian layer.

    Returns:
      statistical_pnl_dollars  — Σ Trade.pnl where opportunistic == 0
      opportunistic_pnl_dollars — Σ Trade.pnl where opportunistic == 1
      statistical_win_rate     — wins / closed for the statistical layer
      opportunistic_win_rate   — wins / closed for the opportunistic layer
      statistical_trades_closed / opportunistic_trades_closed
    """
    stat_pnl = 0.0
    opp_pnl = 0.0
    stat_closed = 0
    stat_wins = 0
    opp_closed = 0
    opp_wins = 0
    with session_scope() as s:
        rows = s.execute(
            select(Trade)
            .where(Trade.timestamp >= start_dt)
            .where(Trade.timestamp < end_dt)
            .where(Trade.pnl.is_not(None))
        ).scalars().all()
        for t in rows:
            pnl = float(t.pnl or 0.0)
            is_opp = bool(int(getattr(t, "opportunistic", 0) or 0))
            if is_opp:
                opp_pnl += pnl
                opp_closed += 1
                if pnl > 0:
                    opp_wins += 1
            else:
                stat_pnl += pnl
                stat_closed += 1
                if pnl > 0:
                    stat_wins += 1
    return {
        "statistical_pnl_dollars": round(stat_pnl, 2),
        "opportunistic_pnl_dollars": round(opp_pnl, 2),
        "statistical_trades_closed": stat_closed,
        "opportunistic_trades_closed": opp_closed,
        "statistical_win_rate": (
            round(stat_wins / stat_closed, 4) if stat_closed > 0 else None
        ),
        "opportunistic_win_rate": (
            round(opp_wins / opp_closed, 4) if opp_closed > 0 else None
        ),
    }


def _current_equity(snapshots: List[Dict[str, Any]]) -> float:
    if not snapshots:
        return float(TUNABLES.trial_starting_equity)
    last = snapshots[-1]
    val = last.get("portfolio_value") if isinstance(last, dict) \
        else getattr(last, "portfolio_value", None)
    return float(val or 0.0)


def _build_narrative(payload: Dict[str, Any]) -> str:
    """Claude-composed narrative when key present, deterministic
    fallback otherwise."""
    deterministic = (
        f"Day {payload['trading_days_elapsed']} of "
        f"{payload['days_total']}. Equity "
        f"${payload['current_equity']:,.2f} "
        f"({payload['total_return_pct']*100:+.2f}%). "
        f"{payload['high_conviction_setups_taken']} of "
        f"{payload['high_conviction_setups_total']} "
        f"high-conviction setups taken; "
        f"{payload['high_conviction_setups_won']} won. "
        f"Projection: {payload['projection']}."
    )
    try:
        if not anthropic_key():
            return deterministic
        from anthropic import Anthropic  # type: ignore
        client = Anthropic(api_key=anthropic_key(), timeout=30.0)
    except Exception:
        return deterministic
    try:
        prompt = (
            "You are writing a one-paragraph status report on a $5,000 "
            "30-day paper-trading trial. Plain English, 2-3 sentences, "
            "cite the numbers. Use the data:\n\n"
            f"- Day {payload['trading_days_elapsed']} of "
            f"{payload['days_total']}\n"
            f"- Starting equity: ${payload['starting_equity']:,.2f}\n"
            f"- Current equity: ${payload['current_equity']:,.2f} "
            f"({payload['total_return_pct']*100:+.2f}%)\n"
            f"- High-conviction setups: "
            f"{payload['high_conviction_setups_taken']} of "
            f"{payload['high_conviction_setups_total']} taken, "
            f"{payload['high_conviction_setups_won']} won\n"
            f"- Hit rate: {payload['hit_rate']}\n"
            f"- Max drawdown: {payload['max_drawdown_pct']*100:.1f}%\n"
            f"- Projection: {payload['projection']}\n"
        )
        resp = client.messages.create(
            model=TUNABLES.memo_model,
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        if resp and resp.content:
            txt = "".join(getattr(b, "text", "")
                                for b in resp.content).strip()
            if txt:
                return txt
    except Exception:
        logger.debug("trial narrative claude failed", exc_info=True)
    return deterministic


# ── route ──────────────────────────────────────────────────────────


@router.get("/trial-scorecard")
async def trial_scorecard() -> Dict[str, Any]:
    starting = float(TUNABLES.trial_starting_equity)
    start_date = _trial_start_date()
    end_date = _trial_end_date(start_date)
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.min.time())

    today = _date.today()
    days_elapsed = max(0, (today - start_date).days)
    trading_days_elapsed = _trading_days_elapsed(start_date, today)
    days_total = int(TUNABLES.trial_duration_days)

    snapshots = _equity_snapshots(start_dt)
    current = _current_equity(snapshots)

    # If no snapshots yet (engine hasn't booked anything), fall back
    # to PaperAccount.cash.
    if current <= 0.0:
        try:
            with session_scope() as s:
                acct = s.query(PaperAccount).first()
                if acct:
                    current = float(acct.last_portfolio_value
                                            or acct.cash
                                            or starting)
        except Exception:
            current = starting

    total_return_dollars = round(current - starting, 2)
    total_return_pct = round(
        ((current - starting) / starting) if starting > 0 else 0.0, 4)

    values = [float(s.get("portfolio_value") or 0.0)
                  if isinstance(s, dict)
                  else float(getattr(s, "portfolio_value", 0.0) or 0.0)
                  for s in snapshots]
    dd = _max_drawdown(values)
    daily_rets = _daily_returns(snapshots)
    sharpe = _sharpe(daily_rets)

    weekly_split = _weekly_predicted_vs_realized(start_date, today
                                                            + timedelta(days=1))
    hc = _high_conviction_aggregate(start_dt, end_dt)
    layer_split = _layer_pnl_split(start_dt, end_dt)

    projection = _classify_projection(
        current, starting, days_elapsed, days_total,
        TUNABLES.trial_target_growth_pct,
        TUNABLES.trial_breach_equity_floor_pct,
    )

    payload = {
        "starting_equity": round(starting, 2),
        "current_equity": round(current, 2),
        "total_return_dollars": total_return_dollars,
        "total_return_pct": total_return_pct,
        "days_elapsed": days_elapsed,
        "days_total": days_total,
        "trading_days_elapsed": trading_days_elapsed,
        "trial_start_date": start_date.isoformat(),
        "trial_end_date": end_date.isoformat(),
        "weekly_pnl_predicted_vs_realized": weekly_split,
        "high_conviction_setups_total": hc["total"],
        "high_conviction_setups_taken": hc["taken"],
        "high_conviction_setups_won": hc["won"],
        "hit_rate": hc["hit_rate"],
        "max_drawdown_pct": dd["pct"],
        "max_drawdown_dollars": dd["dollars"],
        "sharpe_ratio_estimate": sharpe,
        "projection": projection,
        "target_growth_pct": TUNABLES.trial_target_growth_pct,
        "breach_equity_floor_pct": TUNABLES.trial_breach_equity_floor_pct,
        # MITS Phase 7 finishing pass — statistical vs opportunistic
        # layer P&L + win-rate split. Lets the operator see which layer
        # drove crisis-day returns and recalibrate without blending
        # the two layers' edges together.
        "statistical_pnl_dollars": layer_split["statistical_pnl_dollars"],
        "opportunistic_pnl_dollars": layer_split["opportunistic_pnl_dollars"],
        "statistical_win_rate": layer_split["statistical_win_rate"],
        "opportunistic_win_rate": layer_split["opportunistic_win_rate"],
        "statistical_trades_closed": layer_split["statistical_trades_closed"],
        "opportunistic_trades_closed": layer_split["opportunistic_trades_closed"],
        # MITS Phase 11.I — Data Health rollup. Operator sees at a
        # glance whether trades are happening on healthy data.
        "data_health": _compute_data_health(),
    }
    payload["narrative"] = _build_narrative(payload)
    return payload
