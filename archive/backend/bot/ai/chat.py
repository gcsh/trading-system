"""Live chat copilot — converse with the user about their bot and the market.

Given a short live context block (account, positions, today's plan, recent bot
actions) plus the running message history, ask Claude for a concise, plain-English
reply. With no API key it returns a friendly setup hint instead of raising.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from backend.config import TUNABLES, anthropic_key

logger = logging.getLogger(__name__)

CHAT_SYSTEM = """You are the user's friendly trading copilot, living inside their personal autonomous PAPER-trading app. The user ranges from beginner to intermediate.

You are given a live context block (account value, cash, open positions, autonomy state, market regime, recent bot actions, watchlist). Use it to answer concretely and specifically — refer to their actual numbers and positions when relevant.

Style: plain English, concise (a few sentences unless asked for depth). Explain trading concepts simply when asked. Be honest about uncertainty and risk; never promise or guarantee returns. Remember everything here is PAPER money for learning. You are not a licensed financial advisor — add a short reminder of that only when the user asks whether to put in real money. If the user asks you to take an action (e.g. "buy AAPL"), explain that they control trading via the autonomy switch / AI Brain toggle and you can reason about it, but you don't place orders from chat."""


def available() -> bool:
    return bool(anthropic_key())


def _client():
    from anthropic import Anthropic  # type: ignore

    return Anthropic(api_key=anthropic_key(), timeout=30.0)


def chat_reply(
    message: str,
    history: Optional[List[dict]] = None,
    context: str = "",
    client: Any = None,
) -> str:
    """Return Claude's reply. Never raises — returns an error/help string instead."""
    if not (client or anthropic_key()):
        return ("I'm not connected to Claude yet — add your Anthropic API key (the box just "
                "below this chat, or in Settings) and I'll be right with you.")

    messages: List[dict] = []
    for turn in (history or [])[-10:]:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    user_block = (f"Live context:\n{context}\n\n" if context else "") + f"User: {message}"
    messages.append({"role": "user", "content": user_block})

    try:
        cl = client or _client()
        resp = cl.messages.create(
            model=TUNABLES.chat_model,
            max_tokens=TUNABLES.chat_max_tokens,
            system=[{"type": "text", "text": CHAT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        try:
            from backend.bot.ai_cost import record_from_response
            record_from_response(surface="chat", model=TUNABLES.chat_model, response=resp)
        except Exception:
            pass
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        return text or "(no reply)"
    except Exception as exc:
        logger.warning("chat reply failed: %s", exc)
        return f"Sorry — I hit an error talking to the model: {exc}"
