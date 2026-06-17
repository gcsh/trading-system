"""Stage-18b — CFTC Commitment of Traders ingest.

CFTC publishes the weekly COT report Friday afternoons for the prior
Tuesday's settled positions. The disaggregated futures-only report is
available as a public CSV download.

We track three instruments most relevant to the bot:
  • ES — E-mini S&P 500
  • TY — 10-Year Treasury Note
  • DX — DXY (US Dollar Index)

Macro agent reads positioning *extremes*: when noncommercials (specs)
are deeply long ES and short TY, the rally is crowded; when the
reverse, expect mean reversion.

The full CFTC archive can be hundreds of MB. We fetch the small
``deacot_xxxx.txt`` file for the current year only — typically <2 MB.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.cot_report import CotReport

logger = logging.getLogger(__name__)


# Map CFTC commodity codes to our short instrument names.
INSTRUMENTS: Dict[str, str] = {
    "138741": "ES",   # E-MINI S&P 500
    "043602": "TY",   # 10-YEAR U.S. TREASURY NOTES
    "098662": "DX",   # U.S. DOLLAR INDEX
}


@dataclass
class CotRow:
    instrument: str
    report_date: date
    noncomm_long: float
    noncomm_short: float
    comm_long: float
    comm_short: float
    open_interest: float


def _default_fetcher(*, year: Optional[int] = None) -> List[CotRow]:
    """Pull the CFTC disaggregated futures-only annual ZIP and parse it.
    Returns rows for the three instruments we care about."""
    import requests
    y = year or datetime.utcnow().year
    url = (f"https://www.cftc.gov/files/dea/history/"
              f"fut_disagg_txt_{y}.zip")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception:
        logger.warning("CFTC fetch failed", exc_info=True)
        return []
    out: List[CotRow] = []
    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            inner = next((n for n in z.namelist() if n.lower().endswith(".txt")),
                            None)
            if inner is None:
                return out
            with z.open(inner) as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="latin-1"))
                for row in reader:
                    code = (row.get("CFTC_Contract_Market_Code") or "").strip()
                    inst = INSTRUMENTS.get(code)
                    if inst is None:
                        continue
                    try:
                        rd = datetime.strptime(
                            row.get("Report_Date_as_YYYY-MM-DD") or "", "%Y-%m-%d"
                        ).date()
                    except Exception:
                        continue
                    def _num(k):
                        try:
                            return float(row.get(k) or 0)
                        except Exception:
                            return 0.0
                    out.append(CotRow(
                        instrument=inst, report_date=rd,
                        noncomm_long=_num("NonComm_Positions_Long_All"),
                        noncomm_short=_num("NonComm_Positions_Short_All"),
                        comm_long=_num("Comm_Positions_Long_All"),
                        comm_short=_num("Comm_Positions_Short_All"),
                        open_interest=_num("Open_Interest_All"),
                    ))
    except Exception:
        logger.warning("CFTC parse failed", exc_info=True)
    return out


class CotClient:
    def __init__(self, *, fetcher: Optional[Callable[..., List[CotRow]]] = None) -> None:
        self._fetcher = fetcher or _default_fetcher

    def fetch(self, *, year: Optional[int] = None) -> List[CotRow]:
        try:
            return self._fetcher(year=year)
        except Exception:
            logger.warning("cot fetch failed", exc_info=True)
            return []


def _upsert(rows: List[CotRow]) -> int:
    inserted = 0
    try:
        with session_scope() as session:
            for r in rows:
                ts = datetime(r.report_date.year, r.report_date.month,
                                 r.report_date.day)
                existing = session.execute(
                    select(CotReport.id)
                    .where(CotReport.instrument == r.instrument)
                    .where(CotReport.report_date == ts)
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                session.add(CotReport(
                    instrument=r.instrument, report_date=ts,
                    noncommercial_long=r.noncomm_long,
                    noncommercial_short=r.noncomm_short,
                    commercial_long=r.comm_long,
                    commercial_short=r.comm_short,
                    open_interest=r.open_interest,
                ))
                inserted += 1
    except Exception:
        logger.exception("cot upsert failed")
    return inserted


def refresh(*, client: Optional[CotClient] = None,
               year: Optional[int] = None) -> Dict[str, Any]:
    cl = client or CotClient()
    rows = cl.fetch(year=year)
    inserted = _upsert(rows)
    # MITS Phase 8.2 — capture COT report rows to bronze.
    try:
        from backend.bot.data import lake as _lake
        _lake.write_bronze(
            "cot", "weekly_report",
            [{
                "instrument": getattr(r, "instrument", ""),
                "report_date": (r.report_date.isoformat()
                                  if getattr(r, "report_date", None) else ""),
                "noncomm_long": getattr(r, "noncomm_long", 0),
                "noncomm_short": getattr(r, "noncomm_short", 0),
                "comm_long": getattr(r, "comm_long", 0),
                "comm_short": getattr(r, "comm_short", 0),
            } for r in rows],
            request_url="cftc://cot/weekly",
            source_version=__name__,
        )
    except Exception:
        pass
    return {"rows_fetched": len(rows), "rows_inserted": inserted}


def latest_for(instrument: str) -> Optional[Dict[str, Any]]:
    try:
        with session_scope() as session:
            row = session.execute(
                select(CotReport)
                .where(CotReport.instrument == instrument.upper())
                .order_by(desc(CotReport.report_date))
                .limit(1)
            ).scalar_one_or_none()
            return row.to_dict() if row else None
    except Exception:
        return None


def positioning_snapshot() -> Dict[str, Any]:
    """Compact snapshot for the macro agent — net noncommercial positioning
    per instrument as a one-screen read."""
    out: Dict[str, Any] = {}
    for inst in INSTRUMENTS.values():
        out[inst] = latest_for(inst)
    return out
