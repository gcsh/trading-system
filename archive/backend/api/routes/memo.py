"""Stage-11 Trade Memo endpoints.

Three surfaces:
  • ``GET /memo/trade/{id}``        — persisted memo for a trade
  • ``POST /memo/preview``          — generate a memo from a hypothetical context
  • ``POST /memo/regenerate/{id}``  — re-run the memo generator and update the row
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.bot.memo import (
    MEMO_SCHEMA_VERSION,
    TradeMemo,
    build_heuristic_memo,
    get_generator,
)
from backend.db import session_scope
from backend.models.trade import Trade

router = APIRouter(prefix="/memo", tags=["memo"])


def _read_memo_from_detail(detail_json: Optional[str]) -> Optional[Dict[str, Any]]:
    if not detail_json:
        return None
    try:
        d = json.loads(detail_json)
    except Exception:
        return None
    if isinstance(d, dict):
        return d.get("memo")
    return None


# ── persisted memo for a trade ──────────────────────────────────────────


@router.get("/trade/{trade_id}")
async def get_memo(trade_id: int) -> dict:
    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="trade not found")
        memo = _read_memo_from_detail(trade.detail_json)
        ticker = trade.ticker
        action = trade.action
    if memo is None:
        raise HTTPException(
            status_code=404,
            detail=("no memo persisted for this trade (likely a pre-Stage-11 row "
                     "or generator failed). Use POST /memo/regenerate/{id} to build one"),
        )
    return {"trade_id": trade_id, "ticker": ticker, "action": action,
             "memo": memo, "schema_version": MEMO_SCHEMA_VERSION}


# ── preview from hypothetical context ───────────────────────────────────


class PreviewBody(BaseModel):
    ticker: str
    action: str = "BUY_STOCK"
    strategy: str = "preview"
    signal_reason: str = ""
    confidence_num: Optional[float] = None
    regime: Optional[Dict[str, Any]] = None
    analytics: Optional[Dict[str, Any]] = None
    features: Optional[Dict[str, Any]] = None
    optimizer: Optional[Dict[str, Any]] = None
    abstain: Optional[Dict[str, Any]] = None
    cross_asset: Optional[Dict[str, Any]] = None
    stop_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    force_heuristic: bool = False


@router.post("/preview")
async def preview(body: PreviewBody) -> dict:
    """Generate a memo for a hypothetical decision. Used by the UI to
    show operators what the memo would look like BEFORE the order fires."""
    ctx = body.model_dump()
    if body.force_heuristic:
        memo = build_heuristic_memo(
            ticker=body.ticker, action=body.action, strategy=body.strategy,
            signal_reason=body.signal_reason,
            confidence_num=body.confidence_num, regime=body.regime,
            analytics=body.analytics, features=body.features,
            optimizer=body.optimizer, abstain=body.abstain,
            cross_asset=body.cross_asset, stop_pct=body.stop_pct,
            take_profit_pct=body.take_profit_pct,
        )
    else:
        ctx.pop("force_heuristic", None)
        memo = get_generator().generate(context=ctx)
    return {"memo": memo.to_dict(), "schema_version": MEMO_SCHEMA_VERSION}


# ── regenerate an existing trade's memo ─────────────────────────────────


@router.post("/regenerate/{trade_id}")
async def regenerate(trade_id: int) -> dict:
    """Re-run the memo generator on an existing trade + update detail_json.

    Useful for legacy rows that pre-date Stage-11 (no memo persisted) OR
    when you've upgraded the model and want to refresh historical memos.
    """
    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="trade not found")
        existing = {}
        try:
            existing = json.loads(trade.detail_json or "{}") or {}
        except Exception:
            existing = {}
        snap = existing.get("snapshot") or {}
        context = {
            "ticker": trade.ticker, "action": trade.action,
            "strategy": trade.strategy or "preview",
            "signal_reason": trade.reason or "",
            "confidence_num": trade.confidence,
            # We don't have the full Stage-3+4 context on a regenerate;
            # heuristic builder is robust to missing fields.
            "stop_pct": (existing.get("stop_loss_pct") or 0) / 100.0
                          if existing.get("stop_loss_pct") else None,
            "take_profit_pct": (existing.get("take_profit_pct") or 0) / 100.0
                                  if existing.get("take_profit_pct") else None,
        }
        memo = get_generator().generate(context=context).to_dict()
        existing["memo"] = memo
        trade.detail_json = json.dumps(existing)
    return {"trade_id": trade_id, "memo": memo,
             "schema_version": MEMO_SCHEMA_VERSION}
