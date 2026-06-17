"""MITS Phase 8.2 — Cboe put/call ratio bronze writer.

The flow is currently in-memory only (live_tape pulls a daily PCR
estimate from yfinance / the breadth pipeline). Phase 8 adds a tiny
fetcher that snapshots the Cboe equity + index PCRs daily and writes
them to bronze so the historical analog matcher in Phase 8.7 has a
full-history PCR series to embed.

Cboe publishes a CSV at
https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_PUT_CALL_RATIOS.csv
— graceful no-op on fetch failure.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


CBOE_PCR_URL = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/"
    "VIX_PUT_CALL_RATIOS.csv"
)


@dataclass
class PcrPoint:
    date: date
    equity_pcr: Optional[float]
    index_pcr: Optional[float]
    total_pcr: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "equity_pcr": self.equity_pcr,
            "index_pcr": self.index_pcr,
            "total_pcr": self.total_pcr,
        }


def _default_fetcher() -> List[PcrPoint]:
    try:
        import requests
        resp = requests.get(CBOE_PCR_URL, timeout=15)
        if resp.status_code != 200:
            return []
        out: List[PcrPoint] = []
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            try:
                d = datetime.strptime(
                    (row.get("Date") or row.get("DATE") or "").strip(),
                    "%Y-%m-%d",
                ).date()
            except Exception:
                continue
            def _f(key: str) -> Optional[float]:
                try:
                    v = row.get(key)
                    return float(v) if v not in (None, "", "n/a") else None
                except Exception:
                    return None
            out.append(PcrPoint(
                date=d,
                equity_pcr=_f("EQUITY_PUT_CALL_RATIO"),
                index_pcr=_f("INDEX_PUT_CALL_RATIO"),
                total_pcr=_f("TOTAL_PUT_CALL_RATIO"),
            ))
        return out
    except Exception:
        logger.debug("cboe pcr fetch failed", exc_info=True)
        return []


def refresh(*, fetcher=None) -> Dict[str, Any]:
    """Fetch latest PCR table + write rows to the bronze lake."""
    rows = (fetcher or _default_fetcher)()
    if not rows:
        return {"rows": 0, "reason": "no data"}
    try:
        from backend.bot.data import lake as _lake
        _lake.write_bronze(
            "cboe", "put_call_ratio",
            [r.to_dict() for r in rows],
            request_url=CBOE_PCR_URL,
            source_version=__name__,
        )
    except Exception:
        pass
    return {"rows": len(rows)}


__all__ = ["refresh", "PcrPoint", "CBOE_PCR_URL"]
