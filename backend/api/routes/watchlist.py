"""Watchlist CRUD + live-quote enrichment."""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from backend.bot.data.finnhub import FinnhubClient
from backend.db import session_scope
from backend.models.watchlist import WatchlistItem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/watchlist", tags=["watchlist"])

_finnhub_client = FinnhubClient()


def _yf_quote(ticker: str) -> dict | None:
    """Fallback quote via yfinance when Finnhub isn't configured."""
    try:
        import yfinance as yf

        df = yf.download(ticker, period="5d", interval="1d", progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        # yfinance returns multi-index columns for some responses — flatten so
        # df["Close"] is a Series, not a 1-col DataFrame (which breaks float()).
        if hasattr(df.columns, "get_level_values"):
            df.columns = [c[0] for c in df.columns]
        last = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2]) if len(df) > 1 else last
        return {
            "price": last,
            "prev_close": prev,
            "change_pct": ((last - prev) / prev * 100) if prev else 0.0,
        }
    except Exception:
        logger.exception("yfinance quote for %s failed", ticker)
        return None


def _enrich(item: WatchlistItem) -> dict:
    base = item.to_dict()
    quote = None
    if _finnhub_client.available:
        fq = _finnhub_client.quote(item.ticker)
        if fq:
            quote = {
                "price": fq.price,
                "prev_close": fq.prev_close,
                "change_pct": ((fq.price - fq.prev_close) / fq.prev_close * 100)
                if fq.prev_close
                else 0.0,
                "source": "finnhub",
            }
    if quote is None:
        yq = _yf_quote(item.ticker)
        if yq:
            yq["source"] = "yfinance"
            quote = yq
    base["quote"] = quote
    return base


@router.get("/folders")
async def list_folders() -> List[str]:
    """Distinct folder (list_name) values, always including 'default'."""
    with session_scope() as session:
        rows = session.execute(select(WatchlistItem.list_name).distinct()).scalars().all()
    folders = sorted({r for r in rows if r} | {"default"})
    return folders


@router.get("/items")
async def list_items(list_name: str = "default") -> List[dict]:
    with session_scope() as session:
        rows = (
            session.execute(
                select(WatchlistItem)
                .where(WatchlistItem.list_name == list_name)
                .order_by(WatchlistItem.added_at)
            )
            .scalars()
            .all()
        )
        return [_enrich(r) for r in rows]


@router.post("")
async def add_item(payload: dict) -> dict:
    ticker = (payload.get("ticker") or "").strip().upper()
    list_name = (payload.get("list_name") or "default").strip()
    notes = payload.get("notes") or ""
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")
    is_new = False
    with session_scope() as session:
        existing = session.execute(
            select(WatchlistItem).where(
                WatchlistItem.list_name == list_name, WatchlistItem.ticker == ticker
            )
        ).scalar_one_or_none()
        if existing:
            return _enrich(existing)
        item = WatchlistItem(list_name=list_name, ticker=ticker, notes=notes)
        session.add(item)
        session.flush()
        is_new = True
        enriched = _enrich(item)
    # P1.3-FU4 — warm-start IV history backfill so the new ticker crosses
    # the min_samples floor immediately instead of waiting 30 days of
    # live capture. Run in a background thread so the POST returns
    # promptly (a 365-day backfill takes 30-90s).
    if is_new:
        import threading
        from backend.bot.data.iv_history import backfill as _iv_backfill
        def _warm_start() -> None:
            try:
                stats = _iv_backfill(ticker, lookback_days=365,
                                            pace_seconds=0.02)
                import logging
                logging.getLogger(__name__).info(
                    "watchlist warm-start iv backfill for %s: %s",
                    ticker, stats,
                )
            except Exception:
                pass
        threading.Thread(target=_warm_start, name=f"iv-warmstart-{ticker}",
                              daemon=True).start()

        # MITS Phase 0 — historical corpus bootstrap. Separate background
        # thread so it doesn't block the IV warm-start or the HTTP
        # response. Sets corpus_status="building" up front so the UI
        # immediately shows the new-ticker pipeline state.
        from backend.bot.corpus.auto_bootstrap import run_full_bootstrap
        from backend.db import session_scope as _ss
        from backend.models.corpus_status import CorpusStatus as _CS

        try:
            with _ss() as _s:
                _row = _s.execute(
                    select(_CS).where(_CS.ticker == ticker)
                ).scalar_one_or_none()
                if _row is None:
                    _row = _CS(ticker=ticker, status="building")
                    _s.add(_row)
                else:
                    _row.status = "building"
                    _row.error = None
        except Exception:
            logger.debug("corpus_status pre-mark failed for %s",
                              ticker, exc_info=True)

        def _corpus_bootstrap() -> None:
            try:
                result = run_full_bootstrap(ticker)
                logger.info("corpus bootstrap for %s: status=%s",
                                  ticker, result.get("status"))
            except Exception:
                logger.exception("corpus bootstrap thread failed for %s",
                                       ticker)
        threading.Thread(target=_corpus_bootstrap,
                              name=f"corpus-bootstrap-{ticker}",
                              daemon=True).start()
    return enriched


@router.delete("/{item_id}")
async def delete_item(item_id: int) -> dict:
    with session_scope() as session:
        item = session.get(WatchlistItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        session.delete(item)
        return {"deleted": item_id}


@router.patch("/{item_id}")
async def patch_item(item_id: int, payload: dict) -> dict:
    """Toggle per-ticker flags. Currently supports ``options_disabled``."""
    with session_scope() as session:
        item = session.get(WatchlistItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="not found")
        if "options_disabled" in payload:
            item.options_disabled = 1 if bool(payload["options_disabled"]) else 0
        if "notes" in payload:
            item.notes = str(payload["notes"])
        session.flush()
        return _enrich(item)
