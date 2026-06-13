"""MITS Phase 2 (P2.1) — intraday IV via ThetaData straddle inversion.

Locks the workaround contract:
  * Brenner-Subrahmanyam inversion produces the expected IV from a
    mocked straddle quote.
  * Cache: the second call for the same (ticker, timestamp) does not
    re-hit ThetaData.
  * Cache stores BOTH ok and non-ok statuses so failures aren't
    retried forever.
"""
import math
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


# ── helpers ────────────────────────────────────────────────────────────


class _FakeClient:
    """Minimal ThetaDataClient stand-in for compute_intraday_iv_at tests."""

    def __init__(self, *, expirations=None, strikes=None, call_row=None,
                 put_row=None):
        self._expirations = expirations or []
        self._strikes = strikes or []
        self._call_row = call_row
        self._put_row = put_row
        self.calls = []

    def list_expirations(self, ticker):
        self.calls.append(("expirations", ticker))
        return list(self._expirations)

    def list_strikes(self, ticker, expiration):
        self.calls.append(("strikes", ticker, expiration))
        return list(self._strikes)

    def _get_json(self, path, params):
        right = params.get("right", "")
        self.calls.append(("get_json", path, right))
        if right == "C":
            row = self._call_row
        elif right == "P":
            row = self._put_row
        else:
            row = None
        if row is None:
            return None
        return {"response": [{"data": [row]}]}


# Use a fresh in-memory DB for each test so cache rows don't bleed.
@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "intraday_iv_test.sqlite"
    from backend.db import init_db
    monkeypatch.setenv("DB_PATH", str(db_path))
    # Force re-init with the new path.
    import backend.db as _dbmod
    _dbmod._engine = None
    _dbmod._SessionLocal = None
    init_db(str(db_path))
    yield
    _dbmod._engine = None
    _dbmod._SessionLocal = None


# ── tests ──────────────────────────────────────────────────────────────


class TestIntradayIVInversion:
    def test_brenner_subrahmanyam_recovers_iv(self):
        from backend.bot.data.thetadata import compute_intraday_iv_at

        # Construct a synthetic straddle that should yield IV = 0.30 at
        # spot=100, strike=100, DTE=30.
        S = 100.0
        K = 100.0
        days_to_expiry = 30
        T = days_to_expiry / 365.0
        target_iv = 0.30
        k = math.sqrt(2.0 * math.pi) / 2.0
        straddle = target_iv * k * S * math.sqrt(T)
        # Split evenly between call and put (ATM symmetry).
        leg_mid = straddle / 2.0
        bid = leg_mid - 0.05
        ask = leg_mid + 0.05
        timestamp = datetime(2026, 1, 15, 14, 30)
        expiry = timestamp.date() + timedelta(days=days_to_expiry)
        client = _FakeClient(
            expirations=[expiry],
            strikes=[K],
            call_row={"bid": bid, "ask": ask,
                          "timestamp": timestamp.isoformat()},
            put_row={"bid": bid, "ask": ask,
                          "timestamp": timestamp.isoformat()},
        )
        iv = compute_intraday_iv_at(
            "FAKE", timestamp, spot=S,
            client=client,
        )
        assert iv is not None
        assert iv == pytest.approx(target_iv, rel=0.02)

    def test_cache_avoids_repeat_thetadata_calls(self):
        from backend.bot.data.thetadata import compute_intraday_iv_at

        S = 100.0
        K = 100.0
        target_iv = 0.40
        days_to_expiry = 30
        T = days_to_expiry / 365.0
        k = math.sqrt(2.0 * math.pi) / 2.0
        leg_mid = (target_iv * k * S * math.sqrt(T)) / 2.0
        timestamp = datetime(2026, 1, 16, 10, 0)
        expiry = timestamp.date() + timedelta(days=days_to_expiry)
        row = {"bid": leg_mid - 0.05, "ask": leg_mid + 0.05,
                  "timestamp": timestamp.isoformat()}
        client = _FakeClient(
            expirations=[expiry], strikes=[K],
            call_row=row, put_row=row,
        )
        first = compute_intraday_iv_at("FAKE", timestamp, spot=S, client=client)
        call_count_first = len(client.calls)
        # Second call should hit the cache instead of ThetaData.
        second = compute_intraday_iv_at("FAKE", timestamp, spot=S, client=client)
        assert first is not None
        assert first == pytest.approx(second, rel=1e-6)
        assert len(client.calls) == call_count_first, (
            "second call hit ThetaData instead of cache")

    def test_no_quote_caches_failure(self):
        from backend.bot.data.thetadata import compute_intraday_iv_at

        timestamp = datetime(2026, 1, 17, 11, 0)
        expiry = timestamp.date() + timedelta(days=30)
        # call_row=None forces the "no quote" path.
        client = _FakeClient(
            expirations=[expiry], strikes=[100.0],
            call_row=None, put_row=None,
        )
        first = compute_intraday_iv_at("FAKE", timestamp, spot=100.0,
                                                  client=client)
        n_first = len(client.calls)
        second = compute_intraday_iv_at("FAKE", timestamp, spot=100.0,
                                                   client=client)
        assert first is None
        assert second is None
        # Second call must hit cache (failure cached too).
        assert len(client.calls) == n_first

    def test_no_expiration_returns_none(self):
        from backend.bot.data.thetadata import compute_intraday_iv_at
        client = _FakeClient(expirations=[], strikes=[])
        iv = compute_intraday_iv_at("FAKE", datetime(2026, 1, 18, 10),
                                              spot=100.0, client=client)
        assert iv is None

    def test_oob_iv_does_not_persist_value(self):
        from backend.bot.data.thetadata import compute_intraday_iv_at

        # Synthesize a wildly large straddle → IV computes >5.0 → oob.
        timestamp = datetime(2026, 1, 19, 13, 30)
        expiry = timestamp.date() + timedelta(days=10)
        row = {"bid": 90.0, "ask": 90.5,
                  "timestamp": timestamp.isoformat()}
        client = _FakeClient(
            expirations=[expiry], strikes=[100.0],
            call_row=row, put_row=row,
        )
        iv = compute_intraday_iv_at("FAKE", timestamp, spot=100.0,
                                              client=client)
        assert iv is None
