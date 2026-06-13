#!/usr/bin/env python
"""MITS Phase 11.J — cross-vendor parity audit launcher.

Compares yfinance daily closes vs ThetaData EOD closes (from
``stock_bars``) over the audit window. Writes to
``parity_audit_history`` and flags ``market_observations.parity_warn``
for any day where divergence exceeds the suspect threshold.

Usage:

    python bin/parity_audit.py \\
        --tickers all \\
        --start 2021-06-09 \\
        --end 2026-06-09

The audit is idempotent — re-running over the same window UPSERTs
rows (preserving severity / divergence). Safe to wire to a daily cron
that audits the prior trading day.
"""
from __future__ import annotations

import argparse
import logging
import sys
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
        description=("MITS Phase 11.J — parity audit (yfinance vs "
                       "ThetaData EOD closes)."),
    )
    parser.add_argument("--tickers", default="all")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    logger = logging.getLogger("parity_audit")

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    if end < start:
        logger.error("end %s precedes start %s", end, start)
        return 2

    from backend.db import init_db
    init_db()

    tickers = _resolve_tickers(args.tickers)
    if not tickers:
        logger.error("no tickers resolved (arg=%s)", args.tickers)
        return 2

    logger.info(
        "parity audit starting: tickers=%d window=[%s,%s]",
        len(tickers), start, end,
    )

    from backend.bot.corpus.parity_audit import audit_universe

    def _progress(idx: int, ticker: str, stats: dict) -> None:
        logger.info(
            "[%d/%d] %s: audited=%d suspect=%d warn=%d ok=%d missing=%d "
            "obs_flagged=%d yf_dates=%d theta_dates=%d",
            idx, len(tickers), ticker,
            stats.get("rows_audited", 0),
            stats.get("suspect_dates", 0),
            stats.get("warn_dates", 0),
            stats.get("ok_dates", 0),
            stats.get("missing_dates", 0),
            stats.get("obs_flagged", 0),
            stats.get("yf_dates", 0),
            stats.get("theta_dates", 0),
        )

    grand = audit_universe(
        tickers, start_date=start, end_date=end, progress_cb=_progress,
    )
    logger.info("parity audit GRAND TOTAL: %s", grand)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
