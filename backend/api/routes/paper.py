"""Paper-account endpoints: state, positions, reset."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from backend.bot.paper_executor import PaperExecutor

router = APIRouter(prefix="/paper", tags=["paper"])


def _executor(request: Request) -> PaperExecutor:
    executor = request.app.state.engine.executor
    if not isinstance(executor, PaperExecutor):
        raise HTTPException(status_code=400, detail="active broker is not local_paper")
    return executor


@router.get("/state")
async def paper_state(request: Request) -> dict:
    return _executor(request).get_account_state()


@router.get("/positions")
async def paper_positions(request: Request) -> list[dict]:
    return _executor(request).positions()


@router.post("/reset")
async def paper_reset(request: Request, payload: Optional[dict] = None) -> dict:
    starting = None
    if payload:
        starting = payload.get("starting_cash")
    return _executor(request).reset(starting_cash=starting)
