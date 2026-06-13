"""Phase 2 — real option pricing tests (P2.2 / P2.3 / P2.4)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.bot.options import blackscholes as bs
from backend.bot.options.pricing import (
    OptionMark,
    price_at_entry,
    price_for_mark,
    ENTRY_MAX_AGE_SEC,
    MARK_MAX_AGE_SEC,
)


pytestmark = [pytest.mark.unit, pytest.mark.data_integrity]


class TestStubFallback:
    """When ThetaData + BS both fail, the stub still returns a valid
    OptionMark — never crashes the executor."""

    def test_stub_returns_paper_stub_source(self, monkeypatch):
        # Force the chain to fail by mocking the import lookup.
        from backend.bot.options import pricing as p
        monkeypatch.setattr(p, "_chain_mark", lambda *a, **kw: None)
        # No IV hint and no chain → stub falls through.
        result = price_at_entry(
            symbol="AAPL", spot=0.0, strike=100,
            expiration=str(date.today() + timedelta(days=30)),
            right="call",
        )
        assert result.source == "paper_stub"
        assert result.mid > 0

    def test_invalid_inputs_return_stub(self):
        result = price_at_entry(
            symbol="AAPL", spot=100, strike=0,
            expiration="bogus", right="call",
        )
        assert result.source == "paper_stub"


class TestBSFallback:
    """When ThetaData is unavailable but spot + IV hint are present,
    BS produces a real mark."""

    def test_bs_fallback_uses_iv_hint(self, monkeypatch):
        from backend.bot.options import pricing as p
        monkeypatch.setattr(p, "_chain_mark", lambda *a, **kw: None)
        result = price_at_entry(
            symbol="AAPL", spot=200, strike=200,
            expiration=str(date.today() + timedelta(days=30)),
            right="call",
            iv_hint=0.25,
        )
        assert result.source == "bs_fallback"
        # ATM 30-DTE with IV=0.25 should be a meaningful premium.
        assert 2.0 < result.mid < 10.0
        assert result.iv == pytest.approx(0.25, abs=1e-6)
        assert result.delta is not None
        assert result.gamma is not None


class TestChainPath:
    """When a fresh chain quote is available, it's preferred over BS."""

    def test_chain_mark_wins_when_fresh(self, monkeypatch):
        from backend.bot.options import pricing as p

        def fake_chain(symbol, expiration, strike, right):
            return OptionMark(
                bid=1.95, ask=2.05, mid=2.00,
                iv=None, delta=None, gamma=None, theta=None, vega=None,
                source="thetadata", age_seconds=10, underlying=None,
            )

        monkeypatch.setattr(p, "_chain_mark", fake_chain)
        result = price_at_entry(
            symbol="AAPL", spot=200, strike=200,
            expiration=str(date.today() + timedelta(days=30)),
            right="call",
        )
        assert result.source == "thetadata"
        assert result.mid == pytest.approx(2.00, abs=1e-6)
        # Greeks back-filled via BS using recovered IV.
        assert result.iv is not None
        assert result.iv > 0

    def test_stale_chain_falls_to_bs(self, monkeypatch):
        from backend.bot.options import pricing as p

        def stale_chain(symbol, expiration, strike, right):
            return OptionMark(
                bid=1.95, ask=2.05, mid=2.00,
                iv=None, delta=None, gamma=None, theta=None, vega=None,
                source="thetadata",
                age_seconds=ENTRY_MAX_AGE_SEC + 1,
                underlying=None,
            )

        monkeypatch.setattr(p, "_chain_mark", stale_chain)
        result = price_at_entry(
            symbol="AAPL", spot=200, strike=200,
            expiration=str(date.today() + timedelta(days=30)),
            right="call",
            iv_hint=0.25,
        )
        assert result.source == "bs_fallback"


class TestMarkLifecycle:
    """MTM repricing uses a looser staleness rule (≤ 600s) than entry."""

    def test_mark_accepts_quote_older_than_entry_threshold(self, monkeypatch):
        from backend.bot.options import pricing as p

        def chain(symbol, expiration, strike, right):
            return OptionMark(
                bid=1.95, ask=2.05, mid=2.00,
                iv=None, delta=None, gamma=None, theta=None, vega=None,
                source="thetadata",
                age_seconds=ENTRY_MAX_AGE_SEC + 1,
                underlying=None,
            )

        monkeypatch.setattr(p, "_chain_mark", chain)
        result = price_for_mark(
            symbol="AAPL", spot=200, strike=200,
            expiration=str(date.today() + timedelta(days=30)),
            right="call",
            stored_iv=0.25,
        )
        # At entry this same age would have failed over to BS; at mark
        # it's acceptable.
        assert result.source == "thetadata"

    def test_mark_falls_to_bs_with_stored_iv_when_chain_stale(self, monkeypatch):
        from backend.bot.options import pricing as p

        def very_stale(symbol, expiration, strike, right):
            return OptionMark(
                bid=1.95, ask=2.05, mid=2.00,
                iv=None, delta=None, gamma=None, theta=None, vega=None,
                source="thetadata",
                age_seconds=MARK_MAX_AGE_SEC + 1,
                underlying=None,
            )

        monkeypatch.setattr(p, "_chain_mark", very_stale)
        result = price_for_mark(
            symbol="AAPL", spot=200, strike=200,
            expiration=str(date.today() + timedelta(days=30)),
            right="call",
            stored_iv=0.25,
        )
        assert result.source == "bs_fallback"


class TestPaperPositionSchema:
    """P2.2 added entry_iv + greek columns. P1.11 invariant covers the
    other state models; here we verify PaperPosition specifically."""

    def test_paper_position_has_entry_greeks(self):
        from backend.models.paper import PaperPosition
        cols = {c.name for c in PaperPosition.__table__.columns}
        required = {
            "strike", "expiration", "option_type",
            "entry_bid", "entry_ask", "entry_mid",
            "entry_iv", "entry_delta", "entry_gamma",
            "entry_theta", "entry_vega",
            "entry_underlying", "pricing_source",
            "stored_iv", "stored_iv_at",
        }
        missing = required - cols
        assert not missing, (
            f"PaperPosition is missing P2.2 columns: {missing}. "
            f"MTM repricing won't have stored IV to fall back on."
        )


class TestIVRefreshPolicy:
    """P2.4 — IV is refreshed when a fresh chain quote is available,
    preserved when stale. Event-driven, no timer."""

    def test_fresh_chain_refreshes_iv(self, monkeypatch):
        """When ``price_for_mark`` returns a thetadata-source mark with
        a positive IV, the executor MUST update PaperPosition.stored_iv.
        We assert the pricing module emits an IV on chain marks."""
        from backend.bot.options import pricing as p

        def chain(symbol, expiration, strike, right):
            return OptionMark(
                bid=1.95, ask=2.05, mid=2.00,
                iv=None, delta=None, gamma=None, theta=None, vega=None,
                source="thetadata", age_seconds=10, underlying=None,
            )

        monkeypatch.setattr(p, "_chain_mark", chain)
        result = price_at_entry(
            symbol="AAPL", spot=200, strike=200,
            expiration=str(date.today() + timedelta(days=30)),
            right="call",
        )
        # Greeks are back-filled — IV is implied from chain mid.
        assert result.source == "thetadata"
        assert result.iv and result.iv > 0
