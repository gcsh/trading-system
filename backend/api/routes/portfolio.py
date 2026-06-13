"""Portfolio analytics endpoints: equity curve, performance, positions, strategy P&L.

This is the dashboard's data layer. It computes Sharpe ratio, max drawdown, win
rate, and a per-strategy breakdown from the trade + snapshot tables.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from backend.bot.paper_executor import PaperExecutor
from backend.db import session_scope
from backend.models.config import load_config
from backend.models.snapshot import PortfolioSnapshot
from backend.models.trade import Trade

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

TRADING_DAYS_PER_YEAR = 252


def _trial_start(config: dict) -> Optional[datetime]:
    """Parse the trial start date so curve metrics ignore pre-trial snapshots
    (an earlier, lower-balance account would otherwise pollute % / Sharpe / DD)."""
    raw = config.get("trial_start")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        try:
            return datetime.fromisoformat(f"{raw}T00:00:00")
        except Exception:
            return None


# -- math helpers -----------------------------------------------------------

def _returns(values: List[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev <= 0:
            out.append(0.0)
        else:
            out.append((values[i] - prev) / prev)
    return out


def _sharpe(returns: List[float], risk_free: float = 0.0) -> float:
    """Annualised Sharpe ratio. Assumes ``returns`` are per-cycle (we treat
    them as daily). Returns 0 if there's not enough variance to compute."""
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    daily_excess = mean - (risk_free / TRADING_DAYS_PER_YEAR)
    return (daily_excess / std) * math.sqrt(TRADING_DAYS_PER_YEAR)


def _max_drawdown(values: List[float]) -> float:
    """Worst peak-to-trough decline expressed as a positive percentage."""
    if not values:
        return 0.0
    peak = values[0]
    worst = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > worst:
                worst = dd
    return worst * 100


# -- endpoints --------------------------------------------------------------

_RANGE_DELTAS = {
    "1d":  timedelta(days=1),
    "1w":  timedelta(days=7),
    "1m":  timedelta(days=30),
    "3m":  timedelta(days=90),
    "6m":  timedelta(days=180),
    "1y":  timedelta(days=365),
    "all": None,
}


def _last_session_rows(session) -> List[PortfolioSnapshot]:
    """Return every snapshot from the most-recent calendar date that
    has data. Used by ``range=last_session`` and as a fallback when
    ``range=1d`` would return zero rows (weekends, holidays, fresh
    deploys).
    """
    latest = (
        session.execute(
            select(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.timestamp.desc())
            .limit(1)
        ).scalars().first()
    )
    if latest is None or latest.timestamp is None:
        return []
    day_start = latest.timestamp.replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    day_end = day_start + timedelta(days=1)
    return (
        session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.timestamp >= day_start)
            .where(PortfolioSnapshot.timestamp < day_end)
            .order_by(PortfolioSnapshot.timestamp.asc())
        ).scalars().all()
    )


