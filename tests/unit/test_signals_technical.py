from backend.bot.signals import technical


def test_compute_snapshot_returns_none_for_empty():
    import pandas as pd

    assert technical.compute_snapshot(pd.DataFrame()) is None


def test_compute_snapshot_basic_fields(sample_history):
    snap = technical.compute_snapshot(sample_history)
    assert snap is not None
    assert snap.price > 0
    assert 0 <= snap.rsi <= 100
    assert snap.sma20 > 0


def test_oversold_history_drives_rsi_below_30(oversold_history):
    snap = technical.compute_snapshot(oversold_history)
    assert snap is not None
    assert snap.rsi < 30


def test_overbought_history_drives_rsi_above_70(overbought_history):
    snap = technical.compute_snapshot(overbought_history)
    assert snap is not None
    assert snap.rsi > 70


def test_volume_spike_detected(momentum_history):
    snap = technical.compute_snapshot(momentum_history)
    assert snap is not None
    assert snap.volume_spike >= 1.5
