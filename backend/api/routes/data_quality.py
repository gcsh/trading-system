"""Stage-12.B4 Data Quality endpoints.

  • ``POST /data-quality/score`` — score a hypothetical context
  • ``GET  /data-quality/current`` — quality of the most-recent live snapshot
  • ``GET  /data-quality/parity``  — MITS Phase 11.J cross-vendor parity
                                     audit findings (top suspect tickers,
                                     histogram, drill-down by ticker)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from backend.bot.data_quality import score_data_quality
from backend.db import session_scope
from backend.models.parity_audit_history import ParityAuditHistory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/data-quality", tags=["data_quality"])


class ScoreBody(BaseModel):
    snapshot: Optional[Dict[str, Any]] = None
    source_errors: Optional[List[str]] = None
    feed_health: Optional[Dict[str, Any]] = None
    abstain_below: int = 40


@router.post("/score")
async def score(body: ScoreBody) -> dict:
    return score_data_quality(
        snapshot=body.snapshot,
        source_errors=body.source_errors,
        feed_health=body.feed_health,
        abstain_below=body.abstain_below,
    ).to_dict()


@router.get("/parity")
async def parity_findings(
    top_n: int = Query(10, ge=1, le=50),
    histogram_buckets: int = Query(10, ge=4, le=40),
) -> Dict[str, Any]:
    """MITS Phase 11.J — aggregate parity-audit findings.

    Surfaces the Phase 11 cross-vendor (yfinance vs ThetaData) audit
    so the operator can see WHERE the corpus is demoted and WHY. The
    suspect-row count is the headline; the per-ticker breakdown drives
    the drill-down panel; the divergence histogram explains the
    severity distribution.

    Suspect rows are filtered out (or weighted down) of knowledge_graph
    aggregation per the operator's "data-blame principle" directive.
    """
    try:
        with session_scope() as s:
            # Totals + per-severity breakdown.
            severity_rows = s.execute(
                select(ParityAuditHistory.severity,
                       func.count(ParityAuditHistory.id))
                .group_by(ParityAuditHistory.severity)
            ).all()
            severity_counts = {sev or "unknown": int(cnt or 0)
                                for sev, cnt in severity_rows}
            total = sum(severity_counts.values())
            suspect = int(severity_counts.get("suspect", 0))

            # Top-N tickers by suspect-row count.
            top_rows = s.execute(
                select(
                    ParityAuditHistory.ticker,
                    func.count(ParityAuditHistory.id).label("suspect_days"),
                )
                .where(ParityAuditHistory.severity == "suspect")
                .group_by(ParityAuditHistory.ticker)
                .order_by(desc("suspect_days"))
                .limit(int(top_n))
            ).all()
            # Per-ticker AUDITED day-count for percentage.
            top_tickers: List[Dict[str, Any]] = []
            for ticker, suspect_days in top_rows:
                audited = s.execute(
                    select(func.count(ParityAuditHistory.id))
                    .where(ParityAuditHistory.ticker == ticker)
                ).scalar() or 0
                pct = (float(suspect_days) / float(audited)
                          if audited else 0.0)
                top_tickers.append({
                    "ticker": ticker,
                    "suspect_days": int(suspect_days),
                    "audited_days": int(audited),
                    "suspect_pct": round(pct, 4),
                })

            # Divergence-percent histogram (suspect + warn only — ok rows
            # are below the warn threshold and would crowd the chart).
            div_rows = s.execute(
                select(ParityAuditHistory.divergence_pct)
                .where(ParityAuditHistory.severity.in_(("warn", "suspect")))
            ).all()
            divs = [float(r[0]) for r in div_rows
                       if r[0] is not None and r[0] > 0]
            histogram: List[Dict[str, Any]] = []
            if divs:
                max_div = max(divs)
                buckets = int(histogram_buckets)
                step = max(1e-4, max_div / buckets)
                edges = [step * i for i in range(buckets + 1)]
                counts = [0] * buckets
                for v in divs:
                    idx = min(buckets - 1, int(v / step))
                    counts[idx] += 1
                for i in range(buckets):
                    histogram.append({
                        "lo_pct": round(edges[i] * 100, 3),
                        "hi_pct": round(edges[i + 1] * 100, 3),
                        "count": counts[i],
                    })

            most_recent_audit = s.execute(
                select(func.max(ParityAuditHistory.audited_at))
            ).scalar()

    except Exception as exc:
        logger.exception("parity audit aggregate failed")
        raise HTTPException(status_code=500, detail=str(exc))

    suspect_pct = (suspect / total) if total else 0.0
    return {
        "total_audited_rows": total,
        "severity_counts": severity_counts,
        "suspect_total": suspect,
        "suspect_pct_of_total": round(suspect_pct, 4),
        "top_suspect_tickers": top_tickers,
        "divergence_histogram": histogram,
        "disclosure": (
            "Rows with severity=suspect are flagged "
            "parity_warn=True on market_observations and are "
            "down-weighted (or filtered) in knowledge_graph "
            "aggregation per the data-blame principle."
        ),
        "most_recent_audit_at": (most_recent_audit.isoformat()
                                   if most_recent_audit else None),
    }


@router.get("/parity/aggregate-fast")
async def parity_aggregate_fast() -> Dict[str, Any]:
    """MITS Phase 11.1 #7 — DuckDB-backed parity aggregate.

    Identical semantics to ``/parity`` but reads from the bronze
    parquet via DuckDB instead of SQLite. Used by the Lake Status
    grid which polls every 30s and would otherwise lock the SQLite
    main writer behind a 200ms aggregate scan.

    Falls back gracefully — returns the same shape with an empty
    payload when DuckDB / S3 isn't reachable so the UI doesn't have
    to special-case the cold-start.
    """
    try:
        from backend.bot.data import duckdb_reader
        result = duckdb_reader.aggregate_parity_summary()
        return result
    except Exception as exc:
        logger.exception("parity_aggregate_fast failed")
        return {"total_audited_rows": 0,
                  "severity_counts": {"missing": 0, "ok": 0, "suspect": 0},
                  "suspect_pct_of_total": 0.0,
                  "source": f"error: {exc!r}"}


@router.get("/parity/{ticker}")
async def parity_ticker_drilldown(
    ticker: str,
    limit: int = Query(120, ge=1, le=1500),
    severity: Optional[str] = Query(None,
        description="Filter by severity: ok|warn|suspect|missing"),
) -> Dict[str, Any]:
    """Per-ticker parity drill-down (date, severity, divergence_pct)."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")
    try:
        with session_scope() as s:
            q = (select(ParityAuditHistory)
                  .where(ParityAuditHistory.ticker == ticker)
                  .order_by(desc(ParityAuditHistory.audit_date))
                  .limit(int(limit)))
            if severity:
                q = q.where(ParityAuditHistory.severity == severity)
            rows = s.execute(q).scalars().all()
            return {
                "ticker": ticker,
                "rows": [r.to_dict() for r in rows],
                "count": len(rows),
            }
    except Exception as exc:
        logger.exception("parity drilldown failed for %s", ticker)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/current")
async def current() -> dict:
    """Best-effort: pull the live monitoring feed-health, return a
    portfolio-wide quality summary. Per-ticker quality is computed at
    decision time and lives on the event."""
    try:
        from backend.bot.monitoring import feed_summary
        summary = feed_summary()
        fh = {f["name"]: f for f in (summary.get("feeds") or [])}
    except Exception:
        fh = {}
    # Without a snapshot we can only score the FEED dimension.
    return score_data_quality(snapshot=None, source_errors=[], feed_health=fh).to_dict()
