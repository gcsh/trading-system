"""Stage-15 — Claude-per-agent richer reasoning.

The eight specialist agents in ``bot/agents`` each emit a one-line
heuristic reasoning string. Operators want 1-2 sentences of richer
context — but eight separate Claude calls per cycle would be wasteful.

This module batches every agent's vote + context into a **single**
``messages.create`` and asks Claude to produce a one-sentence enrichment
for each. Falls through to the original heuristic strings on any error
so the panel is never empty.

Output format (parsed):
  ``{agent_name: enriched_reasoning_string}``

Cost tracking via ``bot/ai_cost`` (the standard pattern).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from backend.bot.agents import AGENT_FUNCS, AgentVote
from backend.config import TUNABLES, anthropic_key

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You enrich agent reasoning for a trading bot's decision panel.

You will be given an array of agent votes, each with: agent (one of market, flow, options, macro, risk, portfolio, execution, devils_advocate), stance (buy/sell/abstain/hold), confidence (0-1), and a one-line heuristic reason.

For each agent, output ONE additional sentence (≤ 25 words) that:
  • Adds analytical depth not in the heuristic line
  • References specific numbers from the supplied context where possible
  • Stays consistent with the agent's role (Market Regime, Options Flow, Macro/Cross-Asset, Portfolio Risk, Devil's Advocate, etc.)

Return ONLY a JSON object mapping agent name to the enrichment sentence. No preamble, no markdown fences, no extra keys.

Example output:
{"market": "NVDA's 50/200 MA cross 6 days ago confirms the bullish trend phase; recent ADX expansion supports continuation.", "flow": "Dark-pool tape leans 18% bullish over the last hour with 3 large sweeps on the 215C strike."}"""


class AgentVoiceEnricher:
    """Stateful enricher that caches the Anthropic client across calls."""

    def __init__(self, *, api_key: Optional[str] = None,
                    client: Any = None) -> None:
        self._api_key = api_key
        self._client = client

    def _key(self) -> str:
        return self._api_key if self._api_key is not None else anthropic_key()

    @property
    def available(self) -> bool:
        return self._client is not None or bool(self._key())

    def _anthropic(self):
        if self._client is None:
            from anthropic import Anthropic
            self._client = Anthropic(api_key=self._key(), timeout=30.0)
        return self._client

    def enrich(self, *, votes: List[AgentVote],
                  context: Dict[str, Any]) -> Dict[str, str]:
        """Return ``{agent_name: enrichment_sentence}``. Empty dict on any
        failure (callers should fall back to ``vote.reasoning``)."""
        if not self.available or not votes:
            return {}
        payload = {
            "context": {
                "ticker": context.get("ticker"),
                "action": context.get("action"),
                "strategy": context.get("strategy"),
                "regime": (context.get("analytics") or {}).get("regime"),
                "rank": (context.get("analytics") or {}).get("rank"),
                "probability": (context.get("analytics") or {}).get("probability"),
                "features": (context.get("analytics") or {}).get("features")
                              or context.get("features"),
                "cross_asset": context.get("cross_asset"),
            },
            "agents": [
                {
                    "agent": v.agent, "stance": v.stance,
                    "confidence": round(v.confidence, 3),
                    "heuristic": v.reasoning,
                }
                for v in votes
            ],
        }
        try:
            client = self._anthropic()
            model = getattr(TUNABLES, "agents_claude_model",
                              getattr(TUNABLES, "chat_model",
                                       "claude-sonnet-4-6"))
            response = client.messages.create(
                model=model,
                max_tokens=getattr(TUNABLES, "agents_claude_max_tokens", 800),
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                          "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user",
                           "content": "Vote panel:\n" + json.dumps(payload, default=str)}],
            )
            try:
                from backend.bot.ai_cost import record_from_response
                record_from_response(surface="agents", model=model, response=response,
                                        extra={"ticker": context.get("ticker")})
            except Exception:
                pass
            text = "".join(b.text for b in response.content
                            if getattr(b, "type", None) == "text")
            parsed = self._parse_json(text)
            if not isinstance(parsed, dict):
                return {}
            # Validate: keep only string values for known agent names.
            valid_names = {n for n, _, _ in AGENT_FUNCS}
            return {
                str(k): str(v)[:240] for k, v in parsed.items()
                if k in valid_names and isinstance(v, (str, int, float))
            }
        except Exception as exc:
            logger.warning("agent voice enrich failed: %s", exc)
            return {}

    @staticmethod
    def _parse_json(text: str) -> Any:
        text = (text or "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        try:
            return json.loads(text[start: end + 1])
        except Exception:
            return None


# ── module-level singleton ──────────────────────────────────────────────


_ENRICHER: Optional[AgentVoiceEnricher] = None


def get_enricher() -> AgentVoiceEnricher:
    global _ENRICHER
    if _ENRICHER is None:
        _ENRICHER = AgentVoiceEnricher()
    return _ENRICHER


def reset_enricher() -> None:
    """Test helper — drop the cached enricher."""
    global _ENRICHER
    _ENRICHER = None
