"""MITS Phase 11.A — universe loader tests.

Locks the 40-ticker invariant + symbol-shape validation. If the
universe.json file ever drifts to 39 or 41 tickers without this test
being updated, CI fails immediately.
"""
from __future__ import annotations

import json
import os
import re
import tempfile

import pytest


def test_load_universe_returns_40_unique_tickers() -> None:
    from backend.bot.data.universe import load_universe
    tickers = load_universe()
    assert isinstance(tickers, list)
    assert len(tickers) == 40, (
        f"Phase 11 plan locks the universe at 40 tickers; got {len(tickers)}"
    )
    assert len(set(tickers)) == 40, "universe.json has duplicates"
    for t in tickers:
        assert t == t.upper(), f"ticker not normalized to upper: {t!r}"


def test_universe_symbol_shape_valid() -> None:
    """Every ticker is OCC-style: 1-6 uppercase letters, optional .X (1-2 letters)."""
    from backend.bot.data.universe import load_universe
    pattern = re.compile(r"^[A-Z]{1,6}(?:\.[A-Z]{1,2})?$")
    for t in load_universe():
        assert pattern.match(t), f"invalid symbol shape: {t}"


def test_is_in_universe_membership() -> None:
    from backend.bot.data.universe import is_in_universe
    # Spot checks against the locked list.
    assert is_in_universe("AAPL")
    assert is_in_universe("aapl")  # case-insensitive
    assert is_in_universe("BRK.B")
    assert is_in_universe("SPY")
    assert not is_in_universe("ZZZZZ")
    assert not is_in_universe("")


def test_universe_buckets_partition_tickers() -> None:
    """Every bucket's symbols are in the master list, and the union of
    all buckets equals the master list (no orphans, no extras)."""
    from backend.bot.data.universe import get_snapshot
    snap = get_snapshot()
    universe_set = set(snap.tickers)
    union: set = set()
    for syms in snap.buckets.values():
        for s in syms:
            assert s in universe_set, (
                f"bucket symbol {s!r} not in master tickers list"
            )
            union.add(s)
    assert union == universe_set, (
        f"buckets and tickers disagree: only in tickers={universe_set - union}, "
        f"only in buckets={union - universe_set}"
    )


def test_universe_count_matches_declared() -> None:
    from backend.bot.data.universe import get_snapshot, universe_count
    snap = get_snapshot()
    assert snap.count == universe_count() == 40


def test_universe_reload_picks_up_mtime_change(tmp_path, monkeypatch) -> None:
    """Edit universe.json on disk; reload_if_changed should re-read."""
    # Make a fake universe file + point the loader at it.
    fake = {
        "version": "test",
        "description": "test",
        "tickers": ["TESTA", "TESTB", "TESTC"],
        "count": 3,
        "criteria": [],
        "buckets": {"all": ["TESTA", "TESTB", "TESTC"]},
    }
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(fake))

    # Reset the module cache + patch the candidate paths.
    import backend.bot.data.universe as uni
    uni._CACHE = None
    monkeypatch.setattr(uni, "_DEFAULT_PATHS", (str(path),))

    assert uni.load_universe() == ["TESTA", "TESTB", "TESTC"]

    # Now mutate the file and bump mtime, then reload.
    fake["tickers"] = ["TESTA", "TESTB", "TESTC", "TESTD"]
    fake["count"] = 4
    fake["buckets"] = {"all": fake["tickers"]}
    path.write_text(json.dumps(fake))
    # Make sure mtime advances on filesystems with second granularity.
    os.utime(str(path), (path.stat().st_atime + 5, path.stat().st_mtime + 5))

    changed = uni.reload_if_changed()
    assert changed is True
    assert uni.load_universe() == ["TESTA", "TESTB", "TESTC", "TESTD"]

    # Reset for the rest of the test session.
    uni._CACHE = None


def test_universe_rejects_duplicates(tmp_path, monkeypatch) -> None:
    bad = {
        "version": "test", "description": "test",
        "tickers": ["AAPL", "AAPL"], "count": 2, "criteria": [],
        "buckets": {},
    }
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(bad))

    import backend.bot.data.universe as uni
    uni._CACHE = None
    monkeypatch.setattr(uni, "_DEFAULT_PATHS", (str(path),))
    with pytest.raises(ValueError, match="duplicate"):
        uni.get_snapshot()
    uni._CACHE = None


def test_universe_rejects_invalid_symbols(tmp_path, monkeypatch) -> None:
    bad = {
        "version": "test", "description": "test",
        "tickers": ["AAPL", "lower_case_not_allowed"],
        "count": 2, "criteria": [], "buckets": {},
    }
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(bad))

    import backend.bot.data.universe as uni
    uni._CACHE = None
    monkeypatch.setattr(uni, "_DEFAULT_PATHS", (str(path),))
    with pytest.raises(ValueError, match="invalid"):
        uni.get_snapshot()
    uni._CACHE = None
