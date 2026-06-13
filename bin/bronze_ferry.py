#!/usr/bin/env python
"""MITS Phase 11.1 — Bronze ferry from SQLite → S3 parquet.

The Phase 11 backfills wrote directly to SQLite, skipping the bronze
layer that Phase 8 promised as the system of record. This ferry walks
every Phase 11 table, paginates rows in 50k-row batches, and writes
each batch to ``s3://<lake-bucket>/bronze/sqlite_ferry/{table}/dt=YYYY-MM-DD/batch_NNNNNN.parquet``.

Design contract:

* **Resumable** — ``bronze_ferry_state`` table holds a per-table
  watermark (``last_id`` for autoincrement tables, ``last_seen_at`` for
  timestamp-keyed tables). Re-running picks up from where the previous
  run stopped, with one full re-scan of the final partial batch (safe
  because the writer is idempotent on (table, batch_id, sha256)).
* **Idempotent** — each batch path is content-addressable via SHA256
  of the parquet bytes; if S3 already has an object with the same key
  AND content-length we skip the upload.
* **Memory bounded** — we read 50k rows, materialize to parquet bytes
  in a single pyarrow Table, upload, free, and move on. No DataFrames
  larger than the batch size are ever in memory.
* **One-shot + nightly** — invoke with ``--mode oneshot`` (default) to
  walk every table to completion, or ``--mode delta`` for the nightly
  cron pass that just picks up new rows since the watermark.

Usage:

    # One-shot full ferry (run once after Phase 11 backfills land).
    python bin/bronze_ferry.py --mode oneshot

    # Delta pass — nightly cron.
    python bin/bronze_ferry.py --mode delta

    # Single table (debug aid).
    python bin/bronze_ferry.py --table stock_bars
"""
from __future__ import annotations

import argparse
import hashlib
import io
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s — %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout)


logger = logging.getLogger("bronze_ferry")


# ── per-table contract ────────────────────────────────────────────────


@dataclass(frozen=True)
class TableSpec:
    """Watermark + pagination contract for a single ferry-able table."""
    name: str
    # Column to order by + use as watermark cursor. Most Phase 11 tables
    # have an autoincrement integer `id` — for those, the cursor is the
    # largest id we've seen. Time-series tables (stock_bars,
    # option_contract_bars) can use the natural primary timestamp.
    cursor_col: str
    # The column type — "int" (autoincrement id) or "datetime"
    # (timestamp column). Determines how we serialize / parse the
    # watermark string.
    cursor_kind: str  # "int" | "datetime"
    # Optional: extra filter to apply on every batch. None means "no
    # additional filter beyond the cursor".
    extra_where: Optional[str] = None
    # Optional human-friendly partition column — when set, we partition
    # the bronze prefix by `dt=` derived from this column. None means
    # "partition by ferry-time" (which is fine for catalog rebuild).
    partition_by_col: Optional[str] = None

    def cursor_initial(self) -> str:
        return "0" if self.cursor_kind == "int" else "1970-01-01T00:00:00"


# Tables that ferry to bronze. Ordering chosen so the heaviest tables
# go LAST — that way an interruption still ferries the smaller / more
# critical metadata tables.
FERRY_TABLES: List[TableSpec] = [
    TableSpec("data_source_health", "id", "int"),
    TableSpec("parity_audit_history", "id", "int"),
    TableSpec("knowledge_graph", "id", "int"),
    TableSpec("market_outcomes", "id", "int"),
    TableSpec("market_observations", "id", "int"),
    TableSpec("regime_episode_snapshots", "id", "int"),
    TableSpec("iv_history", "id", "int"),
    TableSpec("insider_trades", "id", "int"),
    TableSpec("fund_holdings", "id", "int"),
    TableSpec("news_articles", "id", "int"),
    TableSpec("earnings_transcripts", "id", "int"),
    TableSpec("transcript_paragraphs", "id", "int"),
    TableSpec("fred_observations", "id", "int"),
    TableSpec("stock_bars", "id", "int"),
    TableSpec("option_contract_bars", "id", "int"),
]


# ── watermark table (created on first run) ────────────────────────────


