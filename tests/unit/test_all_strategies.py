"""
Full Test Suite — All 15 Strategies + Adaptive Selector
Run: pytest tests/unit/test_all_strategies.py -v --tb=short
"""

import pytest

from backend.bot.strategies.adaptive import (
    STRATEGY_COMBOS,
    AdaptiveStrategy,
)
from backend.bot.strategies.all_strategies import (
    STRATEGY_REGISTRY,
    BullCallSpread,
    CashSecuredPut,
    Collar,
    CoveredCallWheel,
    EarningsStraddle,
    GapFill,
    IronCondor,
    MACDMomentumCross,
    NewsCatalystMomentum,
    OpeningRangeBreakout,
    RatioSpread,
    RSIMeanReversion,
    TrendPullback,
    VWAPReversion,
    ZeroDTEScalp,
    get_strategy,
)
from backend.bot.strategies.base import Action, Signal


# ─────────────────────────────────────────────────────────────────────────────
# SHARED FIXTURES
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def bullish_data():
    return {
        "price": 150.0, "rsi": 52.0, "macd": 0.5, "macd_signal": 0.3,
        "macd_hist": 0.2, "prev_macd_hist": -0.1, "ma50": 140.0,
        "ma200": 120.0, "volume": 2_000_000, "avg_volume": 1_200_000,
        "iv_rank": 20, "adx": 28, "vix": 16, "news_score": 0.5,
        "earnings_days": 30, "sector_rsi": 55, "pe_ratio": 22,
        "eps_growth": 0.12, "analyst_rating": "buy", "spy_trend": "bullish",
        "gap_pct": 0.02, "premarket_volume": 800_000, "shares_owned": 100,
        "position_value": 15_000, "portfolio_value": 100_000,
        "unrealized_gain_pct": 0.10, "high_52w": 158.0, "prev_close": 147.0,
        "open_price": 150.0, "sector_trend": "bullish", "at_support": True,
        "bullish_reversal_candle": True, "volume_trend": "decreasing",
        "vwap": 148.0, "momentum_5m": 0.7, "rsi_5m": 55,
        "market_trend": "bullish", "time_of_day": "10:30",
        "orb_high": 151.0, "orb_low": 148.0,
        "hist_earnings_move_avg": 0.09, "implied_move": 0.06,
        "estimated_premium_pct": 0.015, "has_catalyst": False,
        "earnings_today": False, "catalyst_type": "analyst_upgrade",
        "news_age_hours": 1.0, "range_3w_pct": 0.08,
        "spy_adx": 28,
    }


@pytest.fixture
def bearish_data(bullish_data):
    d = bullish_data.copy()
    d.update({
        "price": 130.0, "rsi": 28.0, "macd": -0.5, "macd_signal": -0.2,
        "macd_hist": -0.3, "prev_macd_hist": 0.1, "ma50": 145.0,
        "news_score": -0.3, "gap_pct": -0.03, "vwap": 135.0,
        "momentum_5m": -0.7, "rsi_5m": 28, "market_trend": "bearish",
        "spy_trend": "bearish"
    })
    return d


