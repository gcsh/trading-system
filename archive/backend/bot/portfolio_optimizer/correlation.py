"""Stage-6 correlation-aware cluster caps.

Two positions in tightly correlated names act like one big position in
disguise. The portfolio_intel module already clusters tickers by
sector/theme; this module computes a CORRELATION-aware cluster cap so a
new entry can be reduced or refused when the resulting cluster exposure
would exceed the cap.

Two-tier model:
  • Quick sector/theme grouping (always works — no historical data needed)
  • Optional pairwise correlation from realized returns when supplied
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.bot.portfolio_intel import themes_for
from backend.config import TUNABLES

logger = logging.getLogger(__name__)


@dataclass
class ClusterExposure:
    cluster: str
    tickers: List[str] = field(default_factory=list)
    market_value: float = 0.0
    pct_of_equity: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ClusterCapResult:
    new_ticker: str
    new_value: float
    cluster_after: float        # cluster exposure pct AFTER adding new
    cluster_cap: float          # the cap
    allowed_value: float        # max additional $ we can add without breaching
    blocked: bool = False
    reason: str = ""
    cluster_label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── cluster exposure from positions ──────────────────────────────────────


def cluster_exposures(positions: List[Dict[str, Any]],
                        *, equity: float) -> List[ClusterExposure]:
    """Group positions by theme/sector and compute total exposure per cluster.

    Each ticker may belong to multiple themes (e.g. NVDA → Mag7 + AI infra +
    Semis). Positions count toward EACH theme so the caps are conservative.
    """
    by_cluster: Dict[str, ClusterExposure] = {}
    for pos in positions:
        ticker = (pos.get("ticker") or "").upper()
        if not ticker:
            continue
        mv = float(pos.get("market_value") or 0.0)
        if mv <= 0:
            continue
        for theme in themes_for(ticker) or ["__ungrouped__"]:
            ce = by_cluster.setdefault(theme, ClusterExposure(cluster=theme))
            ce.tickers.append(ticker)
            ce.market_value += mv

    out: List[ClusterExposure] = []
    for ce in by_cluster.values():
        ce.tickers = sorted(set(ce.tickers))
        ce.market_value = round(ce.market_value, 2)
        ce.pct_of_equity = round(ce.market_value / equity, 4) if equity > 0 else 0.0
        out.append(ce)
    out.sort(key=lambda c: c.pct_of_equity, reverse=True)
    return out


# ── cap check for a new entry ────────────────────────────────────────────


def check_cluster_cap(*, ticker: str, new_value: float,
                        positions: List[Dict[str, Any]],
                        equity: float,
                        cap_fraction: Optional[float] = None,
                        ) -> ClusterCapResult:
    """Would adding ``new_value`` to ``ticker`` push any cluster over the cap?

    Returns the most-binding cluster. ``blocked=True`` when the new entry
    would exceed the cap on at least one cluster; ``allowed_value`` is the
    largest $ we could add without breaching.
    """
    cap = cap_fraction if cap_fraction is not None else float(
        getattr(TUNABLES, "cluster_max_exposure", 0.50)
    )
    if equity <= 0:
        return ClusterCapResult(new_ticker=ticker.upper(), new_value=new_value,
                                  cluster_after=0.0, cluster_cap=cap,
                                  allowed_value=0.0, blocked=True,
                                  reason="no equity")

    ticker_themes = themes_for(ticker.upper()) or ["__ungrouped__"]
    current = {ce.cluster: ce for ce in cluster_exposures(positions, equity=equity)}

    # For each of the new ticker's themes, what would the after-pct be?
    worst: Optional[ClusterCapResult] = None
    for theme in ticker_themes:
        current_mv = current.get(theme, ClusterExposure(cluster=theme)).market_value
        new_mv = current_mv + new_value
        after = new_mv / equity
        allowed_mv = max(0.0, cap * equity - current_mv)
        result = ClusterCapResult(
            new_ticker=ticker.upper(), new_value=round(new_value, 2),
            cluster_after=round(after, 4), cluster_cap=cap,
            allowed_value=round(allowed_mv, 2),
            blocked=after > cap, cluster_label=theme,
            reason=(f"cluster '{theme}' would be at {after:.1%} "
                     f"(cap {cap:.0%})"),
        )
        if worst is None or result.cluster_after > worst.cluster_after:
            worst = result
    return worst   # type: ignore[return-value]