def _ensure_state_table() -> None:
    """``CREATE TABLE IF NOT EXISTS bronze_ferry_state`` — separate
    from SQLAlchemy models because the ferry shouldn't depend on the
    full app boot to run."""
    from backend.db import session_scope
    from sqlalchemy import text
    with session_scope() as s:
        s.execute(text(
            "CREATE TABLE IF NOT EXISTS bronze_ferry_state ("
            "  table_name VARCHAR(64) PRIMARY KEY,"
            "  last_cursor VARCHAR(64) NOT NULL,"
            "  last_batch_id INTEGER NOT NULL DEFAULT 0,"
            "  rows_ferried_total INTEGER NOT NULL DEFAULT 0,"
            "  last_run_at TIMESTAMP,"
            "  last_status VARCHAR(32),"
            "  last_error TEXT"
            ")"
        ))


def _load_state(table: str) -> Tuple[str, int, int]:
    """Returns (last_cursor, last_batch_id, rows_ferried_total)."""
    from backend.db import session_scope
    from sqlalchemy import text
    spec = next((t for t in FERRY_TABLES if t.name == table), None)
    initial = spec.cursor_initial() if spec else "0"
    with session_scope() as s:
        row = s.execute(text(
            "SELECT last_cursor, last_batch_id, rows_ferried_total "
            "FROM bronze_ferry_state WHERE table_name = :n"
        ), {"n": table}).fetchone()
        if row is None:
            return (initial, 0, 0)
        return (str(row[0]), int(row[1] or 0), int(row[2] or 0))


def _save_state(table: str, last_cursor: str, last_batch_id: int,
                  rows_added: int, status: str, error: Optional[str] = None) -> None:
    from backend.db import session_scope
    from sqlalchemy import text
    with session_scope() as s:
        # Upsert
        existing = s.execute(text(
            "SELECT rows_ferried_total FROM bronze_ferry_state "
            "WHERE table_name = :n"
        ), {"n": table}).fetchone()
        if existing is None:
            s.execute(text(
                "INSERT INTO bronze_ferry_state (table_name, last_cursor, "
                "last_batch_id, rows_ferried_total, last_run_at, last_status, "
                "last_error) VALUES (:n, :c, :b, :r, :ts, :s, :e)"
            ), {"n": table, "c": last_cursor, "b": last_batch_id,
                "r": rows_added, "ts": datetime.utcnow(),
                "s": status, "e": error})
        else:
            new_total = int(existing[0] or 0) + int(rows_added or 0)
            s.execute(text(
                "UPDATE bronze_ferry_state SET last_cursor = :c, "
                "last_batch_id = :b, rows_ferried_total = :r, "
                "last_run_at = :ts, last_status = :s, last_error = :e "
                "WHERE table_name = :n"
            ), {"n": table, "c": last_cursor, "b": last_batch_id,
                "r": new_total, "ts": datetime.utcnow(),
                "s": status, "e": error})


# ── batch read + S3 write ─────────────────────────────────────────────


def _table_exists(table: str) -> bool:
    from backend.db import session_scope
    from sqlalchemy import text
    with session_scope() as s:
        bind = s.get_bind()
        dialect = getattr(bind.dialect, "name", "") if bind else ""
        if dialect == "sqlite":
            row = s.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = :n"
            ), {"n": table}).fetchone()
            return row is not None
        # Other dialects — try a count-1 probe.
        try:
            s.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
            return True
        except Exception:
            return False


def _fetch_batch(spec: TableSpec, last_cursor: str,
                   batch_size: int) -> List[Dict[str, Any]]:
    """Return up to ``batch_size`` rows with ``cursor_col > last_cursor``
    in ascending order. Returns [] when no more rows."""
    from backend.db import session_scope
    from sqlalchemy import text
    # Cast cursor for the SQL — sqlite is loose about types so we keep
    # it as a string for the parameter and let the dialect coerce.
    where_parts = [f"{spec.cursor_col} > :cur"]
    if spec.extra_where:
        where_parts.append(spec.extra_where)
    where = " AND ".join(where_parts)
    sql = (
        f"SELECT * FROM {spec.name} WHERE {where} "
        f"ORDER BY {spec.cursor_col} ASC LIMIT :lim"
    )
    with session_scope() as s:
        result = s.execute(text(sql), {"cur": last_cursor, "lim": batch_size})
        rows = [dict(r._mapping) for r in result.fetchall()]
    return rows


