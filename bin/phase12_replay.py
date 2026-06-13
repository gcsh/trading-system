#!/usr/bin/env python
"""MITS Phase 12.I — full-corpus detector replay for the rebuilt
detection layer.

Wrapper around ``bin/replay_corpus.py`` that:

  1. Forces the cleanup script to run first (idempotent).
  2. Re-runs the full universe through every ENABLED detector,
     including the 25 new Phase 12 detectors.
  3. Triggers outcome_linker over the new observations.
  4. Triggers knowledge_aggregator (now using hierarchical priors).
  5. Prints a comprehensive per-detector + cohort-fix report.

Usage:

    AWS_PROFILE=trading-bot python bin/phase12_replay.py \\
        --start 2021-06-09 \\
        --end 2026-06-09

    # Skip cleanup if already run.
    AWS_PROFILE=trading-bot python bin/phase12_replay.py --skip-cleanup

    # Subset tickers.
    AWS_PROFILE=trading-bot python bin/phase12_replay.py --tickers AAPL,MSFT
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )


def _emit(stage: str, payload) -> None:
    line = {"stage": stage, "ts": datetime.utcnow().isoformat(),
              "payload": payload}
    print(json.dumps(line, default=str))
    sys.stdout.flush()


def _new_phase12_detectors():
    """Return the canonical list of 25 new Phase 12 detector names."""
    return [
        # SMC
        "order_block", "fair_value_gap", "liquidity_sweep_v2",
        "stop_hunt_v2", "premium_discount_zone",
        "market_structure_shift_v2",
        # Wyckoff
        "wyckoff_accumulation_phase", "wyckoff_distribution_phase",
        "wyckoff_spring", "wyckoff_sos", "wyckoff_upthrust",
        # Volume Profile v2
        "poc_retest", "value_area_rejection", "composite_value_area",
        # Catalyst
        "pead_drift", "insider_cluster", "smart_money_inflow",
        "earnings_revision_shift",
        # Macro
        "yield_curve_inversion", "credit_spread_widening",
        "dollar_strength_shift", "composite_macro_regime",
        # Quantitative
        "cross_sectional_momentum", "mean_reversion_z",
        "sector_dispersion",
    ]


def _per_detector_counts(detector_names):
    """Per-detector observation count + 5d win rate from
    market_observations + market_outcomes."""
    from sqlalchemy import func, select
    from backend.db import session_scope
    from backend.models.market_observation import MarketObservation
    from backend.models.market_outcome import MarketOutcome
    out = {}
    with session_scope() as s:
        for name in detector_names:
            obs_n = int(s.execute(
                select(func.count(MarketObservation.id))
                .where(MarketObservation.pattern == name)
            ).scalar_one() or 0)
            # 5d win rate.
            wr_row = s.execute(
                select(func.count(MarketOutcome.id),
                              func.sum(
                                  func.cast(MarketOutcome.was_winner,
                                                  __import__("sqlalchemy").types.Integer)
                              ))
                .join(MarketObservation,
                            MarketObservation.id == MarketOutcome.observation_id)
                .where(MarketObservation.pattern == name)
                .where(MarketOutcome.horizon == "5d")
            ).first()
            n_out = int(wr_row[0] or 0) if wr_row else 0
            wins = int(wr_row[1] or 0) if wr_row else 0
            wr = (wins / n_out) if n_out > 0 else None
            out[name] = {
                "obs_count": obs_n,
                "outcomes_5d": n_out,
                "wins_5d": wins,
                "win_rate_5d": wr,
            }
    return out


def _knowledge_graph_distribution():
    """Compute the N>=30 fraction across knowledge_graph cells."""
    from sqlalchemy import func, select
    from backend.db import session_scope
    from backend.models.knowledge_graph_cell import KnowledgeGraphCell
    out = {}
    with session_scope() as s:
        total = int(s.execute(
            select(func.count(KnowledgeGraphCell.id))
        ).scalar_one() or 0)
        n30 = int(s.execute(
            select(func.count(KnowledgeGraphCell.id))
            .where(KnowledgeGraphCell.sample_size >= 30)
        ).scalar_one() or 0)
        n100 = int(s.execute(
            select(func.count(KnowledgeGraphCell.id))
            .where(KnowledgeGraphCell.sample_size >= 100)
        ).scalar_one() or 0)
        out["total_cells"] = total
        out["n_ge_30"] = n30
        out["n_ge_100"] = n100
        out["frac_n_ge_30"] = (n30 / total) if total else 0.0
        out["frac_n_ge_100"] = (n100 / total) if total else 0.0
        # confidence level counts
        for conf in ("high", "medium", "low", "thin"):
            c = int(s.execute(
                select(func.count(KnowledgeGraphCell.id))
                .where(KnowledgeGraphCell.confidence_level == conf)
            ).scalar_one() or 0)
            out[f"count_{conf}"] = c
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="MITS Phase 12.I — institutional replay.",
    )
    parser.add_argument("--tickers", default="all")
    parser.add_argument("--start", default="2021-06-09")
    parser.add_argument("--end",
                                default=date.today().isoformat())
    parser.add_argument("--skip-cleanup", action="store_true",
                                help="Skip 12.A cleanup (run separately).")
    parser.add_argument("--skip-replay", action="store_true",
                                help="Skip the actual replay (report-only).")
    parser.add_argument(
        "--detectors", default="",
        help=("Comma-separated list of detector names to replay. When"
              " set, only those detectors fire during replay (all other"
              " detectors are skipped). Empty = run every enabled"
              " detector. Used for force-replay of newly-fixed detectors"
              " without re-running the entire corpus."),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    from backend.db import init_db
    init_db()

    # Baseline distribution before replay.
    pre = _knowledge_graph_distribution()
    _emit("baseline_kg_distribution", pre)

    new_det_names = _new_phase12_detectors()
    pre_counts = _per_detector_counts(new_det_names)
    _emit("baseline_new_detector_counts",
            {k: v["obs_count"] for k, v in pre_counts.items()})

    # 1. Cleanup (Phase 12.A).
    if not args.skip_cleanup:
        from bin.phase12_cleanup import (
            delete_ghost_patterns, delete_pre_cutoff_observations,
            cascade_orphan_outcomes, disable_below_baseline_detectors,
        )
        s1 = delete_ghost_patterns(dry_run=False)
        _emit("cleanup_ghost", s1)
        s2 = delete_pre_cutoff_observations(dry_run=False)
        _emit("cleanup_pre_cutoff", s2)
        s3 = cascade_orphan_outcomes(dry_run=False)
        _emit("cleanup_orphans", s3)
        s4 = disable_below_baseline_detectors(dry_run=False)
        _emit("cleanup_disable", s4)

    # 2. Replay.
    if not args.skip_replay:
        from bin.replay_corpus import main as replay_main
        replay_argv = [
            "--tickers", args.tickers,
            "--start", args.start,
            "--end", args.end,
        ]
        if args.detectors:
            replay_argv.extend(["--detectors", args.detectors])
        if args.verbose:
            replay_argv.append("-v")
        rc = replay_main(replay_argv)
        _emit("replay_return_code", rc)

    # 3. Post-replay distribution + counts.
    post = _knowledge_graph_distribution()
    _emit("post_kg_distribution", post)
    post_counts = _per_detector_counts(new_det_names)
    _emit("post_new_detector_counts",
            {k: v["obs_count"] for k, v in post_counts.items()})

    # 4. Per-detector report — Phase 12.2 uses the dynamic per-direction
    # baseline (long vs short vs null) instead of the stale 0.689 long
    # baseline that produced -68pp ghost edges.
    try:
        from backend.api.routes.detector_scorecard import get_baselines
        baselines = get_baselines(force_refresh=True)
    except Exception:
        baselines = {"long": 0.50, "short": 0.50, "neutral": 0.50, "null": 0.50}
    from backend.bot.detectors.direction import STATIC_DIRECTION
    rows = []
    for name, vals in post_counts.items():
        wr = vals["win_rate_5d"]
        direction = STATIC_DIRECTION.get(name) or "null"
        baseline_wr = baselines.get(direction, baselines.get("null", 0.50))
        edge_pp = ((wr - baseline_wr) * 100.0) if wr is not None else None
        rows.append({
            "name": name,
            "direction": direction,
            "baseline": round(float(baseline_wr), 4),
            "observations": vals["obs_count"],
            "outcomes_5d": vals["outcomes_5d"],
            "win_rate_5d": round(wr, 4) if wr is not None else None,
            "edge_pp_vs_baseline": (round(edge_pp, 2)
                                            if edge_pp is not None else None),
        })
    rows.sort(key=lambda r: -(r["edge_pp_vs_baseline"] or -999))
    _emit("per_detector_report", rows)

    # 5. Cohort improvement summary.
    summary = {
        "kg_n_ge_30_pre_fraction": pre["frac_n_ge_30"],
        "kg_n_ge_30_post_fraction": post["frac_n_ge_30"],
        "kg_total_pre": pre["total_cells"],
        "kg_total_post": post["total_cells"],
        "new_detectors_total_obs": sum(v["obs_count"]
                                                for v in post_counts.values()),
        "new_detectors_with_obs": sum(1 for v in post_counts.values()
                                                if v["obs_count"] > 0),
    }
    _emit("phase12_summary", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
