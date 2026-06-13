"""Telegram webhook endpoint — bidirectional commands.

Telegram POSTs every authorized user's message here. We:

  1. Validate the path-segment shared secret (rejecting 403 otherwise —
     prevents random internet traffic from triggering side effects).
  2. Validate the inbound chat_id against the allowlist
     (``SETTINGS.telegram_chat_id`` — supports comma-separated list for
     teams). Unknown chat_ids get a polite "not authorized" reply via
     the notifier.
  3. Dispatch the text to the command handler and send the reply back.

Telegram retries non-200 responses, so this endpoint always returns 200
even on internal failures (we log + return ``{"ok": False}``).
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from backend.bot.notifications.commands import dispatch
from backend.config import SETTINGS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram-webhook"])


def _allowed_chat_ids() -> set[str]:
    """Parse the operator allowlist from settings.

    Default: the single configured chat_id. Operators can comma-separate
    multiple ids in ``TB_TELEGRAM_CHAT_ID`` to allow a small team.
    """
    raw = (SETTINGS.telegram_chat_id or "").strip()
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


@router.post("/webhook/{secret}")
async def webhook(secret: str, request: Request) -> Dict[str, Any]:
    """Receive an Update from Telegram and reply via the notifier.

    Returns 200 OK to Telegram on success, 403 only on secret mismatch
    (forces Telegram to back off the wrong endpoint).
    """
    expected = (SETTINGS.telegram_webhook_secret or "").strip()
    if not expected or secret != expected:
        logger.warning("telegram webhook 403: bad secret")
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        payload = await request.json()
    except Exception:
        logger.warning("telegram webhook: malformed JSON")
        return {"ok": False, "reason": "malformed"}

    notifier = getattr(request.app.state, "telegram_notifier", None)
    if notifier is None or not notifier.enabled:
        # Configuration mismatch: webhook is reachable but the notifier
        # isn't. Return 200 (so Telegram doesn't retry) but log loud.
        logger.warning(
            "telegram webhook: notifier missing/disabled "
            "(secret matched, but no creds configured)"
        )
        return {"ok": False, "reason": "notifier disabled"}

    # Extract message + chat_id from the Update payload.
    message = (payload.get("message")
                  or payload.get("edited_message")
                  or {})
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    text = (message.get("text") or "").strip()

    if not chat_id:
        return {"ok": True, "reason": "no chat_id"}

    allowed = _allowed_chat_ids()
    if chat_id not in allowed:
        logger.warning(
            "telegram webhook: chat_id %s not authorized "
            "(allowlist=%s)",
            chat_id, sorted(allowed),
        )
        # Reply to the unauthorized user so they aren't left wondering.
        # We POST directly with the inbound chat_id, NOT via send_text
        # (which uses our authorized chat_id), so the message goes back
        # to the actual sender.
        try:
            notifier._session.post(
                f"https://api.telegram.org/bot{notifier.bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "Not authorized.",
                    "parse_mode": "HTML",
                },
                timeout=5.0,
            )
        except Exception:
            logger.debug("unauthorized reply failed", exc_info=True)
        return {"ok": False, "reason": "not authorized"}

    if not text:
        return {"ok": True, "reason": "no text"}

    engine = request.app.state.engine
    try:
        reply = dispatch(text, engine)
    except Exception as exc:
        logger.exception("telegram webhook dispatch failed")
        reply = f"<b>Internal error:</b> {repr(exc)[:200]}"

    # Send reply back to the sender's chat_id directly (not via
    # send_text, which uses the configured operator chat_id only).
    try:
        notifier._session.post(
            f"https://api.telegram.org/bot{notifier.bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": reply,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=8.0,
        )
    except Exception:
        logger.exception("telegram webhook reply failed")
        return {"ok": False, "reason": "reply failed"}

    return {"ok": True}
