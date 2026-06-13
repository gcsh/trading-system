"""Multi-Timeframe Confluence Engine.

Scores how well a setup lines up across timeframes (higher timeframes weighted
more). The scorer is pure — it takes a {timeframe: trend} map — so it's fully
testable. ``confluence_for(ticker)`` is the optional, cached, best-effort fetcher
that derives those trends from real candles; it does network I/O so it's meant to
be called on demand (an endpoint / the analytics page), NOT inside the hot loop.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

# Higher timeframes carry more weight in the alignment score.
_TF_WEIGHTS = {"5m": 0.5, "15m": 0.7, "30m": 0.8, "1h": 1.0, "4h": 1.3, "daily": 1.6, "weekly": 1.8}
_CACHE: Dict[str, tuple] = {}
_TTL = 300.0


@dataclass
class ConfluenceScore:
    bullish_alignment: float = 0.0        # 0-1, weighted share of bullish TFs
    bearish_alignment: float = 0.0        # 0-1
    score: float = 0.0                    # max(bull, bear) — overall agreement strength
    direction: str = "neutral"            # bullish | bearish | mixed | neutral
    dominant_tf: Optional[str] = None
    conflicting_timeframes: List[str] = field(default_factory=list)
    timeframes: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def score_confluence(timeframes: Dict[str, str]) -> ConfluenceScore:
    """Weighted trend-alignment across timeframes. ``timeframes`` maps a TF name
    (e.g. '1h','daily') to a trend label ('bullish'|'bearish'|'choppy'|'neutral')."""
    if not timeframes:
        return ConfluenceScore()
    total_w = 0.0
    bull_w = 0.0
    bear_w = 0.0
    for tf, trend in timeframes.items():
        w = _TF_WEIGHTS.get(tf, 1.0)
        total_w += w
        t = str(trend or "").lower()
        if t == "bullish":
            bull_w += w
        elif t == "bearish":
            bear_w += w
    if total_w <= 0:
        return ConfluenceScore(timeframes=dict(timeframes))

    bull = round(bull_w / total_w, 3)
    bear = round(bear_w / total_w, 3)
    score = round(max(bull, bear), 3)
    direction = "bullish" if bull > bear else ("bearish" if bear > bull else ("mixed" if bull > 0 else "neutral"))

    # The dominant (highest-weight) timeframe present.
    dominant = max(timeframes.keys(), key=lambda tf: _TF_WEIGHTS.get(tf, 1.0))
    # Timeframes whose trend opposes the dominant direction.
    conflicting = [
        tf for tf, tr in timeframes.items()
        if direction in ("bullish", "bearish") and str(tr).lower() in ("bullish", "bearish")
        and str(tr).lower() != direction
    ]
    return ConfluenceScore(
        bullish_alignment=bull, bearish_alignment=bear, score=score, direction=direction,
        dominant_tf=dominant, conflicting_timeframes=conflicting, timeframes=dict(timeframes),
    )


def _trend_from_closes(closes: List[float]) -> str:
    if not closes or len(closes) < 5:
        return "neutral"
    n = len(closes)
    window = min(20, n)
    sma = sum(closes[-window:]) / window
    prev_window = closes[-2 * window: -window] if n >= 2 * window else closes[:window]
    prev_sma = sum(prev_window) / len(prev_window) if prev_window else sma
    last = closes[-1]
    rising = sma >= prev_sma
    if last > sma and rising:
        return "bullish"
    if last < sma and not rising:
        return "bearish"
    return "choppy"


def confluence_for(ticker: str) -> Optional[ConfluenceScore]:
    """Best-effort, cached multi-timeframe confluence from real candles.

    On-demand only (network). Returns None if data is unavailable.
    """
    ticker = ticker.upper()
    now = time.monotonic()
    hit = _CACHE.get(ticker)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]
    try:
        from backend.bot.backtest import fetch_candles

        plan = [("1h", "1mo", "1h"), ("daily", "6mo", "1d"), ("weekly", "2y", "1wk")]
        tfs: Dict[str, str] = {}
        for label, period, interval in plan:
            try:
                df = fetch_candles(ticker, period=period, interval=interval)
                if df is None or df.empty or "Close" not in df.columns:
                    continue
                closes = [float(c) for c in df["Close"].astype(float).tolist()]
                tfs[label] = _trend_from_closes(closes)
            except Exception:
                continue
        if not tfs:
            return None
        result = score_confluence(tfs)
    except Exception:
        return None
    _CACHE[ticker] = (now, result)
    return result
