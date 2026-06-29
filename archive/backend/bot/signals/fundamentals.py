"""Fundamental data via yfinance: P/E, EPS growth, revenue trend, analyst ratings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ETFs don't have per-share fundamentals on Yahoo's quoteSummary endpoint;
# hitting it floods the logs with 404s on every cycle. Skip them.
_ETF_TICKERS = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "VTV", "VUG", "VEA", "VWO",
    "AGG", "BND", "TLT", "HYG", "LQD", "XLF", "XLK", "XLE", "XLY", "XLP",
    "XLV", "XLI", "XLU", "XLB", "XLRE", "XLC", "GLD", "SLV", "USO",
    "ARKK", "ARKQ", "ARKG", "ARKF", "ARKW", "SOXX", "SMH", "EFA", "EEM",
    "FXI", "EWZ", "EWJ", "TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SPXU",
})


@dataclass
class FundamentalSnapshot:
    pe_ratio: Optional[float]
    eps: Optional[float]
    revenue_growth: Optional[float]
    analyst_recommendation: Optional[str]

    @property
    def is_attractive(self) -> bool:
        """Cheap heuristic for 'fundamentally attractive'."""
        if self.pe_ratio is None:
            return False
        if self.pe_ratio <= 0 or self.pe_ratio > 40:
            return False
        if self.revenue_growth is not None and self.revenue_growth < 0:
            return False
        return True


def _safe_float(value) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def snapshot_from_info(info: dict) -> FundamentalSnapshot:
    """Build a snapshot from a yfinance ``Ticker.info`` dict (or mock)."""
    return FundamentalSnapshot(
        pe_ratio=_safe_float(info.get("trailingPE")),
        eps=_safe_float(info.get("trailingEps")),
        revenue_growth=_safe_float(info.get("revenueGrowth")),
        analyst_recommendation=info.get("recommendationKey"),
    )


def fetch_fundamentals(ticker: str) -> FundamentalSnapshot:
    """Pull fundamentals from yfinance. Imported lazily so tests can mock.
    ETFs have no fundamentals on Yahoo's endpoint — short-circuit to a
    neutral snapshot instead of generating a 404 every cycle."""
    if ticker.upper() in _ETF_TICKERS:
        return FundamentalSnapshot(pe_ratio=None, eps=None,
                                       revenue_growth=None,
                                       analyst_recommendation=None)
    import yfinance as yf

    info = yf.Ticker(ticker).info or {}
    return snapshot_from_info(info)