@router.get("/equity")
async def equity_curve(
    limit: int = 500,
    rng: str = Query("trial", alias="range"),
) -> Any:
    """Equity snapshots, oldest first.

    ``range`` (URL param, aliased to local ``rng`` so it doesn't shadow
    the builtin) controls how far back to read:
      - ``trial`` (default): from current trial start (legacy behavior).
      - ``1d``/``1w``/``1m``/``3m``/``6m``/``1y``: rolling window from now.
      - ``last_session`` (MITS-P9.4): all snapshots from the latest
        calendar date that has data. Used as a fallback for ``1d`` on
        weekends / holidays so the Today chart is never empty.
      - ``all``: every snapshot in the DB, no time filter.

    Response shape:

      * If the caller used ``range=last_session`` OR a 1d call that
        fell back, the response is a dict
        ``{"snapshots": [...], "dataset_note": "..."}``.
      * Otherwise — for backward compatibility — the response is the
        bare list of snapshot dicts (the legacy contract).
    """
    rng = (rng or "trial").lower()
    fallback_to_last_session = False
    dataset_note: str = ""
    with session_scope() as session:
        query = select(PortfolioSnapshot)
        if rng == "trial":
            start = _trial_start(load_config(session))
            if start is not None:
                query = query.where(PortfolioSnapshot.timestamp >= start)
        elif rng == "last_session":
            rows = _last_session_rows(session)
            if rows:
                latest_dt = rows[-1].timestamp.date().isoformat()
                dataset_note = f"Showing {latest_dt} session — markets closed today."
            return {
                "snapshots": [r.to_dict() for r in rows],
                "dataset_note": dataset_note,
                "range": "last_session",
            }
        elif rng in _RANGE_DELTAS and _RANGE_DELTAS[rng] is not None:
            cutoff = datetime.utcnow() - _RANGE_DELTAS[rng]
            query = query.where(PortfolioSnapshot.timestamp >= cutoff)
        # rng == 'all' → no filter
        rows = (
            session.execute(query.order_by(PortfolioSnapshot.timestamp.asc()))
            .scalars()
            .all()
        )

        # MITS-P9.4 — 1d returns zero rows on weekends/holidays. Fall
        # back to the most recent session so the Today chart isn't
        # empty. The response shape switches to the wrapped form so
        # the UI can surface the "markets closed" hint.
        if rng == "1d" and not rows:
            rows = _last_session_rows(session)
            fallback_to_last_session = True
            if rows:
                latest_dt = rows[-1].timestamp.date().isoformat()
                dataset_note = f"Showing {latest_dt} session — markets closed today."

        # Discontinuity filter (Bug fix 2026-06-02): if an accounting model
        # change (e.g. complex-MTM fix #131) created a step jump > 5% in a
        # single tick, the pre-jump points represent a different valuation
        # regime and would misrepresent the chart's "today's gain." For
        # short-range views (1d/1w), rebase after the LAST such jump so the
        # chart axis reflects the current accounting model only.
        if rng in ("1d", "1w") and len(rows) >= 2:
            DISCONTINUITY_PCT = 0.05
            baseline_idx = 0
            for i in range(1, len(rows)):
                prev_val = rows[i - 1].portfolio_value or 0.0
                if prev_val <= 0:
                    continue
                step = abs((rows[i].portfolio_value or 0.0) - prev_val) / abs(prev_val)
                if step >= DISCONTINUITY_PCT:
                    baseline_idx = i
            if baseline_idx > 0:
                rows = rows[baseline_idx:]

        # Decimate evenly when we have more rows than the requested point
        # budget so the chart payload stays bounded for multi-year ranges.
        n = len(rows)
        if limit and n > limit:
            step = n / limit
            picked = []
            i = 0.0
            while int(i) < n and len(picked) < limit:
                picked.append(rows[int(i)])
                i += step
            # Always keep the last point so the chart's current value matches
            # the live equity reading even after decimation.
            if picked and picked[-1] is not rows[-1]:
                picked.append(rows[-1])
            rows = picked
        out = [row.to_dict() for row in rows]
        if fallback_to_last_session:
            return {
                "snapshots": out,
                "dataset_note": dataset_note,
                "range": "last_session",
                "fallback_from": "1d",
            }
        return out


# /portfolio/performance is hit by Today + Trades pages multiple times per
# render. Probe (2026-06-02) showed 584ms/call. Cache for 5s so successive
# requests within a render cycle share the result.
_PERF_CACHE: dict = {"ts": 0.0, "payload": None}
_PERF_CACHE_TTL = 5.0


