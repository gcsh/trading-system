"""Backtest / strategy-visualization endpoint."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Query

from backend.bot.backtest import run_backtest, run_compare

router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.get("/compare/{ticker}")
async def compare(
    ticker: str,
    strategies: str = Query(..., description="comma-separated strategy names"),
    period: str = "6mo",
    interval: str = "1d",
) -> dict:
    names = [s.strip() for s in strategies.split(",") if s.strip()]
    return run_compare(ticker.upper(), names, period=period, interval=interval)


@router.get("/{strategy_name}/{ticker}")
async def backtest(
    strategy_name: str,
    ticker: str,
    period: str = "6mo",
    interval: str = "1d",
) -> dict:
    return run_backtest(strategy_name, ticker.upper(), period=period, interval=interval)
