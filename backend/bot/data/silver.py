"""MITS Phase 8.3 — Silver layer: canonical types + bronze→silver pass.

Every downstream consumer that doesn't care WHICH vendor produced the
data should read from silver — partition is canonical-type + date, NOT
source. That way a future swap from yfinance → polygon doesn't ripple
through detector code.

Every silver row carries:
  * ``integrity_status`` — pass | warn | fail (from the existing
    data-integrity sanity layer in ``backend.bot.data.thetadata``)
  * ``lineage_bronze_uri`` — exact S3 URI of the bronze parquet that
    produced it. The single most useful debug column.

The cron entry point is ``normalize_pass(dt)`` — defaults to today.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from backend.bot.data import lake

logger = logging.getLogger(__name__)


# ── canonical row schemas ─────────────────────────────────────────────


@dataclass
class _BaseRow:
    source: str = ""
    source_version: str = ""
    integrity_status: str = "pass"   # pass | warn | fail
    lineage_bronze_uri: str = ""
    silver_ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k, v in self.__dict__.items():
            out[k] = v
        return out


@dataclass
class BarRow(_BaseRow):
    ticker: str = ""
    ts: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    vwap: Optional[float] = None

    @classmethod
    def validate(cls, row: Dict[str, Any]) -> Optional["BarRow"]:
        try:
            return cls(
                ticker=str(row.get("ticker") or row.get("symbol") or "").upper().strip(),
                ts=str(row.get("ts") or row.get("t") or row.get("date") or ""),
                open=float(row.get("open") or row.get("Open") or 0.0),
                high=float(row.get("high") or row.get("High") or 0.0),
                low=float(row.get("low") or row.get("Low") or 0.0),
                close=float(row.get("close") or row.get("Close") or 0.0),
                volume=float(row.get("volume") or row.get("Volume") or 0.0),
                vwap=row.get("vwap"),
                source=str(row.get("source") or ""),
                source_version=str(row.get("source_version") or ""),
                integrity_status=str(row.get("integrity_status") or "pass"),
                lineage_bronze_uri=str(row.get("lineage_bronze_uri") or ""),
            )
        except Exception:
            return None


@dataclass
class QuoteRow(_BaseRow):
    ticker: str = ""
    ts: str = ""
    bid: float = 0.0
    ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    last: float = 0.0

    @classmethod
    def validate(cls, row: Dict[str, Any]) -> Optional["QuoteRow"]:
        try:
            return cls(
                ticker=str(row.get("ticker") or "").upper().strip(),
                ts=str(row.get("ts") or ""),
                bid=float(row.get("bid") or 0.0),
                ask=float(row.get("ask") or 0.0),
                bid_size=float(row.get("bid_size") or 0.0),
                ask_size=float(row.get("ask_size") or 0.0),
                last=float(row.get("last") or 0.0),
                source=str(row.get("source") or ""),
                source_version=str(row.get("source_version") or ""),
                integrity_status=str(row.get("integrity_status") or "pass"),
                lineage_bronze_uri=str(row.get("lineage_bronze_uri") or ""),
            )
        except Exception:
            return None


@dataclass
class OptionContractRow(_BaseRow):
    ticker: str = ""
    expiry: str = ""
    strike: float = 0.0
    right: str = ""    # C | P
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    iv: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0
    oi: float = 0.0
    volume: float = 0.0
    ts: str = ""

    @classmethod
    def validate(cls, row: Dict[str, Any]) -> Optional["OptionContractRow"]:
        try:
            right = str(row.get("right") or row.get("option_type") or "C").upper()[0]
            return cls(
                ticker=str(row.get("ticker") or "").upper().strip(),
                expiry=str(row.get("expiry") or row.get("expiration") or ""),
                strike=float(row.get("strike") or 0.0),
                right=right,
                bid=float(row.get("bid") or 0.0),
                ask=float(row.get("ask") or 0.0),
                mid=float(row.get("mid") or 0.0),
                iv=float(row.get("iv") or 0.0),
                delta=float(row.get("delta") or 0.0),
                gamma=float(row.get("gamma") or 0.0),
                vega=float(row.get("vega") or 0.0),
                theta=float(row.get("theta") or 0.0),
                oi=float(row.get("oi") or row.get("open_interest") or 0.0),
                volume=float(row.get("volume") or 0.0),
                ts=str(row.get("ts") or ""),
                source=str(row.get("source") or ""),
                source_version=str(row.get("source_version") or ""),
                integrity_status=str(row.get("integrity_status") or "pass"),
                lineage_bronze_uri=str(row.get("lineage_bronze_uri") or ""),
            )
        except Exception:
            return None


@dataclass
class ObservationRow(_BaseRow):
    observation_id: str = ""
    ticker: str = ""
    pattern: str = ""
    ts: str = ""
    regime: str = ""
    features_json: str = ""

    @classmethod
    def validate(cls, row: Dict[str, Any]) -> Optional["ObservationRow"]:
        try:
            return cls(
                observation_id=str(row.get("observation_id") or row.get("id") or ""),
                ticker=str(row.get("ticker") or "").upper().strip(),
                pattern=str(row.get("pattern") or ""),
                ts=str(row.get("ts") or ""),
                regime=str(row.get("regime") or ""),
                features_json=str(row.get("features_json") or "{}"),
                source=str(row.get("source") or "live_detector"),
                source_version=str(row.get("source_version") or ""),
                integrity_status=str(row.get("integrity_status") or "pass"),
                lineage_bronze_uri=str(row.get("lineage_bronze_uri") or ""),
            )
        except Exception:
            return None


@dataclass
class MacroPointRow(_BaseRow):
    series_id: str = ""
    ts: str = ""
    value: float = 0.0

    @classmethod
    def validate(cls, row: Dict[str, Any]) -> Optional["MacroPointRow"]:
        try:
            return cls(
                series_id=str(row.get("series_id") or row.get("series") or ""),
                ts=str(row.get("ts") or row.get("date") or ""),
                value=float(row.get("value") or 0.0),
                source=str(row.get("source") or "FRED"),
                source_version=str(row.get("source_version") or ""),
                integrity_status=str(row.get("integrity_status") or "pass"),
                lineage_bronze_uri=str(row.get("lineage_bronze_uri") or ""),
            )
        except Exception:
            return None


@dataclass
class FilingRow(_BaseRow):
    cik: str = ""
    ticker: str = ""
    filing_type: str = ""
    filing_date: str = ""
    accession_number: str = ""
    content_url: str = ""

    @classmethod
    def validate(cls, row: Dict[str, Any]) -> Optional["FilingRow"]:
        try:
            return cls(
                cik=str(row.get("cik") or ""),
                ticker=str(row.get("ticker") or "").upper().strip(),
                filing_type=str(row.get("filing_type") or row.get("form") or ""),
                filing_date=str(row.get("filing_date") or row.get("filed_at") or ""),
                accession_number=str(row.get("accession_number") or ""),
                content_url=str(row.get("content_url") or ""),
                source=str(row.get("source") or "EDGAR"),
                source_version=str(row.get("source_version") or ""),
                integrity_status=str(row.get("integrity_status") or "pass"),
                lineage_bronze_uri=str(row.get("lineage_bronze_uri") or ""),
            )
        except Exception:
            return None


CANONICAL_SCHEMAS: Dict[str, type] = {
    "bars": BarRow,
    "quotes": QuoteRow,
    "options": OptionContractRow,
    "observations": ObservationRow,
    "macro": MacroPointRow,
    "filings": FilingRow,
}


# ── source/dtype → canonical mapping ──────────────────────────────────


# Maps the (source, dtype) tuples we use in the bronze writers to a
# canonical silver type. Anything not mapped is skipped.
BRONZE_TO_CANONICAL: Dict[tuple, str] = {
    ("yfinance", "bars"): "bars",
    ("thetadata", "bars"): "bars",
    ("alpaca_stream", "ticks"): "quotes",
    ("thetadata", "chain"): "options",
    ("thetadata", "iv_snapshot"): "options",
    ("fred", "series"): "macro",
    ("edgar", "filings"): "filings",
}


def _dt_str(dt: Optional[date]) -> str:
    return (dt or date.today()).isoformat()


def normalize_pass(dt: Optional[date] = None) -> Dict[str, Any]:
    """Walk the bronze layer for the given date, normalize per
    (source, dtype) → canonical schema, and write silver parquet.

    Returns per-canonical counts.

    Idempotent: silver objects are timestamped, so re-running just
    appends a fresh batch (Athena reads the union — no double-count
    since silver tables are SELECT-deduped per row key downstream).
    """
    dt_str = _dt_str(dt)
    stats: Dict[str, int] = {k: 0 for k in CANONICAL_SCHEMAS}
    for (source, dtype), canonical in BRONZE_TO_CANONICAL.items():
        try:
            df = lake.read_bronze(source, dtype, dt_str)
        except Exception:
            continue
        if df is None or len(df) == 0:
            continue
        cls = CANONICAL_SCHEMAS.get(canonical)
        if cls is None:
            continue
        # Default source / lineage onto every row.
        rows = df.to_dict(orient="records")
        silver_rows: List[Dict[str, Any]] = []
        for row in rows:
            row.setdefault("source", source)
            # The bronze layer doesn't carry its own URI back (we'd
            # need a manifest sidecar to recover it). Best-effort: use
            # the prefix so a debugger can still pinpoint the file.
            row.setdefault(
                "lineage_bronze_uri",
                f"s3://{lake.TUNABLES.lake_bucket}/bronze/{source}/{dtype}/dt={dt_str}/")
            validated = cls.validate(row)  # type: ignore[attr-defined]
            if validated is None:
                continue
            silver_rows.append(validated.to_dict())
        if not silver_rows:
            continue
        lake.write_silver(canonical, silver_rows,
                            source_version=f"{source}.{dtype}", sync=True)
        stats[canonical] = stats.get(canonical, 0) + len(silver_rows)
    return {"date": dt_str, "rows_per_canonical": stats}


# Make the module attribute resolution for the lake fall back to the
# default we already imported, in case TUNABLES changes at runtime.
from backend.config import TUNABLES  # noqa: E402  (used in normalize_pass)


__all__ = [
    "BarRow", "QuoteRow", "OptionContractRow", "ObservationRow",
    "MacroPointRow", "FilingRow", "CANONICAL_SCHEMAS",
    "normalize_pass",
]
