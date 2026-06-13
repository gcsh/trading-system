"""Stage-11 Trade Memo system — hedge-fund-memo per decision.

Every actionable signal (entry OR exit) produces a structured memo the
operator can reason about. The memo is the FOUNDATION the Mission Control
UI + decision-lineage view + multi-agent consensus all sit on.

Two backends:
  • **Claude** when `ANTHROPIC_API_KEY` is set — best quality, names the
    actual story; structured output via the system prompt's JSON schema.
  • **Heuristic** otherwise — composes from existing analytics
    (regime, rank reasoning, optimizer caps, abstain decision) so the UI
    always has something to render.

The memo is generated at decision time and persisted alongside the trade
in ``Trade.detail_json`` so it survives restart + is queryable. Nothing in
this module touches the broker — it's a pure synthesis of data we already
have.

Memo schema (frozen — extend by versioning, don't rename):

    {
      "thesis": str,             # one-sentence headline reasoning
      "confidence": "low|medium|high|very_high",
      "bull_case": [str, ...],
      "bear_case": [str, ...],
      "invalidation": str,       # one-line "if X then exit" rule
      "exit_plan": str,          # TP/SL/time rules in plain English
      "risk_factors": [str, ...],
      "regime_context": str,     # cross-asset + macro framing
      "source": "claude|heuristic",
      "schema_version": int,
    }
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.bot.memo.templates import (
    SYSTEM_PROMPT,
    MEMO_SCHEMA_VERSION,
    confidence_label,
)
from backend.config import TUNABLES, anthropic_key

logger = logging.getLogger(__name__)


@dataclass
class TradeMemo:
    thesis: str = ""
    confidence: str = "medium"
    bull_case: List[str] = field(default_factory=list)
    bear_case: List[str] = field(default_factory=list)
    invalidation: str = ""
    exit_plan: str = ""
    risk_factors: List[str] = field(default_factory=list)
    regime_context: str = ""
    source: str = "heuristic"
    schema_version: int = MEMO_SCHEMA_VERSION
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── heuristic builder (always works) ─────────────────────────────────────


def build_heuristic_memo(*,
                          ticker: str,
                          action: str,
                          strategy: str,
                          signal_reason: str,
                          confidence_num: Optional[float] = None,
                          regime: Optional[Dict[str, Any]] = None,
                          analytics: Optional[Dict[str, Any]] = None,
                          features: Optional[Dict[str, Any]] = None,
                          optimizer: Optional[Dict[str, Any]] = None,
                          abstain: Optional[Dict[str, Any]] = None,
                          cross_asset: Optional[Dict[str, Any]] = None,
                          stop_pct: Optional[float] = None,
                          take_profit_pct: Optional[float] = None,
                          ) -> TradeMemo:
    """Compose a memo from the per-decision context we already collect.

    Designed so the heuristic path is never WORSE than what TradeDetail
    showed before — it just structures the same data into the memo schema
    so the UI + downstream agents can consume it uniformly.
    """
    act = action.upper()
    if "PUT" in act or act.startswith("SELL"):
        direction = "short"
    elif act.startswith("BUY"):
        direction = "long"
    else:
        direction = "neutral"
    regime = regime or {}
    analytics = analytics or {}
    features = features or {}
    optimizer = optimizer or {}
    abstain = abstain or {}
    cross_asset = cross_asset or {}

    grade = (analytics.get("rank") or {}).get("grade") or "—"
    win_prob = (analytics.get("probability") or {}).get("probability")
    rank_reasoning = (analytics.get("rank") or {}).get("reasoning") or []

    # Thesis: one-liner using regime + grade + strategy
    regime_label = regime.get("label") or regime.get("trend") or "unknown regime"
    thesis = (
        f"{action.replace('_', ' ').title()} {ticker} as a {grade}-graded "
        f"{strategy} setup in a {regime_label} tape"
    )

    # Confidence: qualitative from the numeric
    confidence = confidence_label(confidence_num if confidence_num is not None
                                     else (win_prob or 0.5))

    # Bull case: rank components that argued FOR the trade + cross-asset alignment
    bull: List[str] = []
    for r in rank_reasoning:
        # The ranker's reasoning list mixes pro/con. Heuristically pick the
        # pros (high prob, multi-tf confluence, regime alignment).
        if any(k in r.lower() for k in ("high win", "confluence", "regime align",
                                          "aligned", "expanding", "long gamma")):
            bull.append(r)
    if cross_asset.get("regime_label") == "risk_on_compressed_vol" and direction == "long":
        bull.append("cross-asset state risk-on with compressed vol — tape supports longs")
    if features.get("composite_bias") and float(features["composite_bias"]) > 0.3 \
       and direction == "long":
        bull.append(f"composite feature bias +{features['composite_bias']:.2f} confirms direction")
    if not bull:
        bull.append(signal_reason or "signal triggered by strategy logic")

    # Bear case: rank components that argued AGAINST + portfolio risks
    bear: List[str] = []
    for r in rank_reasoning:
        if any(k in r.lower() for k in ("disagrees", "fighting", "pin prob",
                                          "weak", "below", "reject")):
            bear.append(r)
    if cross_asset.get("volatility") == "spiking":
        bear.append("cross-asset volatility spiking — entries are risky")
    if abstain.get("triggered_rules"):
        bear.append(f"abstain rules fired: {', '.join(abstain['triggered_rules'])}")
    if optimizer.get("cluster_blocked"):
        bear.append(f"cluster cap would have blocked — high concentration risk")
    if not bear:
        bear.append("no specific bear flags from the analytics layer")

    # Invalidation: convert stop_loss into a falsifiable statement
    if stop_pct:
        if direction == "long":
            invalidation = (f"price closes {stop_pct*100:.1f}% below entry, OR "
                              f"regime flips bearish, OR cluster cap fires")
        else:
            invalidation = (f"price closes {stop_pct*100:.1f}% above entry, OR "
                              f"vol spikes through the next OPEX")
    else:
        invalidation = "explicit stop-loss not provided; manage on regime flip"

    # Exit plan: explain staged exit + IV-aware behaviour
    parts: List[str] = []
    if take_profit_pct:
        parts.append(f"TP1 at +{take_profit_pct*100:.0f}% takes 50% off (Stage-10 staged exit)")
    if stop_pct:
        parts.append(f"hard stop at -{stop_pct*100:.0f}%")
    parts.append("ATR-trail runner; time-stop after 240 min if MFE < 0.5%")
    if direction == "long":
        parts.append("close immediately if a high-impact macro print enters the ±30 min window")
    exit_plan = "; ".join(parts)

    # Risk factors: optimizer caps + abstain + drawdown context
    risks: List[str] = []
    if optimizer.get("drawdown_pct", 0) > 0.05:
        risks.append(f"portfolio in {optimizer['drawdown_pct']:.1%} drawdown — "
                       f"size already reduced")
    if optimizer.get("recommended_dollar", 0) < optimizer.get("requested_dollar", 1) * 0.5:
        risks.append("optimizer cut requested size by > 50% — caps are binding")
    if features.get("pinning_probability") and float(features["pinning_probability"]) > 0.6:
        risks.append(f"high pin probability ({features['pinning_probability']:.0%}) "
                       f"near a dealer wall")
    if not risks:
        risks.append("no elevated risk flags from current analytics")

    # Regime context: cross-asset + narrative one-liner
    regime_context = (
        f"Regime: {regime_label}. "
        f"Cross-asset: equities {cross_asset.get('equities', 'unknown')}, "
        f"vol {cross_asset.get('volatility', 'unknown')}, "
        f"yields {cross_asset.get('yields', 'unknown')}."
    )

    return TradeMemo(
        thesis=thesis, confidence=confidence,
        bull_case=bull[:5], bear_case=bear[:5],
        invalidation=invalidation, exit_plan=exit_plan,
        risk_factors=risks[:5], regime_context=regime_context,
        source="heuristic",
    )


# ── Claude-backed generator ──────────────────────────────────────────────


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON in memo response")
    return json.loads(text[start: end + 1])


class MemoGenerator:
    """Stateful generator — caches the Anthropic client and falls through
    to the heuristic on any failure (never blocks the trade flow)."""

    def __init__(self, *, api_key: Optional[str] = None, client: Any = None) -> None:
        self._api_key = api_key
        self._client = client

    def _key(self) -> str:
        return self._api_key if self._api_key is not None else anthropic_key()

    @property
    def claude_available(self) -> bool:
        return self._client is not None or bool(self._key())

    def _anthropic(self):
        if self._client is None:
            from anthropic import Anthropic  # type: ignore
            self._client = Anthropic(api_key=self._key(), timeout=30.0)
        return self._client

    def generate(self, *, context: Dict[str, Any]) -> TradeMemo:
        """Produce a memo. Always returns one — Claude path → heuristic
        fallback on any error.

        ``context`` is the same dict the heuristic builder consumes plus
        an optional ``narrative`` field for macro framing.
        """
        heuristic = build_heuristic_memo(**{k: v for k, v in context.items()
                                              if k != "narrative"})
        if not self.claude_available:
            return heuristic
        try:
            client = self._anthropic()
            payload = self._user_prompt(context)
            model = getattr(TUNABLES, "memo_model",
                              getattr(TUNABLES, "chat_model",
                                       "claude-sonnet-4-6"))
            response = client.messages.create(
                model=model,
                max_tokens=getattr(TUNABLES, "memo_max_tokens", 800),
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                          "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": payload}],
            )
            # Stage-12.B6 — record token spend for this call.
            try:
                from backend.bot.ai_cost import record_from_response
                record_from_response(surface="memo", model=model,
                                        response=response,
                                        trade_id=context.get("trade_id"))
            except Exception:
                pass
            text = "".join(b.text for b in response.content
                            if getattr(b, "type", None) == "text")
            parsed = _parse_json(text)
        except Exception as exc:
            logger.warning("memo generate failed (falling back): %s", exc)
            return heuristic
        return TradeMemo(
            thesis=str(parsed.get("thesis") or heuristic.thesis)[:280],
            confidence=str(parsed.get("confidence") or heuristic.confidence),
            bull_case=[str(x)[:200] for x in (parsed.get("bull_case") or heuristic.bull_case)][:6],
            bear_case=[str(x)[:200] for x in (parsed.get("bear_case") or heuristic.bear_case)][:6],
            invalidation=str(parsed.get("invalidation") or heuristic.invalidation)[:280],
            exit_plan=str(parsed.get("exit_plan") or heuristic.exit_plan)[:400],
            risk_factors=[str(x)[:200] for x in (parsed.get("risk_factors")
                                                     or heuristic.risk_factors)][:6],
            regime_context=str(parsed.get("regime_context") or heuristic.regime_context)[:300],
            source="claude",
        )

    @staticmethod
    def _user_prompt(context: Dict[str, Any]) -> str:
        """Compress the decision context into a Claude prompt. Keeps the
        relevant facts, drops the noise."""
        snippet = {
            "ticker": context.get("ticker"),
            "action": context.get("action"),
            "strategy": context.get("strategy"),
            "signal_reason": context.get("signal_reason"),
            "confidence_num": context.get("confidence_num"),
            "regime": context.get("regime"),
            "rank": (context.get("analytics") or {}).get("rank"),
            "probability": (context.get("analytics") or {}).get("probability"),
            "features": context.get("features"),
            "cross_asset": context.get("cross_asset"),
            "stop_pct": context.get("stop_pct"),
            "take_profit_pct": context.get("take_profit_pct"),
            "abstain": context.get("abstain"),
            "optimizer": context.get("optimizer"),
            "narrative": context.get("narrative"),
        }
        return "Decision context:\n" + json.dumps(snippet, default=str, indent=2)


# Module-level singleton — reuses the Anthropic client between calls
_GENERATOR: Optional[MemoGenerator] = None


def get_generator() -> MemoGenerator:
    global _GENERATOR
    if _GENERATOR is None:
        _GENERATOR = MemoGenerator()
    return _GENERATOR


def reset_generator() -> None:
    """Test helper — drop the cached singleton so a fresh key/client takes effect."""
    global _GENERATOR
    _GENERATOR = None
