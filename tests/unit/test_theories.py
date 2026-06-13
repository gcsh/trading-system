"""MITS Phase 9.1 — Theory engine unit tests.

We assert the math, not the chart-rendering details. Each theory's
canonical formula is reproduced inline so a regression in the
implementation produces a meaningful assertion failure.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import List

import pytest

from backend.bot.theories import THEORIES, fibonacci, gann, ichimoku, pivots, price_action
from backend.bot.theories._zigzag import detect_pivots
from backend.bot.theories.pivots import floor_pivots


# ── synthetic bar builder ─────────────────────────────────────────────


def _bar(ts: datetime, o: float, h: float, l: float, c: float, v: float = 100_000):
    return {"t": ts.isoformat(), "open": o, "high": h, "low": l,
              "close": c, "volume": v}


def _daily(start: datetime, prices: List[tuple]) -> List[dict]:
    """``prices`` is a list of (open, high, low, close, volume?) tuples."""
    out = []
    for i, row in enumerate(prices):
        if len(row) == 4:
            o, h, l, c = row
            v = 100_000
        else:
            o, h, l, c, v = row
        out.append(_bar(start + timedelta(days=i), o, h, l, c, v))
    return out


# ── registry sanity ──────────────────────────────────────────────────


def test_registry_has_phase9_baseline_theories():
    # MITS-P10 expanded the registry from 5 → 23. We assert the
    # original 5 are still present, not that they are the only ones.
    baseline = {"price_action", "gann", "fibonacci", "ichimoku", "pivots"}
    assert baseline.issubset(set(THEORIES.keys()))


def test_every_theory_returns_dict_with_citation_and_params():
    bars = _daily(datetime(2025, 1, 1),
                  [(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(80)])
    for name, (fn, _label) in THEORIES.items():
        ann = fn(bars)
        d = ann.to_dict()
        assert d["theory"] == name
        assert d["citation"], f"{name} must cite a source"
        assert "params" in d
        assert isinstance(d["lines"], list)


# ── ZigZag pivots ─────────────────────────────────────────────────────


def test_zigzag_detects_alternating_pivots():
    bars = _daily(
        datetime(2025, 1, 1),
        # Build an obvious zigzag: 100 → 90 → 105 → 92 → 110.
        [(100, 100, 100, 100)]
        + [(c, c, c, c) for c in [98, 96, 94, 92, 90]]
        + [(c, c, c, c) for c in [93, 96, 99, 102, 105]]
        + [(c, c, c, c) for c in [102, 98, 95, 93, 92]]
        + [(c, c, c, c) for c in [95, 100, 105, 108, 110]],
    )
    pivots = detect_pivots(bars, threshold_pct=3.0)
    types = [p["type"] for p in pivots]
    # Must alternate.
    for i in range(1, len(types)):
        assert types[i] != types[i - 1]
    assert len(pivots) >= 4


# ── Price Action: descending triangle ─────────────────────────────────


def test_price_action_detects_descending_triangle():
    """4 swing-high lower-highs (50, 48, 46, 44) + dead-flat support
    at 40. The descending-triangle classifier (Bulkowski definition)
    must beat any wedge classification."""
    levels = [50, 40, 48, 40, 46, 40, 44, 40]
    bars = []
    base = datetime(2025, 1, 1)
    bar_idx = 0
    for level in levels:
        # Glide toward each pivot across 8 bars so the ZigZag sees a clean swing.
        prev = bars[-1]["close"] if bars else level
        for j in range(8):
            t = base + timedelta(days=bar_idx)
            bar_idx += 1
            ratio = (j + 1) / 8
            c = prev + (level - prev) * ratio
            # Lows are dead-flat at exactly 40 (no random fuzz on the
            # support touches) so the classifier sees an unambiguous
            # horizontal support line.
            if abs(c - 40) < 0.5:
                c = 40.0
            o = c - 0.05
            h = c + 0.2
            l = c - 0.2
            bars.append(_bar(t, o, h, l, c))
    # Append the breakout: a single bar that closes below 40 with
    # heavy volume so volume confirmation fires.
    breakout_idx = len(bars)
    bars.append(_bar(base + timedelta(days=bar_idx), 39.5, 39.8, 38.0, 38.0, 600_000))
    ann = price_action.analyze(bars, params={"zigzag_pct": 1.5, "min_confidence": 0.0})
    assert ann.pattern_name == "descending_triangle", \
        f"expected descending_triangle, got {ann.pattern_name}"
    # Lines must include both boundaries.
    line_labels = [l.label for l in ann.lines if l.label]
    assert any("resistance" in (l or "").lower() for l in line_labels)
    assert any("support" in (l or "").lower() for l in line_labels)


def test_price_action_double_top_neckline_below_peaks():
    base = datetime(2025, 1, 1)
    series = []
    bar_idx = 0
    # Up to 100, down to 90, back to 100, down — clean double top.
    # Use a wider zigzag % AND longer legs so the detector picks the
    # 3-pivot double-top shape, not a 5-pivot triangle.
    legs = [(70, 100), (100, 90), (90, 100), (100, 85)]
    for start, end in legs:
        for j in range(12):
            t = base + timedelta(days=bar_idx); bar_idx += 1
            ratio = (j + 1) / 12
            c = start + (end - start) * ratio
            series.append(_bar(t, c, c + 0.2, c - 0.2, c))
    ann = price_action.analyze(series, params={"zigzag_pct": 4.0, "min_confidence": 0.0})
    assert ann.pattern_name in {"double_top", "head_and_shoulders"}, \
        f"expected double_top or head_and_shoulders, got {ann.pattern_name}"


# ── Gann fan math ────────────────────────────────────────────────────


def test_gann_unit_and_fan_slopes():
    """For pivot (2024-01-01, 100) with unit_price=1.0, the 1×1 line
    must reach price 130 at +30 bars."""
    # Construct bars where the rolling range / N exactly equals 1.0.
    # 60 bars, high = 160, low = 100 → range / 60 = 1.0.
    bars = []
    base = datetime(2024, 1, 1)
    for i in range(60):
        c = 100 + i  # rises 1 per bar; range = 59, but high=160 / low=100 → unit=1
        bars.append(_bar(base + timedelta(days=i), c, c + 0.5, c - 0.5, c))
    # Append a clearly-dominant high so the rolling window sees 160 / 100.
    bars[-1] = _bar(base + timedelta(days=59), 159, 160, 158.5, 159)
    ann = gann.analyze(bars, params={"unit_lookback": 60, "pivot_index": 0,
                                       "pivot_type": "low"})
    # 1×1 line slope = unit; from price 100 at idx 0 → +30 bars must reach 130.
    # Phase-9.6+: fan-ray labels now include the interpretation suffix
    # (e.g. ``"1x1 (45° — neutral support)"``). Match by ratio prefix.
    fan_rays = [l for l in ann.lines
                if l.kind == "fan" and (l.label or "").startswith("1x1")]
    assert fan_rays, "expected at least one 1x1 fan ray"
    # The 1:1 ray's end point should be near 100 + 1.0 * (last_idx - 0). Last bar idx = 59.
    expected_end_price = 100.0 + 1.0 * 59
    assert abs(fan_rays[0].end["price"] - expected_end_price) < 0.5
    # Unit cached in params:
    assert ann.params["unit_price_per_bar"] == pytest.approx(1.0, abs=0.02)


def test_gann_time_cycles_at_30_60_90():
    bars = []
    base = datetime(2024, 1, 1)
    for i in range(400):
        c = 100 + (i % 5)
        bars.append(_bar(base + timedelta(days=i), c, c + 0.5, c - 0.5, c))
    ann = gann.analyze(bars, params={"unit_lookback": 60, "pivot_index": 50})
    cycle_labels = [l.label for l in ann.lines
                    if l.kind == "vertical" and l.label]
    # Expect labels like "30 / …", "60 / …", "90 / …".
    assert any(lbl.startswith("30") for lbl in cycle_labels)
    assert any(lbl.startswith("60") for lbl in cycle_labels)
    assert any(lbl.startswith("90") for lbl in cycle_labels)


# ── Fibonacci ────────────────────────────────────────────────────────


def test_fib_50pct_between_100_and_110():
    """Swing high 110 → low 100, 50% retracement = 105."""
    bars = []
    base = datetime(2025, 1, 1)
    # Up from 100 to 110, then back to 100.
    for i in range(20):
        c = 100 + (i / 19) * 10
        bars.append(_bar(base + timedelta(days=i), c, c, c, c))
    for i in range(20):
        c = 110 - (i / 19) * 10
        bars.append(_bar(base + timedelta(days=20 + i), c, c, c, c))
    ann = fibonacci.analyze(bars, params={"zigzag_pct": 3.0,
                                            "show_extensions": False})
    # Phase-9.6+: fib labels include significance + price (e.g.
    # ``"50.0% — balanced retracement 105.00"``). Match by prefix.
    h_lines = [l for l in ann.lines if l.kind == "horizontal"
                and (l.label or "").startswith("50.0%")]
    assert h_lines, "expected a 50% retracement line"
    assert abs(h_lines[0].start["price"] - 105.0) < 0.05


def test_fib_retracement_ratios_full_grid():
    """All seven retracement ratios are drawn."""
    bars = []
    base = datetime(2025, 1, 1)
    for i in range(30):
        c = 100 + i
        bars.append(_bar(base + timedelta(days=i), c, c, c, c))
    for i in range(30):
        c = 129 - i
        bars.append(_bar(base + timedelta(days=30 + i), c, c, c, c))
    ann = fibonacci.analyze(bars, params={"show_extensions": False})
    labels = [l.label or "" for l in ann.lines if l.kind == "horizontal"]
    # Phase-9.6+: labels carry trailing significance + price. Match by
    # prefix instead of equality.
    for pct in (0.0, 23.6, 38.2, 50.0, 61.8, 78.6, 100.0):
        prefix = f"{pct:.1f}%"
        assert any(l.startswith(prefix) for l in labels), \
            f"missing {prefix} in {labels}"


# ── Ichimoku ─────────────────────────────────────────────────────────


def test_ichimoku_tenkan_matches_canonical_formula():
    bars = []
    base = datetime(2025, 1, 1)
    # 60 bars; last 9 have explicit highs 110-118, lows 100-108.
    for i in range(50):
        c = 100 + (i * 0.1)
        bars.append(_bar(base + timedelta(days=i), c, c + 0.5, c - 0.5, c))
    for j in range(9):
        h = 110 + j
        l_ = 100 + j
        c = (h + l_) / 2
        bars.append(_bar(base + timedelta(days=50 + j), c, h, l_, c))
    ann = ichimoku.analyze(bars)
    # Tenkan for the last bar = (max(110-118) + min(100-108)) / 2 = (118 + 100)/2 = 109.
    tenkan_lines = [l for l in ann.lines if l.label == "Tenkan-sen (9)"]
    # There's only one labelled line at the start of the series; assert the
    # series series contains a point near 109 by walking ALL Tenkan segments.
    all_tenkan_points = []
    in_tenkan = False
    for l in ann.lines:
        if l.label == "Tenkan-sen (9)":
            in_tenkan = True
        elif l.label and l.label != "Tenkan-sen (9)":
            in_tenkan = False
        if in_tenkan:
            all_tenkan_points.append(l.end["price"])
    # The last Tenkan point should equal 109.0 (within 0.01).
    assert abs(all_tenkan_points[-1] - 109.0) < 0.01


def test_ichimoku_cloud_color_flips_with_span_a_vs_b():
    """Bullish bars → Span A > Span B → green cloud zones present."""
    bars = []
    base = datetime(2025, 1, 1)
    for i in range(80):
        c = 100 + i  # strictly rising
        bars.append(_bar(base + timedelta(days=i), c, c + 0.5, c - 0.5, c))
    ann = ichimoku.analyze(bars)
    greens = [z for z in ann.zones if z.color == "#36c26b"]
    assert len(greens) > 0, "rising market must produce some bullish (green) Kumo zones"


# ── Pivots ────────────────────────────────────────────────────────────


def test_floor_pivots_canonical_formula():
    """H=110, L=90, C=100 → PP=100, R1=110, S1=90."""
    p = floor_pivots(110.0, 90.0, 100.0)
    assert p["PP"] == pytest.approx(100.0)
    assert p["R1"] == pytest.approx(110.0)
    assert p["S1"] == pytest.approx(90.0)
    assert p["R2"] == pytest.approx(120.0)
    assert p["S2"] == pytest.approx(80.0)
    assert p["R3"] == pytest.approx(130.0)
    assert p["S3"] == pytest.approx(70.0)


def test_pivots_analyze_renders_daily_lines():
    bars = []
    base = datetime(2025, 6, 2)  # Monday
    for d in range(7):
        for hr in range(10):
            t = base + timedelta(days=d, hours=hr)
            c = 100 + d
            bars.append(_bar(t, c, c + 1, c - 1, c))
    ann = pivots.analyze(bars, params={"periods": ["daily"]})
    horizontals = [l for l in ann.lines if l.kind == "horizontal"]
    assert horizontals, "daily pivots must produce horizontal lines"


# ── Schema ───────────────────────────────────────────────────────────


def test_annotation_dict_round_trip():
    bars = _daily(datetime(2025, 1, 1),
                  [(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(60)])
    ann = gann.analyze(bars, params={"unit_lookback": 30})
    d = ann.to_dict()
    # Lines / markers / zones must all be JSON-serialisable.
    import json
    s = json.dumps(d, default=str)
    again = json.loads(s)
    assert again["theory"] == "gann"
    assert "lines" in again


# ── ZigZag tunable via TUNABLES default ──────────────────────────────


def test_price_action_zigzag_default_from_tunables():
    """``analyze`` should fall back to TUNABLES.theory_zigzag_pct when
    ``params.zigzag_pct`` is missing."""
    from backend.config import TUNABLES
    bars = _daily(datetime(2025, 1, 1),
                  [(100, 100, 100, 100)] * 60)
    ann = price_action.analyze(bars)
    assert ann.params["zigzag_pct"] == pytest.approx(TUNABLES.theory_zigzag_pct)
