"""MITS Phase 3 — detect_all skips operator-disabled detectors."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from typing import List

import pandas as pd
import pytest

from backend.bot.detectors import (
    DETECTOR_REGISTRY, clear_detector_config_cache, detect_all,
    disabled_patterns,
)
from backend.bot.detectors.base import Detector, Observation
from backend.db import init_db, session_scope
from backend.models.detector_config import DetectorConfig


pytestmark = [pytest.mark.unit]


@pytest.fixture
def fresh_db():
    import backend.db as db_mod
    prev_engine = db_mod._engine
    prev_session = db_mod._SessionLocal
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(path)
    clear_detector_config_cache()
    try:
        yield
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass
        clear_detector_config_cache()


class _FakeDetector(Detector):
    """Always fires once on the last bar."""
    pattern = "_test_fake_always_fires"
    family = "candlesticks"
    description = "test detector"

    def detect(self, ticker: str, bars, **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 5:
            return []
        return [Observation(
            ticker=ticker,
            pattern=self.pattern,
            timestamp=bars.index[-1].to_pydatetime() if hasattr(bars.index[-1], "to_pydatetime") else datetime.now(),
            spot=float(bars["close"].iloc[-1]),
        )]


def _bars():
    idx = pd.date_range("2026-01-01", periods=30, freq="D")
    return pd.DataFrame({
        "open": [100.0] * 30,
        "high": [101.0] * 30,
        "low": [99.0] * 30,
        "close": [100.5] * 30,
        "volume": [1000.0] * 30,
    }, index=idx)


def test_detect_all_fires_fake_when_enabled(fresh_db):
    """Sanity: with the fake detector registered and no config row,
    detect_all runs it (defaults to enabled)."""
    DETECTOR_REGISTRY[_FakeDetector.pattern] = _FakeDetector()
    try:
        clear_detector_config_cache()
        out = detect_all("SPY", _bars())
        assert any(o.pattern == _FakeDetector.pattern for o in out)
    finally:
        DETECTOR_REGISTRY.pop(_FakeDetector.pattern, None)


def test_detect_all_skips_disabled_detector(fresh_db):
    """Persist enabled=False on the fake detector. detect_all must skip it."""
    DETECTOR_REGISTRY[_FakeDetector.pattern] = _FakeDetector()
    try:
        with session_scope() as s:
            s.add(DetectorConfig(name=_FakeDetector.pattern, enabled=False))
        clear_detector_config_cache()
        out = detect_all("SPY", _bars())
        assert not any(o.pattern == _FakeDetector.pattern for o in out)
        # And the cached disabled set surfaces it.
        assert _FakeDetector.pattern in disabled_patterns()
    finally:
        DETECTOR_REGISTRY.pop(_FakeDetector.pattern, None)


def test_disabled_patterns_set_excludes_enabled(fresh_db):
    """A detector with enabled=True is NOT in the disabled set."""
    DETECTOR_REGISTRY[_FakeDetector.pattern] = _FakeDetector()
    try:
        with session_scope() as s:
            s.add(DetectorConfig(name=_FakeDetector.pattern, enabled=True))
        clear_detector_config_cache()
        assert _FakeDetector.pattern not in disabled_patterns()
    finally:
        DETECTOR_REGISTRY.pop(_FakeDetector.pattern, None)


def test_detect_all_passes_param_overrides(fresh_db):
    """Operator-set params land in the `params` kwarg passed to detect()."""

    captured = {}

    class _CapturingDet(Detector):
        pattern = "_test_capturing"
        family = "candlesticks"

        def default_params(self):
            return {"alpha": 0.5}

        def detect(self, ticker, bars, **kwargs):
            captured["params"] = kwargs.get("params")
            return []

    DETECTOR_REGISTRY[_CapturingDet.pattern] = _CapturingDet()
    try:
        with session_scope() as s:
            s.add(DetectorConfig(
                name=_CapturingDet.pattern, enabled=True,
                params_json=json.dumps({"alpha": 0.9, "beta": 7}),
            ))
        clear_detector_config_cache()
        detect_all("SPY", _bars())
        # Both default + override are merged, override wins.
        assert captured["params"]["alpha"] == 0.9
        assert captured["params"]["beta"] == 7
    finally:
        DETECTOR_REGISTRY.pop(_CapturingDet.pattern, None)
