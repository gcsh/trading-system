"""Flowseeker — urgency scoring + strategy confidence boost. Mocked, no network."""
import backend.bot.signals.flow as flow


def test_flow_urgency_score_range():
    cases = [(0, 0, 1), (50_000, 300, 50), (1_000_000, 5000, 100), (1e9, 1e6, 1)]
    for premium, vol, oi in cases:
        s = flow._urgency(premium, vol, oi)
        assert 0.0 <= s <= 1.0, (premium, vol, oi, s)
    # bigger premium / volume → higher urgency
    assert flow._urgency(1_000_000, 5000, 100) > flow._urgency(60_000, 200, 100)


def test_flow_confidence_boost():
    from backend.bot.strategies.all_strategies import NewsCatalystMomentum

    base = {"news_score": 0.8, "news_age_hours": 1, "price": 100}
    without = NewsCatalystMomentum().analyze("AAPL", base)
    boosted = NewsCatalystMomentum().analyze("AAPL", {**base, "bullish_sweeps": 3})
    assert boosted.confidence > without.confidence
    assert "sweeps" in boosted.reason


# ── #6 session tagging ─────────────────────────────────────────────────────────

def test_session_tagging():
    from datetime import datetime

    try:
        from zoneinfo import ZoneInfo
    except Exception:
        return
    et = ZoneInfo("America/New_York")
    assert flow._session_for(datetime(2026, 5, 15, 9, 0, tzinfo=et).isoformat()) == "pre_market"
    assert flow._session_for(datetime(2026, 5, 15, 10, 0, tzinfo=et).isoformat()) == "regular"
    assert flow._session_for(datetime(2026, 5, 15, 17, 0, tzinfo=et).isoformat()) == "after_hours"
    assert flow._session_for("garbage") == "regular"   # never raises


# ── #5 conviction window ───────────────────────────────────────────────────────

def test_conviction_window_filters_old_sweeps(monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fresh = flow.FlowAlert("AAPL", now.isoformat(), 200, "2026-06-19", 1e5,
                           "call", "sweep", "bullish", 10, 0.9, session="regular")
    old = flow.FlowAlert("AAPL", (now - timedelta(minutes=45)).isoformat(), 205, "2026-06-19",
                         1e5, "call", "sweep", "bullish", 10, 0.9, session="regular")
    monkeypatch.setattr(flow, "flow_for", lambda t: [fresh, old])
    monkeypatch.setattr(flow, "_darkpool_confirms", lambda t, b: False)

    ctx = flow.flow_context("AAPL")
    assert ctx["bullish_sweeps"] == 1            # only the in-window sweep counts
    assert ctx["flow_count"] == 2


def test_premarket_sweeps_surfaced(monkeypatch):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    pm = flow.FlowAlert("AAPL", now.isoformat(), 200, "2026-06-19", 1e5,
                        "call", "sweep", "bullish", 10, 0.9, session="pre_market")
    monkeypatch.setattr(flow, "flow_for", lambda t: [pm])
    monkeypatch.setattr(flow, "_darkpool_confirms", lambda t, b: False)

    ctx = flow.flow_context("AAPL")
    assert ctx["premarket_bullish_sweeps"] == 1


# ── #7 dark-pool cross-reference ───────────────────────────────────────────────

def test_darkpool_confirms_requires_big_print_and_sweep(monkeypatch):
    big = [{"ticker": "AAPL", "premium": 2_000_000.0}]
    small = [{"ticker": "AAPL", "premium": 100_000.0}]
    monkeypatch.setattr(flow, "_recent_darkpool", lambda: big)
    assert flow._darkpool_confirms("AAPL", True) is True
    assert flow._darkpool_confirms("AAPL", False) is False     # needs a sweep too
    monkeypatch.setattr(flow, "_recent_darkpool", lambda: small)
    assert flow._darkpool_confirms("AAPL", True) is False       # print too small


def test_darkpool_boost_beats_plain_sweep_boost():
    from backend.bot.strategies.all_strategies import NewsCatalystMomentum

    # news_score 0.70 keeps the plain-sweep result below the 0.95 cap so the
    # larger dark-pool boost is observable rather than both saturating.
    base = {"news_score": 0.70, "news_age_hours": 1, "price": 100, "bullish_sweeps": 3}
    plain = NewsCatalystMomentum().analyze("AAPL", base)
    confirmed = NewsCatalystMomentum().analyze("AAPL", {**base, "darkpool_confirms": True})
    assert confirmed.confidence > plain.confidence
    assert "dark-pool" in confirmed.reason


# ── #3 alert dedup persisted to SQLite ─────────────────────────────────────────

def test_flow_for_never_raises_on_source_error(monkeypatch):
    # Regression: a yfinance auth/crumb failure must degrade to empty flow, not
    # propagate (which previously 500'd /flow/live during load).
    monkeypatch.setattr(flow, "_uw_flow", lambda t: None)

    def boom(t):
        raise RuntimeError("yfinance HTTP 401: Invalid Crumb")

    monkeypatch.setattr(flow, "_yf_unusual", boom)
    flow._CACHE.clear()
    assert flow.flow_for("SPY") == []
    # live_flow aggregates flow_for and must also stay empty/safe.
    assert flow.live_flow(["SPY", "QQQ"]) == []


def test_alert_id_stable():
    a = flow.FlowAlert("SPY", "2026-05-15T14:00:00+00:00", 500, "2026-06-19", 1e5,
                       "call", "sweep", "bullish", 10, 0.9)
    assert a.alert_id() == a.alert_id() and len(a.alert_id()) == 16


def test_filter_unseen_dedup(temp_db):
    alerts = [
        flow.FlowAlert("SPY", "2026-05-15T14:00:00+00:00", 500, "2026-06-19", 1e5,
                       "call", "sweep", "bullish", 10, 0.9),
        flow.FlowAlert("QQQ", "2026-05-15T14:01:00+00:00", 400, "2026-06-19", 1e5,
                       "put", "sweep", "bearish", 10, 0.8),
    ]
    first = flow.filter_unseen(alerts)
    assert len(first) == 2                 # all new on first push
    second = flow.filter_unseen(alerts)
    assert second == []                    # already seen → not replayed
