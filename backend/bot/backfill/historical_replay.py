"""Historical-replay calibration corpus seeder (P2.1).

The idea
========
Walk historical daily bars for (ticker × strategy), feed each bar into
the strategy's ``analyze()``, capture the signal, then look forward
``forward_bars`` bars to compute the **realised forward outcome**. Write
each (signal, outcome) pair into the ``trades`` table as a *synthetic
closed trade* tagged with ``signal_source="historical_replay"``.

Why this matters
================
Calibration metrics (ECE / Brier) and the cohort matrix need ~30+ closed
trades to be statistically meaningful. The bot would otherwise have to
trade for weeks to bootstrap them. ThetaData's 8 years of history + the
free yfinance daily bars let us produce hundreds-to-thousands of
synthetic trades per strategy in minutes, populating the corpus with
empirical priors immediately.

Strict separation
=================
Synthetic trades are filterable via ``signal_source`` so live P&L
surfaces (``/portfolio/performance``, ``/trades/summary``) keep them
out of "your account" numbers. Calibration / cohort / lesson layers
include them so the model gets smart from history.

Scope of this module
====================
First pass: **stock signals only** (BUY_STOCK / SELL_STOCK and the
single-leg derivative actions that map to a price move). Multi-leg
options strategies are deferred to a follow-up that pulls ThetaData
historical EOD chains.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


HISTORICAL_REPLAY_SOURCE = "historical_replay"
DEFAULT_NOTIONAL = 1000.0      # USD per synthetic trade — scales pnl
DEFAULT_FORWARD_BARS = 5       # ~1 trading week for daily bars
DEFAULT_PERIOD = "2y"          # yfinance window
DEFAULT_INTERVAL = "1d"
DEFAULT_WARMUP_BARS = 50       # need enough bars for SMA200 / RSI to be stable


# Strategies whose Signal.action maps directly to a stock-direction trade we
# can grade against the forward bar close. The options-strategy outcomes
# need historical chains; deferred to a follow-up pass.
STOCK_DIRECTION_ACTIONS = {
    "BUY_STOCK", "SELL_STOCK",
    "BUY_CALL", "BUY_PUT",      # single-leg directional bets — grade by underlying
}


@dataclass
class ReplayStats:
    ticker: str
    strategy: str
    period: str
    interval: str
    bars_scanned: int = 0
    signals_emitted: int = 0
    trades_written: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    win_rate: Optional[float] = None
    avg_pnl: Optional[float] = None
    errors: int = 0

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


def _sig_action_to_pnl_direction(action: str) -> Optional[int]:
    """Sign of the bet relative to underlying. +1 = long, -1 = short, None = skip."""
    a = action.upper()
    if a in ("BUY_STOCK", "BUY_CALL"):
        return 1
    if a in ("SELL_STOCK", "BUY_PUT"):
        # Short-stock and long-put both profit when underlying falls.
        return -1
    return None


def _compute_pnl(direction: int, forward_return_pct: float,
                    notional: float) -> float:
    """forward_return_pct is in PERCENT (e.g. 1.5 = +1.5%). PnL is
    notional · (signed return / 100)."""
    return round(notional * direction * forward_return_pct / 100.0, 2)


def _has_existing(session, ticker: str, strategy: str, source: str) -> int:
    """Count existing synthetic rows for (ticker, strategy, source) so the
    runner can no-op gracefully when re-invoked."""
    from sqlalchemy import select, func
    from backend.models.trade import Trade
    return session.execute(
        select(func.count(Trade.id))
        .where(Trade.ticker == ticker.upper())
        .where(Trade.strategy == strategy)
        .where(Trade.signal_source == source)
    ).scalar() or 0


def _write_synthetic_trade(
    session,
    *,
    ticker: str,
    strategy: str,
    action: str,
    bar_timestamp: datetime,
    entry_price: float,
    pnl: float,
    confidence: float,
    reason: str,
    forward_bars: int,
    notional: float,
    regime_trend: str = "unknown",
    regime_volatility: str = "normal",
    regime_gamma: str = "unknown",
) -> None:
    """Insert one synthetic closed trade + a matching DecisionLog row so
    Brier / ECE / cohort_matrix / journal.similar_trades all see this
    trade with a populated ``win_probability`` (= confidence). Without
    the DecisionLog row, ``label.win_probability`` stays None and
    calibration metrics return None on the whole corpus.

    Both rows are inserted in the same session and flush so the trade
    primary key is available before the DecisionLog row references it.
    """
    from backend.models.trade import Trade
    from backend.models.decision_log import DecisionLog
    qty = (notional / entry_price) if entry_price > 0 else 1.0
    row = Trade(
        timestamp=bar_timestamp,
        ticker=ticker.upper(),
        action=action,
        quantity=round(qty, 4),
        price=round(entry_price, 4),
        strategy=strategy,
        signal_source=HISTORICAL_REPLAY_SOURCE,
        confidence=round(float(confidence or 0.0), 4),
        reason=(reason or "")[:500],
        paper=1,
        pnl=pnl,
        status="closed",
        instrument="stock",
        detail_json=None,
        # P1.5 — synthetic stock trades grade against the yfinance close
        # of the forward bar. Not a "real" fill, mark as paper_stub so the
        # pricing-telemetry surface counts them honestly.
        pricing_source="paper_stub",
        accounting_version=1,
    )
    session.add(row)
    session.flush()  # populate row.id so the DecisionLog FK points correctly

    decision = DecisionLog(
        timestamp=bar_timestamp,
        ticker=ticker.upper(),
        action=action,
        strategy=strategy,
        confidence=round(float(confidence or 0.0), 4),
        status=f"{HISTORICAL_REPLAY_SOURCE}_closed",
        regime_trend=regime_trend,
        regime_volatility=regime_volatility,
        regime_gamma=regime_gamma,
        grade="",
        # confidence IS our best-effort probability for synthetic trades.
        # The live engine derives win_probability from the ranker; here
        # we use the strategy's own confidence as the prior.
        win_probability=round(float(confidence or 0.0), 4),
        trade_id=row.id,
        outcome_pnl=pnl,
        outcome_status="closed",
        # P1.1 — explicit synthetic marker so live-only analytics
        # filter cleanly without a Trade join.
        signal_source=HISTORICAL_REPLAY_SOURCE,
    )
    session.add(decision)


def replay_ticker_strategy(
    *,
    ticker: str,
    strategy_name: str,
    period: str = DEFAULT_PERIOD,
    interval: str = DEFAULT_INTERVAL,
    forward_bars: int = DEFAULT_FORWARD_BARS,
    notional: float = DEFAULT_NOTIONAL,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    max_signals: Optional[int] = None,
    overwrite: bool = False,
    commit: bool = True,
) -> ReplayStats:
    """Replay one (ticker, strategy) combination, writing synthetic trades.

    Idempotent unless ``overwrite=True``: when prior rows exist for this
    (ticker, strategy, source) tuple the function logs and returns the
    cached count without rewriting.

    Returns a :class:`ReplayStats` describing what happened. ``commit``
    can be set False so a caller (CLI) can wrap multiple replays in one
    transaction.
    """
    from backend.bot.backtest import (
        _bar_snapshot, compute_indicators, fetch_candles,
    )
    from backend.bot.strategies.all_strategies import get_strategy
    from backend.bot.strategies.adaptive import AdaptiveStrategy
    from backend.bot.strategies.base import Action
    from backend.db import session_scope

    stats = ReplayStats(
        ticker=ticker.upper(),
        strategy=strategy_name,
        period=period,
        interval=interval,
    )

    df = fetch_candles(ticker, period=period, interval=interval)
    if df is None or df.empty:
        stats.skipped["no_candles"] = 1
        return stats
    stats.bars_scanned = len(df)
    ind = compute_indicators(df)

    if strategy_name in (None, "", "adaptive"):
        strategy = AdaptiveStrategy()
    else:
        try:
            strategy = get_strategy(strategy_name)
        except ValueError:
            stats.skipped["unknown_strategy"] = 1
            return stats

    closes = [float(df["Close"].iloc[i]) for i in range(len(df))]
    timestamps = []
    for idx in df.index:
        if hasattr(idx, "to_pydatetime"):
            timestamps.append(idx.to_pydatetime())
        elif isinstance(idx, datetime):
            timestamps.append(idx)
        else:
            try:
                timestamps.append(datetime.fromisoformat(str(idx)))
            except Exception:
                timestamps.append(datetime.utcnow())

    with session_scope() as session:
        existing = _has_existing(
            session, ticker.upper(), strategy_name, HISTORICAL_REPLAY_SOURCE,
        )
        if existing and not overwrite:
            stats.trades_written = existing
            stats.skipped["already_seeded"] = existing
            return stats
        if existing and overwrite:
            from sqlalchemy import delete, select
            from backend.models.trade import Trade
            from backend.models.decision_log import DecisionLog
            # Cascade: delete DecisionLog rows referencing the about-to-be-
            # deleted trade ids first so we don't orphan them.
            doomed_ids = session.execute(
                select(Trade.id)
                .where(Trade.ticker == ticker.upper())
                .where(Trade.strategy == strategy_name)
                .where(Trade.signal_source == HISTORICAL_REPLAY_SOURCE)
            ).scalars().all()
            if doomed_ids:
                session.execute(
                    delete(DecisionLog).where(DecisionLog.trade_id.in_(doomed_ids))
                )
            session.execute(
                delete(Trade)
                .where(Trade.ticker == ticker.upper())
                .where(Trade.strategy == strategy_name)
                .where(Trade.signal_source == HISTORICAL_REPLAY_SOURCE)
            )

        last_action: Optional[str] = None
        signals = 0
        wins = 0
        sum_pnl = 0.0

        for i in range(warmup_bars, len(df) - forward_bars):
            try:
                snap = _bar_snapshot(df, ind, i)
            except Exception:
                stats.errors += 1
                continue
            try:
                sig = strategy.analyze(ticker.upper(), snap)
            except Exception:
                stats.errors += 1
                continue
            if sig is None:
                continue
            action_str = sig.action.value if hasattr(sig.action, "value") else str(sig.action)

            # Skip HOLD and dedupe consecutive same-direction signals so we
            # don't write the same setup 30 days in a row.
            if action_str == "HOLD":
                continue
            if action_str == last_action:
                continue
            last_action = action_str

            direction = _sig_action_to_pnl_direction(action_str)
            if direction is None:
                # Multi-leg / options strategies — skip in first-pass.
                stats.skipped[f"unsupported_{action_str}"] = (
                    stats.skipped.get(f"unsupported_{action_str}", 0) + 1
                )
                continue
            stats.signals_emitted += 1

            entry = closes[i]
            exit_price = closes[i + forward_bars]
            if entry <= 0:
                stats.errors += 1
                continue
            forward_return_pct = (exit_price - entry) / entry * 100.0
            pnl = _compute_pnl(direction, forward_return_pct, notional)

            # Derive regime tags from the snap so DecisionLog rows are
            # cohort-matchable (P2.1-FU). compute_indicators gives us
            # ADX (trend strength), RSI (momentum), and we can derive
            # volatility from rolling std of returns.
            adx_val = float(snap.get("adx") or 20.0)
            rsi_val = float(snap.get("rsi") or 50.0)
            ma50 = float(snap.get("ma50") or entry)
            ma200 = float(snap.get("ma200") or entry)
            if adx_val >= 25 and entry > ma200:
                rt = "uptrend"
            elif adx_val >= 25 and entry < ma200:
                rt = "downtrend"
            elif adx_val < 18:
                rt = "ranging"
            else:
                rt = "neutral"
            # Volatility: cheap proxy from forward bars stdev — uses
            # info "available at bar i" by reading the trailing 20 bars.
            try:
                window = closes[max(0, i - 20):i]
                if len(window) > 5:
                    import statistics as _stat
                    rets = [(window[k] - window[k - 1]) / window[k - 1]
                                for k in range(1, len(window))
                                if window[k - 1] > 0]
                    if rets:
                        sd = _stat.pstdev(rets)
                        rv = ("high" if sd > 0.03
                                else "low" if sd < 0.008 else "normal")
                    else:
                        rv = "normal"
                else:
                    rv = "normal"
            except Exception:
                rv = "normal"

            _write_synthetic_trade(
                session,
                ticker=ticker.upper(),
                strategy=strategy_name,
                action=action_str,
                bar_timestamp=timestamps[i],
                entry_price=entry,
                pnl=pnl,
                confidence=float(sig.confidence or 0.0),
                reason=(sig.reason or ""),
                forward_bars=forward_bars,
                notional=notional,
                regime_trend=rt,
                regime_volatility=rv,
            )
            signals += 1
            if pnl > 0:
                wins += 1
            sum_pnl += pnl

            if max_signals and signals >= max_signals:
                break

        if commit:
            session.commit()
        stats.trades_written = signals
        stats.win_rate = round(wins / signals, 4) if signals else None
        stats.avg_pnl = round(sum_pnl / signals, 2) if signals else None
    return stats


def replay_universe(
    *,
    tickers: Sequence[str],
    strategies: Sequence[str],
    **kwargs,
) -> Dict[str, Dict[str, Any]]:
    """Cartesian product backfill across (ticker × strategy). Returns a
    nested ``{ticker: {strategy: stats_dict}}``.

    Errors in one (ticker, strategy) cell are logged and don't abort the
    rest of the run.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for t in tickers:
        out[t.upper()] = {}
        for s in strategies:
            try:
                stats = replay_ticker_strategy(
                    ticker=t, strategy_name=s, **kwargs,
                )
            except Exception as exc:
                logger.warning("replay %s/%s failed: %s", t, s, exc, exc_info=True)
                stats = ReplayStats(
                    ticker=t.upper(), strategy=s,
                    period=kwargs.get("period", DEFAULT_PERIOD),
                    interval=kwargs.get("interval", DEFAULT_INTERVAL),
                    errors=1,
                )
            out[t.upper()][s] = stats.to_dict()
    return out


