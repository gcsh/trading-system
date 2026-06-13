"""MITS Phase 8.8 — lake_status FastAPI route shape tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app(temp_db):
    from backend.main import create_app
    return create_app()


def test_lake_status_returns_envelope(app):
    client = TestClient(app)
    with patch("backend.bot.data.lake.stat_layer") as m_stat, \
            patch("backend.bot.data.lake.list_gold_dates") as m_dates, \
            patch("backend.bot.ai.vector_store.namespace_stats") as m_ns:
        from backend.bot.data.lake import LakeLayerStat
        m_stat.return_value = LakeLayerStat(
            layer="bronze", bytes=1024, object_count=4,
            last_modified="2026-06-06T15:30:00",
        )
        m_dates.return_value = ["2026-06-04", "2026-06-05", "2026-06-06"]
        m_ns.return_value = {"regime_snapshots": {"count": 12,
                                                          "last_created_at": None}}
        resp = client.get("/lake/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bucket"]
    assert "layers" in body
    assert "vectors" in body
    assert "recent_snapshots" in body
    assert body["layers"]["bronze"]["bytes"] == 1024


def test_snapshot_now_requires_admin_secret(app, monkeypatch):
    from backend.config import TUNABLES
    monkeypatch.setattr(TUNABLES, "lake_admin_secret", "")
    client = TestClient(app)
    resp = client.post("/lake/snapshot/now")
    assert resp.status_code == 503  # endpoint disabled when secret unset


def test_restore_returns_ssm_command(app):
    client = TestClient(app)
    resp = client.post("/lake/restore?date=2026-06-06")
    assert resp.status_code == 200
    body = resp.json()
    assert "ssm_command" in body
    assert "restore_from_lake.py" in body["ssm_command"]
