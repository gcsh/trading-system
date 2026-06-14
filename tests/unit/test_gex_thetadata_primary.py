"""GEX-A (Phase A) — ThetaData as primary chain source.

Verifies:
  * `_thetadata_chain` converts the ThetaData client's per-strike NBBO +
    OI into the pipeline-shape rows expected by `_normalize`.
  * `gex()` prefers ThetaData when the terminal is reachable and only
    falls through to cboe/yfinance when ThetaData returns None.
  * Freshness fields (`chain_source`, `chain_max_age_seconds`,
    `chain_freshness`) are stamped on the GEXResult.
  * `_classify_freshness` correctly buckets ages.

All mocked — no network, no terminal dependency.
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import backend.bot.signals.gex as gex_mod
from backend.bot.signals.gex import (
    _classify_freshness,
    _thetadata_chain,
    gex,
)


def _quote(strike, right, *, bid=1.0, ask=1.05, ts=None):
    """Minimal OptionQuote stand-in. Only the fields the conversion
    uses (bid, ask, strike, right, timestamp, mid). Mid is a property
    so we replicate via SimpleNamespace + an explicit __dict__."""
    q = SimpleNamespace(
        strike=float(strike),
        right=right,
        bid=float(bid),
        ask=float(ask),
        bid_size=10,
        ask_size=10,
        timestamp=ts,
    )
    # `_thetadata_chain` calls q.mid (which is a @property on real
    # OptionQuote). Mirror that here.
    q.mid = (bid + ask) / 2.0
    return q


class _StubClient:
    def __init__(self, expirations, chain_by_exp, oi_by_exp):
        self._expirations = expirations
        self._chain_by_exp = chain_by_exp
        self._oi_by_exp = oi_by_exp

    def list_expirations(self, symbol):
        return self._expirations

    def chain_snapshot(self, symbol, expiration):
        return self._chain_by_exp.get(expiration, [])

    def chain_open_interest(self, symbol, expiration):
        return self._oi_by_exp.get(expiration, [])


def _build_stub(spot=100.0, *, age_sec=15.0):
    """Realistic-ish chain: 5 strikes around spot, both rights, fresh
    quotes ~`age_sec` seconds old. Mids respect intrinsic value so the
    IV solver succeeds for every strike (otherwise deep-ITM rows would
    fail to bracket their implied vol — that's the *correct* engine
    behavior but it makes test-counting noisy)."""
    expiry = date.today() + timedelta(days=21)
    now = gex_mod._now_et_naive()
    ts = now - timedelta(seconds=age_sec)
    chain = []
    ois = []
    for k in (spot - 10, spot - 5, spot, spot + 5, spot + 10):
        # Mid = intrinsic + 1.50 time value on each side. Ensures every
        # leg solves to a sensible IV in [0.05, 1.0].
        call_intrinsic = max(0.0, spot - k)
        put_intrinsic = max(0.0, k - spot)
        call_mid = call_intrinsic + 1.50
        put_mid = put_intrinsic + 1.50
        chain.append(_quote(k, "CALL",
                                  bid=call_mid - 0.10, ask=call_mid + 0.10, ts=ts))
        chain.append(_quote(k, "PUT",
                                  bid=put_mid - 0.10,  ask=put_mid + 0.10,  ts=ts))
        ois.append(SimpleNamespace(
            strike=float(k), right="CALL",
            open_interest=1500, expiration=expiry, timestamp=ts,
        ))
        ois.append(SimpleNamespace(
            strike=float(k), right="PUT",
            open_interest=1500, expiration=expiry, timestamp=ts,
        ))
    client = _StubClient(
        expirations=[expiry],
        chain_by_exp={expiry: chain},
        oi_by_exp={expiry: ois},
    )
    return client, expiry


def test_thetadata_chain_converts_to_pipeline_shape(monkeypatch):
    client, expiry = _build_stub()
    monkeypatch.setattr(
        "backend.bot.data.thetadata.get_client", lambda: client,
    )
    result = _thetadata_chain("TEST", 100.0, max_dte=45)
    assert result is not None
    rows, max_age = result
    # 5 strikes × 2 rights, all should survive the IV solve.
    assert len(rows) == 10
    sample = rows[0]
    assert set(sample) == {"type", "strike", "oi", "gamma", "iv", "expiry"}
    assert sample["type"] in ("C", "P")
    assert sample["expiry"] == expiry.isoformat()
    assert sample["oi"] == 1500
    assert 0 < sample["iv"] < 5.0
    assert sample["gamma"] > 0
    # 15s old quotes → max_age around 15s.
    assert 5.0 <= max_age <= 60.0


def test_thetadata_chain_returns_none_on_no_expirations(monkeypatch):
    client = _StubClient(expirations=[], chain_by_exp={}, oi_by_exp={})
    monkeypatch.setattr(
        "backend.bot.data.thetadata.get_client", lambda: client,
    )
    assert _thetadata_chain("TEST", 100.0) is None


def test_thetadata_chain_skips_when_terminal_unreachable(monkeypatch):
    def _raise():
        raise RuntimeError("terminal down")
    monkeypatch.setattr(
        "backend.bot.data.thetadata.get_client", _raise,
    )
    # Should swallow and return None so the caller can fall back.
    assert _thetadata_chain("TEST", 100.0) is None


def test_gex_prefers_thetadata_over_yfinance(monkeypatch):
    client, _ = _build_stub()
    monkeypatch.setattr(
        "backend.bot.data.thetadata.get_client", lambda: client,
    )
    monkeypatch.setattr(gex_mod, "_spot", lambda t: 100.0)
    monkeypatch.setattr(gex_mod, "_flashalpha", lambda t, s: None)
    # yfinance path would also return data — proving ThetaData wins.
    monkeypatch.setattr(gex_mod, "_yf_chain",
                            lambda t, s: [{"type": "C", "strike": 100.0,
                                            "oi": 999, "gamma": 0.99,
                                            "iv": 0.99, "expiry": "2030-12-31"}])
    monkeypatch.setattr(gex_mod, "_cboe_chain", lambda t: None)
    gex_mod._CACHE.clear()

    g = gex("TEST")
    assert g.ok is True
    assert g.source == "thetadata"
    assert g.chain_source == "thetadata"
    assert g.chain_freshness in ("fresh", "warm")
    assert g.chain_max_age_seconds is not None
    assert g.chain_max_age_seconds < 120


def test_gex_falls_through_when_thetadata_unavailable(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.data.thetadata.get_client",
        lambda: (_ for _ in ()).throw(RuntimeError("no terminal")),
    )
    monkeypatch.setattr(gex_mod, "_spot", lambda t: 100.0)
    monkeypatch.setattr(gex_mod, "_flashalpha", lambda t, s: None)
    monkeypatch.setattr(gex_mod, "_cboe_chain", lambda t: None)
    monkeypatch.setattr(gex_mod, "_yf_chain", lambda t, s: [
        {"type": "C", "strike": 100.0, "oi": 1000.0,
         "gamma": 0.02, "iv": 0.2, "expiry": "2026-06-19"},
        {"type": "P", "strike": 100.0, "oi": 1000.0,
         "gamma": 0.02, "iv": 0.2, "expiry": "2026-06-19"},
        {"type": "C", "strike": 105.0, "oi": 1000.0,
         "gamma": 0.02, "iv": 0.2, "expiry": "2026-06-19"},
        {"type": "P", "strike": 105.0, "oi": 1000.0,
         "gamma": 0.02, "iv": 0.2, "expiry": "2026-06-19"},
        {"type": "C", "strike": 95.0, "oi": 1000.0,
         "gamma": 0.02, "iv": 0.2, "expiry": "2026-06-19"},
        {"type": "P", "strike": 95.0, "oi": 1000.0,
         "gamma": 0.02, "iv": 0.2, "expiry": "2026-06-19"},
    ])
    gex_mod._CACHE.clear()

    g = gex("TEST")
    assert g.ok is True
    assert g.source == "yfinance"
    assert g.chain_source == "yfinance"
    # yfinance has no per-quote timestamp → unknown bucket.
    assert g.chain_freshness == "unknown"
    assert g.chain_max_age_seconds is None


@pytest.mark.parametrize("age,expected", [
    (5.0, "fresh"),
    (60.0, "fresh"),
    (120.0, "warm"),
    (300.0, "warm"),
    # During off-hours, 6-min stale is still "warm" (acceptable).
    # During RTH, the freshness gate makes it "stale" — we test the
    # off-hours path here so this is deterministic.
])
def test_classify_freshness_thetadata(age, expected, monkeypatch):
    monkeypatch.setattr(
        "backend.bot.calendar.is_us_market_open", lambda: False,
    )
    assert _classify_freshness(age, "thetadata") == expected


def test_classify_freshness_marks_other_sources_unknown():
    assert _classify_freshness(5.0, "yfinance") == "unknown"
    assert _classify_freshness(5.0, "cboe") == "unknown"
    assert _classify_freshness(5.0, "none") == "unknown"


def test_classify_freshness_stale_during_rth(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.calendar.is_us_market_open", lambda: True,
    )
    # 10 min old during market hours → stale.
    assert _classify_freshness(600.0, "thetadata") == "stale"


def test_classify_freshness_multi_hour_always_stale(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.calendar.is_us_market_open", lambda: False,
    )
    # > 6h old is stale even off-hours.
    assert _classify_freshness(8 * 3600.0, "thetadata") == "stale"
