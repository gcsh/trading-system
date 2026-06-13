"""MITS Phase 5 (P5.5) — catalyst gate tests."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from backend.bot.event_risk import CalendarEvent
from backend.bot.gates import catalyst_gate


def _fixed_now() -> datetime:
    # A Tuesday in mid-June, well before any FOMC date so the macro
    # calendar lookup returns rows that are days away by default.
    return datetime(2026, 6, 9, 14, 0, 0)


@pytest.fixture(autouse=True)
def _patch_macro(monkeypatch):
    # By default tests should NOT see any FOMC in the next 24h. Each
    # test that wants a FOMC injection overrides with monkeypatch.
    def _no_macro(year):
        return []
    monkeypatch.setattr(
        "backend.bot.event_risk._macro_events_for_year", _no_macro,
    )
    yield


def _earnings_in_n_days(n: int):
    target = _fixed_now() + timedelta(days=n)

    def _fn(ticker: str):
        return [CalendarEvent(
            name=f"{ticker.upper()} earnings", kind="earnings",
            when=target.isoformat(), impact="high",
            tickers_affected=[ticker.upper()],
        )]
    return _fn


def test_clean_pass_returns_no_multiplier(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.event_risk._earnings_event",
        lambda t: [],
    )
    res = catalyst_gate.check("AAPL", instrument="stock", now=_fixed_now())
    assert res.passes is True
    assert res.conviction_multiplier == 1.0
    assert res.reason is None
    assert res.triggers == []


def test_earnings_in_window_triggers_multiplier(monkeypatch):
    # Earnings tomorrow → within the 5-trading-day window → ×0.5.
    monkeypatch.setattr(
        "backend.bot.event_risk._earnings_event",
        _earnings_in_n_days(2),
    )
    res = catalyst_gate.check("TSLA", instrument="stock", now=_fixed_now())
    assert res.passes is True
    assert res.conviction_multiplier == pytest.approx(0.5)
    assert "earnings" in (res.reason or "").lower()
    assert any(t["kind"] == "earnings" for t in res.triggers)


def test_short_dte_option_into_earnings_abstains(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.event_risk._earnings_event",
        _earnings_in_n_days(3),
    )
    res = catalyst_gate.check(
        "NVDA", instrument="option", dte=5, now=_fixed_now(),
    )
    assert res.passes is False
    assert res.conviction_multiplier == 0.0
    assert "ABSTAIN" in (res.reason or "")


def test_long_dte_option_into_earnings_only_multiplier(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.event_risk._earnings_event",
        _earnings_in_n_days(3),
    )
    res = catalyst_gate.check(
        "NVDA", instrument="option", dte=30, now=_fixed_now(),
    )
    assert res.passes is True
    assert res.conviction_multiplier == pytest.approx(0.5)


def test_earnings_far_away_does_not_trigger(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.event_risk._earnings_event",
        _earnings_in_n_days(45),  # well outside the 5-day window
    )
    res = catalyst_gate.check("AAPL", instrument="stock", now=_fixed_now())
    assert res.passes is True
    assert res.conviction_multiplier == 1.0


def test_fomc_within_window_triggers_multiplier(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.event_risk._earnings_event",
        lambda t: [],
    )
    target = _fixed_now() + timedelta(hours=10)

    def _macro(year):
        if year != target.year:
            return []
        return [CalendarEvent(
            name="FOMC Statement", kind="macro",
            when=target.isoformat(), impact="high",
            tickers_affected=["all"],
        )]
    monkeypatch.setattr(
        "backend.bot.event_risk._macro_events_for_year", _macro,
    )
    res = catalyst_gate.check("SPY", instrument="stock", now=_fixed_now())
    assert res.passes is True
    assert res.conviction_multiplier == pytest.approx(0.5)
    assert "FOMC" in (res.reason or "")


def test_earnings_and_fomc_compound(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.event_risk._earnings_event",
        _earnings_in_n_days(2),
    )
    target = _fixed_now() + timedelta(hours=10)

    def _macro(year):
        if year != target.year:
            return []
        return [CalendarEvent(
            name="FOMC Statement", kind="macro",
            when=target.isoformat(), impact="high",
            tickers_affected=["all"],
        )]
    monkeypatch.setattr(
        "backend.bot.event_risk._macro_events_for_year", _macro,
    )
    res = catalyst_gate.check("TSLA", instrument="stock", now=_fixed_now())
    # 0.5 * 0.5 = 0.25
    assert res.passes is True
    assert res.conviction_multiplier == pytest.approx(0.25)


def test_to_dict_serializes_cleanly(monkeypatch):
    monkeypatch.setattr(
        "backend.bot.event_risk._earnings_event",
        _earnings_in_n_days(3),
    )
    res = catalyst_gate.check("AAPL", instrument="stock", now=_fixed_now())
    d = res.to_dict()
    assert set(d.keys()) == {
        "passes", "conviction_multiplier", "reason", "triggers",
    }
    assert isinstance(d["triggers"], list)