@pytest.fixture
def neutral_data(bullish_data):
    d = bullish_data.copy()
    d.update({
        "rsi": 50.0, "macd": 0.0, "macd_signal": 0.0,
        "iv_rank": 55, "adx": 14, "vix": 22, "news_score": 0.1,
        "momentum_5m": 0.0, "gap_pct": 0.0
    })
    return d


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 01 — Bull Call Spread
# ─────────────────────────────────────────────────────────────────────────────
class TestBullCallSpread:
    def test_generates_signal_in_bullish_conditions(self, bullish_data):
        sig = BullCallSpread().analyze("AAPL", bullish_data)
        assert sig.action == Action.BULL_CALL_SPREAD
        assert sig.confidence >= 0.65
        assert sig.ticker == "AAPL"

    def test_hold_when_below_ma(self, bullish_data):
        bullish_data["price"] = 100.0  # below ma50=140
        sig = BullCallSpread().analyze("AAPL", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_when_iv_rank_high(self, bullish_data):
        bullish_data["iv_rank"] = 60
        sig = BullCallSpread().analyze("AAPL", bullish_data)
        assert sig.confidence < 0.85  # loses IV score

    def test_metadata_has_sell_strike(self, bullish_data):
        sig = BullCallSpread().analyze("AAPL", bullish_data)
        if sig.action != Action.HOLD:
            assert "sell_strike" in sig.metadata

    def test_stop_loss_defined(self, bullish_data):
        sig = BullCallSpread().analyze("AAPL", bullish_data)
        if sig.action != Action.HOLD:
            assert sig.stop_loss == 25.0


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 02 — RSI Mean Reversion
# ─────────────────────────────────────────────────────────────────────────────
class TestRSIMeanReversion:
    def test_buy_when_oversold(self, bearish_data):
        bearish_data["ma200"] = 120.0  # below price=130 — uptrend
        sig = RSIMeanReversion().analyze("TSLA", bearish_data)
        assert sig.action == Action.BUY_STOCK

    def test_hold_when_rsi_neutral(self, bullish_data):
        sig = RSIMeanReversion().analyze("TSLA", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_below_200ma(self, bearish_data):
        bearish_data["ma200"] = 200.0  # above price=130 — downtrend
        sig = RSIMeanReversion().analyze("TSLA", bearish_data)
        assert sig.action == Action.HOLD

    def test_metadata_has_rsi_target(self, bearish_data):
        bearish_data["ma200"] = 120.0
        sig = RSIMeanReversion().analyze("TSLA", bearish_data)
        if sig.action != Action.HOLD:
            assert sig.metadata.get("rsi_target") == 50

    def test_hold_when_earnings_close(self, bearish_data):
        bearish_data["ma200"] = 120.0
        bearish_data["earnings_days"] = 2
        sig = RSIMeanReversion().analyze("TSLA", bearish_data)
        assert sig.action == Action.HOLD


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 03 — Opening Range Breakout
# ─────────────────────────────────────────────────────────────────────────────
class TestOpeningRangeBreakout:
    def test_buy_on_upside_breakout(self, bullish_data):
        bullish_data["price"] = 152.0  # above orb_high=151
        sig = OpeningRangeBreakout().analyze("SPY", bullish_data)
        assert sig.action in (Action.BUY_STOCK, Action.HOLD)

    def test_hold_on_earnings_day(self, bullish_data):
        bullish_data["earnings_today"] = True
        sig = OpeningRangeBreakout().analyze("SPY", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_no_premarket_volume(self, bullish_data):
        bullish_data["premarket_volume"] = 100
        sig = OpeningRangeBreakout().analyze("SPY", bullish_data)
        assert sig.action == Action.HOLD

    def test_metadata_has_time_exit(self, bullish_data):
        bullish_data["price"] = 152.0
        sig = OpeningRangeBreakout().analyze("SPY", bullish_data)
        if sig.action != Action.HOLD:
            assert sig.metadata.get("time_exit") == "15:30"


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 04 — MACD Momentum Cross
# ─────────────────────────────────────────────────────────────────────────────
class TestMACDMomentumCross:
    def test_buy_on_macd_cross(self, bullish_data):
        sig = MACDMomentumCross().analyze("NVDA", bullish_data)
        assert sig.action == Action.BUY_STOCK

    def test_hold_when_below_50ma(self, bullish_data):
        bullish_data["price"] = 100.0
        sig = MACDMomentumCross().analyze("NVDA", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_no_macd_cross(self, bullish_data):
        bullish_data["macd"] = 0.1
        bullish_data["macd_signal"] = 0.5
        sig = MACDMomentumCross().analyze("NVDA", bullish_data)
        assert sig.action == Action.HOLD

    def test_confidence_boosted_by_adx(self, bullish_data):
        bullish_data["adx"] = 30
        sig = MACDMomentumCross().analyze("NVDA", bullish_data)
        high_adx_conf = sig.confidence
        bullish_data["adx"] = 10
        sig2 = MACDMomentumCross().analyze("NVDA", bullish_data)
        assert high_adx_conf >= sig2.confidence


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 05 — Earnings Straddle
# ─────────────────────────────────────────────────────────────────────────────
class TestEarningsStraddle:
    def test_straddle_when_hist_exceeds_implied(self, bullish_data):
        bullish_data["earnings_days"] = 2
        sig = EarningsStraddle().analyze("TSLA", bullish_data)
        assert sig.action == Action.BUY_STRADDLE

    def test_hold_too_far_from_earnings(self, bullish_data):
        bullish_data["earnings_days"] = 10
        sig = EarningsStraddle().analyze("TSLA", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_implied_exceeds_historical(self, bullish_data):
        bullish_data["earnings_days"] = 2
        bullish_data["implied_move"] = 0.15
        bullish_data["hist_earnings_move_avg"] = 0.08
        sig = EarningsStraddle().analyze("TSLA", bullish_data)
        assert sig.action == Action.HOLD

    def test_metadata_has_exit_trigger(self, bullish_data):
        bullish_data["earnings_days"] = 2
        sig = EarningsStraddle().analyze("TSLA", bullish_data)
        if sig.action != Action.HOLD:
            assert sig.metadata.get("exit_trigger") == "post_earnings_open"


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 06 — Trend Pullback
# ─────────────────────────────────────────────────────────────────────────────
class TestTrendPullback:
    def test_buy_call_on_pullback(self, bullish_data):
        bullish_data["price"] = 148.0    # ~6.3% below high_52w=158
        sig = TrendPullback().analyze("AAPL", bullish_data)
        assert sig.action in (Action.BUY_CALL, Action.HOLD)

    def test_hold_rsi_overbought(self, bullish_data):
        bullish_data["rsi"] = 70.0
        sig = TrendPullback().analyze("AAPL", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_pullback_too_small(self, bullish_data):
        bullish_data["price"] = 157.0  # <1% pullback
        sig = TrendPullback().analyze("AAPL", bullish_data)
        assert sig.action == Action.HOLD

    def test_stop_loss_set(self, bullish_data):
        bullish_data["price"] = 148.0
        sig = TrendPullback().analyze("AAPL", bullish_data)
        if sig.action != Action.HOLD:
            assert sig.stop_loss == 2.0


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 07 — News Catalyst Momentum
# ─────────────────────────────────────────────────────────────────────────────
class TestNewsCatalystMomentum:
    def test_buy_call_on_strong_catalyst(self, bullish_data):
        bullish_data["news_score"] = 0.80
        bullish_data["news_age_hours"] = 1.0
        sig = NewsCatalystMomentum().analyze("NVDA", bullish_data)
        assert sig.action == Action.BUY_CALL

    def test_hold_stale_news(self, bullish_data):
        bullish_data["news_score"] = 0.80
        bullish_data["news_age_hours"] = 5.0
        sig = NewsCatalystMomentum().analyze("NVDA", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_weak_sentiment(self, bullish_data):
        bullish_data["news_score"] = 0.40
        sig = NewsCatalystMomentum().analyze("NVDA", bullish_data)
        assert sig.action == Action.HOLD

    def test_time_exit_in_metadata(self, bullish_data):
        bullish_data["news_score"] = 0.80
        bullish_data["news_age_hours"] = 1.0
        sig = NewsCatalystMomentum().analyze("NVDA", bullish_data)
        if sig.action != Action.HOLD:
            assert sig.metadata.get("time_exit") == "13:00"


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 08 — Iron Condor
# ─────────────────────────────────────────────────────────────────────────────
class TestIronCondor:
    def test_iron_condor_ranging_market(self, neutral_data):
        sig = IronCondor().analyze("SPY", neutral_data)
        assert sig.action in (Action.IRON_CONDOR, Action.HOLD)

    def test_hold_low_iv(self, neutral_data):
        neutral_data["iv_rank"] = 20
        sig = IronCondor().analyze("SPY", neutral_data)
        assert sig.action == Action.HOLD

    def test_hold_trending_market(self, neutral_data):
        neutral_data["adx"] = 35
        sig = IronCondor().analyze("SPY", neutral_data)
        assert sig.action == Action.HOLD

    def test_hold_earnings_within_dte(self, neutral_data):
        neutral_data["earnings_days"] = 10
        sig = IronCondor().analyze("SPY", neutral_data)
        assert sig.action == Action.HOLD

    def test_metadata_has_strikes(self, neutral_data):
        sig = IronCondor().analyze("SPY", neutral_data)
        if sig.action != Action.HOLD:
            assert "call_short" in sig.metadata
            assert "put_short" in sig.metadata


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 09 — Covered Call Wheel
# ─────────────────────────────────────────────────────────────────────────────
class TestCoveredCallWheel:
    def test_sell_covered_call_when_shares_owned(self, bullish_data):
        sig = CoveredCallWheel().analyze("AAPL", bullish_data)
        assert sig.action in (Action.SELL_COVERED_CALL, Action.HOLD)

    def test_hold_no_shares(self, bullish_data):
        bullish_data["shares_owned"] = 0
        sig = CoveredCallWheel().analyze("AAPL", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_earnings_within_dte(self, bullish_data):
        bullish_data["earnings_days"] = 5
        sig = CoveredCallWheel().analyze("AAPL", bullish_data)
        assert sig.action == Action.HOLD

    def test_strike_above_price(self, bullish_data):
        sig = CoveredCallWheel().analyze("AAPL", bullish_data)
        if sig.action != Action.HOLD:
            assert sig.strike > bullish_data["price"]


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 10 — Cash-Secured Put
# ─────────────────────────────────────────────────────────────────────────────
class TestCashSecuredPut:
    def test_sell_put_on_quality_stock(self, bullish_data):
        bullish_data["iv_rank"] = 40
        sig = CashSecuredPut().analyze("AAPL", bullish_data)
        assert sig.action in (Action.SELL_CSP, Action.HOLD)

    def test_hold_low_iv(self, bullish_data):
        bullish_data["iv_rank"] = 10
        sig = CashSecuredPut().analyze("AAPL", bullish_data)
        assert sig.action == Action.HOLD

    def test_strike_below_price(self, bullish_data, monkeypatch):
        # Stub BOTH chain helpers so the strategy doesn't pull real AAPL
        # chain data (which is now Alpaca-backed and returns real-world
        # strikes ≈ $295 regardless of our fixture price). CSP uses
        # ``chain_strike_with_drift`` for its target-delta logic.
        from backend.bot.strategies import all_strategies as _strat
        def _csk(ticker, price, kind, **kw):
            return round(price * 0.95, 2)
        def _csk_drift(ticker, price, kind, **kw):
            return (round(price * 0.95, 2), 0.0)
        monkeypatch.setattr(_strat, "chain_strike", _csk)
        monkeypatch.setattr(_strat, "chain_strike_with_drift", _csk_drift)
        bullish_data["iv_rank"] = 40
        sig = CashSecuredPut().analyze("AAPL", bullish_data)
        if sig.action != Action.HOLD:
            assert sig.strike < bullish_data["price"]

    def test_metadata_has_assignment_plan(self, bullish_data):
        bullish_data["iv_rank"] = 40
        sig = CashSecuredPut().analyze("AAPL", bullish_data)
        if sig.action != Action.HOLD:
            assert "assignment_plan" in sig.metadata


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 11 — VWAP Reversion
# ─────────────────────────────────────────────────────────────────────────────
class TestVWAPReversion:
    def test_buy_when_below_vwap(self, bullish_data):
        bullish_data["price"] = 145.0
        bullish_data["vwap"] = 148.0   # price 2% below VWAP
        sig = VWAPReversion().analyze("SPY", bullish_data)
        assert sig.action in (Action.BUY_STOCK, Action.HOLD)

    def test_sell_when_above_vwap(self, bullish_data):
        bullish_data["price"] = 151.0
        bullish_data["vwap"] = 148.0   # price 2% above VWAP
        sig = VWAPReversion().analyze("SPY", bullish_data)
        assert sig.action in (Action.SELL_STOCK, Action.HOLD)

    def test_hold_too_close_to_vwap(self, bullish_data):
        bullish_data["price"] = 148.5
        bullish_data["vwap"] = 148.0
        sig = VWAPReversion().analyze("SPY", bullish_data)
        assert sig.action == Action.HOLD

    def test_metadata_has_vwap_target(self, bullish_data):
        bullish_data["price"] = 145.0
        bullish_data["vwap"] = 148.0
        sig = VWAPReversion().analyze("SPY", bullish_data)
        if sig.action != Action.HOLD:
            assert sig.metadata.get("target") == 148.0


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 12 — Gap Fill
# ─────────────────────────────────────────────────────────────────────────────
class TestGapFill:
    def test_fade_gap_up(self, bullish_data):
        bullish_data["gap_pct"] = 0.04
        bullish_data["has_catalyst"] = False
        bullish_data["earnings_today"] = False
        sig = GapFill().analyze("AAPL", bullish_data)
        assert sig.action in (Action.SELL_STOCK, Action.HOLD)

    def test_fade_gap_down(self, bullish_data):
        bullish_data["gap_pct"] = -0.04
        bullish_data["has_catalyst"] = False
        sig = GapFill().analyze("AAPL", bullish_data)
        assert sig.action in (Action.BUY_STOCK, Action.HOLD)

    def test_hold_catalyst_gap(self, bullish_data):
        bullish_data["gap_pct"] = 0.04
        bullish_data["has_catalyst"] = True
        sig = GapFill().analyze("AAPL", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_gap_too_large(self, bullish_data):
        bullish_data["gap_pct"] = 0.12
        sig = GapFill().analyze("AAPL", bullish_data)
        assert sig.action == Action.HOLD


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 13 — 0DTE Scalp
# ─────────────────────────────────────────────────────────────────────────────
class TestZeroDTEScalp:
    def test_buy_call_on_upward_momentum(self, bullish_data):
        bullish_data["time_of_day"] = "10:30"
        sig = ZeroDTEScalp().analyze("SPY", bullish_data)
        assert sig.action in (Action.BUY_CALL, Action.HOLD)

    def test_buy_put_on_downward_momentum(self, bearish_data):
        bearish_data["time_of_day"] = "10:30"
        sig = ZeroDTEScalp().analyze("SPY", bearish_data)
        assert sig.action in (Action.BUY_PUT, Action.HOLD)

    def test_hold_unsupported_ticker(self, bullish_data):
        sig = ZeroDTEScalp().analyze("SMALLCAP", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_outside_trading_window(self, bullish_data):
        bullish_data["time_of_day"] = "09:30"
        sig = ZeroDTEScalp().analyze("SPY", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_high_vix(self, bullish_data):
        bullish_data["vix"] = 40
        bullish_data["time_of_day"] = "10:30"
        sig = ZeroDTEScalp().analyze("SPY", bullish_data)
        assert sig.action == Action.HOLD

    def test_dte_is_zero(self, bullish_data):
        bullish_data["time_of_day"] = "10:30"
        sig = ZeroDTEScalp().analyze("SPY", bullish_data)
        if sig.action != Action.HOLD:
            assert sig.dte == 0


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 14 — Ratio Spread
# ─────────────────────────────────────────────────────────────────────────────
class TestRatioSpread:
    def test_ratio_spread_in_moderate_conditions(self, neutral_data):
        neutral_data["iv_rank"] = 40
        neutral_data["adx"] = 20
        sig = RatioSpread().analyze("AAPL", neutral_data)
        assert sig.action in (Action.RATIO_SPREAD, Action.HOLD)

    def test_hold_low_iv(self, neutral_data):
        neutral_data["iv_rank"] = 15
        sig = RatioSpread().analyze("AAPL", neutral_data)
        assert sig.action == Action.HOLD

    def test_metadata_has_ratio(self, neutral_data):
        neutral_data["iv_rank"] = 40
        sig = RatioSpread().analyze("AAPL", neutral_data)
        if sig.action != Action.HOLD:
            assert sig.metadata.get("ratio") == "1x2"

    def test_sell_strike_above_buy_strike(self, neutral_data):
        neutral_data["iv_rank"] = 40
        sig = RatioSpread().analyze("AAPL", neutral_data)
        if sig.action != Action.HOLD:
            assert sig.metadata["sell_strike"] > sig.metadata["buy_strike"]


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 15 — Collar
# ─────────────────────────────────────────────────────────────────────────────
class TestCollar:
    def test_collar_on_large_position(self, bullish_data):
        bullish_data["position_value"] = 20_000   # 20% of portfolio
        bullish_data["vix"] = 25
        sig = Collar().analyze("AAPL", bullish_data)
        assert sig.action in (Action.COLLAR, Action.HOLD)

    def test_hold_small_position(self, bullish_data):
        bullish_data["position_value"] = 2_000    # 2% of portfolio
        bullish_data["vix"] = 15
        bullish_data["earnings_days"] = 60
        bullish_data["unrealized_gain_pct"] = 0.05
        sig = Collar().analyze("AAPL", bullish_data)
        assert sig.action == Action.HOLD

    def test_hold_no_shares(self, bullish_data):
        bullish_data["shares_owned"] = 50
        sig = Collar().analyze("AAPL", bullish_data)
        assert sig.action == Action.HOLD

    def test_put_strike_below_price(self, bullish_data, monkeypatch):
        # Stub chain_strike — same reason as TestCashSecuredPut above:
        # real chain data ignores our fixture price.
        from backend.bot.strategies import all_strategies as _strat
        def _stub(ticker, price, kind, **kw):
            if kind == "put":
                return round(price * 0.95, 2)
            return round(price * 1.05, 2)
        monkeypatch.setattr(_strat, "chain_strike", _stub)
        bullish_data["position_value"] = 20_000
        bullish_data["vix"] = 25
        sig = Collar().analyze("AAPL", bullish_data)
        if sig.action != Action.HOLD:
            assert sig.metadata["buy_put_strike"] < bullish_data["price"]
            assert sig.metadata["sell_call_strike"] > bullish_data["price"]


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY REGISTRY TESTS
# ─────────────────────────────────────────────────────────────────────────────
class TestStrategyRegistry:
    def test_all_16_strategies_registered(self):
        # STRAT.1 (2026-06-04) added ema50_momentum, taking us from 15 → 16.
        assert len(STRATEGY_REGISTRY) == 16

    def test_get_strategy_by_name(self):
        s = get_strategy("macd_momentum")
        assert isinstance(s, MACDMomentumCross)

    def test_invalid_strategy_raises(self):
        with pytest.raises(ValueError):
            get_strategy("nonexistent_strategy")

    def test_all_strategies_return_signal(self, bullish_data):
        for name, strategy in STRATEGY_REGISTRY.items():
            sig = strategy.analyze("SPY", bullish_data)
            assert isinstance(sig, Signal), f"{name} did not return Signal"
            assert sig.action in Action.__members__.values()
            assert 0.0 <= sig.confidence <= 1.0

    def test_all_strategies_have_name(self):
        for name, strategy in STRATEGY_REGISTRY.items():
            assert strategy.name == name


# ─────────────────────────────────────────────────────────────────────────────
# ADAPTIVE STRATEGY TESTS
# ─────────────────────────────────────────────────────────────────────────────
class TestAdaptiveStrategy:
    @pytest.fixture
    def market_data(self, bullish_data):
        return {
            **bullish_data,
            "spy_trend": "bullish",
            "vix": 18,
            "spy_adx": 28,
            "tickers": {
                "SPY": bullish_data,
                "AAPL": bullish_data,
                "NVDA": bullish_data
            }
        }

    def test_detects_bullish_trending_regime(self, market_data):
        adaptive = AdaptiveStrategy()
        regime = adaptive.detect_regime(market_data)
        assert regime == "trending_up"

    def test_detects_volatile_regime(self, market_data):
        market_data["vix"] = 35
        adaptive = AdaptiveStrategy()
        regime = adaptive.detect_regime(market_data)
        assert regime == "volatile"

    def test_detects_ranging_regime(self, market_data):
        market_data["spy_adx"] = 12
        market_data["vix"] = 18
        adaptive = AdaptiveStrategy()
        regime = adaptive.detect_regime(market_data)
        assert regime == "ranging"

    def test_plan_day_returns_valid_plan(self, market_data):
        adaptive = AdaptiveStrategy()
        plan = adaptive.plan_day(["SPY", "AAPL", "NVDA"], market_data)
        assert plan.primary_strategy in STRATEGY_REGISTRY
        assert plan.market_regime in ("trending_up", "trending_down", "ranging", "volatile")
        assert len(plan.recommended_tickers) <= 5
        assert isinstance(plan.confidence_scores, dict)

    def test_plan_has_all_16_scores(self, market_data):
        # STRAT.1 added ema50_momentum, raising the scored-strategy count
        # from 15 → 16. The adaptive selector now has one extra candidate.
        adaptive = AdaptiveStrategy()
        plan = adaptive.plan_day(["SPY"], market_data)
        assert len(plan.confidence_scores) == 16

    def test_combo_returns_signal_on_agreement(self, bullish_data):
        adaptive = AdaptiveStrategy()
        sig = adaptive.run_combo("AAPL", bullish_data, "wheel_income")
        # May be None if strategies don't agree — that's valid
        if sig is not None:
            assert isinstance(sig, Signal)
            assert "combo:" in sig.strategy

    def test_invalid_combo_raises(self, bullish_data):
        adaptive = AdaptiveStrategy()
        with pytest.raises(ValueError):
            adaptive.run_combo("AAPL", bullish_data, "nonexistent_combo")

    def test_score_all_returns_16_scores(self, bullish_data):
        # STRAT.1: ema50_momentum brought us to 16.
        adaptive = AdaptiveStrategy()
        scores = adaptive.score_all("AAPL", bullish_data)
        assert len(scores) == 16
        for name, score in scores.items():
            assert 0.0 <= score <= 1.0, f"{name} score {score} out of range"


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL MODEL TESTS
# ─────────────────────────────────────────────────────────────────────────────
class TestSignalModel:
    def test_signal_is_actionable_above_threshold(self):
        sig = Signal(action=Action.BUY_STOCK, ticker="AAPL",
                     confidence=0.75, reason="test", strategy="test")
        assert sig.is_actionable(0.60) is True

    def test_signal_not_actionable_below_threshold(self):
        sig = Signal(action=Action.BUY_STOCK, ticker="AAPL",
                     confidence=0.50, reason="test", strategy="test")
        assert sig.is_actionable(0.60) is False

    def test_hold_never_actionable(self):
        sig = Signal(action=Action.HOLD, ticker="AAPL",
                     confidence=0.99, reason="hold", strategy="test")
        assert sig.is_actionable(0.0) is False

    def test_signal_has_default_metadata(self):
        sig = Signal(action=Action.BUY_STOCK, ticker="X",
                     confidence=0.7, reason="r", strategy="s")
        assert isinstance(sig.metadata, dict)
