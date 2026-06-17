"""Stage-18b — FINRA short interest ingest.

FINRA publishes short-interest data twice a month. The bulk file at
https://cdn.finra.org/equity/regsho/monthly/CNMSshvol{YYYYMMDD}.txt
is a pipe-delimited daily volume file — useful but the short *interest*
(open short positions, settled) is in a separate monthly report.

For the bot's purposes the daily short-volume file is sufficient: it
gives ``shortVolume / totalVolume`` per ticker, which is what the
microstructure agent wants when flagging "breakout + heavy shorting"
squeeze candidates.

We fetch the latest available trading day's file and cache one row per
(ticker, date). The fetcher is injectable for tests.

No API key required. FINRA rate-limits but we fetch one file per day.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.short_interest import ShortInterest

logger = logging.getLogger(__name__)


@dataclass
class ShortVolumeRow:
    ticker: str
    settlement_date: date
    short_volume: float
    total_volume: float

    @property
    def short_pct(self) -> Optional[float]:
        if not self.total_volume:
            return None
        return round(self.short_volume / self.total_volume, 4)


def _default_fetcher(target_date: Optional[date] = None) -> List[ShortVolumeRow]:
    """Pull the FINRA consolidated short-volume file for a given trading
    day (defaults to today). Returns one row per ticker."""
    import requests
    out: List[ShortVolumeRow] = []
    # Try the requested date and walk backward up to 7 days until we
    # find a published file (weekends + holidays don't have a file).
    base_date = target_date or date.today()
    for offset in range(0, 7):
        d = base_date - timedelta(days=offset)
        url = (f"https://cdn.finra.org/equity/regsho/daily/"
                 f"CNMSshvol{d.strftime('%Y%m%d')}.txt")
        try:
            resp = requests.get(url, timeout=20)
        except Exception:
            continue
        if resp.status_code == 200 and resp.text and "|" in resp.text:
            break
    else:
        return out
    reader = csv.DictReader(io.StringIO(resp.text), delimiter="|")
    for row in reader:
        try:
            ticker = (row.get("Symbol") or "").strip().upper()
            if not ticker or ticker == "TOTAL":
                continue
            short_vol = float(row.get("ShortVolume") or 0)
            total_vol = float(row.get("TotalVolume") or 0)
            out.append(ShortVolumeRow(
                ticker=ticker, settlement_date=d,
                short_volume=short_vol, total_volume=total_vol,
            ))
        except Exception:
            continue
    return out


class FinraClient:
    """Stateless wrapper so callers can inject a stub fetcher in tests."""

    def __init__(self, *, fetcher: Optional[Callable[..., List[ShortVolumeRow]]] = None) -> None:
        self._fetcher = fetcher or _default_fetcher

    def fetch(self, target_date: Optional[date] = None) -> List[ShortVolumeRow]:
        try:
            return self._fetcher(target_date)
        except Exception:
            logger.warning("finra fetch failed", exc_info=True)
            return []


# ── upsert + helpers ────────────────────────────────────────────────────


def _upsert(rows: List[ShortVolumeRow], *, tickers: Optional[List[str]] = None
              ) -> int:
    """Persist rows, optionally filtered to a watchlist subset to avoid
    inserting 8000+ rows we'll never query."""
    keep_set = {t.upper() for t in (tickers or [])} or None
    inserted = 0
    try:
        with session_scope() as session:
            for r in rows:
                if keep_set and r.ticker not in keep_set:
                    continue
                # Dedupe by (ticker, settlement_date)
                ts = datetime(r.settlement_date.year,
                                 r.settlement_date.month, r.settlement_date.day)
                existing = session.execute(
                    select(ShortInterest.id)
                    .where(ShortInterest.ticker == r.ticker)
                    .where(ShortInterest.settlement_date == ts)
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                pct = r.short_pct
                session.add(ShortInterest(
                    ticker=r.ticker, settlement_date=ts,
                    short_interest=r.short_volume,
                    avg_daily_volume=r.total_volume,
                    days_to_cover=(round(1.0 / pct, 2) if pct and pct > 0 else None),
                ))
                inserted += 1
    except Exception:
        logger.exception("finra upsert failed")
    return inserted


def refresh(*, tickers: Optional[List[str]] = None,
               client: Optional[FinraClient] = None,
               target_date: Optional[date] = None) -> Dict[str, Any]:
    cl = client or FinraClient()
    rows = cl.fetch(target_date)
    inserted = _upsert(rows, tickers=tickers)
    # MITS Phase 8.2 — capture FINRA short-volume to bronze.
    try:
        from backend.bot.data import lake as _lake
        _lake.write_bronze(
            "finra", "short_volume",
            [{
                "ticker": r.ticker,
                "settlement_date": r.settlement_date.isoformat(),
                "short_volume": r.short_volume,
                "total_volume": r.total_volume,
                "short_pct": r.short_pct,
            } for r in rows],
            request_url="https://cdn.finra.org/equity/regsho/daily/",
            source_version=__name__,
        )
    except Exception:
        pass
    return {"rows_fetched": len(rows), "rows_inserted": inserted}


def latest_for(ticker: str) -> Optional[Dict[str, Any]]:
    try:
        with session_scope() as session:
            row = session.execute(
                select(ShortInterest)
                .where(ShortInterest.ticker == ticker.upper())
                .order_by(desc(ShortInterest.settlement_date))
                .limit(1)
            ).scalar_one_or_none()
            return row.to_dict() if row else None
    except Exception:
        return None


def short_pressure(ticker: str, *, lookback: int = 5) -> Dict[str, Any]:
    """Trend + level read on short volume for one ticker. Used by the
    microstructure agent to flag rising short interest into a setup.
    Returns ``{level: 'high'|'moderate'|'low'|'unknown', trend: 'rising'|
    'falling'|'flat'|'unknown', latest_short_pct: float|None,
    days_to_cover: float|None}``."""
    rows: List[Dict[str, Any]] = []
    try:
        with session_scope() as session:
            orm_rows = list(session.execute(
                select(ShortInterest)
                .where(ShortInterest.ticker == ticker.upper())
                .order_by(desc(ShortInterest.settlement_date))
                .limit(lookback)
            ).scalars().all())
            # Project to plain dicts inside the session to dodge
            # DetachedInstanceError on subsequent attribute reads.
            rows = [{
                "short_interest": r.short_interest,
                "avg_daily_volume": r.avg_daily_volume,
                "days_to_cover": r.days_to_cover,
            } for r in orm_rows]
    except Exception:
        rows = []
    if not rows:
        return {"level": "unknown", "trend": "unknown",
                "latest_short_pct": None, "days_to_cover": None,
                "sample_size": 0}
    latest = rows[0]
    latest_pct = (latest["short_interest"] / latest["avg_daily_volume"]
                     if latest["avg_daily_volume"] else None)
    pcts = [(r["short_interest"] / r["avg_daily_volume"])
              for r in rows
              if r["avg_daily_volume"] and r["short_interest"] is not None]
    if len(pcts) >= 2:
        if pcts[0] - pcts[-1] > 0.05:
            trend = "rising"
        elif pcts[-1] - pcts[0] > 0.05:
            trend = "falling"
        else:
            trend = "flat"
    else:
        trend = "unknown"
    if latest_pct is None:
        level = "unknown"
    elif latest_pct >= 0.40:
        level = "high"
    elif latest_pct >= 0.25:
        level = "moderate"
    else:
        level = "low"
    return {
        "level": level, "trend": trend,
        "latest_short_pct": round(latest_pct, 4) if latest_pct else None,
        "days_to_cover": latest["days_to_cover"],
        "sample_size": len(rows),
    }
