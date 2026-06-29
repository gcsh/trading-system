"""Stage-13.C9 Research Layer endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from backend.bot.research import generate_digest

router = APIRouter(prefix="/research", tags=["research"])


@router.get("/digest")
async def digest() -> dict:
    """What changed today? Walks all researchers and returns findings."""
    return generate_digest().to_dict()
