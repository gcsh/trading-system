"""AI signal blending: combine Claude narrative + ML quant + rule-based strategy."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.bot.ai.claude_signal import ClaudeSignalGenerator
from backend.bot.ai.ml_signal import MLSignalModel
from backend.bot.strategies.base import Action, Signal

logger = logging.getLogger(__name__)


@dataclass
class BlendedSignal:
    """Weighted blend of AI + ML + rule-based recommendations."""

    final_action: Action
    final_confidence: float
    components: Dict[str, Dict[str, Any]]


def _weighted_action(signals: List[tuple[Signal, float]]) -> tuple[Action, float, Dict[str, float]]:
    """Combine actions by summed confidence per direction. Returns (action, conf, scores)."""
    scores: Dict[Action, float] = {}
    for signal, weight in signals:
        if signal.action == Action.HOLD:
            continue
        scores[signal.action] = scores.get(signal.action, 0.0) + signal.confidence * weight
    if not scores:
        return Action.HOLD, 0.0, {}
    best_action = max(scores, key=scores.get)
    total = sum(scores.values()) or 1.0
    # Normalised confidence (0..1) — strong consensus pushes toward 1.
    confidence = min(1.0, scores[best_action] / total * (scores[best_action] / 1.0 + 0.5))
    return best_action, confidence, {a.value: round(s, 3) for a, s in scores.items()}


class SignalBlender:
    """Hold instances of each signal source and blend their output."""

    def __init__(
        self,
        claude: Optional[ClaudeSignalGenerator] = None,
        ml: Optional[MLSignalModel] = None,
    ) -> None:
        self.claude = claude or ClaudeSignalGenerator()
        self.ml = ml or MLSignalModel()

    def blend(
        self,
        ticker: str,
        snapshot: Dict[str, Any],
        rule_signal: Signal,
        ai_config: Dict[str, Any] | None = None,
        news: List[dict] | None = None,
    ) -> Signal:
        config = ai_config or {}
        rule_weight = float(config.get("rule_weight", 1.0))
        claude_weight = float(config.get("claude_weight", 0.5)) if config.get("claude_enabled") else 0.0
        ml_weight = float(config.get("ml_weight", 0.5)) if config.get("ml_enabled") else 0.0

        components: Dict[str, Dict[str, Any]] = {
            "rule": {
                "action": rule_signal.action.value,
                "confidence": rule_signal.confidence,
                "reason": rule_signal.reason,
                "weight": rule_weight,
            }
        }
        signals: List[tuple[Signal, float]] = [(rule_signal, rule_weight)]

        if claude_weight > 0 and self.claude.available:
            claude_sig = self.claude.analyze(ticker, snapshot, news=news)
            components["claude"] = {
                "action": claude_sig.action.value,
                "confidence": claude_sig.confidence,
                "reason": claude_sig.reason,
                "weight": claude_weight,
            }
            signals.append((claude_sig, claude_weight))

        if ml_weight > 0 and self.ml.available:
            ml_sig = self.ml.analyze(ticker, snapshot)
            components["ml"] = {
                "action": ml_sig.action.value,
                "confidence": ml_sig.confidence,
                "reason": ml_sig.reason,
                "weight": ml_weight,
            }
            signals.append((ml_sig, ml_weight))

        # If only the rule-based signal is in play, just return it (preserve reason / metadata).
        if len(signals) == 1:
            rule_signal.metadata.setdefault("ai_components", components)
            return rule_signal

        action, confidence, scores = _weighted_action(signals)
        components["scores"] = scores

        # When the winning action is the rule's, preserve the rule signal
        # whole — strike, dte, stop_loss, take_profit, original metadata.
        # Dropping these turned every CSP into "needs $0.00 cash; have
        # $0.00" because risk reads signal.strike directly.
        if action == rule_signal.action:
            merged_metadata = dict(rule_signal.metadata or {})
            merged_metadata["ai_components"] = components
            rule_signal.metadata = merged_metadata
            rule_signal.confidence = confidence
            return rule_signal

        # Claude (or ML) overrode the action. Carry the winning input's
        # strike/dte/exit levels — falls back to the rule's where the
        # override didn't compute them (e.g. Claude says BUY_STOCK but
        # rule had a CSP strike, which doesn't apply).
        override_source = next(
            (s for s, _w in signals if s.action == action), rule_signal,
        )
        final_strategy = rule_signal.strategy or "blended"
        return Signal(
            ticker=ticker,
            action=action,
            confidence=confidence,
            reason=f"blended: {scores}",
            strategy=final_strategy,
            stop_loss=override_source.stop_loss,
            take_profit=override_source.take_profit,
            strike=override_source.strike,
            dte=override_source.dte,
            metadata={
                **(override_source.metadata or {}),
                "ai_components": components,
                "source": "ai_blender",
            },
        )
