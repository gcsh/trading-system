"""MITS Phase 9.4 — Today portfolio chart fallback.

Asserts the ``range=last_session`` explicit value + the implicit
fallback when ``range=1d`` returns zero rows.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timedelta

import pytest

import backend.db as backend_db
from backend.models.snapshot import PortfolioSnapshot


@pytest.fixture
def isolated_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=f"-p9-{uuid.uuid4().hex}.sqlite")
    os.close(fd)
    # Snapshot existing globals so we restore them; we DON'T touch the
    # production DB at all.
    saved_engine = backend_db._engine
    saved_factory = backend_db._SessionLocal
    backend_db._engine = None
    backend_db._SessionLocal = None
    backend_db.init_db(path)
    try:
        yield backend_db
    finally:
        backend_db._engine = saved_engine
        backend_db._SessionLocal = saved_factory
        try:
            os.remove(path)
        except OSError:
            pass


def _seed_snapshots(at_dt: datetime, n: int = 5) -> None:
    with backend_db.session_scope() as s:
        for i in range(n):
            s.add(PortfolioSnapshot(
                timestamp=at_dt + timedelta(minutes=i * 5),
                portfolio_value=5_000.0 + i * 10,
                cash=4_500.0,
            ))


def test_explicit_last_session_returns_wrapped_payload(isolated_db):
    from backend.api.routes.portfolio import equity_curve
    last_session_dt = datetime.utcnow() - timedelta(days=3, hours=2)
    _seed_snapshots(last_session_dt.replace(microsecond=0), n=4)
    payload = asyncio.run(equity_curve(rng="last_session"))
    assert isinstance(payload, dict)
    assert payload["range"] == "last_session"
    assert payload["snapshots"]
    assert payload["dataset_note"].startswith("Showing ")


def test_1d_falls_back_to_last_session_when_empty(isolated_db):
    from backend.api.routes.portfolio import equity_curve
    # Snapshots from 5 days ago — far outside the 1d window.
    older = datetime.utcnow() - timedelta(days=5, hours=3)
    _seed_snapshots(older.replace(microsecond=0), n=3)
    payload = asyncio.run(equity_curve(rng="1d"))
    # Fallback engages → wrapped payload.
    assert isinstance(payload, dict)
    assert payload["range"] == "last_session"
    assert payload["fallback_from"] == "1d"
    assert len(payload["snapshots"]) == 3


def test_1d_returns_list_when_intraday_present(isolated_db):
    from backend.api.routes.portfolio import equity_curve
    # Drop one snapshot in the last hour.
    recent = datetime.utcnow() - timedelta(minutes=10)
    _seed_snapshots(recent.replace(microsecond=0), n=2)
    payload = asyncio.run(equity_curve(rng="1d"))
    # Legacy list contract preserved.
    assert isinstance(payload, list)
    assert len(payload) >= 1
