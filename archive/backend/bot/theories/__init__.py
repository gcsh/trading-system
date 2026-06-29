"""MITS Phase 9.1 + Phase 10 — Theory engine.

A registry of self-contained "theory" modules. Each one consumes a
list of OHLCV bars and emits a ``TheoryAnnotation`` (lines, markers,
zones, signals) that the editable Theory Studio chart can render.

Phase 9 baseline (5 theories): price_action, gann, fibonacci, ichimoku,
pivots.

Phase 10 extension (18 new theories — 23 total):

  Tier 1 — formulaic:
    bollinger, donchian, keltner, ma_ribbon, avwap, rsi_divergence,
    macd_signal, stochastic, atr_bands, murrey_math.
  Tier 2 — geometric:
    andrews_pitchfork, square_of_9, volume_profile.
  Tier 3 — pattern-heavy (confidence-flag):
    harmonic_patterns, elliott_wave, wyckoff_phases, smc_order_blocks,
    fair_value_gaps.

Each module exposes ``analyze(bars, params=None) -> TheoryAnnotation``.
Math sources cited in each module's docstring. Each theory now also
emits a ``signals`` list (BUY / SELL / BUY_CALL / WATCH / etc.) that
the frontend renders as on-chart flag markers (see Signal schema in
``schema.py``).
"""
from __future__ import annotations

from . import (
    # Phase 9 baseline.
    fibonacci, gann, ichimoku, pivots, price_action,
    # Phase 10 Tier 1 — formulaic.
    bollinger, donchian, keltner, ma_ribbon, avwap,
    rsi_divergence, macd_signal, stochastic, atr_bands, murrey_math,
    # Phase 10 Tier 2 — geometric.
    andrews_pitchfork, square_of_9, volume_profile,
    # Phase 10 Tier 3 — pattern-heavy (confidence-flag).
    harmonic_patterns, elliott_wave, wyckoff_phases,
    smc_order_blocks, fair_value_gaps,
)
from .schema import Line, Marker, Signal, TheoryAnnotation, Zone


THEORIES = {
    # ── Phase 9 baseline ───────────────────────────────────────────
    "price_action":      (price_action.analyze,      "Price Action Patterns"),
    "gann":              (gann.analyze,              "Gann Fans + Time Cycles"),
    "fibonacci":         (fibonacci.analyze,         "Fibonacci Retracements + Extensions"),
    "ichimoku":          (ichimoku.analyze,          "Ichimoku Cloud"),
    "pivots":            (pivots.analyze,            "Pivot Points (Floor)"),
    # ── Phase 10 Tier 1 — formulaic ────────────────────────────────
    "bollinger":         (bollinger.analyze,         "Bollinger Bands + Squeeze"),
    "donchian":          (donchian.analyze,          "Donchian Channels (Turtle)"),
    "keltner":           (keltner.analyze,           "Keltner Channels (ATR)"),
    "ma_ribbon":         (ma_ribbon.analyze,         "Fibonacci EMA Ribbon (Guppy)"),
    "avwap":             (avwap.analyze,             "Anchored VWAP (Shannon)"),
    "rsi_divergence":    (rsi_divergence.analyze,    "RSI Divergence (Wilder)"),
    "macd_signal":       (macd_signal.analyze,       "MACD (Appel)"),
    "stochastic":        (stochastic.analyze,        "Stochastic %K %D (Lane)"),
    "atr_bands":         (atr_bands.analyze,         "ATR Price Bands (Wilder)"),
    "murrey_math":       (murrey_math.analyze,       "Murrey Math 1/8 Levels"),
    # ── Phase 10 Tier 2 — geometric ────────────────────────────────
    "andrews_pitchfork": (andrews_pitchfork.analyze, "Andrews Median Line (Pitchfork)"),
    "square_of_9":       (square_of_9.analyze,       "Gann Square of 9 Harmonics"),
    "volume_profile":    (volume_profile.analyze,    "Volume Profile (POC / Value Area)"),
    # ── Phase 10 Tier 3 — pattern-heavy ────────────────────────────
    "harmonic_patterns": (harmonic_patterns.analyze, "Harmonic Patterns (Carney)"),
    "elliott_wave":      (elliott_wave.analyze,      "Elliott Wave (Frost & Prechter)"),
    "wyckoff_phases":    (wyckoff_phases.analyze,    "Wyckoff Phases A–E"),
    "smc_order_blocks":  (smc_order_blocks.analyze,  "SMC Order Blocks (ICT)"),
    "fair_value_gaps":   (fair_value_gaps.analyze,   "Fair Value Gaps (ICT)"),
}


__all__ = [
    "THEORIES",
    "TheoryAnnotation",
    "Line",
    "Marker",
    "Signal",
    "Zone",
    "price_action", "gann", "fibonacci", "ichimoku", "pivots",
    "bollinger", "donchian", "keltner", "ma_ribbon", "avwap",
    "rsi_divergence", "macd_signal", "stochastic", "atr_bands", "murrey_math",
    "andrews_pitchfork", "square_of_9", "volume_profile",
    "harmonic_patterns", "elliott_wave", "wyckoff_phases",
    "smc_order_blocks", "fair_value_gaps",
]
