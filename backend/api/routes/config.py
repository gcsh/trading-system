"""Config get/save endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from backend.db import session_scope
from backend.models.config import load_config, save_config

router = APIRouter(prefix="/config", tags=["config"])


def _public(cfg: dict) -> dict:
    """Never expose the raw Anthropic key to the browser — report only whether
    one is set, and blank the value so a round-tripped save can't leak/wipe it."""
    key = (cfg.get("anthropic_api_key") or "").strip()
    out = dict(cfg)
    out["anthropic_api_key"] = ""
    out["anthropic_key_set"] = bool(key)
    return out


@router.get("")
async def get_config() -> dict:
    with session_scope() as session:
        return _public(load_config(session))


@router.post("")
async def post_config(payload: dict) -> dict:
    with session_scope() as session:
        current = load_config(session)
        incoming = dict(payload or {})
        incoming.pop("anthropic_key_set", None)
        # A blank/absent key in the payload must not erase a stored key — only a
        # non-empty value overwrites it.
        new_key = (incoming.get("anthropic_api_key") or "").strip()
        incoming["anthropic_api_key"] = new_key or (current.get("anthropic_api_key") or "")
        merged = {**current, **incoming}
        save_config(session, merged)
        return _public(merged)
