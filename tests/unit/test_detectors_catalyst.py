"""MITS Phase 12.E — Catalyst detector unit tests.

These detectors read from DB tables (news_articles, insider_trades,
fund_holdings). The tests run against an empty in-memory DB to verify
graceful-empty behaviour + schema validation. End-to-end fire-tests
happen in the integration suite where Phase 11 data is present.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.catalyst import (
    EarningsRevisionShiftDetector, InsiderClusterDetector,
    PEADDriftDetector, SmartMoneyInflowDetector,
    _classify_revision, build_catalyst_detectors,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _df(n=80, price=100.0):
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    closes = [price + i * 0.05 for i in range(n)]
    return pd.DataFrame({
        "open": closes, "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes], "close": closes,
        "volume": [1_000_000] * n,
    }, index=idx)


def test_catalyst_registry():
    dets = build_catalyst_detectors()
    assert len(dets) == 4
    names = {d.pattern for d in dets}
    assert names == {
        "pead_drift", "insider_cluster", "smart_money_inflow",
        "earnings_revision_shift",
    }
    for d in dets:
        assert d.family == "catalyst"
        # All catalyst detectors must cite an academic source in docstring.
        assert "Cited" in (d.description or "") or "JF" in (d.description or "") \
              or "JAR" in (d.description or "") or "RFS" in (d.description or "")  \
              or "TAR" in (d.description or "") or "NBER" in (d.description or "")


class TestPEADDrift:
    def test_empty_news_returns_no_obs(self):
        out = PEADDriftDetector().detect("AAPL", _df())
        assert out == []

    def test_handles_short_bars(self):
        assert PEADDriftDetector().detect("X", _df(5)) == []


class TestInsiderCluster:
    def test_empty_insider_table(self):
        out = InsiderClusterDetector().detect("AAPL", _df())
        assert out == []

    def test_handles_empty_bars(self):
        assert InsiderClusterDetector().detect("X", None) == []


class TestSmartMoneyInflow:
    def test_empty_fund_table(self):
        out = SmartMoneyInflowDetector().detect("AAPL", _df())
        assert out == []


class TestEarningsRevisionShift:
    def test_empty_news_table(self):
        out = EarningsRevisionShiftDetector().detect("AAPL", _df())
        assert out == []

    def test_classify_raise(self):
        assert _classify_revision("Acme Corp raises guidance") == "raise"
        assert _classify_revision("Acme Corp boosts guidance") == "raise"

    def test_classify_cut(self):
        assert _classify_revision("Acme Corp cuts guidance") == "cut"
        assert _classify_revision("Acme Corp downgraded by analyst") == "cut"

    def test_classify_neutral(self):
        assert _classify_revision("Acme Corp announces conference") is None
