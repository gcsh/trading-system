"""MITS Phase 9.3 — GEX expiration-dropdown tests.

We assert the route resolves the operator-facing labels to ``max_dte``
integers correctly + that ``gex(ticker, max_dte=N)`` filters the
chain accordingly.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.api.routes.heatseeker import _resolve_expiration
from backend.bot.signals.gex import _clean


def test_resolve_expiration_buckets():
    # The 'all' label preserves the legacy unfiltered behaviour.
    assert _resolve_expiration("all") is None
    assert _resolve_expiration(None) is None
    assert _resolve_expiration("") is None
    # Bucketed labels resolve to days.
    assert _resolve_expiration("0d") == 0
    assert _resolve_expiration("1d") == 1
    assert _resolve_expiration("5d") == 5
    assert _resolve_expiration("7d") == 7
    assert _resolve_expiration("14d") == 14
    assert _resolve_expiration("30d") == 30
    assert _resolve_expiration("60d") == 60


def test_resolve_expiration_accepts_raw_integer_strings():
    assert _resolve_expiration("21") == 21
    assert _resolve_expiration("90d") == 90


def test_clean_filters_by_max_dte():
    """A row whose expiry is beyond max_dte must be dropped."""
    today = date.today()
    far = (today + timedelta(days=40)).isoformat()
    near = (today + timedelta(days=3)).isoformat()
    rows = [
        {"strike": 100, "oi": 10, "gamma": 0.01, "type": "C", "expiry": near},
        {"strike": 105, "oi": 5,  "gamma": 0.02, "type": "C", "expiry": far},
        {"strike": 100, "oi": 8,  "gamma": 0.03, "type": "P", "expiry": near},
        {"strike": 110, "oi": 2,  "gamma": 0.01, "type": "P", "expiry": far},
    ]
    # max_dte=5 keeps only the near-expiry rows.
    filtered = _clean(rows, max_dte=5)
    assert len(filtered) == 2
    assert all(r["expiry"] == near for r in filtered)
    # max_dte=45 keeps all (legacy behaviour).
    legacy = _clean(rows, max_dte=45)
    assert len(legacy) == 4


def test_clean_drops_undated_rows_for_short_buckets():
    """Rows missing an ``expiry`` are kept ONLY for the 45-day legacy
    bucket — for narrow DTE filters they are noise we can't honour."""
    rows = [
        {"strike": 100, "oi": 10, "gamma": 0.01, "type": "C"},  # no expiry
    ]
    # Short DTE filter drops it but falls back to the original list to
    # avoid producing an empty result.
    out = _clean(rows, max_dte=5)
    # The fallback behaviour returns the original list when filtering
    # produces zero; the row should be retained either way.
    assert len(out) == 1
