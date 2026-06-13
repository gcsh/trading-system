#!/usr/bin/env python
"""MITS Phase 11.B.2 — ThetaData options EOD chain backfill launcher.

Separate from ``launch_backfill.py`` so a multi-day options pull
doesn't compete with the stock + 13F + Form 4 stock-bar backfills
that are already in flight. Both can run side-by-side on the same
EC2 box; they share the orchestrator's per-source token bucket.

Per-ticker workflow:
  1. List ALL historical expirations via ThetaData.
  2. Filter to expirations whose lifetime overlaps the requested
     window (default 2021-06-09 → today).
  3. For each surviving (ticker, expiry) tuple, the orchestrator runs
     ``bulk_backfill`` with the ``thetadata_options_eod`` callback:
        - lists strikes for the expiry
        - trims to ATM ± ``options_eod_atm_strike_window`` strikes via
          stock_bars
        - per-contract EOD pulls across the threadpool
        - INSERT OR IGNORE into ``option_contract_bars``
        - bronze parquet to S3

Resume contract:
  - Per-(ticker, expiry) chunk = one row in ``backfill_progress``.
  - Re-running the launcher SKIPS already-``done`` chunks.

Usage on EC2:

    AWS_PROFILE=trading-bot ssm send-command ... \\
        --parameters 'commands=["nohup /opt/trading-bot/.venv/bin/python /opt/trading-bot/bin/launch_options_backfill.py --tickers all --start 2021-06-09 --end 2026-06-09 > /var/log/tradingbot/backfill_options.log 2>&1 &"]'

Log lines go to ``/var/log/tradingbot/backfill_options.log`` when the
operator redirects stdout there.
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "MITS Phase 11.B.2 options EOD chain backfill — drives "
            "SyncOrchestrator across (ticker × expiration) tuples."
        ),
    )
    parser.add_argument(
        "--tickers", default="all",
        help="Comma-list of tickers OR 'all' (universe.json).",
    )
    parser.add_argument(
        "--start", default=None,
        help=("Inclusive history start YYYY-MM-DD. Default = "
              "TUNABLES.options_eod_history_start."),
    )
    parser.add_argument(
        "--end", default=None,
        help="Inclusive history end YYYY-MM-DD. Default = today.",
    )
    parser.add_argument(
        "--max-tickers", type=int, default=None,
        help="Cap on tickers (debug aid).",
    )
    parser.add_argument(
        "--max-expirations-per-ticker", type=int, default=None,
        help="Cap expirations processed per ticker (debug aid).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="DEBUG logging.",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    logger = logging.getLogger("launch_options_backfill")

    # Initialize DB so OptionContractBar table exists.
    from backend.db import init_db
    init_db()

    from backend.config import TUNABLES
    from backend.bot.data.sync_orchestrator import get_orchestrator
    from backend.bot.data.thetadata_options_history import (
        list_active_expiration_tokens,
    )

    start = _parse_date(args.start) if args.start else _parse_date(
        TUNABLES.options_eod_history_start)
    end = _parse_date(args.end) if args.end else date.today()
    if end < start:
        logger.error("end %s precedes start %s", end, start)
        return 2

    tickers = _resolve_tickers(args.tickers)
    if args.max_tickers:
        tickers = tickers[: int(args.max_tickers)]
    if not tickers:
        logger.error("no tickers resolved")
        return 2

    orch = get_orchestrator()
    logger.info(
        "options EOD backfill starting: tickers=%d window=[%s,%s]",
        len(tickers), start, end,
    )

    grand_chunks = 0
    grand_done = 0
    grand_skipped = 0
    grand_errored = 0
    grand_rows = 0

    for t_idx, ticker in enumerate(tickers, start=1):
        t0 = time.monotonic()
        try:
            tokens = list_active_expiration_tokens(ticker, start, end)
        except Exception:
            logger.exception("[%d/%d] %s — list_active_expiration_tokens "
                             "failed", t_idx, len(tickers), ticker)
            grand_errored += 1
            continue
        if args.max_expirations_per_ticker:
            tokens = tokens[: int(args.max_expirations_per_ticker)]
        logger.info(
            "[%d/%d] %s — %d expirations to backfill",
            t_idx, len(tickers), ticker, len(tokens),
        )
        per_ticker_rows = 0
        for e_idx, token in enumerate(tokens, start=1):
            try:
                summary = orch.bulk_backfill(
                    source="thetadata_options_eod",
                    ticker=token,  # carries "TICKER|YYYYMMDD"
                    start_date=start,
                    end_date=end,
                    # One chunk per (ticker, expiry) — keep the chunk
                    # window full so the callback's lifetime clamp
                    # gates the date range, not a 365d split.
                    chunk_days=max(1, (end - start).days + 1),
                )
            except Exception:
                logger.exception(
                    "[%d/%d %d/%d] %s — bulk_backfill crashed",
                    t_idx, len(tickers), e_idx, len(tokens), token,
                )
                grand_errored += 1
                continue
            grand_chunks += summary.total_chunks
            grand_done += summary.completed_chunks
            grand_skipped += summary.skipped_chunks
            grand_errored += summary.error_chunks
            grand_rows += summary.rows_written
            per_ticker_rows += summary.rows_written
            if e_idx % 25 == 0 or e_idx == len(tokens):
                logger.info(
                    "[%d/%d %d/%d] %s — running rows=%d",
                    t_idx, len(tickers), e_idx, len(tokens), token,
                    per_ticker_rows,
                )
        elapsed = time.monotonic() - t0
        logger.info(
            "[%d/%d] %s done: expiries=%d rows=%d dur=%.1fs",
            t_idx, len(tickers), ticker, len(tokens),
            per_ticker_rows, elapsed,
        )

    logger.info(
        "options EOD backfill GRAND TOTAL tickers=%d chunks=%d done=%d "
        "skipped=%d error=%d rows=%d",
        len(tickers), grand_chunks, grand_done, grand_skipped,
        grand_errored, grand_rows,
    )
    return 0 if grand_errored == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
