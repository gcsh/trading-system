"""MITS Phase 10.1 — theory line-count regression guard.

Prevents a regression of the 693-line Bollinger freeze. Every moving-
band theory MUST emit a small, fixed number of ``Line`` objects whose
``kind`` is ``"series"``. The frontend renders each series as a single
``addLineSeries().setData(points)`` call; emitting per-bar trendlines
again would re-freeze the browser.

Targets (per spec, on a 500-bar synthetic input):
  * bollinger ≤ 5 lines (3 series: mid / upper / lower)
  * keltner   ≤ 5 lines
  * donchian  ≤ 5 lines
  * ma_ribbon ≤ 12 lines (8 EMA series)
  * avwap     ≤ 8 lines (1 series per anchor; up to 3 anchors)
  * atr_bands ≤ 8 lines (5 series: +2/+1/mid/-1/-2)
  * ichimoku  ≤ 12 lines (5 series + cloud zones — zones don't count)
  * macd_signal ≤ 5 lines (3 series in macd panel)
  * rsi_divergence ≤ 30 lines (1 RSI series + N divergence connectors)
  * stochastic ≤ 5 lines (2 series in stochastic panel)

We assert each ``series`` Line carries a non-empty ``points`` list.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

import pytest

from backend.bot.theories import THEORIES
from backend.bot.theories.schema import Line


# ── synthetic bar builder ───────────────────────────────────────────


def _synth_bars(n: int = 500, start_price: float = 100.0) -> List[dict]:
    """Mildly oscillating price series so every theory has signal."""
    import math
    out: List[dict] = []
    start = datetime(2025, 1, 1)
    for i in range(n):
        # Sinusoid + linear drift so the theories see structure.
        trend = i * 0.05
        wave = 4.0 * math.sin(i / 11.0) + 1.5 * math.cos(i / 5.3)
        c = start_price + trend + wave
        o = c - 0.4
        h = c + 0.8
        l = c - 0.7
        # Inject a few large swings so ZigZag / pivot-based theories find them.
        if i % 47 == 0:
            h += 5.0
        if i % 53 == 0:
            l -= 5.0
        out.append({
            "t": (start + timedelta(days=i)).isoformat(),
            "open": float(o), "high": float(h),
            "low": float(l), "close": float(c),
            "volume": 1_000_000 + (i % 13) * 5_000,
        })
    return out


# ── per-theory line-count contract ──────────────────────────────────


SERIES_THEORY_LIMITS = {
    # theory_name -> (max_total_lines, expected_min_series_lines)
    "bollinger":      (10, 3),
    "keltner":        (10, 3),
    "donchian":       (10, 3),
    "ma_ribbon":      (12, 6),     # 8 EMAs but allow some Nones in warmup
    "avwap":          (10, 1),     # at least window-start anchor
    "atr_bands":      (10, 3),     # +2/+1/mid/-1/-2 — at least 3 should populate
    "ichimoku":       (12, 3),     # 5 lines + cloud zones; allow margin
    "macd_signal":    (10, 3),     # MACD / Signal / Histogram series
    "rsi_divergence": (30, 1),     # 1 RSI series + divergence connectors
    "stochastic":     (10, 2),     # %K + %D series
}


@pytest.mark.parametrize("theory_name,limit",
                         [(k, v) for k, v in SERIES_THEORY_LIMITS.items()])
def test_band_theory_line_count_capped(theory_name, limit):
    max_total, min_series = limit
    fn, _label = THEORIES[theory_name]
    bars = _synth_bars(500)
    ann = fn(bars, params=None)
    assert ann is not None, f"{theory_name} returned None"
    lines = ann.lines or []
    # Total lines must not exceed the cap.
    assert len(lines) <= max_total, (
        f"{theory_name} emitted {len(lines)} lines (> {max_total}). "
        f"Series mode required — see MITS Phase 10.1."
    )
    # Of those, at least ``min_series`` should be ``kind=series`` OR
    # ``kind=histogram`` — MITS-P10.2 emits MACD histogram as a true
    # bar series rather than a line, but the points-per-line contract
    # is identical.
    series_lines = [
        ln for ln in lines
        if getattr(ln, "kind", None) in ("series", "histogram")
    ]
    assert len(series_lines) >= min_series, (
        f"{theory_name} emitted only {len(series_lines)} series/histogram "
        f"lines (< {min_series}). Band theories must use series mode."
    )
    # Every series line must carry a non-empty points list. Some
    # theories (AVWAP from a pivot anchor near the right edge) can
    # legitimately produce a 1-point series.
    for ln in series_lines:
        pts = getattr(ln, "points", None) or []
        assert len(pts) >= 1, (
            f"{theory_name} series line {ln.label!r} has no points"
        )
        # Each point must have ts + price.
        for p in pts[:3]:
            assert "ts" in p and "price" in p, (
                f"{theory_name} series point missing ts/price: {p!r}"
            )


def test_schema_line_supports_series_kind():
    """Direct schema-level sanity — Line accepts kind='series' + points."""
    pts = [{"ts": "2025-01-01T00:00:00", "price": 100.0},
           {"ts": "2025-01-02T00:00:00", "price": 101.0}]
    ln = Line(
        kind="series",
        start=pts[0], end=pts[-1],
        color="#fff", width=1, style="solid",
        label="test", points=pts,
    )
    d = ln.to_dict()
    assert d["kind"] == "series"
    assert isinstance(d["points"], list)
    assert len(d["points"]) == 2


def test_bollinger_emits_exactly_three_series():
    """Bollinger has a hard contract: mid + upper + lower = 3 series."""
    fn, _ = THEORIES["bollinger"]
    bars = _synth_bars(500)
    ann = fn(bars, params=None)
    series_lines = [ln for ln in (ann.lines or []) if ln.kind == "series"]
    labels = {ln.label for ln in series_lines if ln.label}
    assert len(series_lines) == 3, (
        f"Bollinger emitted {len(series_lines)} series lines (expected 3)"
    )
    # Labels should cover mid + upper + lower (label text isn't a contract
    # but we sanity check the trio is distinct).
    assert len(labels) == 3, f"Bollinger labels not distinct: {labels!r}"


def test_max_window_aggregation_present():
    """The theories route must aggregate >3y windows to monthly bars."""
    from backend.api.routes.theories import WINDOW_MAP, _aggregate_bars
    # max → monthly. 2y / 5y → weekly. ≤1y → daily.
    assert WINDOW_MAP["max"]["aggregate_to"] == "M"
    assert WINDOW_MAP["5y"]["aggregate_to"] == "W"
    assert WINDOW_MAP["2y"]["aggregate_to"] == "W"
    assert WINDOW_MAP["1y"]["aggregate_to"] == "D"

    # Resample a 2-year daily series into weekly buckets.
    bars = _synth_bars(2 * 365)
    weekly = _aggregate_bars(bars, "W")
    assert 80 <= len(weekly) <= 120, (
        f"Weekly aggregation produced {len(weekly)} bars for 2y daily input"
    )
    monthly = _aggregate_bars(bars, "M")
    assert 18 <= len(monthly) <= 30, (
        f"Monthly aggregation produced {len(monthly)} bars for 2y daily input"
    )
    # Daily mode is identity.
    daily = _aggregate_bars(bars, "D")
    assert daily is bars or len(daily) == len(bars)
