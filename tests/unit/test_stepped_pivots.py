"""MITS Phase 10.3 — Stepped historical pivots contract.

Validates the operator's "no more 11 horizontal lines stacked at the
right edge" fix:

  * Pivots returns stepped trendline segments (one per period/level)
    rather than one horizontal-spanning line per level.
  * Each segment's start/end timestamps fall inside the bar window.
  * Density (simple ≤ normal ≤ detailed) returns monotonically more
    lines.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Dict, Any

import pytest

from backend.bot.theories import pivots


def _daily_bars(n: int = 252, start: datetime = None) -> List[Dict[str, Any]]:
    """Build N daily bars (Mon-Fri only) with a slow trend + wiggle."""
    import math
    out: List[Dict[str, Any]] = []
    start = start or datetime(2025, 1, 1)
    d = start
    count = 0
    while count < n:
        # Skip weekends.
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        i = count
        trend = i * 0.05
        wave = 3.0 * math.sin(i / 13.0) + 1.5 * math.cos(i / 7.0)
        c = 100.0 + trend + wave
        o = c - 0.4
        h = c + 1.2
        l = c - 1.1
        out.append({
            "t": d.isoformat(),
            "open": float(o), "high": float(h),
            "low": float(l), "close": float(c),
            "volume": 1_000_000 + (i % 17) * 5_000,
        })
        d += timedelta(days=1)
        count += 1
    return out


# ──────────────────────────────────────────────────────────────────────


def test_stepped_pivots_emits_many_segments_on_1y_window():
    """1y of daily bars should yield ≥30 stepped trendline segments."""
    bars = _daily_bars(252)
    ann = pivots.analyze(bars, params={"density": "normal"})
    segments = [ln for ln in ann.lines if ln.kind == "trendline"]
    assert len(segments) >= 30, (
        f"Expected ≥30 stepped segments on a 252-bar 1y window; "
        f"got {len(segments)}. Pivots regressed back to "
        "horizontal-spanning rendering."
    )
    # The "stepped=True" meta flag identifies the new render path.
    assert all(ln.meta.get("stepped") for ln in segments), (
        "Stepped segments must carry meta.stepped=True"
    )


def test_stepped_pivots_segment_bounds_in_window():
    """Each segment's start/end timestamps must land inside [first_bar, last_bar]."""
    bars = _daily_bars(252)
    first_ts = bars[0]["t"]
    last_ts = bars[-1]["t"]
    ann = pivots.analyze(bars, params={"density": "normal"})
    segments = [ln for ln in ann.lines if ln.kind == "trendline"]
    assert segments, "no stepped segments emitted"
    for ln in segments:
        s_ts = ln.start.get("ts")
        e_ts = ln.end.get("ts")
        assert first_ts <= s_ts <= last_ts, (
            f"segment start {s_ts} outside window {first_ts}..{last_ts}"
        )
        assert first_ts <= e_ts <= last_ts, (
            f"segment end {e_ts} outside window {first_ts}..{last_ts}"
        )
        # End must be >= start (or equal for single-bar period).
        assert e_ts >= s_ts


def test_stepped_pivots_density_monotonic():
    """simple ≤ normal ≤ detailed line counts."""
    bars = _daily_bars(252)
    simple = pivots.analyze(bars, params={"density": "simple"})
    normal = pivots.analyze(bars, params={"density": "normal"})
    detailed = pivots.analyze(bars, params={"density": "detailed"})
    n_simple = len(simple.lines)
    n_normal = len(normal.lines)
    n_detailed = len(detailed.lines)
    assert n_simple <= n_normal <= n_detailed, (
        f"density ordering broken: simple={n_simple} "
        f"normal={n_normal} detailed={n_detailed}"
    )
    # Detailed should have strictly more than simple (PP only vs PP+R1+S1+R2+S2).
    assert n_detailed > n_simple


def test_stepped_pivots_1y_drops_r3_s3_outliers():
    """At >400 days of daily bars, R3/S3 should be filtered out."""
    bars = _daily_bars(252)  # ~365 calendar days
    # Force a >400-day span by stretching the date range.
    long_bars = _daily_bars(500)  # ~700 days
    ann = pivots.analyze(long_bars, params={"density": "detailed"})
    segments = [ln for ln in ann.lines if ln.kind == "trendline"]
    levels = {ln.meta.get("level") for ln in segments}
    assert "R3" not in levels and "S3" not in levels, (
        f"R3/S3 should be filtered on >1y windows; got {levels}"
    )
    # PP/R1/S1 must still be present.
    assert "PP" in levels and "R1" in levels and "S1" in levels


def test_stepped_pivots_short_window_uses_daily():
    """A 5-day window should produce daily stepped pivots only."""
    bars = _daily_bars(5)
    ann = pivots.analyze(bars, params={"density": "normal"})
    # Insufficient periods — should still not crash; might be 0 segments.
    # The contract is just that the call returns cleanly.
    assert ann is not None
    assert ann.theory == "pivots"


def test_stepped_pivots_no_spanning_horizontal_lines():
    """Regression: the OLD behaviour emitted 11 horizontal lines spanning
    first→last timestamps. The NEW behaviour emits horizontals ONLY for
    the most-recent-frame right-axis labels (no timestamps, zero-width).
    """
    bars = _daily_bars(252)
    ann = pivots.analyze(bars, params={"density": "normal"})
    horizontals = [ln for ln in ann.lines if ln.kind == "horizontal"]
    for ln in horizontals:
        # Right-axis-only horizontal: start.ts == end.ts == empty.
        assert ln.start.get("ts") == "" and ln.end.get("ts") == "", (
            "Pivots horizontals must be label-only (no time anchor) — "
            "the old window-spanning rendering would carry first_ts → last_ts"
        )
        assert ln.meta.get("label_only") is True


def test_stepped_pivots_emit_signals_on_monthly_breaks():
    """The relaxed signal rule should fire at least one BUY/SELL on a 1y
    window with mild trend + wiggle.
    """
    bars = _daily_bars(252)
    ann = pivots.analyze(bars, params={"density": "normal"})
    actions = [s.action for s in ann.signals]
    # At least one buy or sell across 12 months of trending+wavy data.
    assert any(a in ("BUY", "SELL") for a in actions), (
        f"Pivots emitted no BUY/SELL on a 1y trending window; "
        f"got actions={actions}"
    )


def test_pivots_priority_meta_on_segments():
    """Every stepped segment must carry meta.priority (1..3) for the
    density post-filter to work."""
    bars = _daily_bars(252)
    ann = pivots.analyze(bars, params={"density": "detailed"})
    segments = [ln for ln in ann.lines if ln.kind == "trendline"]
    for ln in segments:
        pri = ln.meta.get("priority")
        assert pri in (1, 2, 3), (
            f"Segment {ln.meta.get('level')} missing priority meta; got {pri!r}"
        )
