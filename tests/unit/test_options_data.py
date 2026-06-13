"""Free options-data adapter: config-driven IV mapping + safe degradation."""
import backend.bot.data.options as o
from backend.config import TUNABLES


def test_iv_rank_estimate_is_config_driven():
    floor, rng = TUNABLES.iv_rank_iv_floor, TUNABLES.iv_rank_iv_range
    assert o._iv_rank_estimate(None) == 50          # no IV → neutral default
    assert o._iv_rank_estimate(floor) == 0          # at the floor → 0
    assert o._iv_rank_estimate(floor + rng) == 100  # top of the band → 100
    assert o._iv_rank_estimate(5.0) == 100          # clamped, never overflows
    mid = o._iv_rank_estimate(floor + rng / 2)
    assert 40 <= mid <= 60                           # midpoint maps near 50


def test_options_snapshot_degrades_safely(monkeypatch):
    # Every chain source fails / unavailable → must not raise, returns
    # safe stub. WARN.4 (2026-06-04) inserted Alpaca between thetadata
    # and yfinance; stub all four providers so we exercise the
    # all-providers-down path the test was written for.
    def boom(*a, **k):
        raise RuntimeError("feed down")

    monkeypatch.setattr(o, "_atm_from_thetadata", lambda *a, **k: None)
    try:
        from backend.bot.data import alpaca_options as _alp
        monkeypatch.setattr(_alp, "atm_from_alpaca", lambda *a, **k: None)
    except Exception:
        pass
    monkeypatch.setattr(o, "_atm_from_yfinance", boom)
    monkeypatch.setattr(o, "_atm_from_cboe", lambda *a, **k: None)
    monkeypatch.setattr(o, "_earnings", lambda t: (999, False))
    o._CACHE.clear()

    snap = o.options_snapshot("AAPL", 100.0)
    assert snap["has_options"] is False
    assert snap["iv_rank"] == 50
    assert "implied_move" in snap and snap["earnings_days"] == 999


def test_options_snapshot_uses_real_chain_when_available(monkeypatch):
    # Force the yfinance path by stubbing thetadata + alpaca + cboe to
    # None (otherwise the test hits a live provider and breaks determinism).
    monkeypatch.setattr(o, "_atm_from_thetadata", lambda t, s: None)
    try:
        from backend.bot.data import alpaca_options as _alp
        monkeypatch.setattr(_alp, "atm_from_alpaca", lambda *a, **k: None)
    except Exception:
        pass
    monkeypatch.setattr(o, "_atm_from_cboe", lambda t, s: None)
    monkeypatch.setattr(o, "_atm_from_yfinance",
                        lambda t, s: {"iv_atm": 0.48, "implied_move": 0.06, "dte": 30, "expiry": "2026-06-19", "source": "yfinance"})
    monkeypatch.setattr(o, "_earnings", lambda t: (5, False))
    # Force the linear-estimator path so the assertion against
    # ``_iv_rank_estimate`` is deterministic regardless of what's in
    # the iv_history table.
    monkeypatch.setattr(
        o, "_iv_rank_with_history",
        lambda ticker, iv: (o._iv_rank_estimate(iv), True),
    )
    o._CACHE.clear()

    snap = o.options_snapshot("TSLA", 440.0)
    assert snap["has_options"] is True
    assert snap["iv_atm"] == 0.48
    assert snap["options_source"] == "yfinance"
    assert snap["earnings_days"] == 5
    # iv_rank derived from the real IV via the config-driven mapping
    assert snap["iv_rank"] == o._iv_rank_estimate(0.48)
