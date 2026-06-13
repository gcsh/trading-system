"""MITS Phase 9.5 — Lake health monitor tests.

Asserts:

  * ``run_health_check`` writes a new alert when a threshold trips.
  * A second pass with the condition cleared auto-resolves the alert.
  * Vector-shrink detection requires a prior baseline (no false
    positive on first run).
  * ``record_bronze_failure`` + 24h trim feeds into the
    ``write_failures`` alert.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import backend.db as backend_db
from backend.bot.monitoring import lake_health


class _FakeStat:
    def __init__(self, last_modified=None, bytes_=0, object_count=0):
        self.last_modified = last_modified
        self.bytes = bytes_
        self.object_count = object_count


@pytest.fixture
def isolated_db():
    fd, path = tempfile.mkstemp(suffix=f"-p9-lh-{uuid.uuid4().hex}.sqlite")
    os.close(fd)
    saved_engine = backend_db._engine
    saved_factory = backend_db._SessionLocal
    backend_db._engine = None
    backend_db._SessionLocal = None
    backend_db.init_db(path)
    # Reset in-process state between tests.
    lake_health._LAST_VECTOR_COUNT = None
    lake_health._BRONZE_WRITE_FAILURES.clear()
    try:
        yield backend_db
    finally:
        backend_db._engine = saved_engine
        backend_db._SessionLocal = saved_factory
        try:
            os.remove(path)
        except OSError:
            pass


def _stat_for(layer, last_mod_iso):
    return _FakeStat(last_modified=last_mod_iso, bytes_=1000, object_count=10)


def test_bronze_stale_alert_fires_then_resolves(isolated_db):
    stale_iso = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    fresh_iso = datetime.now(timezone.utc).isoformat()
    with patch("backend.bot.monitoring.lake_health.lake.stat_layer") as m:
        def _side(layer):
            if layer == "bronze":
                return _stat_for(layer, stale_iso)
            return _stat_for(layer, fresh_iso)
        m.side_effect = _side
        result = lake_health.run_health_check()
        kinds = [a["kind"] for a in result.fired]
        assert "bronze_stale" in kinds
        # Re-run with FRESH bronze.
        m.side_effect = lambda layer: _stat_for(layer, fresh_iso)
        result2 = lake_health.run_health_check()
        assert result2.fired == []
        assert len(result2.auto_resolved) >= 1


def test_first_pass_no_vector_shrink_alert(isolated_db):
    """The monitor needs a prior sample to detect a shrink."""
    with patch("backend.bot.monitoring.lake_health.lake.stat_layer") as m:
        fresh = datetime.now(timezone.utc).isoformat()
        m.side_effect = lambda layer: _stat_for(layer, fresh)
        with patch("backend.bot.ai.vector_store.namespace_stats",
                   return_value={"ns_a": {"count": 100}}):
            result = lake_health.run_health_check()
    kinds = [a["kind"] for a in result.fired]
    assert "vector_shrink" not in kinds


def test_vector_shrink_alert_on_drop(isolated_db):
    """A second pass where the vector total dropped must fire."""
    with patch("backend.bot.monitoring.lake_health.lake.stat_layer") as m:
        fresh = datetime.now(timezone.utc).isoformat()
        m.side_effect = lambda layer: _stat_for(layer, fresh)
        # First pass — establishes baseline of 100.
        with patch("backend.bot.ai.vector_store.namespace_stats",
                   return_value={"ns_a": {"count": 100}}):
            lake_health.run_health_check()
        # Second pass — total fell to 80.
        with patch("backend.bot.ai.vector_store.namespace_stats",
                   return_value={"ns_a": {"count": 80}}):
            result = lake_health.run_health_check()
    assert any(a["kind"] == "vector_shrink" for a in result.fired)


def test_write_failure_counter_24h_trim(isolated_db):
    """``record_bronze_failure`` increments a 24h-windowed counter."""
    # 30h-old entry should fall out of the window.
    lake_health._BRONZE_WRITE_FAILURES.append(
        datetime.now(timezone.utc) - timedelta(hours=30)
    )
    for _ in range(15):
        lake_health.record_bronze_failure()
    # Within the 24h window we have 15 fresh + 0 stale = 15.
    assert lake_health._failures_last_24h() == 15


def test_write_failures_alert_when_above_threshold(isolated_db):
    from backend.config import TUNABLES
    threshold = int(TUNABLES.lake_alert_write_failure_threshold)
    for _ in range(threshold + 2):
        lake_health.record_bronze_failure()
    with patch("backend.bot.monitoring.lake_health.lake.stat_layer") as m:
        fresh = datetime.now(timezone.utc).isoformat()
        m.side_effect = lambda layer: _stat_for(layer, fresh)
        result = lake_health.run_health_check()
    assert any(a["kind"] == "write_failures" for a in result.fired)
