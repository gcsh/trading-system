"""Portfolio Intelligence — pure tests over realistic position lists."""
from backend.bot.portfolio_intel import assess_portfolio, beta_of, sector_of, themes_for


def _stock(ticker, qty=10, price=100.0):
    return {"ticker": ticker, "kind": "stock", "quantity": qty,
            "avg_cost": price, "current_price": price, "market_value": qty * price}


def test_empty_positions_return_zero_baseline():
    r = assess_portfolio([])
    assert r.positions_count == 0
    assert r.total_market_value == 0.0
    assert r.macro_risk == "LOW"
    assert r.concentration_flags == []


def test_single_position_flags_concentration():
    r = assess_portfolio([_stock("NVDA", qty=10, price=200)])
    assert r.biggest_position["ticker"] == "NVDA"
    assert r.biggest_position["pct"] == 1.0
    assert r.macro_risk == "HIGH"
    assert any("NVDA single-name" in f for f in r.concentration_flags)
    assert r.diversification == 0.0      # 1 - HHI(1²) = 0


def test_ai_infrastructure_theme_detected_and_clustered():
    positions = [_stock("NVDA", 10, 200), _stock("AMD", 10, 150),
                 _stock("SMCI", 5, 600), _stock("AVGO", 2, 1500)]
    r = assess_portfolio(positions)
    # Single-sector tech book → top sector dominates and macro risk lifts.
    assert r.top_sector == "Semis" and r.top_sector_pct > 0.9
    assert r.top_theme in ("AI infrastructure", "Semis")
    # Correlation cluster surfaces the overlap.
    clusters = {c["label"]: set(c["tickers"]) for c in r.correlation_clusters}
    assert "AI infrastructure" in clusters
    assert {"NVDA", "AMD", "SMCI"}.issubset(clusters["AI infrastructure"])
    assert r.macro_risk in ("HIGH", "MODERATE")


def test_net_beta_is_value_weighted():
    # SPY (β 1.0) $5k + NVDA (β 1.7) $5k → ≈ 1.35
    positions = [_stock("SPY", 10, 500), _stock("NVDA", 25, 200)]
    r = assess_portfolio(positions)
    assert 1.30 <= r.net_beta <= 1.40
    assert r.net_delta == r.total_market_value         # long stocks → delta == value


def test_diversified_book_lowers_macro_risk():
    positions = [_stock("AAPL", 5, 200), _stock("JPM", 10, 150),
                 _stock("XOM", 10, 110), _stock("JNJ", 8, 160),
                 _stock("WMT", 10, 90)]
    r = assess_portfolio(positions)
    assert r.diversification > 0.7
    assert r.macro_risk == "LOW"
    assert r.top_sector_pct < 0.4


def test_options_and_stocks_break_down_by_kind():
    positions = [
        _stock("AAPL", 10, 180),
        {"ticker": "TSLA", "kind": "option", "option_type": "call",
         "quantity": 1, "avg_cost": 500.0},
    ]
    r = assess_portfolio(positions)
    assert "stock" in r.by_kind and "option" in r.by_kind
    # Call adds +0.5 × its value to net delta; stock adds full value.
    assert r.net_delta < r.total_market_value


def test_helpers_round_trip_known_and_unknown_tickers():
    assert sector_of("NVDA") == "Semis"
    assert sector_of("Z9-ZZ") == "Other"
    assert beta_of("NVDA") > 1.0
    assert beta_of("UNKNOWN") == 1.0
    assert "Mag7" in themes_for("AAPL")
    assert themes_for("RANDOM") == []
