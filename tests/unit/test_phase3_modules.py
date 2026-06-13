"""Phase 3 foundation — flowintel (dealer positioning + flow profile) +
execution_intel (slippage / order-shaping helpers)."""
from backend.bot.execution_intel import (
    compute_slippage, should_slice, suggested_limit_price, volatility_adjusted_size,
)
from backend.bot.flowintel import (
    DealerPositioning, FlowProfile, analyze, dealer_positioning, flow_profile,
)


# ── flowintel: dealer positioning ───────────────────────────────────────────

def test_dealer_positioning_long_gamma_near_wall_pins():
    gex = {
        "ok": True, "spot_price": 754.5, "dealer_regime": "long_gamma",
        "gamma_flip": 755.0, "call_wall": 755.0, "put_wall": 750.0,
        "net_gex_total": 10_000_000_000, "opex_day": False,
    }
    d = dealer_positioning(gex)
    assert d.regime == "long_gamma"
    assert d.dominant_wall == "call"
    assert d.hedging_pressure == "high"
    assert d.pinning_probability >= 0.75       # within 0.07% of a wall → strong pin
    assert any("dampen" in n.lower() for n in d.notes)


def test_dealer_positioning_short_gamma_far_from_walls():
    gex = {
        "ok": True, "spot_price": 100.0, "dealer_regime": "short_gamma",
        "gamma_flip": 110.0, "call_wall": 115.0, "put_wall": 90.0,
        "net_gex_total": -2_000_000_000, "opex_day": False,
    }
    d = dealer_positioning(gex)
    assert d.regime == "short_gamma"
    assert d.pinning_probability <= 0.25       # short-gamma rejects pinning
    assert any("amplify" in n.lower() for n in d.notes)


def test_dealer_positioning_safe_on_unavailable_gex():
    d = dealer_positioning({"ok": False})
    assert d.regime == "unknown" and d.pinning_probability == 0.0


# ── flowintel: flow profile ──────────────────────────────────────────────────

def _alert(side="bullish", trade_type="sweep", urgency=0.8, premium=120_000.0,
            session="regular", strike=500.0, expiry="2026-06-19"):
    return {"sentiment": side, "trade_type": trade_type, "urgency_score": urgency,
            "premium": premium, "session": session, "ticker": "SPY",
            "strike": strike, "expiry": expiry,
            "option_type": "call" if side == "bullish" else "put"}


def test_flow_profile_bullish_lean_with_repeats_and_darkpool():
    alerts = [
        _alert(),                                # SPY 500C
        _alert(strike=500, urgency=0.9),         # same SPY 500C → repeat
        _alert(urgency=0.7),                     # another SPY 500C → repeat continues
        _alert("bearish", strike=480),
        {"sentiment": "bullish", "trade_type": "darkpool", "premium": 2_000_000.0,
         "urgency_score": 0.5, "session": "regular", "ticker": "SPY",
         "strike": 0, "expiry": "", "option_type": "call"},
    ]
    p = flow_profile(alerts)
    assert p.bullish_sweeps == 3 and p.bearish_sweeps == 1
    assert p.darkpool_confirms is True
    assert p.repeat_orders >= 1                  # the SPY 500C sweeps
    assert p.direction == "bullish"
    assert 0.0 <= p.sweep_aggressiveness <= 1.0


def test_flow_profile_empty():
    p = flow_profile([])
    assert p.direction == "neutral"
    assert p.bullish_sweeps == 0 and p.bearish_sweeps == 0


def test_analyze_safe_when_signals_unavailable(monkeypatch):
    import backend.bot.flowintel as fi

    monkeypatch.setattr(fi, "__name__", fi.__name__)   # noop, fixture present
    out = fi.analyze("ZZZZ")
    assert "dealer_positioning" in out and "flow_profile" in out


# ── execution_intel ─────────────────────────────────────────────────────────

def test_slippage_signed_for_buy_and_sell():
    buy = compute_slippage(expected_price=100.0, fill_price=100.05, side="BUY")
    assert buy.is_adverse and buy.slippage == 0.05 and buy.slippage_bps == 5.0

    sell = compute_slippage(expected_price=100.0, fill_price=99.90, side="SELL")
    assert sell.is_adverse and round(sell.slippage, 4) == 0.10
    assert sell.slippage_bps == 10.0

    improved = compute_slippage(expected_price=100.0, fill_price=99.95, side="BUY")
    assert improved.is_adverse is False
    assert improved.slippage < 0


