"""Heatseeker (GEX) — computation, walls/flip, regime, cache, strategy wiring.
All mocked; no network."""
import backend.bot.signals.gex as gex


def _synthetic_chain(spot):
    """Heavy calls above spot, heavy puts below — a clean GEX shape."""
    rows = []
    for k in (spot - 10, spot - 5, spot, spot + 5, spot + 10):
        rows.append({"type": "C", "strike": float(k), "oi": 2000.0 if k >= spot else 200.0, "gamma": 0.02, "iv": 0.2, "expiry": "2026-06-19"})
        rows.append({"type": "P", "strike": float(k), "oi": 2000.0 if k <= spot else 200.0, "gamma": 0.02, "iv": 0.2, "expiry": "2026-06-19"})
    return rows


def _mock_chain(monkeypatch, spot=100.0, chain=None):
    monkeypatch.setattr(gex, "_spot", lambda t: spot)
    monkeypatch.setattr(gex, "_flashalpha", lambda t, s: None)
    monkeypatch.setattr(gex, "_cboe_chain", lambda t: None)
    monkeypatch.setattr(gex, "_yf_chain", lambda t, s: chain if chain is not None else _synthetic_chain(spot))
    gex._CACHE.clear()


def test_gex_computed_from_chain(monkeypatch):
    _mock_chain(monkeypatch, 100.0)
    g = gex.gex("TEST")
    assert g.ok is True and g.source == "yfinance"
    assert g.spot_price == 100.0
    assert len(g.gex_by_strike) == 5
    assert any(r["call_gex"] > 0 for r in g.gex_by_strike)
    assert any(r["put_gex"] < 0 for r in g.gex_by_strike)


def test_gex_aggregate_summary_fields(monkeypatch):
    _mock_chain(monkeypatch, 100.0)
    g = gex.gex("TEST")
    # Per-strike OI columns are present.
    assert all({"call_oi", "put_oi", "total_oi"} <= set(r) for r in g.gex_by_strike)
    # Totals reconcile with the per-strike rows.
    assert round(g.call_gex_total + g.put_gex_total, 2) == round(g.net_gex_total, 2)
    assert g.total_oi == g.total_call_oi + g.total_put_oi
    assert g.total_oi > 0
    # ATM IV (0.20) drives a positive 1-day expected move.
    assert g.atm_iv == 0.2
    assert g.expected_move is not None and g.expected_move > 0


def test_gamma_flip_correct(monkeypatch):
    _mock_chain(monkeypatch, 100.0)
    g = gex.gex("TEST")
    assert g.gamma_flip is not None
    assert 90.0 <= g.gamma_flip <= 110.0       # within the strike range
    assert g.call_wall >= 100.0 and g.put_wall <= 100.0


def test_dealer_regime_assigned(monkeypatch):
    _mock_chain(monkeypatch, 100.0)
    g = gex.gex("TEST")
    assert g.dealer_regime in ("long_gamma", "short_gamma")


def test_gex_cache_works(monkeypatch):
    calls = {"n": 0}

    def yf(t, s):
        calls["n"] += 1
        return _synthetic_chain(100.0)

    monkeypatch.setattr(gex, "_spot", lambda t: 100.0)
    monkeypatch.setattr(gex, "_flashalpha", lambda t, s: None)
    monkeypatch.setattr(gex, "_cboe_chain", lambda t: None)
    monkeypatch.setattr(gex, "_yf_chain", yf)
    gex._CACHE.clear()

    gex.gex("TEST")
    gex.gex("TEST")
    assert calls["n"] == 1   # second call served from the 60s cache


def test_strategy_gex_context():
    from backend.bot.strategies.all_strategies import IronCondor, ZeroDTEScalp

    # Iron condor would otherwise fire (high IV, ranging, no earnings) — short
    # gamma blocks it.
    ic = IronCondor().analyze("SPY", {"iv_rank": 60, "adx": 15, "earnings_days": 999, "price": 100, "dealer_regime": "short_gamma"})
    assert ic.action.name == "HOLD" and "short-gamma" in ic.reason

    # 0DTE would otherwise fire — long gamma blocks it.
    z = ZeroDTEScalp().analyze("SPY", {"time_of_day": "11:00", "vix": 15, "dealer_regime": "long_gamma",
                                       "momentum_5m": 1.0, "rsi_5m": 60, "price": 100})
    assert z.action.name == "HOLD" and "long-gamma" in z.reason


# ── #1 staleness guard ─────────────────────────────────────────────────────────

def test_staleness_guard():
    from datetime import datetime, timezone

    assert gex._is_stale("2020-01-01T00:00:00+00:00") is True
    assert gex._is_stale(datetime.now(timezone.utc).isoformat()) is False
    assert gex._is_stale("not-a-timestamp") is False   # never raises


def test_fresh_result_not_stale(monkeypatch):
    _mock_chain(monkeypatch, 100.0)
    g = gex.gex("TEST")
    assert g.stale is False


# ── #2 OPEX detection ──────────────────────────────────────────────────────────

def test_is_opex_day():
    from datetime import date, timedelta

    third_fri = gex._third_friday(2026, 5)        # 2026-05-15
    assert third_fri == date(2026, 5, 15)
    assert gex.is_opex_day(third_fri) is True      # any Friday is OPEX (weeklies)
    assert gex.is_opex_day(third_fri - timedelta(days=1)) is False   # Thursday


def test_is_opex_week():
    from datetime import timedelta

    third_fri = gex._third_friday(2026, 5)
    assert gex.is_opex_week(third_fri) is True
    assert gex.is_opex_week(third_fri - timedelta(days=3)) is True   # same week
    assert gex.is_opex_week(third_fri - timedelta(days=7)) is False  # week prior


# ── #4 wall / flip shift vs previous snapshot ──────────────────────────────────

def test_flip_direction_tracks_previous(monkeypatch):
    monkeypatch.setattr(gex.TUNABLES, "gex_cache_ttl", 0.0)   # force recompute
    spot = {"v": 100.0}
    monkeypatch.setattr(gex, "_spot", lambda t: spot["v"])
    monkeypatch.setattr(gex, "_flashalpha", lambda t, s: None)
    monkeypatch.setattr(gex, "_cboe_chain", lambda t: None)
    monkeypatch.setattr(gex, "_yf_chain", lambda t, s: _synthetic_chain(spot["v"]))
    gex._CACHE.clear()

    first = gex.gex("TEST")
    assert first.prev_gamma_flip is None and first.flip_direction is None

    spot["v"] = 120.0                       # shift the whole chain up
    second = gex.gex("TEST")
    assert second.prev_gamma_flip == first.gamma_flip
    assert second.prev_call_wall == first.call_wall
    assert second.flip_direction in ("up", "down", "flat")
    if second.gamma_flip > first.gamma_flip:
        assert second.flip_direction == "up"


# ── #8 regime-history persistence ──────────────────────────────────────────────

def test_regime_history_roundtrip(temp_db, monkeypatch):
    _mock_chain(monkeypatch, 100.0)
    stored = gex.store_regime_snapshot("TEST")
    assert stored is not None and stored["ticker"] == "TEST"

    hist = gex.regime_history("TEST")
    assert len(hist) == 1
    assert hist[0]["dealer_regime"] in ("long_gamma", "short_gamma")
    assert hist[0]["spot_price"] == 100.0
