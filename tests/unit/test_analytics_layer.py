"""Analytical layer: regime + features + confluence + probability + ranking.

Each piece is a pure function so the tests are deterministic — no network, no DB.
"""
from backend.bot.analytics import AnalyticsEngine, gate_by_grade
from backend.bot.confluence import ConfluenceScore, score_confluence
from backend.bot.features import build_features
from backend.bot.probability import score_signal
from backend.bot.ranker import rank_trade
from backend.bot.regime import detect_regime
from backend.bot.strategies.base import Action, Signal


# ── regime ───────────────────────────────────────────────────────────────────

def test_regime_bullish_low_vol():
    snap = {"price": 100, "ma50": 95, "ma200": 90, "vix": 12, "adx": 28,
            "volume": 1.0e6, "avg_volume": 8.0e5, "dealer_regime": "long_gamma"}
    r = detect_regime(snap)
    assert r.trend == "bullish" and r.volatility == "low"
    assert r.momentum == "expanding"
    assert r.gamma == "long_gamma" and r.risk == "risk_on"
    assert r.confidence >= 0.5


def test_regime_bearish_high_vol_short_gamma():
    snap = {"price": 90, "ma50": 95, "ma200": 100, "vix": 30, "adx": 30,
            "volume": 2.0e6, "avg_volume": 1.0e6, "dealer_regime": "short_gamma"}
    r = detect_regime(snap)
    assert r.trend == "bearish" and r.volatility == "high"
    assert r.gamma == "short_gamma" and r.risk == "risk_off"
    assert "macd_momentum" in r.preferred_strategies or "news_catalyst_momentum" in r.preferred_strategies


def test_regime_choppy_unknown_safe():
    r = detect_regime({})    # sparse snapshot must not raise
    assert r.trend == "unknown"
    assert r.preferred_strategies                       # always non-empty fallback


# ── features ────────────────────────────────────────────────────────────────

def test_features_normalise_directional_bias():
    feats = build_features({
        "price": 100, "ma50": 95, "ma200": 90, "rsi": 65,
        "macd": 0.5, "macd_signal": 0.3, "volume": 1.2e6, "avg_volume": 1.0e6,
        "bullish_sweeps": 7, "bearish_sweeps": 1, "news_score": 0.4,
        "gamma_flip": 95, "dealer_regime": "short_gamma",
    })
    assert feats["rsi_bias"] > 0
    assert feats["trend_bias"] > 0
    assert feats["flow_bullishness"] > 0
    assert feats["composite_bias"] > 0
    assert feats["gex_flip_distance"] == 5.0      # 5% above flip
    assert feats["dealer_regime"] == "short_gamma"


def test_features_nan_safe_on_empty_snapshot():
    feats = build_features({})
    assert feats["composite_bias"] == 0.0
    assert feats["flow_bullishness"] == 0.0


# ── confluence ─────────────────────────────────────────────────────────────

def test_confluence_aligned_bullish():
    score = score_confluence({"1h": "bullish", "daily": "bullish", "weekly": "bullish"})
    assert score.direction == "bullish"
    assert score.bullish_alignment == 1.0
    assert score.conflicting_timeframes == []
    assert score.dominant_tf == "weekly"


def test_confluence_conflicts_called_out():
    score = score_confluence({"1h": "bullish", "daily": "bullish", "15m": "bearish"})
    assert score.direction == "bullish"
    assert "15m" in score.conflicting_timeframes
    assert 0.4 < score.bullish_alignment < 1.0


# ── probability + ranking ───────────────────────────────────────────────────

def _bullish_snapshot():
    return {
        "price": 100, "ma50": 96, "ma200": 90, "rsi": 62, "macd": 0.4, "macd_signal": 0.2,
        "adx": 28, "vix": 14, "iv_rank": 25, "volume": 1.2e6, "avg_volume": 1.0e6,
        "bullish_sweeps": 6, "bearish_sweeps": 1, "news_score": 0.4,
        "gamma_flip": 95, "dealer_regime": "short_gamma", "darkpool_confirms": True,
    }


def test_probability_boosted_when_everything_aligns():
    snap = _bullish_snapshot()
    sig = Signal(action=Action.BUY_STOCK, ticker="AAPL", confidence=0.6,
                 reason="trend", strategy="x", stop_loss=2.0, take_profit=6.0)
    feats = build_features(snap)
    regime = detect_regime(snap)
    conf = score_confluence({"1h": "bullish", "daily": "bullish", "weekly": "bullish"})
    p = score_signal(sig, feats, regime, confluence=conf)
    assert p.direction == "LONG"
    assert p.probability > 0.7                       # aligned setup lifts the base 0.60
    assert p.risk_reward == 3.0
    assert p.confidence > 0.5


def test_probability_dragged_down_against_regime():
    snap = _bullish_snapshot()
    sig = Signal(action=Action.SELL_STOCK, ticker="AAPL", confidence=0.6,
                 reason="counter-trend short", strategy="x", stop_loss=2.0, take_profit=4.0)
    feats = build_features(snap)
    regime = detect_regime(snap)
    p = score_signal(sig, feats, regime)
    assert p.direction == "SHORT"
    assert p.probability < 0.6                       # fighting bullish regime / flow


def test_ranker_grades_an_aligned_setup_highly():
    snap = _bullish_snapshot()
    sig = Signal(action=Action.BUY_STOCK, ticker="AAPL", confidence=0.7,
                 reason="trend", strategy="x", stop_loss=2.0, take_profit=8.0)
    feats = build_features(snap)
    regime = detect_regime(snap)
    conf = score_confluence({"1h": "bullish", "daily": "bullish", "weekly": "bullish"})
    p = score_signal(sig, feats, regime, confluence=conf)
    rank = rank_trade(p, regime, conf, feats)
    assert rank.grade in ("A", "A+")
    assert 0.0 <= rank.score <= 1.0
    assert any("regime" in r or "win prob" in r for r in rank.reasoning)


def test_ranker_rejects_a_weak_counter_trend_short():
    snap = _bullish_snapshot()
    sig = Signal(action=Action.SELL_STOCK, ticker="AAPL", confidence=0.5,
                 reason="counter-trend", strategy="x", stop_loss=3.0, take_profit=3.0)
    feats = build_features(snap)
    regime = detect_regime(snap)
    conf = score_confluence({"1h": "bullish", "daily": "bullish", "weekly": "bullish"})
    p = score_signal(sig, feats, regime, confluence=conf)
    rank = rank_trade(p, regime, conf, feats)
    assert rank.grade in ("Reject", "C")
    assert gate_by_grade(rank, "B") is False         # blocked at min-grade B
    assert gate_by_grade(rank, None) is True         # default config doesn't gate


# ── coordinator ─────────────────────────────────────────────────────────────

def test_analytics_engine_evaluate_returns_full_result():
    snap = _bullish_snapshot()
    sig = Signal(action=Action.BUY_STOCK, ticker="AAPL", confidence=0.65,
                 reason="trend", strategy="x", stop_loss=2.0, take_profit=6.0)
    result = AnalyticsEngine().evaluate("AAPL", snap, sig)
    d = result.to_dict()
    for key in ("regime", "features", "confluence", "probability", "rank"):
        assert key in d
    assert d["regime"]["trend"] == "bullish"
    assert d["probability"]["direction"] == "LONG"
    assert d["rank"]["grade"] in ("A+", "A", "B", "C", "Reject")
