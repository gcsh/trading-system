"""MITS Phase 8.5 — Incremental vector indexing pass.

Reads new rows since the per-namespace ``LakeSyncWatermark``, embeds
them, upserts to pgvector. Idempotent: the watermark stores the last
indexed row_id per namespace so re-runs are cheap.

Run on cron every 30 min during market hours (see scheduler).
Backfill CLI in ``bin/backfill_vectors.py`` walks all-time once at
deploy.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select

from backend.bot.ai import vector_store
from backend.db import session_scope
from backend.models.lake_sync import LakeSyncWatermark

logger = logging.getLogger(__name__)


def _watermark_row(session, namespace: str) -> LakeSyncWatermark:
    row = session.execute(
        select(LakeSyncWatermark).where(
            LakeSyncWatermark.layer == "vector",
            LakeSyncWatermark.scope == namespace,
        )
    ).scalar_one_or_none()
    if row is None:
        row = LakeSyncWatermark(
            layer="vector", scope=namespace, last_row_id=0,
            rows_written=0, status="init", detail="",
        )
        session.add(row)
        session.flush()
    return row


def _bump(row: LakeSyncWatermark, *, last_id: int, written: int) -> None:
    row.last_row_id = max(int(last_id), int(row.last_row_id or 0))
    row.rows_written = int(row.rows_written or 0) + int(written)
    row.last_sync_at = datetime.utcnow()
    row.status = "ok"


def index_pass(*, full: bool = False) -> Dict[str, Any]:
    """Walk every namespace's source table and upsert any new rows.

    ``full=True`` is the backfill path — resets the watermark to 0 so
    every row is re-embedded. Set False on cron.
    """
    vector_store.ensure_schema()
    summary: Dict[str, int] = {}
    summary.update(_pass_regime_snapshots(full=full))
    summary.update(_pass_market_observations(full=full))
    summary.update(_pass_eod_theses(full=full))
    summary.update(_pass_closed_trades(full=full))
    return summary


def _pass_regime_snapshots(*, full: bool) -> Dict[str, int]:
    from backend.models.intraday_regime_event import IntradayRegimeEvent
    written = 0
    with session_scope() as s:
        row = _watermark_row(s, "regime_snapshots")
        last_id = 0 if full else int(row.last_row_id or 0)
        events = s.execute(
            select(IntradayRegimeEvent)
            .where(IntradayRegimeEvent.id > last_id)
            .order_by(IntradayRegimeEvent.id.asc())
            .limit(2000)
        ).scalars().all()
        for ev in events:
            ok = vector_store.index_regime_snapshot(
                key=f"regime:{ev.id}",
                regime_state=str(getattr(ev, "regime_state", "") or ""),
                spy_30m=getattr(ev, "spy_30m_change_pct", None),
                vix_level=getattr(ev, "vix_level", None),
                breadth=getattr(ev, "breadth_pct_above_50d", None),
                put_call=getattr(ev, "put_call_ratio", None),
                sector_dispersion=getattr(ev, "sector_dispersion", None),
                top_flow_summary=str(getattr(ev, "context_summary", "") or ""),
                date_iso=(ev.timestamp.date().isoformat()
                              if getattr(ev, "timestamp", None) else ""),
            )
            if ok:
                written += 1
            last_id = max(last_id, int(ev.id))
        _bump(row, last_id=last_id, written=written)
    return {"regime_snapshots_written": written}


def _pass_market_observations(*, full: bool) -> Dict[str, int]:
    from backend.models.market_observation import MarketObservation
    written = 0
    with session_scope() as s:
        row = _watermark_row(s, "market_observations")
        last_id = 0 if full else int(row.last_row_id or 0)
        rows = s.execute(
            select(MarketObservation)
            .where(MarketObservation.id > last_id)
            .order_by(MarketObservation.id.asc())
            .limit(5000)
        ).scalars().all()
        for obs in rows:
            ok = vector_store.index_market_observation(
                observation_id=str(obs.id),
                ticker=str(getattr(obs, "ticker", "") or ""),
                pattern=str(getattr(obs, "pattern", "") or ""),
                regime=str(getattr(obs, "regime", "") or ""),
                features_json=str(getattr(obs, "features_json", "") or ""),
                date_iso=(getattr(obs, "ts", None).isoformat()
                              if getattr(obs, "ts", None) else ""),
            )
            if ok:
                written += 1
            last_id = max(last_id, int(obs.id))
        _bump(row, last_id=last_id, written=written)
    return {"market_observations_written": written}


def _pass_eod_theses(*, full: bool) -> Dict[str, int]:
    from backend.models.eod_analysis import EodAnalysis
    written = 0
    with session_scope() as s:
        row = _watermark_row(s, "eod_theses")
        last_id = 0 if full else int(row.last_row_id or 0)
        rows = s.execute(
            select(EodAnalysis)
            .where(EodAnalysis.id > last_id)
            .order_by(EodAnalysis.id.asc())
            .limit(2000)
        ).scalars().all()
        for an in rows:
            thesis_text = (str(getattr(an, "thesis", "") or "")
                                + " " + str(getattr(an, "narrative", "") or ""))
            ok = vector_store.index_eod_thesis(
                analysis_id=str(an.id),
                ticker=str(getattr(an, "ticker", "") or ""),
                analysis_date=(getattr(an, "analysis_date", None).isoformat()
                                  if getattr(an, "analysis_date", None) else ""),
                thesis_text=thesis_text,
                regime=str(getattr(an, "regime", "") or ""),
            )
            if ok:
                written += 1
            last_id = max(last_id, int(an.id))
        _bump(row, last_id=last_id, written=written)
    return {"eod_theses_written": written}


def _pass_closed_trades(*, full: bool) -> Dict[str, int]:
    from backend.models.trade import Trade
    written = 0
    with session_scope() as s:
        row = _watermark_row(s, "closed_trades")
        last_id = 0 if full else int(row.last_row_id or 0)
        rows = s.execute(
            select(Trade)
            .where(Trade.id > last_id)
            .where(Trade.status.in_(["closed", "closed_by_reset"]))
            .order_by(Trade.id.asc())
            .limit(2000)
        ).scalars().all()
        for tr in rows:
            detail = getattr(tr, "detail_json", None) or {}
            context_text = ""
            if isinstance(detail, dict):
                hyp = detail.get("opportunity_hypothesis") or {}
                context_text = (str(hyp.get("thesis") or "")
                                    + " " + str(detail.get("memo_summary") or ""))
            outcome = "win" if (tr.pnl or 0) > 0 else "loss"
            ok = vector_store.index_closed_trade(
                trade_id=str(tr.id),
                ticker=str(getattr(tr, "ticker", "") or ""),
                strategy=str(getattr(tr, "strategy", "") or ""),
                regime=str(getattr(tr, "regime", "") or "") or None,
                outcome=outcome,
                pnl=float(tr.pnl or 0.0),
                entry_iso=(getattr(tr, "opened_at", None).isoformat()
                              if getattr(tr, "opened_at", None) else ""),
                context_summary=context_text,
            )
            if ok:
                written += 1
            last_id = max(last_id, int(tr.id))
        _bump(row, last_id=last_id, written=written)
    return {"closed_trades_written": written}


__all__ = ["index_pass"]
