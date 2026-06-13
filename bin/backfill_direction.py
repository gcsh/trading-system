#!/usr/bin/env python3
"""MITS Phase 15 follow-up — direction column backfill on legacy historical_replay rows.

Uses backend.bot.detectors.direction's authoritative map to fill
NULL direction values on market_observations where source='historical_replay'.
Patches in batches; commits each batch; safe to re-run (idempotent on
already-filled rows because the WHERE filter only matches NULLs).

Per-pattern strategy:
  1. Try static resolution first (resolve_direction(pattern, features=None)).
     If static yields a direction, bulk-UPDATE all NULL rows for that
     pattern in one statement.
  2. Otherwise the pattern needs features to resolve (dynamic resolver
     or feature-direction passthrough). Stream the rows, parse features
     per row, resolve, and UPDATE per row inside a per-pattern txn.
  3. Patterns that resolve to None even with features stay NULL and are
     listed in the summary for operator review.

Defaults to dry-run; pass --apply to commit.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def _distinct_null_patterns(engine) -> List[Tuple[str, int]]:
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT pattern, COUNT(*) AS n FROM market_observations "
            "WHERE source='historical_replay' AND direction IS NULL "
            "GROUP BY pattern ORDER BY n DESC"
        )).fetchall()
    return [(r[0], int(r[1])) for r in rows]


def _bulk_update_pattern(engine, pattern: str, direction: str,
                         dry_run: bool) -> int:
    """Single-statement UPDATE for all NULL rows matching pattern."""
    if dry_run:
        with engine.begin() as conn:
            n = conn.execute(text(
                "SELECT COUNT(*) FROM market_observations "
                "WHERE source='historical_replay' AND direction IS NULL "
                "AND pattern=:pat"
            ), {"pat": pattern}).scalar() or 0
        return int(n)
    with engine.begin() as conn:
        res = conn.execute(text(
            "UPDATE market_observations SET direction=:dir "
            "WHERE source='historical_replay' AND direction IS NULL "
            "AND pattern=:pat"
        ), {"dir": direction, "pat": pattern})
        return int(res.rowcount or 0)


def _per_row_update_pattern(engine, pattern: str, dry_run: bool,
                            batch_size: int = 2000
                            ) -> Tuple[Dict[str, int], int]:
    """Resolve direction per-row using features. Returns (counts_by_dir, unresolved)."""
    from backend.bot.detectors.direction import resolve_direction
    counts: Dict[str, int] = defaultdict(int)
    unresolved = 0
    last_id = 0
    while True:
        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT id, features FROM market_observations "
                "WHERE source='historical_replay' AND direction IS NULL "
                "AND pattern=:pat AND id > :last "
                "ORDER BY id ASC LIMIT :lim"
            ), {"pat": pattern, "last": last_id, "lim": batch_size}).fetchall()
            if not rows:
                break
            updates: Dict[str, List[int]] = defaultdict(list)
            for _id, feats_str in rows:
                last_id = _id
                feats = {}
                if feats_str:
                    try:
                        feats = json.loads(feats_str)
                    except Exception:
                        feats = {}
                direction = resolve_direction(pattern, feats)
                if direction in ("long", "short", "neutral"):
                    counts[direction] += 1
                    updates[direction].append(_id)
                else:
                    unresolved += 1
            if not dry_run:
                for direction, ids in updates.items():
                    # SQLite has a default 999-param limit. Chunk to 500.
                    for i in range(0, len(ids), 500):
                        chunk = ids[i:i + 500]
                        placeholders = ",".join(
                            f":id{i + j}" for j in range(len(chunk)))
                        params = {f"id{i + j}": v for j, v in enumerate(chunk)}
                        params["dir"] = direction
                        conn.execute(text(
                            f"UPDATE market_observations SET direction=:dir "
                            f"WHERE id IN ({placeholders})"
                        ), params)
    return dict(counts), unresolved


def backfill(engine, dry_run: bool) -> Dict[str, object]:
    from backend.bot.detectors.direction import resolve_direction

    pattern_rows = _distinct_null_patterns(engine)
    logging.info("found %d distinct patterns with NULL direction",
                 len(pattern_rows))

    per_pattern: Dict[str, Dict[str, object]] = {}
    totals: Dict[str, int] = defaultdict(int)
    unmapped_patterns: List[Tuple[str, int]] = []

    for pattern, n_null in pattern_rows:
        static_dir = resolve_direction(pattern, None)
        if static_dir in ("long", "short", "neutral"):
            updated = _bulk_update_pattern(engine, pattern, static_dir, dry_run)
            per_pattern[pattern] = {
                "mode": "static",
                "direction": static_dir,
                "updated": updated,
                "null_before": n_null,
            }
            totals[static_dir] += updated
            logging.info("[%s] static→%s, %d rows %s",
                         pattern, static_dir, updated,
                         "would update" if dry_run else "updated")
            continue
        # Dynamic / feature-driven pattern. Stream rows.
        counts, unresolved = _per_row_update_pattern(engine, pattern, dry_run)
        resolved_total = sum(counts.values())
        per_pattern[pattern] = {
            "mode": "dynamic",
            "directions": counts,
            "unresolved": unresolved,
            "null_before": n_null,
            "updated": resolved_total,
        }
        for d, c in counts.items():
            totals[d] += c
        if resolved_total == 0:
            unmapped_patterns.append((pattern, n_null))
        logging.info("[%s] dynamic: long=%d short=%d neutral=%d "
                     "unresolved=%d (of %d NULL)",
                     pattern, counts.get("long", 0), counts.get("short", 0),
                     counts.get("neutral", 0), unresolved, n_null)

    return {
        "per_pattern": per_pattern,
        "totals": dict(totals),
        "unmapped_patterns": unmapped_patterns,
    }


def _null_count(engine) -> int:
    with engine.begin() as conn:
        return int(conn.execute(text(
            "SELECT COUNT(*) FROM market_observations "
            "WHERE source='historical_replay' AND direction IS NULL"
        )).scalar() or 0)


def _distribution(engine) -> List[Tuple[Optional[str], int]]:
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT direction, COUNT(*) FROM market_observations "
            "WHERE source='historical_replay' GROUP BY direction"
        )).fetchall()
    return [(r[0], int(r[1])) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Commit updates. Without this, dry-run only.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Explicit dry-run flag (default behaviour).")
    args = parser.parse_args()
    dry_run = not args.apply

    _setup_logging()
    t0 = time.time()

    from backend.db import get_engine
    engine = get_engine()

    before = _null_count(engine)
    logging.info("== %s ==", "DRY-RUN" if dry_run else "APPLY")
    logging.info("NULL direction rows (source=historical_replay) BEFORE: %d",
                 before)

    result = backfill(engine, dry_run=dry_run)

    after = _null_count(engine)
    dist = _distribution(engine)
    elapsed = time.time() - t0

    summary = {
        "mode": "dry_run" if dry_run else "apply",
        "null_before": before,
        "null_after": after,
        "delta": before - after,
        "totals_by_direction": result["totals"],
        "patterns_left_null": [
            {"pattern": p, "null_count": n}
            for p, n in result["unmapped_patterns"]
        ],
        "distribution_after": [
            {"direction": d, "count": c} for d, c in dist
        ],
        "per_pattern": result["per_pattern"],
        "elapsed_sec": round(elapsed, 1),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