# ── CLI ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":  # pragma: no cover — manual ops helper
    import argparse, json
    parser = argparse.ArgumentParser(
        description="Historical replay → synthetic calibration corpus")
    parser.add_argument("--ticker", action="append", required=True,
                            help="Ticker symbol; repeat for multiple.")
    parser.add_argument("--strategy", action="append", required=True,
                            help="Strategy name; repeat for multiple.")
    parser.add_argument("--period", default=DEFAULT_PERIOD,
                            help="yfinance period (e.g. 2y, 5y, 8y).")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--forward-bars", type=int, default=DEFAULT_FORWARD_BARS,
                            help="Bars ahead to read forward outcome (default 5).")
    parser.add_argument("--notional", type=float, default=DEFAULT_NOTIONAL)
    parser.add_argument("--max-signals", type=int, default=None,
                            help="Per (ticker, strategy) cap.")
    parser.add_argument("--overwrite", action="store_true",
                            help="Delete prior synthetic rows for these "
                                  "(ticker, strategy) pairs before re-running.")
    args = parser.parse_args()

    result = replay_universe(
        tickers=args.ticker,
        strategies=args.strategy,
        period=args.period,
        interval=args.interval,
        forward_bars=args.forward_bars,
        notional=args.notional,
        max_signals=args.max_signals,
        overwrite=args.overwrite,
    )
    print(json.dumps(result, indent=2, default=str))
