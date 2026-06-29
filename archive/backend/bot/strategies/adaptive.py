"""Adaptive selector: regime detection, daily planning, combo runner."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.bot.strategies.all_strategies import STRATEGY_REGISTRY
from backend.bot.strategies.base import Action, Signal


# Pre-defined multi-strategy combos. Each entry lists strategies that must all
# agree (action != HOLD and confidences average above threshold) for the combo
# to fire as a single Signal.
STRATEGY_COMBOS: Dict[str, List[str]] = {
    "wheel_income": ["cash_secured_put", "covered_call_wheel"],
    "trend_breakout": ["macd_momentum", "opening_range_breakout"],
    "vol_premium": ["iron_condor", "ratio_spread"],
    "earnings_play": ["earnings_straddle", "news_catalyst_momentum"],
    "hedged_growth": ["trend_pullback", "collar"],
}


# Regime → ranked strategy preference. The top of each list is the bot's
# primary pick when that regime is detected.
REGIME_PREFERENCE: Dict[str, List[str]] = {
    "trending_up": [
        "macd_momentum",
        "trend_pullback",
        "bull_call_spread",
        "opening_range_breakout",
        "cash_secured_put",
    ],
    "trending_down": [
        "news_catalyst_momentum",
        "opening_range_breakout",
        "vwap_reversion",
        "collar",
    ],
    "ranging": [
        "iron_condor",
        "ratio_spread",
        "vwap_reversion",
        "covered_call_wheel",
        "gap_fill",
    ],
    "volatile": [
        "earnings_straddle",
        "news_catalyst_momentum",
        "zero_dte_scalp",
        "collar",
    ],
}


@dataclass
class DayPlan:
    """The selector's plan for one trading session."""

    primary_strategy: str
    market_regime: str
    recommended_tickers: List[str]
    confidence_scores: Dict[str, float] = field(default_factory=dict)
    combo: Optional[str] = None
    reason: str = ""