def _rows_to_parquet_bytes(rows: List[Dict[str, Any]]) -> bytes:
    """Materialize ``rows`` into parquet bytes via pyarrow."""
    if not rows:
        return b""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise RuntimeError(
            "bronze_ferry needs pyarrow — `pip install pyarrow`") from e
    # Normalize: coerce datetimes / decimals to safe types pyarrow can
    # auto-detect. We don't try to be clever — pyarrow.Table.from_pylist
    # handles ints / floats / strings / None / datetimes natively.
    safe_rows: List[Dict[str, Any]] = []
    for r in rows:
        safe: Dict[str, Any] = {}
        for k, v in r.items():
            if hasattr(v, "isoformat") and not isinstance(v, str):
                safe[k] = v.isoformat()
            else:
                safe[k] = v
        safe_rows.append(safe)
    table = pa.Table.from_pylist(safe_rows)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


def _s3_client():
    try:
        import boto3  # type: ignore
        return boto3.client("s3")
    except Exception:
        logger.debug("boto3 unavailable", exc_info=True)
        return None


def _s3_object_exists(client, bucket: str, key: str,
                          expected_size: Optional[int]) -> bool:
    if client is None:
        return False
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        if expected_size is None:
            return True
        return int(head.get("ContentLength") or -1) == int(expected_size)
    except Exception:
        return False


def _s3_put(client, bucket: str, key: str, body: bytes,
              sha256_hex: str) -> bool:
    if client is None:
        return False
    try:
        client.put_object(
            Bucket=bucket, Key=key, Body=body,
            ContentType="application/octet-stream",
            ServerSideEncryption="AES256",
            Metadata={"sha256": sha256_hex, "row_count_bytes": str(len(body))},
        )
        return True
    except Exception:
        logger.exception("s3 put failed: %s/%s", bucket, key)
        return False


# ── per-table ferry ───────────────────────────────────────────────────


def ferry_table(spec: TableSpec, *, batch_size: int = 50000,
                  max_batches: Optional[int] = None,
                  dry_run: bool = False) -> Dict[str, Any]:
    """Walk one table to completion (oneshot) or until ``max_batches`` is
    hit (delta mode). Returns a summary dict."""
    from backend.config import TUNABLES
    bucket = TUNABLES.lake_bucket
    client = _s3_client()
    if client is None and not dry_run:
        logger.error("no S3 client — install boto3 or set AWS creds")
        return {"table": spec.name, "rows": 0, "batches": 0,
                "error": "no_s3_client"}
    if not _table_exists(spec.name):
        logger.info("ferry_table: %s does not exist, skipping", spec.name)
        return {"table": spec.name, "rows": 0, "batches": 0,
                "skipped": "table_missing"}
    last_cursor, last_batch_id, total_so_far = _load_state(spec.name)
    rows_added = 0
    batches_added = 0
    started = time.time()
    while True:
        if max_batches is not None and batches_added >= max_batches:
            logger.info("ferry_table: %s — hit max_batches=%d, stopping",
                          spec.name, max_batches)
            break
        rows = _fetch_batch(spec, last_cursor, batch_size)
        if not rows:
            logger.info("ferry_table: %s — caught up (cursor=%s)",
                          spec.name, last_cursor)
            break
        last_batch_id += 1
        body = _rows_to_parquet_bytes(rows)
        sha256_hex = hashlib.sha256(body).hexdigest()
        dt_partition = date.today().isoformat()
        key = (
            f"bronze/sqlite_ferry/{spec.name}/dt={dt_partition}/"
            f"batch_{last_batch_id:06d}_{sha256_hex[:12]}.parquet"
        )
        if dry_run:
            logger.info("DRY ferry %s batch=%d rows=%d sha256=%s size=%d",
                          spec.name, last_batch_id, len(rows),
                          sha256_hex[:12], len(body))
            ok = True
        else:
            if _s3_object_exists(client, bucket, key, len(body)):
                logger.info("ferry %s batch=%d already present @ %s",
                              spec.name, last_batch_id, key)
                ok = True
            else:
                ok = _s3_put(client, bucket, key, body, sha256_hex)
        if not ok:
            err = f"s3_put_failed batch={last_batch_id}"
            _save_state(spec.name, last_cursor, last_batch_id - 1,
                          rows_added=0, status="error", error=err)
            return {"table": spec.name, "rows": rows_added,
                      "batches": batches_added, "error": err}
        # Advance cursor — last row's cursor_col is the new high-water mark.
        last_row = rows[-1]
        if spec.cursor_kind == "int":
            last_cursor = str(int(last_row.get(spec.cursor_col) or 0))
        else:
            v = last_row.get(spec.cursor_col)
            last_cursor = (v.isoformat() if hasattr(v, "isoformat")
                              else str(v))
        rows_added += len(rows)
        batches_added += 1
        # Persist incremental state so a crash doesn't lose progress.
        _save_state(spec.name, last_cursor, last_batch_id, len(rows),
                      status="running")
        if len(rows) < batch_size:
            # Last partial batch — we're caught up.
            logger.info("ferry %s — last batch was partial (%d < %d), done",
                          spec.name, len(rows), batch_size)
            break
    _save_state(spec.name, last_cursor, last_batch_id, 0,
                  status="done")
    elapsed = time.time() - started
    logger.info(
        "ferry %s DONE rows_this_run=%d batches_this_run=%d elapsed=%.1fs cursor=%s",
        spec.name, rows_added, batches_added, elapsed, last_cursor,
    )
    return {"table": spec.name, "rows": rows_added,
              "batches": batches_added, "cursor": last_cursor,
              "elapsed": elapsed}


