"""MITS Phase 9 — integration test for /theories endpoints."""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import create_app


def _fake_bars(n=80):
    out = []
    from datetime import datetime, timedelta
    base = datetime(2024, 1, 1)
    for i in range(n):
        c = 100 + i * 0.5
        out.append({
            "t": (base + timedelta(days=i)).isoformat(),
            "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 100_000,
        })
    return out


def test_get_theories_registry_lists_five_theories():
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/theories")
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["theories"]}
        assert names == {"price_action", "gann", "fibonacci", "ichimoku", "pivots"}


def test_get_theory_gann_returns_bars_and_annotation():
    app = create_app()
    bars = _fake_bars(120)
    with patch("backend.api.routes.theories.fetch_bars",
               return_value={"bars": bars, "source": "test", "interval": "1d", "window": "all"}):
        with TestClient(app) as client:
            r = client.get("/theories/gann/AAPL?window=1y")
            assert r.status_code == 200
            j = r.json()
            assert j["theory"] == "gann"
            assert len(j["bars"]) == 120
            assert "annotation" in j
            assert "lines" in j["annotation"]
            assert j["annotation"]["citation"]


def test_post_save_then_get_returns_saved_overlay():
    app = create_app()
    bars = _fake_bars(60)
    with patch("backend.api.routes.theories.fetch_bars",
               return_value={"bars": bars, "source": "test", "interval": "1d", "window": "all"}):
        with TestClient(app) as client:
            # First fetch the auto annotation.
            r = client.get("/theories/fibonacci/TSLA?window=1y")
            assert r.status_code == 200
            ann = r.json()["annotation"]
            # Save it.
            save = client.post(
                "/theories/fibonacci/TSLA/save?window=1y",
                json={"annotation": ann, "created_by": "test"},
            )
            assert save.status_code == 200
            assert save.json()["ok"] is True
            # Refetch — saved row is present.
            r2 = client.get("/theories/fibonacci/TSLA?window=1y")
            assert r2.json()["saved"] is not None
            # Delete the saved overlay.
            d = client.delete("/theories/fibonacci/TSLA/saved?window=1y")
            assert d.json()["ok"] is True
            r3 = client.get("/theories/fibonacci/TSLA?window=1y")
            assert r3.json()["saved"] is None
