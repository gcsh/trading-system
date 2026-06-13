#!/usr/bin/env python
"""MITS Phase 8.4 — Disaster recovery: rebuild SQLite from S3 gold layer.

Usage:

    python bin/restore_from_lake.py --date YYYY-MM-DD --confirm

Steps:

  1. Verifies a gold snapshot for the requested date exists.
  2. Backs up the live DB to ``<db>.before_restore_<ts>``.
  3. For each table in ``SNAPSHOT_TABLES``: TRUNCATE + INSERT
     from the parquet snapshot.
  4. Reports rows/table.

This is a destructive operation. It REFUSES to run without
``--confirm``.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

# Make backend importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s restore: %(message)s",
)
log = logging.getLogger("restore")


def _parse_date(arg: str) -> date:
    try:
        return datetime.strptime(arg, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"--date must be YYYY-MM-DD: {exc}")


def _backup_db(db_path: Path) -> Path:
    if not db_path.exists():
        return db_path  # nothing to back up
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = db_path.with_suffix(db_path.suffix + f".before_restore_{ts}")
    shutil.copy2(db_path, dest)
    log.info("backed up live DB to %s", dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True,
                          help="Snapshot date (YYYY-MM-DD) in S3 gold layer.")
    parser.add_argument("--confirm", action="store_true",
                          help="REQUIRED. Confirms the DB will be overwritten.")
    parser.add_argument("--db-path", default=None,
                          help="Override DB path (defaults to backend SETTINGS).")
    parser.add_argument("--dry-run", action="store_true",
                          help="List what would be restored, no writes.")
    args = parser.parse_args()

    target = _parse_date(args.date)
    if not args.confirm and not args.dry_run:
        log.error("--confirm flag is REQUIRED. Aborting.")
        return 2

    from backend.bot.data import gold, lake
    from backend.config import SETTINGS
    from backend.db import get_engine, init_db
    from sqlalchemy import text

    db_path = Path(args.db_path or SETTINGS.db_path)
    log.info("target restore date: %s", target.isoformat())
    log.info("target SQLite DB:   %s", db_path)

    if not args.dry_run:
        _backup_db(db_path)
        init_db(str(db_path))

    engine = get_engine()
    report = {"date": target.isoformat(), "dry_run": args.dry_run,
                  "tables": {}}
    for table in gold.SNAPSHOT_TABLES:
        df = lake.read_gold(table, target.isoformat())
        rows = 0 if df is None else len(df)
        if rows == 0:
            report["tables"][table] = {"status": "no_snapshot", "rows": 0}
            log.warning("no snapshot for %s on %s", table, target)
            continue
        if args.dry_run:
            report["tables"][table] = {"status": "would_restore", "rows": rows}
            continue
        try:
            # Drop columns the parquet has that the DB doesn't (the
            # snapshot might carry manifest cols like fetch_ts).
            from sqlalchemy import inspect
            insp = inspect(engine)
            if table not in insp.get_table_names():
                log.warning("table %s missing in live DB; skipping", table)
                report["tables"][table] = {"status": "missing_table", "rows": 0}
                continue
            cols = [c["name"] for c in insp.get_columns(table)]
            df_use = df[[c for c in df.columns if c in cols]]
            with engine.begin() as conn:
                conn.execute(text(f'DELETE FROM "{table}"'))
                records = df_use.to_dict(orient="records")
                if records:
                    keys = list(records[0].keys())
                    placeholders = ", ".join(f":{k}" for k in keys)
                    columns_sql = ", ".join(f'"{k}"' for k in keys)
                    stmt = text(
                        f'INSERT INTO "{table}" ({columns_sql}) '
                        f'VALUES ({placeholders})'
                    )
                    for batch_start in range(0, len(records), 500):
                        conn.execute(stmt, records[batch_start: batch_start + 500])
            report["tables"][table] = {"status": "restored", "rows": rows}
            log.info("restored %s rows into %s", rows, table)
        except Exception as exc:
            report["tables"][table] = {"status": "error", "rows": rows,
                                          "detail": str(exc)}
            log.error("restore failed for %s: %s", table, exc)

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
