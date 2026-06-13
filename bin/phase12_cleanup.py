#!/usr/bin/env python
"""MITS Phase 12.A — Detection-layer cleanup.

Operator audit revealed:

* 9 ghost-strategy patterns polluting ``market_observations``:
  ``bull_call_spread, cash_secured_put, covered_call_wheel, exit_manager,
  gap_fill, iron_condor, macd_momentum, rsi_mean_reversion,
  vwap_reversion`` — these are STRATEGIES not pattern detectors. They
  were emitting as observations and skewing every downstream cohort.

* Pre-2021-06-09 observations no longer have backing bars in the silver
  layer (the ThetaData 5y backfill window starts there). Outcomes that
  reference these are dangling.

* 14 detectors have NEGATIVE edge against the 68.9 percent corpus
  baseline 5-day win rate:
    choch (-5.5), talib_inverted_hammer (-5.0), talib_hammer (-4.8),
    lvn_rejection (-2.9), bos (-1.8), consolidation (-1.7),
    talib_marubozu (-1.7), hvn_acceptance (-1.7), failed_breakout
    (-1.2), talib_engulfing (-0.7), talib_harami (-0.5),
    talib_spinning_top (-0.5), bull_flag (-0.1), talib_doji.
  These stay in the registry but get ``enabled = False`` in
  DetectorConfig so the engine masks them. They can be reviewed and
  re-enabled by the operator at any time without code changes.

Steps performed (idempotent):

  1. DELETE ghost-strategy MarketObservation rows.
  2. DELETE pre-2021-06-09 MarketObservation rows.
  3. CASCADE DELETE orphan MarketOutcome rows.
  4. Mark 14 below-baseline detectors ``enabled = False``.
  5. Force-fire knowledge_graph recompute_cells() so cohorts coherent.

Usage:

    AWS_PROFILE=trading-bot python bin/phase12_cleanup.py
    AWS_PROFILE=trading-bot python bin/phase12_cleanup.py --dry-run

Outputs a JSON line per step plus a final summary.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── operator-locked policy constants (audit findings) ─────────────────

GHOST_PATTERNS: List[str] = [
    "bull_call_spread",
    "cash_secured_put",
    "covered_call_wheel",
    "exit_manager",
    "gap_fill",
    "iron_condor",
    "macd_momentum",
    "rsi_mean_reversion",
    "vwap_reversion",
]

PRE_BAR_CUTOFF_DATE = datetime(2021, 6, 9)

BELOW_BASELINE_DETECTORS: List[str] = [
    "choch",
    "talib_inverted_hammer",
    "talib_hammer",
    "lvn_rejection",
    "bos",
    "consolidation",
    "talib_marubozu",
    "hvn_acceptance",
    "failed_breakout",
    "talib_engulfing",
    "talib_harami",
    "talib_spinning_top",
    "bull_flag",
    "talib_doji",
]


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def _emit(stage: str, payload: Dict) -> None:
    line = {"stage": stage, "ts": datetime.utcnow().isoformat(), **payload}
    print(json.dumps(line, default=str))
    sys.stdout.flush()


def _count_observations() -> int:
    from sqlalchemy import func, select
    from backend.db import session_scope
    from backend.models.market_observation import MarketObservation
    with session_scope() as s:
        return int(s.execute(
            select(func.count(MarketObservation.id))
        ).scalar_one() or 0)


def _count_outcomes() -> int:
    from sqlalchemy import func, select
    from backend.db import session_scope
    from backend.models.market_outcome import MarketOutcome
    with session_scope() as s:
        return int(s.execute(
            select(func.count(MarketOutcome.id))
        ).scalar_one() or 0)


def _count_per_pattern_for(patterns: List[str]) -> Dict[str, int]:
    """Return current row counts for the supplied pattern names."""
    from sqlalchemy import func, select
    from backend.db import session_scope
    from backend.models.market_observation import MarketObservation
    out: Dict[str, int] = {}
    with session_scope() as s:
        for pat in patterns:
            n = int(s.execute(
                select(func.count(MarketObservation.id))
                .where(MarketObservation.pattern == pat)
            ).scalar_one() or 0)
            out[pat] = n
    return out


def delete_ghost_patterns(dry_run: bool) -> Dict[str, int]:
    """Step 1 — purge the 9 ghost-strategy patterns.

    SQLite enforces FOREIGN KEY constraints when PRAGMA foreign_keys
    is ON. To avoid a constraint violation we delete the dependent
    market_outcomes rows FIRST (anti-join), then the observations
    themselves.
    """
    counts_before = _count_per_pattern_for(GHOST_PATTERNS)
    total_target = sum(counts_before.values())
    if dry_run:
        return {"target_rows": total_target, "per_pattern": counts_before,
                "deleted": 0}
    from sqlalchemy import delete, select
    from backend.db import session_scope
    from backend.models.market_observation import MarketObservation
    from backend.models.market_outcome import MarketOutcome
    with session_scope() as s:
        # 1. Drop outcomes that reference soon-to-be-deleted obs rows.
        s.execute(
            delete(MarketOutcome)
            .where(MarketOutcome.observation_id.in_(
                select(MarketObservation.id)
                .where(MarketObservation.pattern.in_(GHOST_PATTERNS))
            ))
        )
        # 2. Drop the observations.
        result = s.execute(
            delete(MarketObservation)
            .where(MarketObservation.pattern.in_(GHOST_PATTERNS))
        )
    return {"target_rows": total_target, "per_pattern": counts_before,
            "deleted": int(result.rowcount or 0)}


def delete_pre_cutoff_observations(dry_run: bool) -> Dict[str, int]:
    """Step 2 — drop observations whose timestamp precedes the silver
    bar floor (2021-06-09). Dependent outcomes go first per the FK."""
    from sqlalchemy import func, select
    from backend.db import session_scope
    from backend.models.market_observation import MarketObservation
    from backend.models.market_outcome import MarketOutcome
    with session_scope() as s:
        target = int(s.execute(
            select(func.count(MarketObservation.id))
            .where(MarketObservation.timestamp < PRE_BAR_CUTOFF_DATE)
        ).scalar_one() or 0)
    if dry_run or target == 0:
        return {"target_rows": target, "deleted": 0}
    from sqlalchemy import delete
    with session_scope() as s:
        s.execute(
            delete(MarketOutcome)
            .where(MarketOutcome.observation_id.in_(
                select(MarketObservation.id)
                .where(MarketObservation.timestamp < PRE_BAR_CUTOFF_DATE)
            ))
        )
        result = s.execute(
            delete(MarketObservation)
            .where(MarketObservation.timestamp < PRE_BAR_CUTOFF_DATE)
        )
    return {"target_rows": target, "deleted": int(result.rowcount or 0)}


def cascade_orphan_outcomes(dry_run: bool) -> Dict[str, int]:
    """Step 3 — drop ``market_outcomes`` whose observation_id no longer
    resolves. SQLite doesn't enforce ON DELETE CASCADE for our FK so we
    sweep manually."""
    from sqlalchemy import func, select
    from backend.db import session_scope
    from backend.models.market_observation import MarketObservation
    from backend.models.market_outcome import MarketOutcome
    with session_scope() as s:
        # Anti-join — outcomes whose observation_id is not in market_observations.
        orphan_q = (
            select(func.count(MarketOutcome.id))
            .where(~MarketOutcome.observation_id.in_(
                select(MarketObservation.id)
            ))
        )
        target = int(s.execute(orphan_q).scalar_one() or 0)
    if dry_run or target == 0:
        return {"target_rows": target, "deleted": 0}
    from sqlalchemy import delete
    with session_scope() as s:
        result = s.execute(
            delete(MarketOutcome)
            .where(~MarketOutcome.observation_id.in_(
                select(MarketObservation.id)
            ))
        )
    return {"target_rows": target, "deleted": int(result.rowcount or 0)}


def disable_below_baseline_detectors(dry_run: bool) -> Dict[str, int]:
    """Step 4 — write enabled=False onto the 14 detectors flagged by the
    edge audit. UPSERT so detectors that don't have a config row yet
    still get one."""
    from sqlalchemy import select
    from backend.db import session_scope
    from backend.models.detector_config import DetectorConfig
    touched = 0
    inserted = 0
    if dry_run:
        return {"target": len(BELOW_BASELINE_DETECTORS), "touched": 0,
                "inserted": 0}
    with session_scope() as s:
        for name in BELOW_BASELINE_DETECTORS:
            row = s.execute(
                select(DetectorConfig).where(DetectorConfig.name == name)
            ).scalar_one_or_none()
            if row is None:
                row = DetectorConfig(
                    name=name, enabled=False, params_json="{}",
                    source="builtin",
                )
                s.add(row)
                inserted += 1
            elif row.enabled:
                row.enabled = False
            row.last_updated_at = datetime.utcnow()
            touched += 1
    # Force a re-read on the live engine.
    try:
        from backend.bot.detectors import clear_detector_config_cache
        clear_detector_config_cache()
    except Exception:
        pass
    return {"target": len(BELOW_BASELINE_DETECTORS),
            "touched": touched, "inserted": inserted}


def force_aggregator(dry_run: bool) -> Dict[str, object]:
    """Step 5 — recompute every knowledge_graph cell so cohorts stay
    coherent after the ghost + pre-cutoff purge."""
    if dry_run:
        return {"skipped": "dry-run"}
    from backend.bot.corpus.knowledge_aggregator import recompute_cells
    return recompute_cells()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="MITS Phase 12.A — detection layer cleanup.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print row counts that WOULD be deleted but do not modify.",
    )
    parser.add_argument(
        "--skip-aggregator", action="store_true",
        help="Skip the final knowledge_graph recompute.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    from backend.db import init_db
    init_db()

    obs_before = _count_observations()
    out_before = _count_outcomes()
    _emit("baseline", {"observations": obs_before, "outcomes": out_before,
                          "dry_run": args.dry_run})

    s1 = delete_ghost_patterns(args.dry_run)
    _emit("step1_ghost_purge", s1)

    s2 = delete_pre_cutoff_observations(args.dry_run)
    _emit("step2_pre_cutoff_purge", s2)

    s3 = cascade_orphan_outcomes(args.dry_run)
    _emit("step3_orphan_outcomes", s3)

    s4 = disable_below_baseline_detectors(args.dry_run)
    _emit("step4_disable_detectors",
            {**s4, "names": BELOW_BASELINE_DETECTORS})

    if not args.skip_aggregator:
        s5 = force_aggregator(args.dry_run)
        _emit("step5_aggregator", s5)

    obs_after = _count_observations()
    out_after = _count_outcomes()
    _emit("done", {
        "observations_before": obs_before,
        "observations_after": obs_after,
        "observations_deleted": obs_before - obs_after,
        "outcomes_before": out_before,
        "outcomes_after": out_after,
        "outcomes_deleted": out_before - out_after,
        "dry_run": args.dry_run,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
