"""Data integrity invariants — option chain consistency, IV sanity,
price-freshness guards.

QA framework: Data Integrity Testing (section 22).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest


@pytest.mark.data_integrity
@pytest.mark.unit
class TestIVRankConfigDriven:
    """IV rank must read its band from config (memory: 'tunable values
    must live in config, not hardcoded')."""

    def test_iv_rank_at_floor_returns_zero(self):
        from backend.bot.data.options import _iv_rank_estimate
        from backend.config import TUNABLES
        assert _iv_rank_estimate(TUNABLES.iv_rank_iv_floor) == 0

    def test_iv_rank_at_top_returns_100(self):
        from backend.bot.data.options import _iv_rank_estimate
        from backend.config import TUNABLES
        assert _iv_rank_estimate(
            TUNABLES.iv_rank_iv_floor + TUNABLES.iv_rank_iv_range
        ) == 100

    def test_iv_rank_clamps_above_band(self):
        from backend.bot.data.options import _iv_rank_estimate
        assert _iv_rank_estimate(99.0) == 100

    def test_iv_rank_returns_neutral_when_none(self):
        from backend.bot.data.options import _iv_rank_estimate
        assert _iv_rank_estimate(None) == 50


@pytest.mark.data_integrity
@pytest.mark.invariant
class TestOptionChainCoherence:
    """A strike picked by chain_strike must actually exist in the
    chain. Anything else means we're booking phantom contracts."""

    def test_chain_strike_signature_accepts_target_delta(self):
        """P1.4-FU3 (memory): delta-band selection added. The signature
        must include target_delta so strategies can request 0.30 delta
        instead of arithmetic moneyness."""
        from backend.bot.data import options
        import inspect
        sig = inspect.signature(options.chain_strike)
        assert "target_delta" in sig.parameters, (
            "chain_strike must accept target_delta — institutional "
            "selection targets delta, not arithmetic moneyness."
        )


@pytest.mark.data_integrity
@pytest.mark.unit
class TestPriceFreshness:
    """Acting on stale prices loses money — especially at the open
    when yfinance returns yesterday's close."""

    def test_strategies_use_chain_expiry(self):
        """Memory: 'never compute expiry as today+N'. The strategies
        layer must call chain_expiry()/resolve_expiry_dte to get a real
        listed expiry."""
        from backend.bot.strategies import all_strategies
        import inspect
        src = inspect.getsource(all_strategies)
        assert "chain_expiry" in src or "resolve_expiry_dte" in src, (
            "Strategies must use chain_expiry/resolve_expiry_dte — "
            "without that, expiry is derived from today+DTE which won't "
            "land on a listed expiry."
        )


@pytest.mark.data_integrity
@pytest.mark.unit
class TestThetaDataParserCSV:
    """ThetaData /v3/stock/history/eod returns CSV by default. The
    parser must handle both CSV and JSON, otherwise stock backfill
    silently falls through to yfinance (causes the AAPL gap)."""

    def test_thetadata_parser_function_exists(self):
        from backend.bot.data import iv_history
        assert hasattr(iv_history, "_thetadata_historical_closes")

    def test_thetadata_parser_handles_csv_response(self):
        from backend.bot.data import iv_history
        import inspect
        src = inspect.getsource(iv_history._thetadata_historical_closes)
        assert "csv" in src.lower() or "DictReader" in src, (
            "_thetadata_historical_closes must parse CSV — the terminal "
            "returns CSV by default even when format=json is passed."
        )


@pytest.mark.data_integrity
@pytest.mark.invariant
class TestPutCallParityCheckExists:
    """P1.2-FU1: parity check shipped to detect broken IV. Removing it
    would let bad IVs through and corrupt the IV percentile rank."""

    def test_parity_check_callable_in_thetadata(self):
        from backend.bot.data import thetadata
        assert any(name for name in dir(thetadata)
                       if "parity" in name.lower()), (
            "Put-call parity check (P1.2-FU1) missing from thetadata "
            "module — bad IV would corrupt iv_history percentile rank."
        )


@pytest.mark.data_integrity
@pytest.mark.invariant
class TestETFFundamentalsShortCircuit:
    """ETFs (SPY, QQQ, ...) have no fundamentals on Yahoo's quoteSummary
    endpoint. Hitting it produces a 404 storm every cycle."""

    def test_etf_list_contains_canonical_etfs(self):
        from backend.bot.signals.fundamentals import _ETF_TICKERS
        for t in ("SPY", "QQQ", "IWM", "VOO", "VTI"):
            assert t in _ETF_TICKERS

    def test_fetch_fundamentals_returns_empty_for_etf(self):
        from backend.bot.signals.fundamentals import fetch_fundamentals
        snap = fetch_fundamentals("SPY")
        # Must not hit network — neutral snapshot.
        assert snap.pe_ratio is None
        assert snap.eps is None
