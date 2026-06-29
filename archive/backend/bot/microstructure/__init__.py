"""Stage-4 microstructure layer — order-book proxies from available data.

Honest caveat up front: we don't have a paid Level-2 feed (Polygon, dxFeed,
Databento). The metrics here are **proxies** computed from the bar-level
data we DO have:
  • Volume + price-range patterns → sweep / absorption probability
  • Yfinance quote (bid/ask) → spread + imbalance estimate
  • Intraday volume vs ADV → urgency
  • Up-volume / down-volume tape proxy → aggressive flow

The output is a ``MicrostructureSnapshot`` that strategies can fuse into
their composite bias. When a real L2 feed lands in Stage 7 (data-source
health), the implementation here can be swapped without touching callers.

**What you should NOT trust this for**: spoof detection in any rigorous
sense, iceberg detection, latency-arb edge. Those need real depth + tick
timing.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MicrostructureSnapshot:
    ticker: str
    spread_bps: float = 0.0
    bid_ask_imbalance: float = 0.0      # -1 (sell-heavy) → +1 (buy-heavy)
    aggressive_flow: float = 0.0         # -1 → +1 from up-volume vs down-volume
    sweep_probability: float = 0.0       # 0 → 1
    absorption_probability: float = 0.0  # 0 → 1 ("eating" liquidity at a level)
    urgency: float = 0.0                 # 0 → 1 (relative pace of trading)
    interpretation: str = ""             # human-readable summary
    source: str = "proxy"                # "proxy" until L2 lands
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── building blocks ────────────────────────────────────────────────────────


def _imbalance_from_quote(bid_size: float, ask_size: float) -> float:
    """Simple normalized book imbalance: (bid - ask) / (bid + ask)."""
    total = bid_size + ask_size
    if total <= 0:
        return 0.0
    return round((bid_size - ask_size) / total, 4)


def _spread_bps(bid: float, ask: float, mid: float) -> float:
    if mid <= 0 or ask <= 0 or bid <= 0 or ask < bid:
        return 0.0
    return round(((ask - bid) / mid) * 1e4, 2)


def _sweep_from_bars(bars: List[Dict[str, Any]], avg_volume: float) -> float:
    """Sweep probability proxy: the last bar's volume vs the ADV-derived
    expected per-bar volume, weighted by range expansion."""
    if not bars or avg_volume <= 0:
        return 0.0
    last = bars[-1]
    bar_vol = float(last.get("volume") or 0)
    high = float(last.get("high") or 0)
    low = float(last.get("low") or 0)
    close = float(last.get("close") or 0)
    open_ = float(last.get("open") or close)
    # Expected vol per bar for an N-bar session
    expected = avg_volume / max(1, len(bars))
    vol_ratio = bar_vol / max(expected, 1.0)
    range_pct = (high - low) / close if close > 0 else 0.0
    direction = 1.0 if close > open_ else -1.0
    score = min(1.0, vol_ratio * 0.2) * (1.0 + 5.0 * range_pct)
    return round(min(1.0, max(0.0, score)) * direction * 0.5 + 0.5
                  if direction < 0 else min(1.0, max(0.0, score)), 4)


def _absorption_from_bars(bars: List[Dict[str, Any]]) -> float:
    """Absorption: high volume + small price change → market maker absorbing
    a large order at a level (often a reversal precursor)."""
    if not bars:
        return 0.0
    last = bars[-1]
    bar_vol = float(last.get("volume") or 0)
    high = float(last.get("high") or 0)
    low = float(last.get("low") or 0)
    close = float(last.get("close") or 0)
    open_ = float(last.get("open") or close)
    if close <= 0 or bar_vol <= 0:
        return 0.0
    range_pct = (high - low) / close
    move_pct = abs(close - open_) / close
    # avg volume of last 20 bars
    last20 = bars[-21:-1]
    avg_vol = sum(float(b.get("volume") or 0) for b in last20) / max(1, len(last20))
    if avg_vol <= 0:
        return 0.0
    vol_ratio = bar_vol / avg_vol
    # High vol, small body → absorption
    if vol_ratio > 1.5 and move_pct < 0.003:
        return round(min(1.0, (vol_ratio - 1.5) / 3.0), 4)
    return 0.0


def _aggressive_flow(bars: List[Dict[str, Any]]) -> float:
    """Up-volume / down-volume proxy: signed sum of bars' volume where the
    sign comes from close vs open."""
    if not bars:
        return 0.0
    up = sum(float(b.get("volume") or 0) for b in bars
              if float(b.get("close") or 0) > float(b.get("open") or 0))
    down = sum(float(b.get("volume") or 0) for b in bars
                if float(b.get("close") or 0) < float(b.get("open") or 0))
    total = up + down
    if total <= 0:
        return 0.0
    return round((up - down) / total, 4)


def _urgency(bars: List[Dict[str, Any]], avg_volume: float) -> float:
    """Recent pace vs typical pace."""
    if not bars or avg_volume <= 0:
        return 0.0
    recent = bars[-5:]
    if not recent:
        return 0.0
    recent_avg = sum(float(b.get("volume") or 0) for b in recent) / len(recent)
    expected = avg_volume / max(1, len(bars))
    ratio = recent_avg / max(expected, 1.0)
    return round(min(1.0, ratio / 3.0), 4)


# ── top-level snapshot ─────────────────────────────────────────────────────


def assess_microstructure(
    *,
    ticker: str,
    bars: Optional[List[Dict[str, Any]]] = None,
    avg_volume: float = 0.0,
    bid: float = 0.0,
    ask: float = 0.0,
    bid_size: float = 0.0,
    ask_size: float = 0.0,
) -> MicrostructureSnapshot:
    """Build a microstructure snapshot from the available proxies.

    All inputs are optional — when one isn't supplied the corresponding
    metric stays at 0 and a note is added so the UI knows.
    """
    bars = bars or []
    notes: List[str] = []
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0

    spread_bps = _spread_bps(bid, ask, mid)
    imbalance = _imbalance_from_quote(bid_size, ask_size)
    aggressive = _aggressive_flow(bars)
    sweep = _sweep_from_bars(bars, avg_volume)
    absorption = _absorption_from_bars(bars)
    urgency = _urgency(bars, avg_volume)

    if not bars:
        notes.append("no bar data — sweep/absorption/urgency unavailable")
    if bid_size <= 0 or ask_size <= 0:
        notes.append("no L1 size data — bid/ask imbalance is 0")

    pieces = []
    if aggressive > 0.3:
        pieces.append("aggressive buy-side tape")
    elif aggressive < -0.3:
        pieces.append("aggressive sell-side tape")
    if sweep > 0.6:
        pieces.append("possible sweep")
    if absorption > 0.5:
        pieces.append("absorption at level — watch for reversal")
    if urgency > 0.7:
        pieces.append("elevated pace")
    interpretation = "; ".join(pieces) or "neutral microstructure"

    return MicrostructureSnapshot(
        ticker=ticker.upper(),
        spread_bps=spread_bps,
        bid_ask_imbalance=imbalance,
        aggressive_flow=aggressive,
        sweep_probability=sweep,
        absorption_probability=absorption,
        urgency=urgency,
        interpretation=interpretation,
        source="proxy",
        notes=notes or [
            "metrics derived from bar data — real L2 feed (Polygon / dxFeed) "
            "needed for spoof / iceberg / hidden-liquidity detection",
        ],
    )
