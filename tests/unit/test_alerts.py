"""Alert center unit tests."""
from backend.bot.alerts import Alert, AlertCenter


def test_fire_appends_to_history():
    center = AlertCenter()
    center.fire(Alert(title="t", body="b", severity="info"))
    assert len(center.history) == 1
    assert center.recent()[0]["title"] == "t"


def test_fire_invokes_broadcaster():
    received = []
    center = AlertCenter(broadcaster=lambda payload: received.append(payload))
    center.fire(Alert(title="t", body="b"))
    assert received and received[0]["title"] == "t"


def test_fire_from_event_submitted_is_success():
    center = AlertCenter()
    alert = center.fire_from_event({
        "status": "submitted", "ticker": "AAPL", "action": "BUY_STOCK", "reason": "ok",
    })
    assert alert is not None
    assert alert.severity == "success"
    assert alert.category == "order"


def test_fire_from_event_rejected_is_warning():
    center = AlertCenter()
    alert = center.fire_from_event({
        "status": "rejected", "ticker": "AAPL", "action": "BUY_STOCK", "risk": "no buying power",
    })
    assert alert is not None
    assert alert.severity == "warning"


def test_fire_from_event_signal_only_is_info():
    center = AlertCenter()
    alert = center.fire_from_event({
        "status": "signal_only", "ticker": "AAPL", "action": "BUY_STOCK", "reason": "auto-exec off",
    })
    assert alert is not None
    assert alert.severity == "info"


def test_fire_from_event_unknown_status_returns_none():
    center = AlertCenter()
    alert = center.fire_from_event({"status": "hold", "ticker": "AAPL"})
    assert alert is None


def test_recent_respects_limit():
    center = AlertCenter()
    for i in range(20):
        center.fire(Alert(title=f"a{i}", body="b"))
    assert len(center.recent(limit=5)) == 5
