from backend.bot.signals import fundamentals


def test_snapshot_parses_yfinance_info():
    info = {
        "trailingPE": "18.5",
        "trailingEps": "6.21",
        "revenueGrowth": "0.12",
        "recommendationKey": "buy",
    }
    snap = fundamentals.snapshot_from_info(info)
    assert snap.pe_ratio == 18.5
    assert snap.eps == 6.21
    assert snap.revenue_growth == 0.12
    assert snap.analyst_recommendation == "buy"
    assert snap.is_attractive is True


def test_snapshot_handles_missing_fields():
    snap = fundamentals.snapshot_from_info({})
    assert snap.pe_ratio is None
    assert snap.is_attractive is False


def test_snapshot_rejects_negative_revenue_growth():
    snap = fundamentals.snapshot_from_info(
        {"trailingPE": "20", "revenueGrowth": "-0.05"}
    )
    assert snap.is_attractive is False


def test_snapshot_rejects_extreme_pe():
    snap = fundamentals.snapshot_from_info({"trailingPE": "200"})
    assert snap.is_attractive is False