@router.get("/performance")
async def performance() -> dict:
    """Headline metrics: total P&L, win rate, Sharpe, max drawdown, etc.

    Excludes ``closed_by_reset`` (Bug fix 2026-06-02): administrative resets
    from ``soft_reset`` get ``status=closed_by_reset`` with ``pnl=0``. They
    inflate trade-count and force win_rate to 0% / Brier to nonsense.

    Cached for ``_PERF_CACHE_TTL`` seconds.
    """
    import time as _time
    _now = _time.monotonic()
    _cached = _PERF_CACHE.get("payload")
    if _cached is not None and (_now - _PERF_CACHE.get("ts", 0)) < _PERF_CACHE_TTL:
        return _cached
    with session_scope() as session:
        trades = (
            session.execute(
                select(Trade)
                .where(Trade.status != "closed_by_reset")
                # Exclude the historical-replay synthetic corpus (P2.1) —
                # those rows feed calibration/cohort but must not pollute
                # the operator's "your account performance" surface.
                .where(Trade.signal_source != "historical_replay")
            )
            .scalars()
            .all()
        )
        snapshots = (
            session.execute(select(PortfolioSnapshot).order_by(PortfolioSnapshot.timestamp))
            .scalars()
            .all()
        )

        closed = [t for t in trades if t.pnl is not None]
        wins = [t.pnl for t in closed if t.pnl > 0]
        losses = [t.pnl for t in closed if t.pnl < 0]
        realized_pnl = sum(t.pnl for t in closed) if closed else 0.0
        win_rate = (len(wins) / len(closed)) if closed else 0.0
        avg_gain = (sum(wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(losses) / len(losses)) if losses else 0.0
        profit_factor = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)
        if profit_factor == float("inf"):
            profit_factor = 0.0  # avoid JSON serialisation issues; clients can infer

        # Only the CURRENT trial's snapshots count — pre-trial ones (an older,
        # lower-balance account) otherwise corrupt %, Sharpe, drawdown and today's
        # P&L. "Since start" is always measured from the trial's starting cash.
        config = load_config(session)
        start = _trial_start(config)
        if start is not None:
            trial_snaps = [s for s in snapshots if s.timestamp and s.timestamp >= start]
            snapshots = trial_snaps or snapshots
        values = [s.portfolio_value for s in snapshots]
        starting_cash = float(config.get("paper_cash_override") or 0.0)
        equity_end = values[-1] if values else starting_cash
        equity_start = starting_cash or (values[0] if values else 0.0)
        equity_change_pct = (
            ((equity_end - equity_start) / equity_start * 100) if equity_start else 0.0
        )
        # Total P&L is ACCOUNT-LEVEL: realized (closed trades) + unrealized (open
        # positions, marked to market in the equity snapshots). Reporting only
        # closed P&L showed $0 while the account was genuinely down on open
        # positions — the disconnect the dashboard exposed.
        total_pnl = equity_end - equity_start
        unrealized_pnl = total_pnl - realized_pnl
        sharpe = _sharpe(_returns(values))
        max_dd = _max_drawdown(values)

        today_cutoff = datetime.utcnow() - timedelta(days=1)
        trades_today = sum(1 for t in trades if t.timestamp and t.timestamp >= today_cutoff)
        # Today's P&L = intraday account change (so it reflects open positions
        # moving), falling back to realized-today when there's no intraday snapshot.
        today_values = [s.portfolio_value for s in snapshots if s.timestamp and s.timestamp >= today_cutoff]

        # Discontinuity filter (Bug fix 2026-06-02): when the complex-MTM
        # accounting fix (#131) deployed mid-day, the bot restart re-valued
        # open positions from $0 → market-value. The snapshot at the moment
        # of restart shows a step jump that looks like a +$700 gain but is
        # purely an accounting model change. Detect single-tick jumps > 5%
        # and rebase the "today's baseline" AFTER the jump so pnl_today
        # reflects real intraday change rather than the rebasing artifact.
        DISCONTINUITY_PCT = 0.05
        if len(today_values) >= 2:
            baseline_idx = 0
            for i in range(1, len(today_values)):
                prev = today_values[i - 1]
                if prev <= 0:
                    continue
                step = abs(today_values[i] - prev) / abs(prev)
                if step >= DISCONTINUITY_PCT:
                    # Real trades don't move account equity 5%+ in a single
                    # 30-second snapshot tick. Treat as accounting change.
                    baseline_idx = i
            today_values = today_values[baseline_idx:]

        # Issue 11c — use the unified today-P&L helper so every surface
        # ( /portfolio/performance, /bot/status, /today/summary ) reports
        # the same number. The legacy intraday-swing fallback stays as a
        # secondary hint only.
        try:
            from backend.bot.today_pnl import compute_today_pnl
            _tp = compute_today_pnl(session)
            pnl_today = float(_tp.get("total_today") or 0.0)
        except Exception:
            if today_values:
                pnl_today = equity_end - today_values[0]
            else:
                pnl_today = sum(t.pnl for t in closed if t.timestamp and t.timestamp >= today_cutoff)

        _result = {
            "trade_count": len(trades),
            "closed_count": len(closed),
            "open_count": len(trades) - len(closed),
            "trades_today": trades_today,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "pnl_today": round(pnl_today, 2),
            "avg_gain": round(avg_gain, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "equity_start": round(equity_start, 2),
            "equity_end": round(equity_end, 2),
            "equity_change_pct": round(equity_change_pct, 2),
            "snapshot_count": len(snapshots),
        }
        _PERF_CACHE["ts"] = _now
        _PERF_CACHE["payload"] = _result
        return _result


@router.get("/risk")
async def portfolio_risk(request: Request) -> dict:
    """Portfolio-wide risk: sector / theme concentration, correlation clusters,
    net beta, net delta, diversification, macro risk label."""
    from backend.bot.portfolio_intel import assess_portfolio

    engine = getattr(request.app.state, "engine", None)
    positions: List[Dict[str, Any]] = []
    if engine is not None and hasattr(engine.executor, "positions"):
        try:
            positions = engine.executor.positions() or []
        except Exception:
            positions = []
    return assess_portfolio(positions).to_dict()


@router.get("/context")
async def portfolio_context(
    request: Request,
    candidate: Optional[str] = Query(default=None),
    direction: str = Query(default="LONG"),
) -> dict:
    """Correlation-aware portfolio block (MITS Phase 14.B).

    Returns net long / short notional, leverage, sector + theme weights,
    pairwise return correlations across the open book, and a SPY -3%
    stress projection. Pass ``candidate=TICKER`` (+ optional
    ``direction=LONG|SHORT``) to also receive ``candidate_max_correlation``
    and the worst-correlated peer name.
    """
    from backend.bot.portfolio_intel.portfolio_context import (
        build_portfolio_context,
    )

    engine = getattr(request.app.state, "engine", None)
    positions: List[Dict[str, Any]] = []
    equity = 0.0
    if engine is not None and engine.executor is not None:
        try:
            if hasattr(engine.executor, "positions"):
                positions = engine.executor.positions() or []
            if hasattr(engine.executor, "get_account_state"):
                state = engine.executor.get_account_state(
                    positions=positions
                ) or {}
                equity = float(state.get("portfolio_value") or 0.0)
        except Exception:
            positions = []
            equity = 0.0

    ctx = build_portfolio_context(
        positions=positions,
        equity=equity,
        candidate_ticker=candidate,
        candidate_direction=direction,
    )
    return ctx.to_dict()


@router.get("/by-strategy")
async def by_strategy() -> List[dict]:
    """P&L bucketed by strategy. Excludes the synthetic historical-replay
    corpus (P2.1) so the operator's "your account performance" surface
    only reflects live decisions, and ``closed_by_reset`` rows from
    soft-reset administrative closes (bug fix 2026-06-02)."""
    buckets: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"trade_count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}
    )
    with session_scope() as session:
        trades = (
            session.execute(
                select(Trade)
                .where(Trade.status != "closed_by_reset")
                .where(Trade.signal_source != "historical_replay")
            )
            .scalars()
            .all()
        )
        for t in trades:
            name = t.strategy or "unknown"
            bucket = buckets[name]
            bucket["trade_count"] += 1
            if t.pnl is not None:
                bucket["total_pnl"] += t.pnl
                if t.pnl > 0:
                    bucket["wins"] += 1
                elif t.pnl < 0:
                    bucket["losses"] += 1
    out: List[dict] = []
    for name, bucket in buckets.items():
        closed = bucket["wins"] + bucket["losses"]
        out.append(
            {
                "strategy": name,
                "trade_count": int(bucket["trade_count"]),
                "wins": int(bucket["wins"]),
                "losses": int(bucket["losses"]),
                "win_rate": round(bucket["wins"] / closed, 4) if closed else 0.0,
                "total_pnl": round(bucket["total_pnl"], 2),
            }
        )
    out.sort(key=lambda r: r["total_pnl"], reverse=True)
    return out


