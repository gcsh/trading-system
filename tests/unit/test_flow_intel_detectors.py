"""MITS Phase 5 (P5.4) — flow-intel detector tests."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.bot.detectors import DETECTOR_REGISTRY
from backend.bot.detectors.flow_intel import (
    CallBlockBuyDetector,
    CallSweepUnusualDetector,
    DarkPoolCallLeanDetector,
    DarkPoolPutLeanDetector,
    FLOW_PATTERNS,
    PutBlockBuyDetector,
    PutSweepUnusualDetector,
    build_flow_intel_detectors,
)


def _alert(sentiment="bullish", trade_type="sweep",
              premium=500_000.0, urgency_score=0.85, session="open"):
    return dict(
        ticker="AAPL", sentiment=sentiment, trade_type=trade_type,
        premium=premium, urgency_score=urgency_score, session=session,
        strike=150.0, option_type="call",
    )


def _bars() -> pd.DataFrame:
    idx = pd.date_range("2026-06-02", periods=10, freq="D")
    return pd.DataFrame({
        "open": [100.0] * 10, "high": [101.0] * 10,
        "low": [99.0] * 10, "close": [100.5] * 10,
        "volume": [1_000_000.0] * 10,
    }, index=idx)


def test_all_flow_patterns_registered():
    for pat in FLOW_PATTERNS:
        assert pat in DETECTOR_REGISTRY, f"{pat} not registered"
        assert DETECTOR_REGISTRY[pat].family == "flow_intel"


def test_call_sweep_fires_when_premium_and_urgency_clear_floors():
    det = CallSweepUnusualDetector()
    bars = _bars()
    alerts = [
        _alert(sentiment="bullish", trade_type="sweep",
               premium=600_000.0, urgency_score=0.85),
    ]
    out = det.detect("AAPL", bars, alerts=alerts)
    assert len(out) == 1
    assert out[0].pattern == "flow_call_sweep_unusual"
    assert out[0].timeframe == "1d"
    assert out[0].features["n_alerts"] == 1


def test_call_sweep_skips_when_premium_below_floor():
    det = CallSweepUnusualDetector()
    bars = _bars()
    alerts = [
        _alert(sentiment="bullish", premium=10_000.0, urgency_score=0.90),
    ]
    out = det.detect("AAPL", bars, alerts=alerts)
    assert out == []


def test_call_sweep_skips_when_urgency_below_floor():
    det = CallSweepUnusualDetector()
    bars = _bars()
    alerts = [
        _alert(sentiment="bullish", premium=600_000.0, urgency_score=0.1),
    ]
    out = det.detect("AAPL", bars, alerts=alerts)
    assert out == []


def test_put_sweep_independent_from_call_sweep():
    det = PutSweepUnusualDetector()
    bars = _bars()
    alerts = [
        _alert(sentiment="bearish", trade_type="sweep",
               premium=600_000.0, urgency_score=0.85),
    ]
    out = det.detect("AAPL", bars, alerts=alerts)
    assert len(out) == 1
    assert out[0].pattern == "flow_put_sweep_unusual"


def test_call_block_buy_recognises_block_type():
    det = CallBlockBuyDetector()
    bars = _bars()
    alerts = [
        _alert(sentiment="bullish", trade_type="block",
               premium=1_500_000.0, urgency_score=0.5),
    ]
    out = det.detect("AAPL", bars, alerts=alerts)
    assert len(out) == 1
    assert out[0].pattern == "flow_call_block_buy"


def test_block_buy_promotes_huge_sweeps():
    det = CallBlockBuyDetector()
    bars = _bars()
    alerts = [
        _alert(sentiment="bullish", trade_type="sweep",
               premium=1_500_000.0, urgency_score=0.85),
    ]
    out = det.detect("AAPL", bars, alerts=alerts)
    assert len(out) == 1


def test_put_block_buy_only_fires_on_bearish_alerts():
    det = PutBlockBuyDetector()
    bars = _bars()
    bullish_block = [
        _alert(sentiment="bullish", trade_type="block",
               premium=2_000_000.0, urgency_score=0.5),
    ]
    assert det.detect("AAPL", bars, alerts=bullish_block) == []
    bearish_block = [
        dict(_alert(sentiment="bearish", trade_type="block",
                       premium=2_000_000.0, urgency_score=0.5),
             option_type="put")
    ]
    out = det.detect("AAPL", bars, alerts=bearish_block)
    assert len(out) == 1


def test_dark_pool_call_lean_requires_sweeps_and_darkpool():
    det = DarkPoolCallLeanDetector()
    bars = _bars()
    only_sweeps = [
        _alert(sentiment="bullish", trade_type="sweep",
               premium=400_000.0, urgency_score=0.85),
    ]
    assert det.detect("AAPL", bars, alerts=only_sweeps) == []
    paired = only_sweeps + [
        _alert(sentiment=None, trade_type="darkpool",
               premium=1_500_000.0, urgency_score=0.0),
    ]
    out = det.detect("AAPL", bars, alerts=paired)
    assert len(out) == 1
    assert out[0].pattern == "flow_dark_pool_call_lean"
    assert out[0].features["sweep_count"] == 1
    assert out[0].features["darkpool_count"] == 1


def test_dark_pool_put_lean_mirror():
    det = DarkPoolPutLeanDetector()
    bars = _bars()
    paired = [
        _alert(sentiment="bearish", trade_type="sweep",
               premium=400_000.0, urgency_score=0.85),
        _alert(sentiment=None, trade_type="darkpool",
               premium=1_500_000.0, urgency_score=0.0),
    ]
    out = det.detect("AAPL", bars, alerts=paired)
    assert len(out) == 1
    assert out[0].pattern == "flow_dark_pool_put_lean"


def test_build_flow_intel_detectors_returns_six():
    out = build_flow_intel_detectors()
    assert {d.pattern for d in out} == set(FLOW_PATTERNS)


def test_flow_pattern_descriptions_present():
    for pat in FLOW_PATTERNS:
        det = DETECTOR_REGISTRY[pat]
        assert det.description, f"{pat} missing description"
