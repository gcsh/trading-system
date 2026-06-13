"""MITS Phase 15 follow-up — direction backfill on legacy historical_replay rows.

Seeds synthetic market_observations rows with mixed patterns, runs the
backfill, asserts long/short get tagged correctly, neutral-only
detector (sector_dispersion) stays NULL, and a second invocation is a
no-op.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text

from backend.db import get_engine, session_scope
from backend.models.market_observation import MarketObservation  # noqa: F401  (table registration)

import importlib.util
import pathlib

_BACKFILL_PATH = pathlib.Path(__file__).resolve().parents[2] / "bin" / "backfill_direction.py"
_spec = importlib.util.spec_from_file_location("backfill_direction", _BACKFILL_PATH)
backfill_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill_mod)


pytestmark = [pytest.mark.unit]


def _seed(engine) -> None:
    """5 rows: 2 bull_flag (→long), 2 wyckoff_distribution_phase (→short),
    1 sector_dispersion (no mapping; stays NULL).

    Inserted via raw SQL so the ORM's column default='long' doesn't fire —
    legacy pre-12.1 production rows have literal NULL direction values,
    which is what we're backfilling.
    """
    ts = datetime(2025, 1, 1, 14, 30, 0).isoformat()
    rows = [
        ("AAPL", "bull_flag"),
        ("MSFT", "bull_flag"),
        ("AAPL", "wyckoff_distribution_phase"),
        ("MSFT", "wyckoff_distribution_phase"),
        ("SPY", "sector_dispersion"),
    ]
    with engine.begin() as conn:
        for ticker, pattern in rows:
            conn.execute(text(
                "INSERT INTO market_observations "
                "(ticker, pattern, timestamp, timeframe, regime, vol_state, "
                " time_bucket, source, direction, parity_warn, created_at) "
                "VALUES (:t, :p, :ts, '1d', 'unknown', 'normal', 'rth', "
                "        'historical_replay', NULL, 0, :ts)"
            ), {"t": ticker, "p": pattern, "ts": ts})


def _directions_by_pattern(session) -> dict:
    out = {}
    for row in session.query(MarketObservation).all():
        out.setdefault(row.pattern, []).append(row.direction)
    return out


def test_backfill_tags_long_short_and_leaves_neutral_null(temp_db):
    engine = get_engine()
    _seed(engine)

    result = backfill_mod.backfill(engine, dry_run=False)

    with session_scope() as s:
        by_pat = _directions_by_pattern(s)

    assert sorted(by_pat["bull_flag"]) == ["long", "long"]
    assert sorted(by_pat["wyckoff_distribution_phase"]) == ["short", "short"]
    assert by_pat["sector_dispersion"] == [None]

    # Totals from the result reflect only the rows actually updated
    # (per-pattern static path is what fires for these three patterns).
    assert result["totals"].get("long") == 2
    assert result["totals"].get("short") == 2
    assert "neutral" not in result["totals"] or result["totals"]["neutral"] == 0

    # sector_dispersion appears in unmapped_patterns (it resolved to None).
    unmapped_names = [p for p, _ in result["unmapped_patterns"]]
    assert "sector_dispersion" in unmapped_names


def test_backfill_is_idempotent_on_rerun(temp_db):
    engine = get_engine()
    _seed(engine)

    backfill_mod.backfill(engine, dry_run=False)
    second = backfill_mod.backfill(engine, dry_run=False)

    # Second pass updates nothing — only sector_dispersion remains NULL,
    # and it has no mapping, so totals must be empty / zero.
    assert second["totals"] == {} or all(
        v == 0 for v in second["totals"].values())

    # sector_dispersion still NULL.
    with engine.begin() as conn:
        nulls = conn.execute(text(
            "SELECT pattern FROM market_observations "
            "WHERE direction IS NULL"
        )).fetchall()
    assert [r[0] for r in nulls] == ["sector_dispersion"]


def test_backfill_dry_run_does_not_commit(temp_db):
    engine = get_engine()
    _seed(engine)

    backfill_mod.backfill(engine, dry_run=True)

    with engine.begin() as conn:
        nulls = conn.execute(text(
            "SELECT COUNT(*) FROM market_observations "
            "WHERE direction IS NULL"
        )).scalar()
    # All 5 stay NULL on dry-run.
    assert int(nulls) == 5
