"""Analytics coordinator — the single entry point that runs the analytical
layer (features → regime → confluence → probability → ranking) for one ticker
and one signal, returning a unified result the engine and the UI both consume.

Loop-safe by default: confluence (the only network-bound piece) is skipped unless
explicitly enabled, so calling this every cycle is cheap.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

from backend.bot.confluence import ConfluenceScore, confluence_for, score_confluence
from backend.bot.features import build_features
from backend.bot.probability import SignalProbability, score_signal
from backend.bot.ranker import TradeRank, passes_min_grade, rank_trade
from backend.bot.regime import MarketRegime, detect_regime
from backend.bot.strategies.base import Signal


@dataclass
class AnalyticsResult:
    regime: MarketRegime = field(default_factory=MarketRegime)
    features: Dict[str, Any] = field(default_factory=dict)
    confluence: Optional[ConfluenceScore] = None
    probability: SignalProbability = field(default_factory=SignalProbability)
    rank: TradeRank = field(default_factory=TradeRank)

    def to_dict(self) -> dict:
        d = asdict(self)
        # Replace nested dataclasses with their dict form for clean JSON.
        d["regime"] = self.regime.to_dict()
        d["probability"] = self.probability.to_dict()
        d["rank"] = self.rank.to_dict()
        d["confluence"] = self.confluence.to_dict() if self.confluence else None
        return d


class AnalyticsEngine:
    """Stateless coordinator. ``evaluate`` is pure given the inputs."""

    def evaluate(
        self,
        ticker: str,
        snapshot: Dict[str, Any],
        signal: Signal,
        *,
        confluence: Optional[ConfluenceScore] = None,
        fetch_confluence: bool = False,
        ml_weight: float = 0.0,
    ) -> AnalyticsResult:
        features = build_features(snapshot)
        regime = detect_regime(snapshot)
        # Confluence is opt-in: pass one in, or ask the engine to fetch (network).
        if confluence is None and fetch_confluence:
            try:
                confluence = confluence_for(ticker)
            except Exception:
                confluence = None
        probability = score_signal(signal, features, regime,
                                      confluence=confluence, ml_weight=ml_weight)
        rank = rank_trade(probability, regime, confluence, features)
        return AnalyticsResult(
            regime=regime, features=features, confluence=confluence,
            probability=probability, rank=rank,
        )


def gate_by_grade(rank: TradeRank, min_grade: Optional[str]) -> bool:
    """True if the trade is allowed to execute given the minimum grade."""
    return passes_min_grade(rank.grade, min_grade)


# Re-export the pure scoring helper so callers don't need to know the submodule.
__all__ = [
    "AnalyticsEngine", "AnalyticsResult", "gate_by_grade",
    "score_confluence", "build_features", "detect_regime",
]
