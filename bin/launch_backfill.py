#!/usr/bin/env python
"""MITS Phase 11.G — foreground backfill launcher.

Usage (from /opt/trading-bot or repo root):

    python bin/launch_backfill.py \\
        --source thetadata_stocks_daily \\
        --tickers all \\
        --start 2006-01-01 \\
        --end 2026-06-09

  ``--source`` values:
      thetadata_stocks_daily
      thetadata_stocks_intraday_1m / _5m / _15m / _60m
      thetadata_iv_history
      fred
  ``--tickers`` is either ``all`` (uses universe.json) or a comma list,
  e.g. ``AAPL,MSFT,SPY``. For ``--source fred`` the tickers list is the
  FRED series_id set; defaults to the 50-series expanded panel.
  ``--start`` / ``--end`` are inclusive YYYY-MM-DD.

Logs are emitted via stdlib ``logging`` to stderr/stdout (and to
``journalctl`` when the launcher runs under systemd or via nohup
redirect into ``/var/log/tradingbot/backfill_*.log``).

This script is the one the agent kicks off via SSM with nohup so the
job survives the SSM session disconnect. It does NOT daemonize itself
— that's the operator's call.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

# Resolve repo root so ``from backend...`` works when launched directly
# (nohup, systemd, tmux, etc) without requiring the operator to ``cd``
# into the repo first.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s — %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout)


def _parse_date(s: str) -> date:
    """Accept ``YYYY-MM-DD`` (default) or ``YYYYqN`` (quarter token).

    Quarter tokens are normalized: ``--start 2021q1`` → 2021-01-01,
    ``--end 2026q2`` → 2026-06-30. Convenience for the AlphaVantage
    transcript launcher whose natural unit is a fiscal quarter.
    """
    raw = (s or "").strip()
    if not raw:
        raise ValueError("empty date string")
    # Quarter token path.
    if "q" in raw.lower() and "-" not in raw:
        from backend.bot.data.alphavantage_transcripts import (
            parse_quarter_token,
        )
        year, quarter = parse_quarter_token(raw)
        if quarter == 1:
            return date(year, 1, 1)
        if quarter == 2:
            return date(year, 6, 30)
        if quarter == 3:
            return date(year, 9, 30)
        return date(year, 12, 31)
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _resolve_tickers(arg: str, source: str) -> List[str]:
    arg = (arg or "").strip()
    if not arg or arg.lower() == "all":
        if source == "fred":
            from backend.bot.data.fred_expanded import EXPANDED_SERIES
            return list(EXPANDED_SERIES)
        if source == "edgar_13f":
            # The 13F backfill iterates CIKs (treated as "tickers" by
            # the orchestrator) — one row per (fund_cik, quarter).
            from backend.bot.data.watched_funds import watched_fund_ciks
            return list(watched_fund_ciks())
        from backend.bot.data.universe import load_universe
        return load_universe()
    # For edgar_13f a comma list is a CIK list; we accept either
    # zero-padded or unpadded forms and zero-pad here.
    if source == "edgar_13f":
        out: List[str] = []
        for token in arg.split(","):
            token = token.strip()
            if not token:
                continue
            digits = "".join(c for c in token if c.isdigit())
            if digits:
                out.append(digits.zfill(10))
        return out
    return [t.strip().upper() for t in arg.split(",") if t.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "MITS Phase 11 backfill launcher — drives SyncOrchestrator "
            "for bulk + delta pulls across the universe."
        ),
    )
    parser.add_argument(
        "--source", required=True,
        help=(
            "Source key (thetadata_stocks_daily, "
            "thetadata_stocks_intraday_1m, thetadata_iv_history, fred, ...)"
        ),
    )
    parser.add_argument(
        "--tickers", default="all",
        help=("Comma list of tickers / series_ids OR 'all' (default) for "
              "the universe / 50-series FRED panel."),
    )
    parser.add_argument(
        "--funds", default=None,
        help=("Alias for --tickers when --source=edgar_13f. Comma list of "
              "fund CIKs (zero-padded or unpadded) OR 'all' for the full "
              "100-fund watched roster."),
    )
    parser.add_argument(
        "--start", required=True,
        help="Inclusive start date YYYY-MM-DD",
    )
    parser.add_argument(
        "--end", required=True,
        help="Inclusive end date YYYY-MM-DD",
    )
    parser.add_argument(
        "--chunk-days", type=int, default=None,
        help=("Override chunk window (calendar days). Defaults to source-"
              "family value from TUNABLES."),
    )
    parser.add_argument(
        "--max-tickers", type=int, default=None,
        help="Cap the ticker list (debug aid).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose / DEBUG logging.",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    logger = logging.getLogger("launch_backfill")

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    if end < start:
        logger.error("end %s precedes start %s", end, start)
        return 2

    # Initialize the DB so models register + tables exist on first run.
    from backend.db import init_db
    init_db()

    # Memory pressure guard (MITS Phase 11.1 #9). Bail rather than
    # silently OOM-killing the long-running backfill.
    try:
        from backend.bot.data.memory_guard import (
            memory_status, wait_until_ok,
        )
        status = memory_status()
        if not status.ok:
            logger.warning(
                "backfill: memory pressure too high at launch "
                "(%.1f%% used, %.2f GB free). Waiting up to 5min...",
                status.percent, status.available_gb,
            )
            if not wait_until_ok(max_seconds=300, sleep_seconds=30):
                logger.error(
                    "backfill: memory pressure didn't clear — aborting. "
                    "Watermark protects partial progress; re-run later."
                )
                return 4
            logger.info("backfill: memory pressure cleared, proceeding.")
    except Exception:
        logger.debug("backfill: memory_guard probe failed (proceeding)",
                          exc_info=True)

    # `--funds` is the operator-facing alias for `--tickers` when the
    # source is 13F. If the operator passes both, `--funds` wins (the
    # default `--tickers` is "all" and we don't want to silently
    # swallow an explicit fund list).
    ticker_arg = args.funds if (args.funds and args.source == "edgar_13f") \
        else args.tickers
    tickers = _resolve_tickers(ticker_arg, args.source)
    if args.max_tickers:
        tickers = tickers[: int(args.max_tickers)]
    if not tickers:
        logger.error("no tickers resolved (source=%s, arg=%s)",
                          args.source, args.tickers)
        return 2

    from backend.bot.data.sync_orchestrator import get_orchestrator

    orch = get_orchestrator()
    logger.info(
        "backfill starting: source=%s tickers=%d window=[%s,%s] chunk_days=%s",
        args.source, len(tickers), start, end, args.chunk_days,
    )

    grand_completed = 0
    grand_errored = 0
    grand_skipped = 0
    grand_rows = 0
    grand_chunks = 0

    for idx, ticker in enumerate(tickers, start=1):
        logger.info("[%d/%d] %s starting", idx, len(tickers), ticker)
        try:
            summary = orch.bulk_backfill(
                source=args.source,
                ticker=ticker,
                start_date=start,
                end_date=end,
                chunk_days=args.chunk_days,
            )
        except Exception:
            logger.exception("[%d/%d] %s — bulk_backfill crashed",
                                  idx, len(tickers), ticker)
            grand_errored += 1
            continue
        grand_chunks += summary.total_chunks
        grand_completed += summary.completed_chunks
        grand_errored += summary.error_chunks
        grand_skipped += summary.skipped_chunks
        grand_rows += summary.rows_written
        logger.info(
            "[%d/%d] %s done: chunks_total=%d done=%d skipped=%d error=%d "
            "rows=%d dur=%.1fs",
            idx, len(tickers), ticker,
            summary.total_chunks, summary.completed_chunks,
            summary.skipped_chunks, summary.error_chunks,
            summary.rows_written, summary.duration_sec,
        )

    logger.info(
        "backfill GRAND TOTAL source=%s tickers=%d chunks=%d done=%d "
        "skipped=%d error=%d rows=%d",
        args.source, len(tickers), grand_chunks, grand_completed,
        grand_skipped, grand_errored, grand_rows,
    )
    return 0 if grand_errored == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
