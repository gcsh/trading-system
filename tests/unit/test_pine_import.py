"""Pine-script translator unit tests."""
from backend.bot.pine_import import translate_pine


def test_macd_crossover_translates():
    r = translate_pine("longCond = ta.crossover(macdLine, signalLine)")
    assert "buy when macd crosses above signal" in r.rules


def test_macd_crossunder_translates():
    r = translate_pine("shortCond = ta.crossunder(macdLine, signalLine)")
    assert "sell when macd crosses below signal" in r.rules


def test_rsi_thresholds():
    r = translate_pine("x = ta.rsi(close, 14) < 30\ny = ta.rsi(close,14) > 70")
    assert "buy when rsi < 30" in r.rules
    assert "sell when rsi > 70" in r.rules


def test_price_vs_sma():
    r = translate_pine("c = close > ta.sma(close, 50)")
    assert "buy when price above ma50" in r.rules


def test_empty_source():
    r = translate_pine("")
    assert r.rules == []


def test_unrecognized_reported_as_skipped():
    r = translate_pine("strategy.entry('L', strategy.long, when=barstate.isconfirmed)")
    # No supported indicator → reported in skipped.
    assert any("strategy.entry" in s for s in r.skipped)


def test_dedup_repeated_rules():
    r = translate_pine("a = ta.rsi(close,14) < 30\nb = ta.rsi(close,14) < 30")
    assert r.rules.count("buy when rsi < 30") == 1
