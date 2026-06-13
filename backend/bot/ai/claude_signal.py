"""Claude API signal generator.

Builds a structured prompt with the market snapshot + recent news, asks Claude
for a directional view, and converts the response into a :class:`Signal`.

Uses prompt caching on the static system instructions so per-cycle cost is
just the dynamic user-message tokens.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.bot.strategies.base import Action, Signal
from backend.config import SETTINGS

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are a quantitative trading assistant. For each ticker you are given a market snapshot, you must return a single JSON object with this exact shape:

{"action": "BUY_STOCK" | "SELL_STOCK" | "BUY_CALL" | "BUY_PUT" | "HOLD", "confidence": 0.0-1.0, "reasoning": "one short sentence"}

Rules:
- Only return BUY signals when the technical setup AND news/context align bullishly. Same for bearish.
- Confidence above 0.7 is reserved for setups where multiple signals confirm.
- HOLD when the picture is mixed or unclear — this is the safe default.
- Never recommend complex options (spreads, condors) — the bot handles those separately.
- Return ONLY the JSON object. No prose before or after.
"""


@dataclass
class ClaudeResult:
    signal: Signal
    raw_text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


def _summarise_snapshot(snapshot: Dict[str, Any]) -> str:
    """Pull just the fields Claude needs and format them as compact YAML-ish text."""
    keys = [
        "price", "rsi", "macd", "macd_signal", "ma50", "ma200",
        "volume", "avg_volume", "iv_rank", "adx", "vix",
        "news_score", "earnings_days", "pe_ratio",
        "spy_trend", "market_trend", "high_52w", "prev_close",
    ]
    lines = []
    for key in keys:
        if key in snapshot:
            lines.append(f"  {key}: {snapshot[key]}")
    return "\n".join(lines)


def _summarise_news(news: List[dict] | None, limit: int = 5) -> str:
    if not news:
        return "  (no recent news)"
    out = []
    for item in news[:limit]:
        title = item.get("headline") or item.get("title") or ""
        if not title:
            continue
        out.append(f"  - {title[:200]}")
    return "\n".join(out) if out else "  (no recent news)"


def build_messages(ticker: str, snapshot: Dict[str, Any], news: List[dict] | None) -> List[dict]:
    """User-message payload for a single ticker request."""
    body = (
        f"Ticker: {ticker}\n"
        f"Snapshot:\n{_summarise_snapshot(snapshot)}\n\n"
        f"Recent headlines:\n{_summarise_news(news)}\n\n"
        "Return the JSON object now."
    )
    return [{"role": "user", "content": body}]


def _parse_response(text: str) -> dict:
    """Extract the JSON object from Claude's reply. Robust against stray text."""
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in response")
    return json.loads(text[start : end + 1])


def _to_signal(ticker: str, parsed: dict) -> Signal:
    action_str = (parsed.get("action") or "HOLD").upper()
    try:
        action = Action(action_str)
    except ValueError:
        action = Action.HOLD
    confidence = float(parsed.get("confidence", 0.0))
    confidence = max(0.0, min(1.0, confidence))
    reason = (parsed.get("reasoning") or "")[:200]
    return Signal(
        ticker=ticker,
        action=action,
        confidence=confidence,
        reason=reason,
        strategy="claude_ai",
        metadata={"source": "claude_ai", "raw": parsed},
    )


class ClaudeSignalGenerator:
    """Stateful wrapper around the Anthropic client."""

    def __init__(self, api_key: Optional[str] = None, client: Any = None) -> None:
        self.api_key = api_key if api_key is not None else SETTINGS.anthropic_api_key
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self.api_key) or self._client is not None

    def _anthropic(self):
        if self._client is None:
            from anthropic import Anthropic  # type: ignore

            self._client = Anthropic(api_key=self.api_key, timeout=30.0)
        return self._client

    def analyze(
        self,
        ticker: str,
        snapshot: Dict[str, Any],
        news: List[dict] | None = None,
    ) -> Signal:
        if not self.available:
            return Signal.hold(ticker, "claude_ai", "claude api key missing")
        try:
            client = self._anthropic()
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=200,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=build_messages(ticker, snapshot, news),
            )
            text = "".join(
                block.text for block in response.content if getattr(block, "type", None) == "text"
            )
            parsed = _parse_response(text)
            return _to_signal(ticker, parsed)
        except Exception as exc:
            logger.exception("claude_ai analyze failed: %s", exc)
            return Signal.hold(ticker, "claude_ai", f"claude error: {exc}")
