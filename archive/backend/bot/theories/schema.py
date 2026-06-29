"""Pydantic v1/v2-compatible schema for theory annotations.

Lightweight dataclasses + ``to_dict`` accessors. We avoid hard-binding
to a particular Pydantic version because the rest of the codebase
mixes Pydantic v1 and v2; this module only needs serialisable shapes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional


PointDict = Dict[str, Any]  # {"ts": iso8601 str, "price": float}


LineKind = Literal[
    "trendline",   # operator-drawn boundary lines between two points
    "ray",         # half-line extending right from a point (Gann fan, etc.)
    "horizontal",  # full-width horizontal level
    "fan",         # part of a Gann fan group; rendered as a ray with a label
    "vertical",    # vertical time-cycle marker
    "cloud_band",  # area between two reference lines (Ichimoku Kumo)
    "histogram",   # MITS Phase 10.2 — bar histogram (MACD hist, etc.) rendered
                   #   as addHistogramSeries() on a sub-panel. ``points``
                   #   carries the bar values; positive bars green, negative red.
    "series",      # MITS Phase 10.1 — N-point continuous time series.
                   #   ONE Line for the whole curve (renders as a
                   #   lightweight-charts ``addLineSeries().setData()``)
                   #   instead of N-1 ``trendline`` segments. Critical for
                   #   moving-band theories (Bollinger, Keltner, Donchian,
                   #   MA Ribbon, AVWAP, ATR bands, Ichimoku, MACD, etc.)
                   #   where the per-bar trendline-emission pattern
                   #   produced 600+ lines on a 250-bar window and froze
                   #   the browser. ``points`` carries the curve;
                   #   ``start`` / ``end`` are filled with the first/last
                   #   data point so non-aware renderers can still draw
                   #   the curve as a single trendline endpoint pair.
]
LineStyle = Literal["solid", "dashed", "dotted"]
MarkerShape = Literal["arrow_up", "arrow_down", "circle", "square", "text"]


@dataclass
class Line:
    kind: LineKind
    start: PointDict
    end: PointDict
    color: str = "#888"
    width: int = 1
    style: LineStyle = "solid"
    label: Optional[str] = None
    # ``meta`` is a structured side-channel so the frontend can colour /
    # sort / cross-link levels without parsing the label string. The
    # canonical keys are theory-specific (e.g. pivots use
    # ``{timeframe, level}``; gann uses ``{ratio, direction,
    # interpretation}``; fibonacci uses ``{pct, kind, significance}``).
    meta: Dict[str, Any] = field(default_factory=dict)
    # MITS Phase 10.1 — when ``kind == "series"`` the renderer reads
    # ``points`` (a list of ``{ts, price}`` dicts) and draws ONE
    # continuous line; otherwise ``points`` is empty and only
    # ``start`` / ``end`` are honoured (legacy trendline shape).
    points: List[PointDict] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Marker:
    ts: str
    price: float
    label: Optional[str] = None
    color: str = "#fff"
    shape: MarkerShape = "circle"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Zone:
    """A coloured rectangle anchored by two points.

    ``x1``/``x2`` are ISO timestamps; ``y1``/``y2`` are prices. Used
    for shaded fan areas, Ichimoku cloud bodies, projected-target
    regions on price-action patterns, etc.
    """
    x1: str
    y1: float
    x2: str
    y2: float
    color: str = "#888"
    opacity: float = 0.18
    label: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# MITS Phase 10 — actionable per-theory signal emitter. Each theory now
# returns a ``signals`` list alongside its lines/markers/zones so the UI
# can draw flag markers (BUY/SELL/CALL/PUT) with a hover popover that
# explains the action in plain English. The backend AI Brain consumes
# the same shape for trade-context injection.
SignalAction = Literal[
    "BUY", "SELL",
    "BUY_CALL", "BUY_PUT",
    "SELL_CALL", "SELL_PUT",
    "BUY_VERTICAL_CALL", "BUY_VERTICAL_PUT",
    "IRON_CONDOR", "STRADDLE",
    "EXIT_LONG", "EXIT_SHORT",
    "WATCH",
]
SignalInstrument = Literal["stock", "call", "put", "spread"]


@dataclass
class Signal:
    """An actionable trade hint emitted by a theory.

    Conforms to MITS-P10 schema:

      * ``action`` — discrete action verb (BUY / SELL / BUY_CALL / …).
      * ``ts`` — ISO timestamp the signal anchors to (the bar that
        triggered it). Frontend draws the flag at this x-coordinate.
      * ``price`` — the price level the action is taken at (entry).
      * ``confidence`` — 0..1 self-reported confidence from the theory.
      * ``reasoning`` — plain-English sentence the operator (a beginner)
        can read without theory background.
      * ``target_price`` / ``stop_loss`` — optional R-multiple anchors.
      * ``instrument`` / ``dte_target`` / ``strike`` — option-leg hints
        when the theory wants to express opinion through an option
        chain rather than the underlying.
      * ``theory_anchor`` — optional theory-specific dict that lets the
        frontend cross-link the signal back to the line/zone that
        produced it (e.g. ``{"level": "R1", "timeframe": "daily"}``).
    """
    action: SignalAction
    ts: str
    price: float
    confidence: float = 0.5
    reasoning: str = ""
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    instrument: SignalInstrument = "stock"
    dte_target: Optional[int] = None
    strike: Optional[float] = None
    theory_anchor: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TheoryAnnotation:
    theory: str
    pattern_name: Optional[str] = None
    confidence: Optional[float] = None
    lines: List[Line] = field(default_factory=list)
    markers: List[Marker] = field(default_factory=list)
    zones: List[Zone] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)
    citation: str = ""
    notes: List[str] = field(default_factory=list)
    # ``primer`` powers the 3-column Theory Primer panel below the chart.
    # Keys: ``what_it_measures`` / ``how_to_read`` / ``key_levels_now``.
    primer: Dict[str, Any] = field(default_factory=dict)
    # ``extras`` carries optional theory-specific callout boxes / overlays
    # the canvas-overlay layer should draw on top of the chart (Price-
    # Action patterns use this to label "Falling resistance", "Sell here",
    # etc.). Each entry is a free-form dict; the frontend ignores entries
    # it doesn't understand.
    extras: List[Dict[str, Any]] = field(default_factory=list)
    # MITS Phase 10 — actionable BUY/SELL/option hints. Each theory's
    # ``analyze()`` populates this list when it sees a trigger condition.
    # Empty list = theory has no actionable view right now.
    signals: List["Signal"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "theory": self.theory,
            "pattern_name": self.pattern_name,
            "confidence": self.confidence,
            "lines": [l.to_dict() for l in self.lines],
            "markers": [m.to_dict() for m in self.markers],
            "zones": [z.to_dict() for z in self.zones],
            "params": self.params,
            "citation": self.citation,
            "notes": list(self.notes),
            "primer": dict(self.primer or {}),
            "extras": list(self.extras or []),
            "signals": [s.to_dict() for s in (self.signals or [])],
        }


# ── shared helpers for theory modules ─────────────────────────────────


def bar_ts(bar: Dict[str, Any]) -> str:
    """Return the ISO timestamp for a bar, defensively."""
    t = bar.get("t") or bar.get("timestamp") or bar.get("date")
    return str(t) if t is not None else ""


def bar_close(bar: Dict[str, Any]) -> float:
    return float(bar.get("close") or 0.0)


def bar_high(bar: Dict[str, Any]) -> float:
    return float(bar.get("high") or 0.0)


def bar_low(bar: Dict[str, Any]) -> float:
    return float(bar.get("low") or 0.0)


def bar_open(bar: Dict[str, Any]) -> float:
    return float(bar.get("open") or 0.0)


def bar_volume(bar: Dict[str, Any]) -> float:
    return float(bar.get("volume") or 0.0)
