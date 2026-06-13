"""GET/PUT round-trip for the Telegram filter config route."""
import pytest
from fastapi.testclient import TestClient


pytestmark = [pytest.mark.unit]


@pytest.fixture()
def client(temp_db):
    from backend.main import create_app
    app = create_app()
    return TestClient(app)


def test_config_get_returns_defaults(client):
    r = client.get("/notifications/telegram/config")
    assert r.status_code == 200
    body = r.json()
    # Defaults from Tunables.
    assert body["min_severity"] == "info"
    assert body["rate_limit_per_window"] >= 1
    assert body["rate_limit_window_minutes"] >= 1
    assert body["category_deny_list"] == []


def test_config_put_round_trips(client):
    payload = {
        "min_severity": "warning",
        "category_deny_list": ["risk", "signal"],
        "rate_limit_per_window": 8,
        "rate_limit_window_minutes": 15,
        "quiet_hours_start": "21:00",
        "quiet_hours_end": "06:30",
        "quiet_hours_tz": "UTC",
    }
    r = client.put("/notifications/telegram/config", json=payload)
    assert r.status_code == 200
    saved = r.json()
    assert saved["min_severity"] == "warning"
    assert saved["category_deny_list"] == ["risk", "signal"]
    assert saved["rate_limit_per_window"] == 8

    # GET reflects the save.
    r2 = client.get("/notifications/telegram/config")
    assert r2.json()["min_severity"] == "warning"
    assert r2.json()["rate_limit_per_window"] == 8


def test_config_put_partial_payload_keeps_defaults(client):
    """Omitted keys fall back to defaults, not None."""
    r = client.put(
        "/notifications/telegram/config",
        json={"min_severity": "danger"},
    )
    assert r.status_code == 200
    saved = r.json()
    assert saved["min_severity"] == "danger"
    # Other fields still populated from defaults.
    assert saved["rate_limit_per_window"] >= 1
    assert saved["rate_limit_window_minutes"] >= 1
