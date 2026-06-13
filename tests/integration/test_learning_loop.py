"""Learning loop — DecisionLog persistence + outcome linkage + insights aggregation."""
from backend.bot.learning import insights, log_decision, record_outcome


def _event(ticker, action="BUY_STOCK", status="submitted", strategy="adaptive",
           grade="A", regime="bullish", prob=0.7, trade_id=None):
    return {
        "ticker": ticker, "action": action, "status": status, "strategy": strategy,
        "confidence": 0.7, "trade_id": trade_id,
        "analytics": {
            "regime": {"trend": regime, "volatility": "normal", "gamma": "long_gamma",
                       "label": f"{regime} · normal-vol"},
            "probability": {"probability": prob, "direction": "LONG"},
            "rank": {"grade": grade, "score": 0.78},
            "features": {"rsi_14": 60, "composite_bias": 0.3},
        },
    }


def test_log_and_aggregate(temp_db):
    # Two AAPL submitted A grades + one TSLA rejected C grade.
    assert log_decision(_event("AAPL", grade="A", regime="bullish"))
    assert log_decision(_event("AAPL", grade="A+", regime="bullish"))
    log_decision(_event("TSLA", status="rejected", grade="C", regime="choppy", prob=0.45))

    out = insights()
    assert out["decisions_analyzed"] == 3
    assert "adaptive" in out["by_strategy"]
    assert out["by_strategy"]["adaptive"]["count"] == 3
    assert out["by_regime"]["bullish"]["submitted"] == 2
    assert "A" in out["by_grade"] and out["by_grade"]["A"]["submitted"] == 1
    assert "A+" in out["by_grade"]


def test_outcome_linkage_drives_win_rate(temp_db):
    log_decision(_event("AAPL", strategy="trend_pullback"))
    log_decision(_event("AAPL", strategy="trend_pullback"))
    log_decision(_event("AAPL", strategy="trend_pullback"))

    # Three closes — two wins, one loss.
    assert record_outcome("AAPL", 50.0)
    assert record_outcome("AAPL", 30.0)
    assert record_outcome("AAPL", -25.0)

    out = insights()
    strat = out["by_strategy"]["trend_pullback"]
    assert strat["closed"] == 3
    assert strat["wins"] == 2 and strat["losses"] == 1
    assert strat["win_rate"] == round(2 / 3, 3)
    assert strat["total_pnl"] == 55.0


def test_record_outcome_safe_when_no_match(temp_db):
    # Nothing logged for this ticker → record_outcome returns False, no exception.
    assert record_outcome("ZZZZ", 10.0) is False


def test_failing_combos_flagged(temp_db):
    # 6 submitted + closed losing trades for the same strategy/regime.
    for _ in range(6):
        log_decision(_event("XYZ", strategy="weak_strat", regime="choppy"))
        record_outcome("XYZ", -10.0)
    out = insights()
    fails = out["failing_combos"]
    assert fails and fails[0]["combo"] == "weak_strat::choppy"
    assert fails[0]["closed"] >= 5
    assert fails[0]["win_rate"] < 0.40
