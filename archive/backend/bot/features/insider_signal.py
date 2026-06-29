"""MITS Phase 11.E — insider (Form 4) signal aggregator.

Surfaces 4 features for the Brain prompt + EodAnalysis primer:

  - ``net_insider_purchase_30d(ticker)`` — sum of buy notional minus
    sell notional in the trailing 30 calendar days. Positive = bullish
    insider tilt; negative = bearish.
  - ``insider_cluster_count_30d(ticker)`` — distinct insiders who
    purchased in the trailing 30d. Three+ clustered buyers is the
    historically actionable signal (per Lakonishok-Lee 2001).
  - ``role_weighted_net_purchase(ticker)`` — net purchase notional with
    role weights: CEO 3x, CFO 2x, Director 1x, 10%-owner 1x.
  - ``insider_summary(ticker)`` — combined dict for the Brain prompt.

Buy codes considered: P (open-market purchase). Sale codes considered:
S (open-market sale). Exercises (M), grants (A), tax-withholds (F),
and gifts (G) are EXCLUDED — they're either non-discretionary or
non-cash, and Lakonishok-Lee showed they have no predictive power on
forward returns.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from sqlalchemy import and_, func, select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.insider_trade import InsiderTrade

logger = logging.getLogger(__name__)


# Codes that count as "buy notional" for the discretionary signal.
_BUY_CODES = ("P",)
# Codes that count as "sell notional".
_SELL_CODES = ("S",)


def _role_weight(row: InsiderTrade) -> float:
    """Lakonishok-Lee style role multiplier. CEO is the most predictive;
    everyone else flatlines at 1x. We approximate CEO/CFO via the
    ``insider_role`` field — most Form 4s carry a title string.
    """
    title = (row.insider_role or "").lower()
    if "ceo" in title or "chief executive" in title:
        return float(getattr(TUNABLES, "insider_role_weight_ceo", 3.0))
    if "cfo" in title or "chief financial" in title:
        return float(getattr(TUNABLES, "insider_role_weight_cfo", 2.0))
    if row.is_director:
        return float(getattr(TUNABLES, "insider_role_weight_director", 1.0))
    if row.is_10pct_owner:
        return float(getattr(TUNABLES, "insider_role_weight_10pct", 1.0))
    if row.is_officer:
        return float(getattr(TUNABLES, "insider_role_weight_officer", 1.5))
    return 1.0


def _window_start(window_days: int, *, as_of: Optional[date]) -> date:
    return (as_of or date.today()) - timedelta(days=int(window_days))


def _query_window(ticker: str, *, window_days: int,
                       as_of: Optional[date]):
    cutoff = _window_start(window_days, as_of=as_of)
    with session_scope() as s:
        rows = s.execute(
            select(InsiderTrade)
            .where(InsiderTrade.ticker == ticker.upper())
            .where(InsiderTrade.transaction_date >= cutoff)
        ).scalars().all()
        # Expunge so callers can read attributes outside the session.
        for r in rows:
            s.expunge(r)
        return rows


def net_insider_purchase_30d(ticker: str, *,
                                  window_days: int = 30,
                                  as_of: Optional[date] = None) -> float:
    """Net purchase notional (USD) over the trailing ``window_days``.

    ``buy notional - sell notional``. Returns 0.0 when no transactions
    or when the price field is missing on every row (Form 4s
    occasionally elide price for non-cash codes — those are already
    filtered out, but defense in depth here)."""
    rows = _query_window(ticker, window_days=window_days, as_of=as_of)
    net = 0.0
    for r in rows:
        notional = (r.total_value
                    if r.total_value is not None
                    else (r.shares or 0) * (r.price or 0))
        if r.transaction_code in _BUY_CODES:
            net += float(notional or 0)
        elif r.transaction_code in _SELL_CODES:
            net -= float(notional or 0)
    return round(net, 2)


def insider_cluster_count_30d(ticker: str, *,
                                    window_days: int = 30,
                                    as_of: Optional[date] = None) -> int:
    """Distinct insiders who purchased (code P) in the trailing
    ``window_days``."""
    rows = _query_window(ticker, window_days=window_days, as_of=as_of)
    distinct: set = set()
    for r in rows:
        if r.transaction_code in _BUY_CODES and \
                (r.shares or 0) > 0 and (r.price or 0) > 0:
            distinct.add((r.insider_name or "").strip().lower())
    return len(distinct)


def role_weighted_net_purchase(ticker: str, *,
                                       window_days: int = 30,
                                       as_of: Optional[date] = None) -> float:
    """Role-weighted net purchase notional (USD). Same as
    :func:`net_insider_purchase_30d` but each transaction's notional
    is multiplied by the role weight."""
    rows = _query_window(ticker, window_days=window_days, as_of=as_of)
    weighted = 0.0
    for r in rows:
        notional = (r.total_value
                    if r.total_value is not None
                    else (r.shares or 0) * (r.price or 0))
        if r.transaction_code in _BUY_CODES:
            weighted += float(notional or 0) * _role_weight(r)
        elif r.transaction_code in _SELL_CODES:
            weighted -= float(notional or 0) * _role_weight(r)
    return round(weighted, 2)


@dataclass
class InsiderSummary:
    ticker: str
    window_days: int
    net_purchase_usd: float
    role_weighted_net_purchase_usd: float
    cluster_count_buyers: int
    cluster_count_sellers: int
    total_buys: int
    total_sells: int
    most_recent_transaction_date: Optional[str]

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "window_days": self.window_days,
            "net_purchase_usd": self.net_purchase_usd,
            "role_weighted_net_purchase_usd":
                self.role_weighted_net_purchase_usd,
            "cluster_count_buyers": self.cluster_count_buyers,
            "cluster_count_sellers": self.cluster_count_sellers,
            "total_buys": self.total_buys,
            "total_sells": self.total_sells,
            "most_recent_transaction_date":
                self.most_recent_transaction_date,
        }


def insider_summary(ticker: str, *, window_days: int = 30,
                       as_of: Optional[date] = None) -> InsiderSummary:
    """Bundled summary suitable for direct inclusion in the Brain
    prompt or the EodAnalysis primer."""
    rows = _query_window(ticker, window_days=window_days, as_of=as_of)
    net = 0.0
    weighted = 0.0
    buyers: set = set()
    sellers: set = set()
    total_buys = 0
    total_sells = 0
    last_dt: Optional[date] = None
    for r in rows:
        notional = (r.total_value
                    if r.total_value is not None
                    else (r.shares or 0) * (r.price or 0))
        if r.transaction_code in _BUY_CODES:
            net += float(notional or 0)
            weighted += float(notional or 0) * _role_weight(r)
            total_buys += 1
            buyers.add((r.insider_name or "").strip().lower())
        elif r.transaction_code in _SELL_CODES:
            net -= float(notional or 0)
            weighted -= float(notional or 0) * _role_weight(r)
            total_sells += 1
            sellers.add((r.insider_name or "").strip().lower())
        if r.transaction_date and (last_dt is None or r.transaction_date > last_dt):
            last_dt = r.transaction_date
    return InsiderSummary(
        ticker=ticker.upper(),
        window_days=int(window_days),
        net_purchase_usd=round(net, 2),
        role_weighted_net_purchase_usd=round(weighted, 2),
        cluster_count_buyers=len(buyers),
        cluster_count_sellers=len(sellers),
        total_buys=total_buys,
        total_sells=total_sells,
        most_recent_transaction_date=(
            last_dt.isoformat() if last_dt else None),
    )


__all__ = [
    "InsiderSummary",
    "net_insider_purchase_30d",
    "insider_cluster_count_30d",
    "role_weighted_net_purchase",
    "insider_summary",
]
