"""Telegram notifier control surface.

Routes:

    GET  /notifications/telegram/status   healthcheck snapshot
    GET  /notifications/telegram/config   current filter config (UI loads this)
    PUT  /notifications/telegram/config   persist filter config
    POST /notifications/telegram/test     send a canned test message

The filter config lives inside the bot_config JSON blob (so it's
persisted alongside all the other UI-tweakable settings; reset-aware
via the existing system_reset module).
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from backend.bot.notifications.filters import TelegramFilterConfig
from backend.bot.notifications.formatters import format_test_message
from backend.db import session_scope
from backend.models.config import load_config, save_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications/telegram", tags=["notifications"])


def _get_notifier(request: Request):
    notifier = getattr(request.app.state, "telegram_notifier", None)
    if notifier is None:
        raise HTTPException(
            status_code=503,
            detail="telegram notifier not wired into app state",
        )
    return notifier


@router.get("/status")
async def telegram_status(request: Request) -> Dict[str, Any]:
    notifier = _get_notifier(request)
    return notifier.healthcheck()


@router.get("/config")
async def get_filter_config() -> Dict[str, Any]:
    with session_scope() as session:
        cfg = load_config(session) or {}
    persisted = cfg.get("telegram_filters") or {}
    return TelegramFilterConfig.from_dict(persisted).to_dict()


@router.put("/config")
async def put_filter_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Save the operator-tweaked filter config.

    Round-trips: posts arbitrary keys, server merges them onto the
    env-tunable defaults via ``TelegramFilterConfig.from_dict`` (so
    omitted fields fall back rather than getting wiped to None).
    """
    merged = TelegramFilterConfig.from_dict(payload or {}).to_dict()
    with session_scope() as session:
        current = load_config(session) or {}
        current["telegram_filters"] = merged
        save_config(session, current)
    return merged


@router.post("/test")
async def test_send(request: Request) -> Dict[str, Any]:
    """Fire a known canned message to confirm wiring.

    Bypasses filters so the test always reaches the operator.
    """
    notifier = _get_notifier(request)
    text = format_test_message()
    delivered = notifier.send_text(text, bypass_filters=True)
    return {
        "ok": bool(delivered),
        "enabled": notifier.enabled,
        "health": notifier.healthcheck(),
    }
