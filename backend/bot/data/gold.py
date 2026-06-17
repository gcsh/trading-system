"""MITS Phase 8.4 — Gold-layer nightly snapshot of the bot's SQLite DB.

23:30 ET cron snapshots every persisted table to S3 parquet under
``gold/{table}/dt=YYYY-MM-DD/snapshot.parquet``. Idempotent — re-running
the same date overwrites.

The list ``SNAPSHOT_TABLES`` is the canonical disaster-recovery contract.
Any new bot-state table MUST be added here AND to
``PAPER_STATE_TABLES`` or ``EXTERNAL_CACHE_TABLES`` in
``backend/bot/system_reset.py``. Tables that don't exist in the live DB
are SKIPPED gracefully — the snapshot job logs and moves on.

Restore is intentionally not exposed via HTTP (too dangerous). Use
``bin/restore_from_lake.py --date YYYY-MM-DD --confirm``.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect, text

from backend.bot.data import lake
from backend.db import get_engine

logger = logging.getLogger(__name__)


# Complete table list per spec (verified against backend/models/*).
SNAPSHOT_TABLES: List[str] = [
    # MITS-0 corpus
    "market_observations",
    "market_outcomes",
    "knowledge_graph",
    "knowledge_graph_history",
    "pattern_priors",
    "corpus_status",
    # market-data caches that are bot-derived
    "iv_history",
    "intraday_iv_cache",
    "gex_regime_history",
    # trade/account state
    "trades",
    "paper_positions",
    "paper_account",
    "portfolio_snapshots",
    # decision pipeline
    "decision_log",
    "execution_log",
    # EOD analysis pipeline
    "eod_analysis",
    "eod_prediction_outcomes",
    # detector ledger
    "detector_config",
    "detector_suggestions",
    "weekly_retrospectives",
    # regime / intraday observability
    "intraday_regime_events",
    "regime_episode_snapshots",
    # external feeds (cache class)
    "ingest_watermarks",
    "fred_observations",
    "edgar_filings",
    "short_interest",
    "cot_reports",
    "breadth_snapshots",
    "earnings_call_intel",
    # operator-curated state
    "watchlist_items",
    "seen_flow_alerts",
    "bot_config",
    # experiments + Phase 8 sync watermark
    "experiment_record",
    "lake_sync_watermark",
]


def _existing_tables() -> set:
    engine = get_engine()
    try:
        return set(inspect(engine).get_table_names())
    except Exception:
        return set()


def _dump_table(table_name: str) -> List[Dict[str, Any]]:
    """Pull every row of a table as a list of dicts.

    Returns ``[]`` if the table is missing or the query fails. Any
    non-JSON-friendly values are stringified so the parquet writer
    doesn't choke on mixed types.
    """
    engine = get_engine()
    try:
        with engine.begin() as conn:
            rows = conn.execute(text(f'SELECT * FROM "{table_name}"')).mappings().all()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        d: Dict[str, Any] = {}
        for k, v in dict(row).items():
            if isinstance(v, (dict, list)):
                d[k] = json.dumps(v, default=str)
            elif isinstance(v, datetime):
                d[k] = v.isoformat()
            elif isinstance(v, date):
                d[k] = v.isoformat()
            else:
                d[k] = v
        out.append(d)
    return out


def run_snapshot_pass(target_date: Optional[date] = None) -> Dict[str, Any]:
    """Snapshot every table in ``SNAPSHOT_TABLES`` to S3.

    Returns a per-table {row_count, s3_uri, status} map.
    """
    target_date = target_date or date.today()
    existing = _existing_tables()
    out: Dict[str, Any] = {
        "date": target_date.isoformat(),
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
    }
    for table in SNAPSHOT_TABLES:
        if table not in existing:
            out["tables"][table] = {"status": "missing", "rows": 0}
            continue
        try:
            rows = _dump_table(table)
            uri = lake.write_gold(table, rows, ts=target_date, sync=True)
            out["tables"][table] = {
                "status": "ok" if uri else "skipped",
                "rows": len(rows),
                "uri": uri or "",
            }
        except Exception as exc:
            logger.warning("gold snapshot failed for %s: %s", table, exc)
            out["tables"][table] = {"status": "error", "rows": 0,
                                       "detail": str(exc)}
    return out


def list_snapshots_for_date(target_date: date) -> Dict[str, List[str]]:
    """Per-table list of available snapshot dates near the target.

    Used by ``restore_from_lake`` to confirm we have the data before
    overwriting the live DB.
    """
    out: Dict[str, List[str]] = {}
    for table in SNAPSHOT_TABLES:
        try:
            out[table] = lake.list_gold_dates(table)
        except Exception:
            out[table] = []
    return out


__all__ = [
    "SNAPSHOT_TABLES", "run_snapshot_pass", "list_snapshots_for_date",
]