def test_suggested_limit_uses_atr_then_caps():
    # ATR drives the offset; capped by max_bps when ATR is huge.
    near = suggested_limit_price(side="BUY", mid=100.0, atr=0.5, atr_fraction=0.1)
    assert near < 100.0 and round(100.0 - near, 4) == 0.05

    far = suggested_limit_price(side="BUY", mid=100.0, atr=50.0, atr_fraction=0.1,
                                  max_bps=25.0)
    assert far == round(100.0 - (100.0 * 25 / 10_000), 4)   # capped at 25 bps


def test_volatility_adjusted_size_shrinks_on_wide_atr():
    base = 100.0
    # Wide ATR → shrink size.
    smaller = volatility_adjusted_size(base, atr=5.0, price=100.0, atr_pct_target=0.02)
    assert 50.0 <= smaller < base
    # Normal ATR → no change (clamped to base).
    same = volatility_adjusted_size(base, atr=2.0, price=100.0, atr_pct_target=0.02)
    assert same == base


def test_should_slice_when_order_dwarfs_a_bar():
    assert should_slice(notional=100_000, avg_dollar_volume=500_000, max_impact_pct=0.05) is True
    assert should_slice(notional=1_000, avg_dollar_volume=500_000) is False
    assert should_slice(notional=10_000, avg_dollar_volume=0) is False        # safe


# ── execution telemetry: persistence + insights ─────────────────────────────

def test_log_execution_persists_and_aggregates(temp_db):
    from backend.bot.execution_intel import insights as exec_insights, log_execution

    log_execution(ticker="AAPL", side="BUY", quantity=10,
                   expected_price=180.0, fill_price=180.05, trade_id=None)   # adverse
    log_execution(ticker="AAPL", side="BUY", quantity=10,
                   expected_price=180.0, fill_price=179.98, trade_id=None)   # improved
    log_execution(ticker="MSFT", side="SELL", quantity=5,
                   expected_price=400.0, fill_price=399.80, trade_id=None)   # adverse SELL

    out = exec_insights()
    assert out["count"] == 3
    assert out["adverse_rate"] > 0.5                        # 2 of 3 adverse
    assert "BUY" in out["by_side"] and "SELL" in out["by_side"]
    assert "AAPL" in out["by_ticker"] and out["by_ticker"]["AAPL"]["count"] == 2


# ── features → ranker: pinning penalty ──────────────────────────────────────

def test_features_include_pinning_probability_from_snapshot():
    from backend.bot.features import build_features

    f = build_features({
        "price": 100.0, "ma50": 100.0, "ma200": 100.0,
        "call_wall": 100.5, "put_wall": 95.0, "gamma_flip": 99.5,
        "dealer_regime": "long_gamma", "net_gex_total": 8e9,
    })
    assert "pinning_probability" in f
    assert f["pinning_probability"] > 0.5            # near the call wall + long gamma
    assert f["dominant_wall"] == "call"


def test_ranker_downgrades_long_trade_into_call_wall_pin():
    from backend.bot.features import build_features
    from backend.bot.probability import score_signal
    from backend.bot.ranker import rank_trade
    from backend.bot.regime import detect_regime
    from backend.bot.strategies.base import Action, Signal

    snap = {
        "price": 100.0, "ma50": 99.0, "ma200": 95.0, "rsi": 60, "vix": 14,
        "adx": 26, "volume": 1.2e6, "avg_volume": 1.0e6,
        "call_wall": 100.3, "put_wall": 95.0, "gamma_flip": 99.0,
        "dealer_regime": "long_gamma", "net_gex_total": 8e9,
        "bullish_sweeps": 4, "bearish_sweeps": 1,
    }
    feats = build_features(snap)
    regime = detect_regime(snap)
    sig = Signal(action=Action.BUY_STOCK, ticker="AAPL", confidence=0.7,
                  reason="trend", strategy="x", stop_loss=2.0, take_profit=6.0)
    prob = score_signal(sig, feats, regime)
    pinned = rank_trade(prob, regime, None, feats)
    # And the same setup without the wall right at price (move the wall away).
    feats_no_pin = dict(feats); feats_no_pin["pinning_probability"] = 0.0
    feats_no_pin["dominant_wall"] = "neutral"
    clean = rank_trade(prob, regime, None, feats_no_pin)
    assert pinned.score < clean.score                # pinned setup ranks lower
    assert any("wall" in r for r in pinned.reasoning)
