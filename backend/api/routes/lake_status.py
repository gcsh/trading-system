"""MITS Phase 8.8 — Lake Status + admin controls.

Read-only ``GET /lake/status`` for the UI. Destructive endpoints
(``POST /lake/snapshot/now``, ``POST /lake/vectors/reindex``) are
gated by a shared-secret header (``X-Lake-Admin-Secret``) that the
operator configures via ``TB_LAKE_ADMIN_SECRET``. Empty secret =
endpoint refuses, no exceptions.

MITS Phase 9.5 — adds ``GET /lake/health/alerts`` (read) +
``POST /lake/health/alerts/{id}/ack`` (acknowledge).  The hourly
``_lake_health_check`` cron in ``backend/bot/scheduler.py`` writes new
``LakeHealthAlert`` rows when thresholds trip; this endpoint surfaces
them to the operator banner.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException
from sqlalchemy import select

from backend.bot.data import gold, lake
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.lake_health_alert import LakeHealthAlert

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lake", tags=["lake"])

# 60-second TTL cache for the lake_status payload. The handler walks
# S3 layers + pgvector + gold snapshot dates on every call — quick on
# a warm bucket but enough to add up across rapid cockpit polls.
# Pattern matches the ``_THESIS_CACHE_TTL_SEC`` idiom in
# ``backend/api/routes/analysis.py`` (module-level dict, monotonic
# clock, expires_at comparison).
_LAKE_STATUS_CACHE_TTL_SEC = 60.0
_LAKE_STATUS_CACHE: Dict[str, Any] = {"value": None, "expires_at": 0.0}
_LAKE_STATUS_CACHE_LOCK = threading.Lock()

# MITS Phase 11.I — separate router so the operator UI hits a stable
# path (`/lake-status/sources`) without colliding with the existing
# `/lake/...` admin surface.
status_router = APIRouter(prefix="/lake-status", tags=["lake_status"])


# Canonical Phase 11 source roster. Mirrors the list in
# ``backend.bot.monitoring.source_health._EXPECTED_SOURCES`` so the UI
# always renders the full grid even before the 00:01 ET cron has
# written its first row for a brand-new source.
_PHASE11_SOURCES = [
    "thetadata_stocks_daily",
    "thetadata_stocks_intraday_1m",
    "thetadata_stocks_intraday_5m",
    "thetadata_iv_history",
    "thetadata_options_eod",
    "fred",
    "edgar_form4",
    "edgar_13f",
    "alpaca_quotes",
    "finnhub_news",
    "alphavantage_transcripts",
    "detector_replay",
]


def _require_admin(secret: Optional[str]) -> None:
    expected = (TUNABLES.lake_admin_secret or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Lake admin endpoints disabled (set TB_LAKE_ADMIN_SECRET)",
        )
    if (secret or "").strip() != expected:
        raise HTTPException(
            status_code=403, detail="invalid admin secret",
        )


@router.get("/status")
async def lake_status() -> Dict[str, Any]:
    """Return per-layer byte/object counts + vector namespace stats."""
    now = time.monotonic()
    with _LAKE_STATUS_CACHE_LOCK:
        if (_LAKE_STATUS_CACHE["value"] is not None
                and _LAKE_STATUS_CACHE["expires_at"] > now):
            return _LAKE_STATUS_CACHE["value"]
    layers: Dict[str, Any] = {}
    for prefix in ("bronze", "silver", "gold", "athena"):
        stat = lake.stat_layer(prefix)
        layers[prefix] = {
            "bytes": stat.bytes,
            "object_count": stat.object_count,
            "last_modified": stat.last_modified,
        }
    # Vector stats — best-effort; gracefully empty if pgvector
    # isn't reachable from this host.
    vector_stats: Dict[str, Any] = {}
    try:
        from backend.bot.ai import vector_store
        vector_stats = vector_store.namespace_stats()
    except Exception:
        vector_stats = {}
    # Recent gold snapshot dates for the disaster-recovery readout.
    snapshot_dates: Dict[str, list] = {}
    for table in gold.SNAPSHOT_TABLES[:8]:  # Top-8 critical tables.
        try:
            snapshot_dates[table] = lake.list_gold_dates(table)[-7:]
        except Exception:
            snapshot_dates[table] = []
    payload = {
        "enabled": bool(TUNABLES.lake_bronze_enabled),
        "bucket": TUNABLES.lake_bucket,
        "region": TUNABLES.lake_region,
        "fetched_at": datetime.utcnow().isoformat(),
        "layers": layers,
        "vectors": vector_stats,
        "recent_snapshots": snapshot_dates,
    }
    with _LAKE_STATUS_CACHE_LOCK:
        _LAKE_STATUS_CACHE["value"] = payload
        _LAKE_STATUS_CACHE["expires_at"] = (
            time.monotonic() + _LAKE_STATUS_CACHE_TTL_SEC
        )
    return payload


_snapshot_lock = threading.Lock()
_reindex_lock = threading.Lock()


@router.get("/memory")
async def memory_pressure() -> Dict[str, Any]:
    """MITS Phase 11.1 #9 — process memory chip data.

    The Lake Status page paints a green/yellow/red chip from this
    endpoint. Backfill / embed / ferry crons gate themselves on the
    same probe so the operator can predict whether a manual launch
    will run or auto-defer.
    """
    try:
        from backend.bot.data.memory_guard import memory_status
        status = memory_status()
        return {
            **status.to_dict(),
            "pause_threshold_pct": float(getattr(
                TUNABLES, "backfill_memory_pause_pct", 85.0)),
            "warn_threshold_pct": float(getattr(
                TUNABLES, "backfill_memory_warn_pct", 70.0)),
        }
    except Exception as e:
        return {"ok": True, "color": "green", "percent": 0.0,
                  "available_gb": 0.0, "total_gb": 0.0,
                  "error": str(e)}


@router.get("/duckdb")
async def duckdb_health() -> Dict[str, Any]:
    """MITS Phase 11.1 #7 — DuckDB read-layer health.

    Confirms the analytics path is live + httpfs is loaded so the
    operator can see whether the Lake Status grid + Parity panel are
    using DuckDB or the SQLite fallback.
    """
    try:
        from backend.bot.data import duckdb_reader
        return duckdb_reader.healthcheck()
    except Exception as e:
        return {"ok": False, "reason": f"import_failed: {e!r}"}


@router.post("/snapshot/now")
async def snapshot_now(
    x_lake_admin_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_admin(x_lake_admin_secret)
    if not _snapshot_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="snapshot already running")
    try:
        stats = gold.run_snapshot_pass()
        return {"ok": True, "stats": stats}
    finally:
        _snapshot_lock.release()


@router.post("/vectors/reindex")
async def reindex_vectors(
    full: bool = False,
    x_lake_admin_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    _require_admin(x_lake_admin_secret)
    if not _reindex_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="reindex already running")
    try:
        from backend.bot.ai import vector_indexing
        stats = vector_indexing.index_pass(full=bool(full))
        return {"ok": True, "stats": stats}
    finally:
        _reindex_lock.release()


@router.get("/health/alerts")
async def list_health_alerts(
    include_resolved: bool = False,
    limit: int = 100,
) -> Dict[str, Any]:
    """MITS Phase 9.5 — surface lake-health alerts to the UI.

    Default returns active alerts only (``resolved_at IS NULL``).
    Pass ``include_resolved=true`` to also include historic
    auto-resolved + operator-acknowledged rows.
    """
    with session_scope() as session:
        query = select(LakeHealthAlert)
        if not include_resolved:
            query = query.where(LakeHealthAlert.resolved_at.is_(None))
        query = query.order_by(LakeHealthAlert.created_at.desc()).limit(limit)
        rows = session.execute(query).scalars().all()
        out: List[Dict[str, Any]] = [r.to_dict() for r in rows]
    return {
        "alerts": out,
        "active_count": sum(1 for a in out if a["resolved_at"] is None),
    }


@router.post("/health/alerts/{alert_id}/ack")
async def ack_health_alert(
    alert_id: int,
    actor: Optional[str] = Header(default=None, alias="X-Actor"),
) -> Dict[str, Any]:
    with session_scope() as session:
        row = session.get(LakeHealthAlert, int(alert_id))
        if row is None:
            raise HTTPException(status_code=404, detail="alert not found")
        if row.resolved_at is None:
            row.resolved_at = datetime.utcnow()
            row.resolved_by = (actor or "operator")
        session.flush()
        return {"ok": True, "alert": row.to_dict()}


@status_router.get("/sources")
async def lake_status_sources(days: int = 7) -> Dict[str, Any]:
    """MITS Phase 11.I — per-source health for the 9+ source grid.

    Returns one entry per Phase 11 source with:
      - status (green/yellow/red) from the most recent
        ``data_source_health`` row
      - latest snapshot date + computed_at
      - rows_written / pulls_successful / pulls_attempted (rolling 24h)
      - avg_latency_ms (most recent snapshot)
      - last_error_text (NULL when green)
      - sparkline: list of {date, rows_written, status} over the past
        ``days`` calendar days (default 7) for the UI sparkline.

    Missing sources (no row yet) are still emitted with
    ``status='unknown'`` + empty sparkline so the operator can see the
    full roster.
    """
    from datetime import date as _date, timedelta as _td
    from sqlalchemy import desc
    from backend.models.data_source_health import DataSourceHealth

    days = max(1, min(int(days or 7), 30))
    cutoff = _date.today() - _td(days=days - 1)

    out: List[Dict[str, Any]] = []
    try:
        with session_scope() as s:
            for source in _PHASE11_SOURCES:
                rows = s.execute(
                    select(DataSourceHealth)
                    .where(DataSourceHealth.source == source)
                    .where(DataSourceHealth.snapshot_date >= cutoff)
                    .order_by(desc(DataSourceHealth.snapshot_date))
                ).scalars().all()
                if not rows:
                    out.append({
                        "source": source,
                        "status": "unknown",
                        "snapshot_date": None,
                        "computed_at": None,
                        "pulls_attempted": 0,
                        "pulls_successful": 0,
                        "rows_written_24h": 0,
                        "avg_latency_ms": None,
                        "last_error_text": None,
                        "sparkline": [],
                    })
                    continue
                latest = rows[0]
                sparkline = [
                    {
                        "date": (r.snapshot_date.isoformat()
                                  if r.snapshot_date else None),
                        "rows_written": int(r.rows_written or 0),
                        "status": r.status or "unknown",
                    }
                    for r in reversed(rows)
                ]
                out.append({
                    "source": source,
                    "status": latest.status or "unknown",
                    "snapshot_date": (latest.snapshot_date.isoformat()
                                       if latest.snapshot_date else None),
                    "computed_at": (latest.computed_at.isoformat()
                                     if latest.computed_at else None),
                    "pulls_attempted": int(latest.pulls_attempted or 0),
                    "pulls_successful": int(latest.pulls_successful or 0),
                    "rows_written_24h": int(latest.rows_written or 0),
                    "avg_latency_ms": latest.avg_latency_ms,
                    "last_error_text": latest.last_error_text,
                    "sparkline": sparkline,
                })
    except Exception as exc:
        logger.exception("lake_status sources query failed")
        raise HTTPException(status_code=500, detail=str(exc))

    # Roll-up summary the UI uses to colour the "Data Health" badge on
    # the Trial Scorecard.
    counts = {"green": 0, "yellow": 0, "red": 0, "unknown": 0}
    for r in out:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if counts["red"] > 0:
        rollup = "red"
    elif counts["yellow"] >= 1 or counts["unknown"] >= 3:
        rollup = "yellow"
    else:
        rollup = "green"
    return {
        "sources": out,
        "count_by_status": counts,
        "rollup_status": rollup,
        "days": days,
        "fetched_at": datetime.utcnow().isoformat(),
    }


@router.post("/restore")
async def restore_instructions(date: str) -> Dict[str, Any]:
    """Restore is too dangerous to perform via HTTP. Return the SSM
    command an operator should run on the EC2 host instead."""
    cmd = (
        "aws ssm send-command "
        "--instance-ids i-0426a45181d08adff "
        "--document-name AWS-RunShellScript "
        "--parameters "
        f"commands='cd /opt/trading-bot && sudo -u tradingbot ./.venv/bin/python bin/restore_from_lake.py --date {date} --confirm'"
    )
    return {
        "ok": False,
        "message": "Restore must be run via SSM, not HTTP.",
        "ssm_command": cmd,
    }
