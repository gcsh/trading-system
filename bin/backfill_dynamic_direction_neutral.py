#!/usr/bin/env python3
"""MITS Phase 16 cross-phase scan O1 — tag NULL direction rows on
``historical_replay`` dynamic patterns as ``'neutral'``.

Companion to ``bin/backfill_direction.py``. That script resolves
direction per-row when features carry enough signal; this script
covers the patterns where the legacy historical_replay didn't carry
features metadata at all, leaving 31k rows stuck at NULL.

``'neutral'`` is the honest fallback ("we don't know which side this
row leaned") — same shape 15.E follow-up #4 used for static-None
patterns. We never invent ``long`` or ``short``.

The 7 patterns are enumerated explicitly so the script can't
accidentally tag a pattern that future replays will populate with
real features.

Defaults to dry-run; pass ``--apply`` to commit.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Dict, List, Tuple

from sqlalchemy import text


# Seven dynamic patterns the cross-phase scan flagged. These come from
# the legacy historical_replay pipeline that wrote MarketObservation
# rows without populating the features blob, so the per-row resolver
# in backfill_direction.py has nothing to work with.
TARGET_PATTERNS: Tuple[str, ...] = (
    "liquidity_sweep",
    "lvn_rejection",
    "bos",
    "stop_hunt",
    "cross_sectional_momentum",
    "smart_money_inflow",
    "p4_smoke",
)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def _null_count_by_pattern(engine) -> List[Tuple[str, int]]:
    """Current NULL-direction count per target pattern on historical_replay."""
    placeholders = ",".join(f":p{i}" for i in range(len(TARGET_PATTERNS)))
    params = {f"p{i}": p for i, p in enumerate(TARGET_PATTERNS)}
    with engine.begin() as conn:
        rows = conn.execute(text(
            f"SELECT pattern, COUNT(*) FROM market_observations "
            f"WHERE source='historical_replay' AND direction IS NULL "
            f"AND pattern IN ({placeholders}) "
            f"GROUP BY pattern ORDER BY pattern"
        ), params).fetchall()
    return [(r[0], int(r[1])) for r in rows]


def _null_count_total(engine) -> int:
    """Total NULL-direction rows on historical_replay (any pattern)."""
    with engine.begin() as conn:
        return int(conn.execute(text(
            "SELECT COUNT(*) FROM market_observations "
            "WHERE source='historical_replay' AND direction IS NULL"
        )).scalar() or 0)


def _apply_pattern(engine, pattern: str, dry_run: bool) -> int:
    """Set ``direction='neutral'`` for one pattern. Returns affected rows."""
    if dry_run:
        with engine.begin() as conn:
            return int(conn.execute(text(
                "SELECT COUNT(*) FROM market_observations "
                "WHERE source='historical_replay' AND direction IS NULL "
                "AND pattern=:pat"
            ), {"pat": pattern}).scalar() or 0)
    with engine.begin() as conn:
        res = conn.execute(text(
            "UPDATE market_observations SET direction='neutral' "
            "WHERE source='historical_replay' AND direction IS NULL "
            "AND pattern=:pat"
        ), {"pat": pattern})
        return int(res.rowcount or 0)


def backfill(engine, dry_run: bool) -> Dict[str, object]:
    per_pattern: Dict[str, Dict[str, object]] = {}
    total = 0
    for pat in TARGET_PATTERNS:
        n = _apply_pattern(engine, pat, dry_run)
        per_pattern[pat] = {
            "updated": n,
            "mode": "dry_run" if dry_run else "apply",
        }
        total += n
        logging.info("[%s] %d rows %s",
                     pat, n, "would update" if dry_run else "updated")
    return {"per_pattern": per_pattern, "total_updated": total}


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

    before_total = _null_count_total(engine)
    before_per_pat = _null_count_by_pattern(engine)
    logging.info("== %s ==", "DRY-RUN" if dry_run else "APPLY")
    logging.info("NULL direction total (historical_replay) BEFORE: %d",
                 before_total)
    for pat, n in before_per_pat:
        logging.info("  [%s] %d", pat, n)

    result = backfill(engine, dry_run=dry_run)

    after_total = _null_count_total(engine)
    elapsed = time.time() - t0

    summary = {
        "mode": "dry_run" if dry_run else "apply",
        "patterns": list(TARGET_PATTERNS),
        "null_total_before": before_total,
        "null_total_after": after_total,
        "delta": before_total - after_total,
        "rows_updated": result["total_updated"],
        "per_pattern": result["per_pattern"],
        "null_per_pattern_before": [
            {"pattern": p, "n": n} for p, n in before_per_pat
        ],
        "elapsed_sec": round(elapsed, 1),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
