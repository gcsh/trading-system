#!/usr/bin/env python
"""MITS Phase 8.5 — One-shot vector backfill.

Embeds every existing row in the four canonical namespaces
(regime_snapshots, market_observations, eod_theses, closed_trades)
and upserts to pgvector.

Run once at deploy time, then the 30-min indexing cron picks up the
incremental work.

Usage:

    python bin/backfill_vectors.py        # incremental (skip already-indexed)
    python bin/backfill_vectors.py --full # re-embed everything

Best-effort: namespaces whose source table is empty just log a zero.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s backfill: %(message)s",
)
log = logging.getLogger("backfill")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true",
                          help="Re-embed every row (ignore watermark).")
    args = parser.parse_args()

    from backend.bot.ai import vector_indexing, vector_store
    vector_store.ensure_schema()
    log.info("starting vector backfill (full=%s)", args.full)
    stats = vector_indexing.index_pass(full=args.full)
    print(json.dumps(stats, indent=2))
    log.info("vector backfill done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
