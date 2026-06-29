"""MITS Phase 11.E — smart-money (13F) aggregation features.

Watched-fund 13F holdings feed three Brain-prompt features:

  - ``funds_holding_count(ticker)`` — how many of the 100 watched funds
    currently hold the name (most recent quarter on file).
  - ``net_funds_adding(ticker, quarter_end)`` — net add/trim activity
    in the most recent quarter (positive = funds increased on net;
    negative = funds trimmed on net).
  - ``top_funds_concentration(ticker)`` — average ``pct_of_portfolio``
    weight across the top 5 watched funds (by total AUM) that hold the
    name.
  - ``smart_money_summary(ticker)`` — bundled dict for the Brain prompt.

Quarter resolution defaults to the most recent ``quarter_end_date`` in
the table for the ticker; the caller can pin a specific quarter via
``as_of_quarter_end``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import desc, func, select

from backend.bot.data.watched_funds import (
    load_watched_funds, watched_fund_ciks,
)
from backend.db import session_scope
from backend.models.fund_holding import FundHolding

logger = logging.getLogger(__name__)


def _watched_cik_set() -> set:
    return set(watched_fund_ciks())


def _most_recent_quarter_end(ticker: str) -> Optional[date]:
    watched = list(_watched_cik_set())
    if not watched:
        return None
    with session_scope() as s:
        row = s.execute(
            select(func.max(FundHolding.quarter_end_date))
            .where(FundHolding.ticker == ticker.upper())
            .where(FundHolding.fund_cik.in_(watched))
        ).scalar_one_or_none()
        if row is None:
            return None
        # SQLAlchemy returns ``date`` for Date columns on SQLite; in
        # certain dialects it may come back as a string. Defensively
        # coerce so the caller always receives a date instance.
        if isinstance(row, date):
            return row
        try:
            return datetime.strptime(str(row)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def funds_holding_count(ticker: str, *,
                                as_of_quarter_end: Optional[date] = None) -> int:
    """Number of watched funds holding ``ticker`` as of the latest
    quarter (or the supplied ``as_of_quarter_end``)."""
    qend = as_of_quarter_end or _most_recent_quarter_end(ticker)
    if qend is None:
        return 0
    watched = list(_watched_cik_set())
    if not watched:
        return 0
    with session_scope() as s:
        count = s.execute(
            select(func.count(func.distinct(FundHolding.fund_cik)))
            .where(FundHolding.ticker == ticker.upper())
            .where(FundHolding.quarter_end_date == qend)
            .where(FundHolding.fund_cik.in_(watched))
            .where((FundHolding.shares.is_not(None)) &
                   (FundHolding.shares > 0))
        ).scalar_one()
    return int(count or 0)


def net_funds_adding(ticker: str, *,
                            as_of_quarter_end: Optional[date] = None) -> int:
    """Net (# funds adding) - (# funds trimming) in the supplied
    quarter (defaults to latest)."""
    qend = as_of_quarter_end or _most_recent_quarter_end(ticker)
    if qend is None:
        return 0
    watched = list(_watched_cik_set())
    if not watched:
        return 0
    adding = 0
    trimming = 0
    with session_scope() as s:
        rows = s.execute(
            select(FundHolding.change_from_prior_qtr)
            .where(FundHolding.ticker == ticker.upper())
            .where(FundHolding.quarter_end_date == qend)
            .where(FundHolding.fund_cik.in_(watched))
            .where(FundHolding.change_from_prior_qtr.is_not(None))
        ).scalars().all()
    for delta in rows:
        try:
            d = float(delta)
        except (TypeError, ValueError):
            continue
        if d > 0:
            adding += 1
        elif d < 0:
            trimming += 1
    return adding - trimming


def top_funds_concentration(ticker: str, *,
                                    top_n: int = 5,
                                    as_of_quarter_end: Optional[date] = None
                                    ) -> float:
    """Mean ``pct_of_portfolio`` weight across the top-N watched funds
    holding ``ticker`` (sorted by value_usd descending). Returns 0.0
    when no watched fund holds it."""
    qend = as_of_quarter_end or _most_recent_quarter_end(ticker)
    if qend is None:
        return 0.0
    watched = list(_watched_cik_set())
    if not watched:
        return 0.0
    with session_scope() as s:
        rows = s.execute(
            select(FundHolding.pct_of_portfolio,
                    FundHolding.value_usd)
            .where(FundHolding.ticker == ticker.upper())
            .where(FundHolding.quarter_end_date == qend)
            .where(FundHolding.fund_cik.in_(watched))
            .where(FundHolding.pct_of_portfolio.is_not(None))
            .order_by(desc(FundHolding.value_usd))
            .limit(int(top_n))
        ).all()
    if not rows:
        return 0.0
    pcts = [float(r[0]) for r in rows if r[0] is not None]
    if not pcts:
        return 0.0
    return round(sum(pcts) / len(pcts), 4)


@dataclass
class SmartMoneySummary:
    ticker: str
    quarter_end_date: Optional[str]
    funds_holding: int
    funds_adding: int
    funds_trimming: int
    net_funds_adding: int
    top5_avg_pct_portfolio: float
    top_holders: List[Dict[str, object]]

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "quarter_end_date": self.quarter_end_date,
            "funds_holding": self.funds_holding,
            "funds_adding": self.funds_adding,
            "funds_trimming": self.funds_trimming,
            "net_funds_adding": self.net_funds_adding,
            "top5_avg_pct_portfolio": self.top5_avg_pct_portfolio,
            "top_holders": self.top_holders,
        }


def smart_money_summary(ticker: str, *,
                              as_of_quarter_end: Optional[date] = None,
                              top_n: int = 5) -> SmartMoneySummary:
    qend = as_of_quarter_end or _most_recent_quarter_end(ticker)
    watched = list(_watched_cik_set())
    funds_holding = 0
    funds_adding = 0
    funds_trimming = 0
    top: List[Dict[str, object]] = []
    if qend is not None and watched:
        with session_scope() as s:
            rows = s.execute(
                select(FundHolding)
                .where(FundHolding.ticker == ticker.upper())
                .where(FundHolding.quarter_end_date == qend)
                .where(FundHolding.fund_cik.in_(watched))
                .order_by(desc(FundHolding.value_usd))
            ).scalars().all()
            for r in rows:
                if (r.shares or 0) <= 0:
                    continue
                funds_holding += 1
                delta = r.change_from_prior_qtr
                if delta is not None:
                    if float(delta) > 0:
                        funds_adding += 1
                    elif float(delta) < 0:
                        funds_trimming += 1
            for r in rows[:int(top_n)]:
                top.append({
                    "fund_name": r.fund_name,
                    "fund_cik": r.fund_cik,
                    "shares": r.shares,
                    "value_usd": r.value_usd,
                    "pct_of_portfolio": r.pct_of_portfolio,
                    "change_from_prior_qtr": r.change_from_prior_qtr,
                })
    return SmartMoneySummary(
        ticker=ticker.upper(),
        quarter_end_date=(qend.isoformat() if qend else None),
        funds_holding=funds_holding,
        funds_adding=funds_adding,
        funds_trimming=funds_trimming,
        net_funds_adding=funds_adding - funds_trimming,
        top5_avg_pct_portfolio=top_funds_concentration(
            ticker, top_n=top_n, as_of_quarter_end=qend),
        top_holders=top,
    )


__all__ = [
    "SmartMoneySummary",
    "funds_holding_count",
    "net_funds_adding",
    "top_funds_concentration",
    "smart_money_summary",
]
