#!/usr/bin/env python
"""MITS Phase 11.H — full-corpus detector replay launcher.

Walks the universe (or a subset), reads daily silver bars from
``stock_bars``, runs every enabled detector, persists observations,
then runs outcome_linker + knowledge_aggregator + a fresh history
snapshot.

Usage:

    python bin/replay_corpus.py \\
        --tickers all \\
        --start 2021-06-09 \\
        --end 2026-06-09

    python bin/replay_corpus.py \\
        --tickers AAPL,MSFT \\
        --start 2024-01-01 \\
        --end 2026-06-09

    # OPERATOR-only — drop the existing corpus first.
    python bin/replay_corpus.py \\
        --tickers all \\
        --start 2021-06-09 \\
        --end 2026-06-09 \\
        --clean

Pipeline phases (in order):

  1. Optional ``--clean`` wipe of ``market_observations`` +
     ``market_outcomes`` so the corpus rebuilds from scratch.
  2. Per-ticker silver-bar replay (writes new MarketObservation rows).
  3. Outcome linker over the freshly-landed observations.
  4. Knowledge aggregator (recompute_cells).
  5. History snapshot (snapshot_cells_to_history) so sparklines reset.

Resumable: each step is idempotent on already-landed rows.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s — %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout)


def _parse_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def _resolve_tickers(arg: str) -> List[str]:
    arg = (arg or "").strip()
    if not arg or arg.lower() == "all":
        from backend.bot.data.universe import load_universe
        return load_universe()
    return [t.strip().upper() for t in arg.split(",") if t.strip()]


def _clean_corpus(logger: logging.Logger) -> None:
    """Drop every row from market_observations + market_outcomes so the
    replay rebuilds a fresh corpus. Confirmation is the operator's
    ``--clean`` flag — they passed it intentionally."""
    from sqlalchemy import delete
    from backend.db import session_scope
    from backend.models.market_observation import MarketObservation
    from backend.models.market_outcome import MarketOutcome
    with session_scope() as s:
        obs_before = s.execute(
            delete(MarketObservation)
        ).rowcount or 0
        out_before = s.execute(
            delete(MarketOutcome)
        ).rowcount or 0
    logger.warning(
        "--clean: dropped %d market_observations + %d market_outcomes",
        obs_before, out_before,
    )


def _watermark_replay_progress(ticker: str, end_date: date,
                                       summary) -> None:
    """Persist a backfill_progress + watermark row so the next replay
    can pick up where this one left off (or skip already-done tickers).
    """
    from backend.db import session_scope
    from backend.models.backfill_progress import BackfillProgress
    from backend.models.data_watermark import DataWatermark
    from sqlalchemy import select
    try:
        with session_scope() as s:
            wm = s.execute(
                select(DataWatermark)
                .where(DataWatermark.source == "detector_replay")
                .where(DataWatermark.ticker == ticker)
            ).scalar_one_or_none()
            if wm is None:
                wm = DataWatermark(
                    source="detector_replay", ticker=ticker,
                )
                s.add(wm)
                s.flush()
            wm.last_synced_ts = datetime.utcnow()
            wm.last_synced_through_date = end_date.isoformat()
            wm.rows_last_sync = int(summary.observations_inserted or 0)
            wm.success = 1 if summary.errors == 0 else 0
            wm.updated_at = datetime.utcnow()
            # Also leave a backfill_progress breadcrumb so the
            # source row is auditable.
            prog = BackfillProgress(
                source="detector_replay",
                ticker=ticker,
                date_range_start=end_date.isoformat(),
                date_range_end=end_date.isoformat(),
                status="done" if summary.errors == 0 else "error",
                last_completed_date=end_date.isoformat(),
                rows_written=int(summary.observations_inserted or 0),
                retry_count=0,
                started_at=datetime.utcnow(),
                completed_at=datetime.utcnow(),
            )
            s.add(prog)
    except Exception:
        logging.getLogger(__name__).debug(
            "watermark write failed for %s", ticker, exc_info=True,
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "MITS Phase 11.H — full-corpus detector replay. Walks "
            "stock_bars silver layer, emits MarketObservation rows, "
            "and refreshes the knowledge_graph."
        ),
    )
    parser.add_argument(
        "--tickers", default="all",
        help=("Comma list of tickers OR 'all' for the universe."),
    )
    parser.add_argument(
        "--start", required=True, help="YYYY-MM-DD inclusive start",
    )
    parser.add_argument(
        "--end", required=True, help="YYYY-MM-DD inclusive end",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help=("DESTRUCTIVE: drop existing market_observations + "
              "market_outcomes before replay. Use only for the "
              "operator-approved clean corpus rebuild."),
    )
    parser.add_argument(
        "--skip-outcome-link", action="store_true",
        help=("Skip step 3 (outcome_linker). For debugging the replay "
              "phase only."),
    )
    parser.add_argument(
        "--skip-aggregator", action="store_true",
        help=("Skip steps 4+5 (knowledge_aggregator + snapshot). "
              "For debugging."),
    )
    parser.add_argument(
        "--intraday-source", default="thetadata_stocks_intraday_5m",
        help=("Watermark source key to consult for intraday backfill "
              "readiness. Intraday-only detectors are skipped when this "
              "watermark hasn't reached --end yet."),
    )
    parser.add_argument(
        "--detectors", default="",
        help=("Comma-separated list of detector patterns to keep. When"
              " set, only observations matching these patterns are"
              " persisted (force-replay path for newly-fixed"
              " detectors). Empty = persist every detector's"
              " observations."),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
    )
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    logger = logging.getLogger("replay_corpus")

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    if end < start:
        logger.error("end %s precedes start %s", end, start)
        return 2

    from backend.db import init_db
    init_db()

    if args.clean:
        _clean_corpus(logger)

    tickers = _resolve_tickers(args.tickers)
    if not tickers:
        logger.error("no tickers resolved (arg=%s)", args.tickers)
        return 2

    logger.info(
        "replay starting: tickers=%d window=[%s,%s] clean=%s "
        "intraday_source=%s",
        len(tickers), start, end, args.clean, args.intraday_source,
    )

    from backend.bot.corpus.replay_from_silver import replay_universe

    def _progress(idx: int, total: int, ticker: str, summary) -> None:
        logger.info(
            "[%d/%d] %s done: bars=%d emitted=%d inserted=%d "
            "skipped=%d errors=%d dur=%.1fs",
            idx, total, ticker, summary.bars_read,
            summary.observations_emitted, summary.observations_inserted,
            summary.observations_skipped, summary.errors,
            summary.duration_sec,
        )
        _watermark_replay_progress(ticker, end, summary)

    t_replay_start = time.time()
    pattern_filter = None
    if args.detectors:
        pattern_filter = [p.strip() for p in args.detectors.split(",")
                          if p.strip()]
        if pattern_filter:
            logger.info("replay filtered to detectors: %s", pattern_filter)
    grand = replay_universe(
        tickers,
        start_date=start,
        end_date=end,
        intraday_source=args.intraday_source,
        progress_cb=_progress,
        pattern_filter=pattern_filter,
    )
    t_replay = time.time() - t_replay_start
    logger.info(
        "replay GRAND TOTAL: tickers=%d bars=%d emitted=%d inserted=%d "
        "skipped=%d errors=%d duration=%.1fs",
        grand["tickers"], grand["bars_read"],
        grand["observations_emitted"], grand["observations_inserted"],
        grand["observations_skipped"], grand["errors"], t_replay,
    )
    # Top-15 detectors by emission count.
    by_det = sorted(grand["per_detector"].items(),
                       key=lambda kv: -kv[1])[:15]
    logger.info("top-15 detectors: %s", by_det)

    # ── step 3 — outcome linker ─────────────────────────────────────────
    if not args.skip_outcome_link:
        from backend.bot.corpus.outcome_linker import link_outcomes_batch
        logger.info("outcome linker starting...")
        t_ol = time.time()
        # Process per-ticker so we don't load the world into memory.
        # link_outcomes_batch defaults to limit=5000 obs/run; loop
        # until the inserted count plateaus.
        ol_grand = {"observations_processed": 0,
                       "outcomes_inserted": 0,
                       "outcomes_skipped": 0, "errors": 0}
        for ticker in tickers:
            for _ in range(6):  # at most 6 rounds × 5000 obs = 30k per ticker
                stats = link_outcomes_batch(ticker=ticker, limit=5000)
                for k in ol_grand:
                    ol_grand[k] += stats.get(k, 0)
                if stats.get("observations_processed", 0) == 0:
                    break
        logger.info(
            "outcome linker total: %s (dur=%.1fs)",
            ol_grand, time.time() - t_ol,
        )

    # ── step 4 — knowledge aggregator ───────────────────────────────────
    if not args.skip_aggregator:
        from backend.bot.corpus.knowledge_aggregator import recompute_cells
        logger.info("knowledge aggregator starting...")
        t_kg = time.time()
        ka_stats = recompute_cells()
        logger.info(
            "knowledge_graph recompute: %s (dur=%.1fs)",
            ka_stats, time.time() - t_kg,
        )

        # ── step 5 — history snapshot ───────────────────────────────────
        from backend.bot.corpus.knowledge_aggregator import (
            snapshot_cells_to_history,
        )
        logger.info("history snapshot starting...")
        t_hs = time.time()
        hs_stats = snapshot_cells_to_history()
        logger.info(
            "history snapshot: %s (dur=%.1fs)",
            hs_stats, time.time() - t_hs,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