# ── memory guard (Phase 11.1 — sub-phase 9) ───────────────────────────


def _memory_pressure_ok() -> bool:
    """Defer to backend.bot.data.memory_guard if available, else best-
    effort psutil probe. ``True`` = safe to proceed."""
    try:
        from backend.bot.data.memory_guard import memory_pressure_ok
        return bool(memory_pressure_ok())
    except Exception:
        pass
    try:
        import psutil  # type: ignore
        return psutil.virtual_memory().percent < 85.0
    except Exception:
        return True


# ── CLI ───────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "MITS Phase 11.1 — Bronze ferry. Walks Phase 11 SQLite tables "
            "and writes parquet batches to s3://<lake>/bronze/sqlite_ferry/."
        ),
    )
    parser.add_argument(
        "--mode", default="oneshot", choices=["oneshot", "delta"],
        help=(
            "oneshot: walk every table to completion (one-time pass after "
            "Phase 11 backfills). delta: stop after N batches per table — "
            "the nightly cron mode."
        ),
    )
    parser.add_argument(
        "--table", default=None,
        help="Ferry only this table (debug aid).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=50000,
        help="Rows per parquet batch (default 50000).",
    )
    parser.add_argument(
        "--delta-max-batches", type=int, default=20,
        help=("In --mode=delta, max batches per table per run. Caps the "
              "nightly job at ~1M rows per table = ~20 batches × 50k."),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Don't upload, just log batch metadata.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="DEBUG logging",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    from backend.db import init_db
    init_db()
    _ensure_state_table()

    if not _memory_pressure_ok():
        logger.warning("memory pressure too high, sleeping 30s once...")
        time.sleep(30)
        if not _memory_pressure_ok():
            logger.error("memory still high, aborting ferry run")
            return 3

    if args.table:
        specs = [s for s in FERRY_TABLES if s.name == args.table]
        if not specs:
            logger.error("unknown table: %s", args.table)
            return 2
    else:
        specs = list(FERRY_TABLES)

    max_batches = args.delta_max_batches if args.mode == "delta" else None
    grand = {"tables": [], "rows": 0, "batches": 0}
    for spec in specs:
        if not _memory_pressure_ok():
            logger.warning("ferry: memory pressure mid-run — stopping early")
            break
        result = ferry_table(spec, batch_size=args.batch_size,
                                max_batches=max_batches,
                                dry_run=args.dry_run)
        grand["tables"].append(result)
        grand["rows"] += int(result.get("rows") or 0)
        grand["batches"] += int(result.get("batches") or 0)
    logger.info(
        "BRONZE FERRY GRAND TOTAL mode=%s rows=%d batches=%d tables=%d",
        args.mode, grand["rows"], grand["batches"], len(grand["tables"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
