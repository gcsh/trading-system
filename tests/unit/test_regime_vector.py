"""MITS Phase 15.A — RegimeVector consolidation tests."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from backend.bot.regime import vector as rv_mod
from backend.bot.regime.vector import (
    RegimeDimension,
    RegimeVector,
    build_regime_vector,
)


@dataclass
class _FakeIntradayState:
    state: str = "trending_up"


class _FakeIntradayClassifier:
    """Mimics the surface that ``build_regime_vector`` reads off the live
    ``IntradayRegimeClassifier`` — just ``_cache`` + ``_cache_at``."""

    def __init__(self, state: str = "trending_up", age_sec: float = 5.0) -> None:
        self._cache = _FakeIntradayState(state=state)
        self._cache_at = time.time() - age_sec


class _MissingCacheClassifier:
    """Has no ``_cache`` attribute at all — should degrade to yellow."""


def _aapl_snapshot() -> dict:
    return {
        "price": 195.10,
        "ma50": 192.0,
        "ma200": 180.0,
        "iv_rank": 42.0,
        "vix": 14.5,
        "features": {
            "iv_rank": 42.0,
            "dealer_regime": "long_gamma",
            "dominant_wall": "call",
            "pinning_probability": 0.35,
        },
    }


@pytest.fixture(autouse=True)
def _isolate_iv_cache(monkeypatch):
    """Each test gets its own iv_regime cache + macro-row stub."""
    from backend.bot.iv_regime import _CACHE
    _CACHE.clear()

    # Stub out the iv_regime DB query so we don't touch the live DB.
    from backend.bot import iv_regime as _ivmod
    from backend.bot.iv_regime import IVRegimeReport

    def _fake_classify(ticker: str, *, force: bool = False) -> IVRegimeReport:
        report = IVRegimeReport(
            ticker=ticker.upper(), regime="mean_reverting",
            confidence=0.85, sample_count=120, current_iv=0.22,
            mean_iv=0.21, std_iv=0.03, slope=0.0001,
            autocorr_lag1=0.15, recent_std=0.03, trailing_std=0.03,
            note="stubbed",
        )
        _ivmod._CACHE[ticker.upper()] = (time.monotonic(), report)
        return report
    monkeypatch.setattr(_ivmod, "classify_ticker", _fake_classify)

    # Stub macro DB lookup so it returns a deterministic recent row.
    from datetime import datetime, timedelta

    class _FakeMacroRow:
        def __init__(self):
            self.timestamp = datetime.utcnow() - timedelta(seconds=120)

        def to_dict(self):
            return {"features": {"regime": "risk_on"}}

    class _FakeScalar:
        def __init__(self, row):
            self._row = row

        def scalar_one_or_none(self):
            return self._row

    class _FakeSession:
        def __init__(self, row):
            self._row = row

        def execute(self, *_a, **_kw):
            return _FakeScalar(self._row)

    class _FakeCtx:
        def __init__(self, row):
            self._row = row

        def __enter__(self):
            return _FakeSession(self._row)

        def __exit__(self, *exc):
            return False

    def _fake_session_scope():
        return _FakeCtx(_FakeMacroRow())

    monkeypatch.setattr(rv_mod, "session_scope", _fake_session_scope)
    yield


def test_builder_returns_all_seven_dimensions():
    snap = _aapl_snapshot()
    rv = build_regime_vector(
        ticker="AAPL", snapshot=snap,
        intraday_classifier=_FakeIntradayClassifier(),
    )
    assert isinstance(rv, RegimeVector)
    assert rv.ticker == "AAPL"
    for dim in (rv.trend, rv.volatility_state, rv.iv_rank, rv.iv_regime,
                  rv.intraday_regime, rv.gamma_state, rv.macro_regime):
        assert isinstance(dim, RegimeDimension)
        assert dim.source
        assert dim.health in ("green", "yellow", "red")

    d = rv.to_dict()
    for key in ("trend", "volatility_state", "iv_rank", "iv_regime",
                "intraday_regime", "gamma_state", "macro_regime",
                "ticker", "as_of", "health"):
        assert key in d
    # Values from the synthetic snapshot.
    assert rv.trend.value == "bullish"
    assert rv.iv_rank.value == 42.0
    assert rv.iv_regime.value == "mean_reverting"
    assert rv.intraday_regime.value == "trending_up"
    assert rv.macro_regime.value == "risk_on"
    assert rv.gamma_state.value["regime"] == "long_gamma"
    assert rv.gamma_state.value["dominant_wall"] == "call"
    assert rv.gamma_state.value["pinning_probability"] == 0.35


def test_missing_intraday_cache_degrades_to_yellow():
    snap = _aapl_snapshot()
    rv = build_regime_vector(
        ticker="AAPL", snapshot=snap,
        intraday_classifier=_MissingCacheClassifier(),
    )
    assert rv.intraday_regime.value == "unknown"
    assert rv.intraday_regime.health == "yellow"
    # One yellow dim alone should not produce a red composite.
    assert rv.health in ("yellow", "green")


def test_intraday_classifier_none_yields_yellow_dim():
    rv = build_regime_vector(
        ticker="AAPL", snapshot=_aapl_snapshot(),
        intraday_classifier=None,
    )
    assert rv.intraday_regime.health == "yellow"
    assert rv.intraday_regime.value == "unknown"


def test_composite_health_red_when_two_dims_stale(monkeypatch):
    """Force ≥2 dim freshness above the red age threshold → composite red."""
    from backend.config import TUNABLES
    monkeypatch.setattr(TUNABLES, "regime_vector_red_age_sec", 60)
    monkeypatch.setattr(TUNABLES, "regime_vector_yellow_age_sec", 10)

    snap = _aapl_snapshot()
    # Intraday cache aged WAY past the red threshold.
    stale_intraday = _FakeIntradayClassifier(age_sec=3600)

    # Force the iv_rank dim's freshness past red by aging the iv cache.
    from backend.bot.iv_regime import _CACHE, IVRegimeReport
    _CACHE["AAPL"] = (
        time.monotonic() - 7200.0,
        IVRegimeReport(
            ticker="AAPL", regime="mean_reverting", confidence=0.8,
            sample_count=100,
        ),
    )

    rv = build_regime_vector(
        ticker="AAPL", snapshot=snap,
        intraday_classifier=stale_intraday,
    )
    # Either two stale dims or any single dim past red age forces red.
    assert rv.health == "red"


def test_per_dim_override_macro_event_only_stays_green():
    """Phase 15.A.1: macro source has a 14d red / 7d yellow override.
    A 6-day-old macro observation should remain green."""
    dim = RegimeDimension(
        value="risk_on",
        freshness_seconds=6 * 86400,  # 6 days
        source="macro",
        health="green",
    )
    rv_mod._apply_freshness_health(dim)
    assert dim.health == "green"


def test_per_dim_override_intraday_yellow_band():
    """Phase 15.A.1: intraday override is 300s red / 60s yellow.
    A 120s-old intraday dim should be yellow."""
    dim = RegimeDimension(
        value="trending_up",
        freshness_seconds=120.0,
        source="intraday",
        health="green",
    )
    rv_mod._apply_freshness_health(dim)
    assert dim.health == "yellow"


def test_no_override_falls_through_to_global_red(monkeypatch):
    """Phase 15.A.1: a dim with no override (source='regime') aged past
    the global red threshold should flip to red."""
    from backend.config import TUNABLES
    monkeypatch.setattr(TUNABLES, "regime_vector_red_age_sec", 3600)
    monkeypatch.setattr(TUNABLES, "regime_vector_yellow_age_sec", 600)
    dim = RegimeDimension(
        value="bullish",
        freshness_seconds=3700.0,
        source="regime",
        health="green",
    )
    rv_mod._apply_freshness_health(dim)
    assert dim.health == "red"


def test_summary_text_contains_all_dims():
    rv = build_regime_vector(
        ticker="AAPL", snapshot=_aapl_snapshot(),
        intraday_classifier=_FakeIntradayClassifier(),
    )
    s = rv.summary_text()
    assert isinstance(s, str) and s
    # All six dimensional axes named in the summary.
    for token in ("trend=", "vol=", "iv_rank=", "iv_regime=",
                    "intraday=", "gamma=", "macro=", "health="):
        assert token in s
    # No accidental newlines — must be paste-into-prompt friendly.
    assert "\n" not in s
