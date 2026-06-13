"""Asset-class profile + config-driven tunables (no network)."""
from backend.bot.market_profile import is_crypto, profile
from backend.config import TUNABLES, _as_float, _as_int


# ── crypto detection ─────────────────────────────────────────────────────────

def test_is_crypto_detects_pairs():
    for sym in ("BTC-USD", "ETH-USD", "ETH-USDT", "SOL-USDC", "btc-usd"):
        assert is_crypto(sym) is True, sym


def test_is_crypto_rejects_equities():
    for sym in ("AAPL", "SPY", "MSFT", "BRK-B", "", None, "BTC", "BTCUSD"):
        assert is_crypto(sym) is False, sym


def test_profile_crypto_vs_equity():
    c = profile("BTC-USD")
    assert c.asset_class == "crypto"
    assert c.trades_247 is True
    assert c.regime_anchor == TUNABLES.crypto_regime_anchor
    assert c.fee_bps == TUNABLES.crypto_fee_bps

    e = profile("AAPL")
    assert e.asset_class == "equity"
    assert e.trades_247 is False
    assert e.regime_anchor == "SPY"
    assert e.fee_bps == TUNABLES.backtest_commission_bps


# ── config-driven tunables ───────────────────────────────────────────────────

def test_env_parsing_helpers():
    assert _as_float("27", 18.0) == 27.0
    assert _as_float(None, 18.0) == 18.0
    assert _as_float("", 18.0) == 18.0
    assert _as_float("garbage", 18.0) == 18.0
    assert _as_int("45", 30) == 45
    assert _as_int(None, 30) == 30
    assert _as_int("x", 30) == 30


def test_tunables_have_expected_defaults():
    # Guards against accidental default drift in the central config.
    assert TUNABLES.default_iv_rank == 25.0
    assert TUNABLES.vix_fallback == 18.0
    assert TUNABLES.candle_cache_ttl == 300.0
    assert TUNABLES.backtest_commission_bps == 2.0
    assert TUNABLES.trial_days == 30
    assert TUNABLES.validation_tolerance_pct == 0.5


def test_stub_defaults_sourced_from_config():
    # Proves the market-data stubs are config-driven, not hardcoded literals.
    from backend.bot.market_data import STUB_DEFAULTS

    assert STUB_DEFAULTS["iv_rank"] == TUNABLES.default_iv_rank
    assert STUB_DEFAULTS["pe_ratio"] == TUNABLES.default_pe_ratio
    assert STUB_DEFAULTS["range_3w_pct"] == TUNABLES.default_range_3w_pct
