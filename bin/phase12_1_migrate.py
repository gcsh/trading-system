#!/usr/bin/env python3
"""MITS Phase 12.1 — direction-column migration + bulk backfill.

Idempotent, safe to re-run on the EC2 box.

Steps:
  1. ALTER TABLE market_observations ADD COLUMN direction VARCHAR.
     Skipped silently when the column already exists.
  2. CREATE INDEX ix_market_obs_pattern_direction (pattern, direction).
  3. Backfill direction on every row using the authoritative resolver.
  4. Re-score every market_outcomes row's was_winner via the
     direction-aware rule.
  5. Print before/after stats so the operator can sanity-check.

Usage on EC2:

    sudo -u trading-bot AWS_PROFILE=trading-bot \
        /opt/trading-bot/venv/bin/python /opt/trading-bot/bin/phase12_1_migrate.py
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict

from sqlalchemy import text

# Imports are heavy; only import when we run.
def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def _column_exists(conn, table: str, column: str) -> bool:
    res = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    cols = {row[1] for row in res}
    return column in cols


def _index_exists(conn, name: str) -> bool:
    res = conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=:n"
    ), {"n": name}).fetchone()
    return res is not None


def step1_alter_table(engine) -> Dict[str, Any]:
    out = {"column_added": False, "index_added": False}
    with engine.begin() as conn:
        if not _column_exists(conn, "market_observations", "direction"):
            conn.execute(text(
                "ALTER TABLE market_observations ADD COLUMN direction VARCHAR"
            ))
            out["column_added"] = True
        if not _index_exists(conn, "ix_market_obs_pattern_direction"):
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_market_obs_pattern_direction "
                "ON market_observations (pattern, direction)"
            ))
            out["index_added"] = True
        if not _index_exists(conn, "ix_market_observations_direction"):
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_market_observations_direction "
                "ON market_observations (direction)"
            ))
    return out


def step2_backfill_direction(engine, batch_size: int = 5000
                                          ) -> Dict[str, Any]:
    from backend.bot.detectors.direction import resolve_direction
    stats = {"scanned": 0, "set_long": 0, "set_short": 0,
              "set_neutral": 0, "set_null": 0, "errors": 0}
    offset = 0
    while True:
        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT id, pattern, features FROM market_observations "
                "ORDER BY id ASC LIMIT :lim OFFSET :off"
            ), {"lim": batch_size, "off": offset}).fetchall()
            if not rows:
                break
            updates = []
            for row in rows:
                _id, pattern, features_str = row
                feats = {}
                if features_str:
                    try:
                        feats = json.loads(features_str)
                    except Exception:
                        feats = {}
                try:
                    direction = resolve_direction(pattern, feats)
                except Exception:
                    direction = None
                    stats["errors"] += 1
                if direction == "long":
                    stats["set_long"] += 1
                elif direction == "short":
                    stats["set_short"] += 1
                elif direction == "neutral":
                    stats["set_neutral"] += 1
                else:
                    stats["set_null"] += 1
                updates.append({"id": _id, "direction": direction})
            for u in updates:
                conn.execute(text(
                    "UPDATE market_observations SET direction=:direction "
                    "WHERE id=:id"
                ), u)
            stats["scanned"] += len(rows)
        offset += batch_size
        if stats["scanned"] % 50000 == 0:
            logging.info("direction backfill: %d rows scanned", stats["scanned"])
    return stats


def step3_rescore_outcomes(engine) -> Dict[str, Any]:
    from backend.bot.corpus.outcome_linker import rescore_winners_in_place
    return rescore_winners_in_place(batch_size=10000)


def step4_recompute_cells() -> Dict[str, Any]:
    from backend.bot.corpus.knowledge_aggregator import recompute_cells
    return recompute_cells(ticker=None)


def _row_count(engine, sql: str, params: Dict[str, Any] | None = None) -> int:
    with engine.begin() as conn:
        return int(conn.execute(text(sql), params or {}).scalar() or 0)


def main():
    _setup_logging()
    t0 = time.time()

    from backend.db import _engine as engine, init_db
    if engine is None:
        init_db()
        from backend.db import _engine as engine  # type: ignore

    logging.info("== Step 1: schema migration ==")
    step1 = step1_alter_table(engine)
    logging.info("step1: %s", step1)

    logging.info("== Step 2: direction backfill ==")
    step2 = step2_backfill_direction(engine)
    logging.info("step2: %s", step2)

    logging.info("== Step 3: rescore market_outcomes.was_winner ==")
    step3 = step3_rescore_outcomes(engine)
    logging.info("step3: %s", step3)

    logging.info("== Step 4: knowledge_graph recompute_cells ==")
    step4 = step4_recompute_cells()
    logging.info("step4: %s", step4)

    # Coverage snapshot for verification.
    cov = {
        "obs_long": _row_count(engine,
            "SELECT COUNT(*) FROM market_observations WHERE direction='long'"),
        "obs_short": _row_count(engine,
            "SELECT COUNT(*) FROM market_observations WHERE direction='short'"),
        "obs_neutral": _row_count(engine,
            "SELECT COUNT(*) FROM market_observations WHERE direction='neutral'"),
        "obs_null": _row_count(engine,
            "SELECT COUNT(*) FROM market_observations WHERE direction IS NULL"),
        "outcomes": _row_count(engine,
            "SELECT COUNT(*) FROM market_outcomes"),
        "winners": _row_count(engine,
            "SELECT COUNT(*) FROM market_outcomes WHERE was_winner=1"),
    }
    logging.info("coverage: %s", cov)
    elapsed = time.time() - t0
    print(json.dumps({
        "step1": step1, "step2": step2, "step3": step3,
        "step4_cells": {k: step4.get(k) for k in (
            "cohorts", "cells_inserted", "cells_updated",
            "observations_seen", "cells_high", "cells_medium",
            "cells_low", "cells_thin")},
        "coverage": cov,
        "elapsed_sec": round(elapsed, 1),
    }, indent=2))


if __name__ == "__main__":
    sys.exit(main() or 0)
