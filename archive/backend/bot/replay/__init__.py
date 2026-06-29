"""Stage-8 session replay.

Replays historical candles through a strategy bar-by-bar, surfacing the
sequence of signals the bot WOULD have emitted. Distinct from Stage-1's
backtest engine (which simulates a long/flat P&L curve) — replay outputs
the raw signal stream so the user can debug "why did the bot fire here?".

Deterministic given the historical candle data. Uses the same
``MarketDataAdapter._bar_snapshot``-style flattener so the snapshot at each
bar is identical to what the live engine would have seen.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ReplayEvent:
    bar_index: int
    timestamp: str
    close: float
    action: str
    confidence: float
    reason: str = ""
    strategy: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ReplayReport:
    ticker: str
    strategy: str
    period: str
    interval: str
    n_bars: int
    n_signals: int
    actions: Dict[str, int] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── runner ─────────────────────────────────────────────────────────────────


def replay_session(*, strategy_name: str, ticker: str,
                     period: str = "1mo", interval: str = "1d",
                     limit_events: int = 200,
                     ) -> ReplayReport:
    """Pull candles + indicators (same path the backtest uses), step the
    strategy bar-by-bar, and return every signal emitted."""
    # Inline the imports — replay is a heavy path; we don't want it on the
    # cold-start critical path of every server boot.
    from backend.bot.backtest import (
        _bar_snapshot, compute_indicators, fetch_candles,
    )
    from backend.bot.strategies.all_strategies import get_strategy
    from backend.bot.strategies.adaptive import AdaptiveStrategy

    df = fetch_candles(ticker, period=period, interval=interval)
    if df is None or df.empty:
        return ReplayReport(ticker=ticker.upper(), strategy=strategy_name,
                              period=period, interval=interval,
                              n_bars=0, n_signals=0,
                              notes=[f"no candles available for {ticker}"])
    ind = compute_indicators(df)
    if strategy_name in (None, "", "adaptive"):
        strategy = AdaptiveStrategy()
    else:
        try:
            strategy = get_strategy(strategy_name)
        except ValueError:
            return ReplayReport(ticker=ticker.upper(), strategy=strategy_name,
                                  period=period, interval=interval,
                                  n_bars=len(df), n_signals=0,
                                  notes=[f"unknown strategy '{strategy_name}'"])

    events: List[ReplayEvent] = []
    last_action: Optional[str] = None
    actions_count: Dict[str, int] = {}
    for i in range(len(df)):
        snap = _bar_snapshot(df, ind, i)
        try:
            sig = strategy.analyze(ticker, snap)
        except Exception:
            continue
        if sig is None:
            continue
        action = sig.action.value
        # only record action transitions to avoid the same-action spam
        if action == last_action and action != "HOLD":
            continue
        last_action = action
        actions_count[action] = actions_count.get(action, 0) + 1
        if len(events) < limit_events:
            ts = df.index[i]
            events.append(ReplayEvent(
                bar_index=i,
                timestamp=(ts.isoformat() if hasattr(ts, "isoformat")
                            else str(ts)),
                close=float(df["Close"].iloc[i]),
                action=action,
                confidence=round(float(sig.confidence or 0.0), 3),
                reason=sig.reason or "",
                strategy=sig.strategy or strategy_name,
            ))

    return ReplayReport(
        ticker=ticker.upper(), strategy=strategy_name,
        period=period, interval=interval,
        n_bars=len(df), n_signals=sum(actions_count.values()),
        actions=actions_count,
        events=[e.to_dict() for e in events],
    )