@router.get("/positions")
async def positions(request: Request) -> List[dict]:
    """Open positions across whichever executor is active.

    Local paper has rich data; live brokers fall back to whatever the executor
    exposes via ``get_account_state``. If the executor doesn't track per-
    position state, we return an empty list.
    """
    executor = getattr(request.app.state.engine, "executor", None)
    if executor is None:
        return []
    if isinstance(executor, PaperExecutor):
        return executor.positions()
    if hasattr(executor, "positions"):
        try:
            result = executor.positions()
            if isinstance(result, list):
                return result
        except Exception:
            return []
    return []


@router.get("/overview")
async def overview(request: Request) -> dict:
    """One-shot endpoint that bundles status + performance + a thin equity curve.

    Useful for the dashboard's top-of-page render so we only hit the API once.
    """
    perf = await performance()
    curve_rows = await equity_curve(limit=120)
    pos = await positions(request)
    engine = getattr(request.app.state, "engine", None)
    status_payload: Dict[str, Any] = {}
    if engine is not None:
        status_payload = {
            "running": engine.status.running,
            "strategy": engine.status.active_strategy,
            "market_regime": getattr(engine.status, "market_regime", None),
            "day_plan": getattr(engine.status, "day_plan", None),
            "broker": engine.executor.__class__.__name__ if engine.executor else None,
        }
    return {
        "status": status_payload,
        "performance": perf,
        "equity_curve": curve_rows,
        "positions": pos,
    }