class AdaptiveStrategy:
    """Score every strategy each cycle and pick a primary based on regime."""

    name = "adaptive"

    # -- regime -------------------------------------------------------------
    def detect_regime(self, market_data: Dict[str, Any]) -> str:
        """Classify the broader market regime.

        Order of checks matters: a high-VIX market is *volatile* even if SPY
        is trending. Ranging is the residual classification when ADX is weak.
        """
        vix = market_data.get("vix", 15)
        spy_adx = market_data.get("spy_adx", market_data.get("adx", 20))
        spy_trend = market_data.get("spy_trend", "neutral")

        if vix >= 30:
            return "volatile"
        if spy_adx < 15:
            return "ranging"
        if spy_adx >= 22:
            if spy_trend == "bullish":
                return "trending_up"
            if spy_trend == "bearish":
                return "trending_down"
        return "ranging"

    # -- scoring ------------------------------------------------------------
    def score_all(self, ticker: str, data: Dict[str, Any]) -> Dict[str, float]:
        """Return a {strategy_name: confidence} dict for one ticker.

        HOLD signals get a 0.0 score so the caller can sort and pick the best.
        """
        scores: Dict[str, float] = {}
        for name, strategy in STRATEGY_REGISTRY.items():
            sig = strategy.analyze(ticker, data)
            scores[name] = sig.confidence if sig.action != Action.HOLD else 0.0
        return scores

    # -- planning -----------------------------------------------------------
    def plan_day(self, tickers: List[str], market_data: Dict[str, Any]) -> DayPlan:
        """Pick the day's primary strategy and best ticker shortlist.

        Strategy scores are averaged across ``tickers``. Ties broken by regime
        preference order so the choice is stable across small data changes.
        """
        regime = self.detect_regime(market_data)
        per_ticker = market_data.get("tickers", {}) or {}

        # If the caller didn't provide per-ticker dicts, score every strategy
        # using the top-level market_data dict (acceptable for indices/ETFs).
        score_universe = per_ticker if per_ticker else {t: market_data for t in tickers}

        averaged: Dict[str, float] = {name: 0.0 for name in STRATEGY_REGISTRY}
        for ticker in tickers:
            data = score_universe.get(ticker, market_data)
            for name, score in self.score_all(ticker, data).items():
                averaged[name] += score
        for name in averaged:
            averaged[name] /= max(len(tickers), 1)

        preferred = REGIME_PREFERENCE.get(regime, list(STRATEGY_REGISTRY.keys()))

        # Adaptive feedback (Phase 3): down-weight strategies the learning loop
        # has flagged as failing in the CURRENT regime. Same scoring math; the
        # selector just picks the next-best instead of repeating yesterday's
        # mistake. Pure read of /learning/insights — best-effort, never raises.
        bad_combos: set = set()
        try:
            from backend.bot.learning import insights as _learn

            bad_combos = {f["combo"] for f in (_learn() or {}).get("failing_combos", [])}
        except Exception:
            bad_combos = set()

        def _penalty(name: str) -> float:
            return 0.3 if f"{name}::{regime}" in bad_combos else 1.0

        primary = max(
            preferred,
            key=lambda n: (averaged.get(n, 0.0) * _penalty(n), -preferred.index(n)),
        )

        # Shortlist the top 5 tickers by primary-strategy confidence.
        ticker_scores: List[tuple[str, float]] = []
        primary_strategy = STRATEGY_REGISTRY[primary]
        for ticker in tickers:
            data = score_universe.get(ticker, market_data)
            sig = primary_strategy.analyze(ticker, data)
            ticker_scores.append((ticker, sig.confidence if sig.action != Action.HOLD else 0.0))
        ticker_scores.sort(key=lambda x: x[1], reverse=True)
        recommended = [t for t, score in ticker_scores[:5] if score > 0]

        return DayPlan(
            primary_strategy=primary,
            market_regime=regime,
            recommended_tickers=recommended,
            confidence_scores=averaged,
            reason=(
                f"regime={regime}, avg score {averaged[primary]:.2f}"
                + (f", down-weighted {len(bad_combos)} failing combo(s)" if bad_combos else "")
            ),
        )

    # -- combos -------------------------------------------------------------
    def run_combo(
        self,
        ticker: str,
        data: Dict[str, Any],
        combo_name: str,
        threshold: float = 0.6,
    ) -> Optional[Signal]:
        if combo_name not in STRATEGY_COMBOS:
            raise ValueError(f"unknown combo: {combo_name}")
        strategies = STRATEGY_COMBOS[combo_name]
        signals = [STRATEGY_REGISTRY[name].analyze(ticker, data) for name in strategies]
        if any(s.action == Action.HOLD for s in signals):
            return None
        avg_conf = sum(s.confidence for s in signals) / len(signals)
        if avg_conf < threshold:
            return None
        primary = max(signals, key=lambda s: s.confidence)
        return Signal(
            action=primary.action,
            ticker=ticker,
            confidence=avg_conf,
            reason="; ".join(s.reason for s in signals),
            strategy=f"combo:{combo_name}",
            stop_loss=primary.stop_loss,
            take_profit=primary.take_profit,
            strike=primary.strike,
            dte=primary.dte,
            metadata={"combo": combo_name, "members": strategies, **primary.metadata},
        )

    # -- single-cycle convenience ------------------------------------------
    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        """Pick the highest-confidence non-HOLD signal across all 15 strategies."""
        best: Signal = Signal.hold(ticker, self.name, "no strategy produced a signal")
        for strategy in STRATEGY_REGISTRY.values():
            sig = strategy.analyze(ticker, data)
            if sig.action == Action.HOLD:
                continue
            if sig.confidence > best.confidence:
                best = sig
        if best.action != Action.HOLD:
            best.strategy = f"adaptive→{best.strategy}"
        return best
