"""Action enum, Signal model, and Strategy base class for the 15-strategy suite."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class Action(str, Enum):
    HOLD = "HOLD"
    BUY_STOCK = "BUY_STOCK"
    SELL_STOCK = "SELL_STOCK"
    BUY_CALL = "BUY_CALL"
    BUY_PUT = "BUY_PUT"
    BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
    BUY_STRADDLE = "BUY_STRADDLE"
    IRON_CONDOR = "IRON_CONDOR"
    SELL_COVERED_CALL = "SELL_COVERED_CALL"
    SELL_CSP = "SELL_CSP"
    RATIO_SPREAD = "RATIO_SPREAD"
    COLLAR = "COLLAR"
    # Synthetic actions used only by the engine's exit manager for clear labeling
    # when a held position is force-closed (e.g. option hit expiry / TP / SL).
    CLOSE_OPTION = "CLOSE_OPTION"


@dataclass
class Signal:
    """A strategy's trade recommendation.

    ``stop_loss`` and ``take_profit`` are percentage thresholds (e.g. 25.0 = 25%).
    ``strike`` and ``dte`` are populated for option strategies.
    ``metadata`` carries strategy-specific structured data (spread legs, targets, etc.).
    """

    action: Action
    ticker: str = ""
    confidence: float = 0.0
    reason: str = ""
    strategy: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    strike: Optional[float] = None
    dte: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def hold(cls, ticker: str = "", strategy: str = "", reason: str = "no signal") -> "Signal":
        return cls(action=Action.HOLD, ticker=ticker, confidence=0.0, reason=reason, strategy=strategy)

    def is_actionable(self, threshold: float = 0.6) -> bool:
        return self.action != Action.HOLD and self.confidence >= threshold


class Strategy:
    """Base class. Each strategy reads a flat data dict and returns a :class:`Signal`.

    UI-facing metadata (label + description + category) lives on the class
    so the frontend can render the strategy list from `/strategies/catalog`
    without hardcoding names. Subclasses override `name` (the slug used in
    the registry + signal_source column), `label` (human-friendly title),
    `description` (1-line summary), and `category` (one of: stock,
    long_option, defined_risk, premium_selling, complex).
    """

    name: str = "base"
    label: str = "Base"
    description: str = ""
    category: str = "stock"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:  # pragma: no cover - interface
        raise NotImplementedError
