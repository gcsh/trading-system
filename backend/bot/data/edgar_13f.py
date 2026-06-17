"""MITS Phase 11.E — 13F-HR (institutional holdings) parser + backfill.

For each watched-fund CIK, walk the submissions index for every
``13F-HR`` filing and parse the Information Table XML into
:class:`backend.models.fund_holding.FundHolding` rows.

The 13F Information Table is a separate XML inside the filing archive
(``form13fInfoTable.xml`` typically). Each ``<infoTable>`` element is
one position:

    <infoTable>
      <nameOfIssuer>APPLE INC</nameOfIssuer>
      <titleOfClass>COM</titleOfClass>
      <cusip>037833100</cusip>
      <value>123456789</value>   <!-- thousands of USD pre-2023, dollars post -->
      <shrsOrPrnAmt>
         <sshPrnamt>1000000</sshPrnamt>
         <sshPrnamtType>SH</sshPrnamtType>
      </shrsOrPrnAmt>
      ...
    </infoTable>

Reporting unit changed from "thousands" to "dollars" in Q3 2022 per
SEC rule; we treat ``value`` as raw, then heuristically multiply by
1000 for filings before 2023-01-01 to normalize to USD. The
``filing_date`` lets us pick the right scaling.

``change_from_prior_qtr`` is computed at write time by querying the
same fund's prior 13F row for the same CUSIP.
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select, insert as sa_insert
from sqlalchemy.exc import IntegrityError

from backend.bot.data.sync_orchestrator import CallbackResult
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.fund_holding import FundHolding

logger = logging.getLogger(__name__)


SEC_BASE = "https://www.sec.gov"
SEC_DATA_BASE = "https://data.sec.gov"


# Reporting-unit transition: SEC reporting threshold change shifted
# the value field unit from "thousands" to "actual dollars" for
# filings starting in Q3 2022. We treat 2023-01-01 as the practical
# cutoff (filings of FY2022 may straddle).
_VALUE_UNIT_CUTOFF = date(2023, 1, 1)


# ── shapes ────────────────────────────────────────────────────────────


@dataclass
class FundHoldingRow:
    fund_cik: str
    fund_name: str
    quarter_end_date: date
    cusip: str
    issuer_name: Optional[str]
    ticker: Optional[str]
    shares: Optional[float]
    value_usd: Optional[float]
    pct_of_portfolio: Optional[float]
    filing_date: date
    accession_number: str
    source_url: str


@dataclass
class FundQuarter:
    fund_cik: str
    fund_name: str
    accession_number: str
    filing_date: date
    quarter_end_date: date
    primary_doc: str
    holdings: List[FundHoldingRow] = field(default_factory=list)


# ── HTTP ──────────────────────────────────────────────────────────────


def _user_agent() -> str:
    return (getattr(TUNABLES, "sec_user_agent", "") or "").strip()


def _http_get(url: str) -> Tuple[int, bytes]:
    """Shares the per-process EDGAR token bucket with ``edgar_form4`` so
    parallel form4 + 13f backfills don't collectively bust SEC's
    10 req/sec ceiling. Also catches the SEC's HTML rate-limit page and
    synthesizes a 429 status so the orchestrator can back off."""
    import time as _time
    import requests
    from backend.bot.data.edgar_form4 import _edgar_bucket
    ua = _user_agent()
    if not ua:
        raise RuntimeError(
            "edgar_13f: TB_SEC_USER_AGENT not set; SEC requires UA")
    _edgar_bucket().acquire()
    timeout = float(getattr(TUNABLES, "edgar_http_timeout_sec", 30.0))
    resp = requests.get(
        url,
        headers={
            "User-Agent": ua,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/xml, */*",
        },
        timeout=timeout,
    )
    body = resp.content
    if resp.status_code == 200 and body and b"Request Rate Threshold Exceeded" in body[:2000]:
        logger.warning("edgar_13f: SEC rate-limit page received; backing off 30s")
        _time.sleep(30.0)
        return (429, body)
    return (resp.status_code, body)


# ── filings index walker ──────────────────────────────────────────────


def list_13f_filings(cik: str, since: date
                          ) -> List[Tuple[str, date, str]]:
    """``[(accession_number, filing_date, primary_document), ...]`` for
    every ``13F-HR`` (and ``13F-HR/A`` amendment) filing for ``cik``
    with ``filing_date >= since``."""
    url = f"{SEC_DATA_BASE}/submissions/CIK{cik}.json"
    status, body = _http_get(url)
    if status == 404:
        return []
    if status != 200:
        raise RuntimeError(
            f"edgar_13f: submissions fetch failed status={status} cik={cik}")
    payload = json.loads(body.decode("utf-8"))
    out: List[Tuple[str, date, str]] = []

    def _ingest(block: Dict[str, Any]) -> None:
        accs = block.get("accessionNumber") or []
        forms = block.get("form") or []
        dates = block.get("filingDate") or []
        prims = block.get("primaryDocument") or []
        for i in range(len(accs)):
            form = forms[i] if i < len(forms) else ""
            if not form.startswith("13F-HR"):
                continue
            try:
                fd = datetime.strptime(dates[i], "%Y-%m-%d").date()
            except Exception:
                continue
            if fd < since:
                continue
            out.append((accs[i], fd,
                         prims[i] if i < len(prims) else ""))

    recent = (payload.get("filings") or {}).get("recent") or {}
    _ingest(recent)
    older_files = (payload.get("filings") or {}).get("files") or []
    for finfo in older_files:
        if not isinstance(finfo, dict):
            continue
        name = finfo.get("name") or ""
        if not name:
            continue
        sub_url = f"{SEC_DATA_BASE}/submissions/{name}"
        try:
            sstatus, sbody = _http_get(sub_url)
            if sstatus == 200:
                spayload = json.loads(sbody.decode("utf-8"))
                _ingest(spayload)
        except Exception:
            logger.warning(
                "edgar_13f: failed to walk older file %s for cik=%s",
                name, cik, exc_info=True,
            )
    return out


# ── XML fetch + parse ─────────────────────────────────────────────────


def _accession_no_dashes(accession: str) -> str:
    return accession.replace("-", "")


def _index_url(cik: str, accession: str) -> str:
    cik_int = str(int(cik))
    return (f"{SEC_BASE}/Archives/edgar/data/{cik_int}/"
            f"{_accession_no_dashes(accession)}/")


def _find_information_table_url(cik: str, accession: str) -> Optional[str]:
    idx = _index_url(cik, accession)
    status, body = _http_get(idx)
    if status != 200:
        return None
    text = body.decode("utf-8", errors="ignore")
    # The information table is typically named ``infotable.xml`` or
    # ``form13fInfoTable.xml`` or similar. Match any .xml that contains
    # "info" or "table" or "13f" in its filename.
    candidates = re.findall(r'href="([^"]+\.xml)"', text)
    for href in candidates:
        low = href.lower()
        if "filingsummary" in low:
            continue
        if "infotable" in low or "info_table" in low or \
                "13f" in low or "table" in low:
            if href.startswith("/"):
                return f"{SEC_BASE}{href}"
            return idx + href
    # Fallback — return the first non-FilingSummary xml.
    for href in candidates:
        if "filingsummary" in href.lower():
            continue
        if href.startswith("/"):
            return f"{SEC_BASE}{href}"
        return idx + href
    return None


# Strip XML namespace prefix on tags. 13F filings use
# ``http://www.sec.gov/edgar/document/thirteenf/informationtable`` as
# the default namespace which makes ElementTree pathing a pain. The
# simplest robust approach: walk the parsed tree and look at
# ``local-name`` rather than the namespaced tag.
def _local(tag: str) -> str:
    if not tag:
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _walk_findtext(elt, name: str) -> Optional[str]:
    if elt is None:
        return None
    for child in elt.iter():
        if _local(child.tag).lower() == name.lower():
            return (child.text or "").strip() or None
    return None


def _walk_findall(elt, name: str):
    out = []
    if elt is None:
        return out
    for child in elt.iter():
        if _local(child.tag).lower() == name.lower():
            out.append(child)
    return out


def parse_information_table(xml_bytes: bytes,
                                  *, fund_cik: str, fund_name: str,
                                  accession: str, filing_date: date,
                                  quarter_end_date: date,
                                  source_url: str
                                  ) -> List[FundHoldingRow]:
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        logger.warning(
            "edgar_13f: XML parse failed for fund=%s accession=%s",
            fund_cik, accession, exc_info=True,
        )
        return []
    rows: List[FundHoldingRow] = []
    info_blocks = _walk_findall(root, "infoTable")
    if not info_blocks:
        return []
    # Total portfolio value for pct_of_portfolio. Sum AFTER we resolve
    # the per-row value scaling so the percentages are self-consistent.
    raw_values: List[float] = []

    pre_cutoff = filing_date < _VALUE_UNIT_CUTOFF
    scale = 1000.0 if pre_cutoff else 1.0

    parsed: List[Tuple[str, str, Optional[float], Optional[float]]] = []
    for blk in info_blocks:
        cusip = _walk_findtext(blk, "cusip") or ""
        if not cusip:
            continue
        issuer = _walk_findtext(blk, "nameOfIssuer") or None
        # SEC value field — see scaling note above.
        raw_val = _walk_findtext(blk, "value")
        try:
            value_raw = float(raw_val) if raw_val else None
        except (TypeError, ValueError):
            value_raw = None
        value_usd = (value_raw * scale) if value_raw is not None else None
        shares_raw = _walk_findtext(blk, "sshPrnamt")
        try:
            shares = float(shares_raw) if shares_raw else None
        except (TypeError, ValueError):
            shares = None
        parsed.append((cusip, issuer or "", shares, value_usd))
        if value_usd is not None:
            raw_values.append(value_usd)
    total_value = sum(raw_values) or None
    for cusip, issuer, shares, value_usd in parsed:
        pct = None
        if value_usd is not None and total_value and total_value > 0:
            pct = round(100.0 * (value_usd / total_value), 4)
        rows.append(FundHoldingRow(
            fund_cik=fund_cik,
            fund_name=fund_name,
            quarter_end_date=quarter_end_date,
            cusip=cusip,
            issuer_name=issuer or None,
            ticker=None,  # resolved later by CUSIP→ticker map
            shares=shares,
            value_usd=value_usd,
            pct_of_portfolio=pct,
            filing_date=filing_date,
            accession_number=accession,
            source_url=source_url,
        ))
    return rows


def _quarter_end_from_filing(filing_date: date) -> date:
    """13F-HR is due 45 days after the quarter end. Infer the reporting
    quarter end as the most recent calendar quarter end strictly before
    ``filing_date - 1`` (i.e. the quarter that just closed)."""
    candidates = [
        date(filing_date.year, 3, 31),
        date(filing_date.year, 6, 30),
        date(filing_date.year, 9, 30),
        date(filing_date.year, 12, 31),
        date(filing_date.year - 1, 12, 31),
        date(filing_date.year - 1, 9, 30),
    ]
    # Pick the most recent quarter end strictly before filing_date.
    valid = [d for d in candidates if d < filing_date]
    if not valid:
        return date(filing_date.year, 3, 31)
    return max(valid)


# ── CUSIP → ticker map (in-process, fed by universe + universe-CIK map) ─


_CUSIP_TICKER_MAP: Dict[str, str] = {}
_CUSIP_TICKER_MAP_LOADED = False


def _cusip_ticker_map() -> Dict[str, str]:
    """Best-effort CUSIP→ticker map. EDGAR doesn't ship a single
    canonical map, but ``company_tickers.json`` carries CIK + ticker;
    CUSIPs we fill in from 13F results themselves on first sighting
    (most-frequent issuer name → ticker via the universe loader).

    This is intentionally narrow — we only need to resolve CUSIPs to
    tickers that are in our universe, because that's all the
    smart-money feature layer cares about. Non-universe CUSIPs land
    with ``ticker=NULL``.
    """
    global _CUSIP_TICKER_MAP_LOADED
    if _CUSIP_TICKER_MAP_LOADED:
        return _CUSIP_TICKER_MAP
    try:
        from backend.bot.data.universe import load_universe
        universe = set(load_universe())
    except Exception:
        universe = set()
    # Issuer-name → ticker map (uppercased, normalized). We probe this
    # with the issuer name from 13F rows during write.
    name_to_ticker = {
        "APPLE INC": "AAPL", "MICROSOFT CORP": "MSFT",
        "NVIDIA CORP": "NVDA", "AMAZON COM INC": "AMZN",
        "META PLATFORMS INC": "META", "ALPHABET INC": "GOOG",
        "TESLA INC": "TSLA",
        "ADVANCED MICRO DEVICES INC": "AMD",
        "BERKSHIRE HATHAWAY INC": "BRK.B",
        "VISA INC": "V", "MASTERCARD INC": "MA",
        "COSTCO WHOLESALE CORP": "COST",
        "JPMORGAN CHASE & CO": "JPM",
        "BANK OF AMERICA CORP": "BAC",
        "GOLDMAN SACHS GROUP INC": "GS",
        "MORGAN STANLEY": "MS",
        "UNITEDHEALTH GROUP INC": "UNH",
        "ELI LILLY & CO": "LLY", "ELI LILLY AND CO": "LLY",
        "JOHNSON & JOHNSON": "JNJ",
        "WALMART INC": "WMT", "HOME DEPOT INC": "HD",
        "MCDONALDS CORP": "MCD",
        "CATERPILLAR INC": "CAT",
        "EXXON MOBIL CORP": "XOM",
        "CHEVRON CORP NEW": "CVX", "CHEVRON CORP": "CVX",
        "NETFLIX INC": "NFLX",
        "WALT DISNEY CO": "DIS",
        "BROADCOM INC": "AVGO",
        "TAIWAN SEMICONDUCTOR MFG CO LTD": "TSM",
        "PALANTIR TECHNOLOGIES INC": "PLTR",
        "COINBASE GLOBAL INC": "COIN",
        "SHOPIFY INC": "SHOP",
    }
    # Pre-known CUSIPs for the largest names (CUSIP 9-char issuer code
    # is stable; 13F files truncate the trailing check digit
    # occasionally).
    cusip_to_ticker = {
        "037833100": "AAPL", "594918104": "MSFT",
        "67066G104": "NVDA", "023135106": "AMZN",
        "30303M102": "META", "02079K305": "GOOG",
        "02079K107": "GOOG", "88160R101": "TSLA",
        "007903107": "AMD",  "084670702": "BRK.B",
        "92826C839": "V",    "57636Q104": "MA",
        "22160K105": "COST", "46625H100": "JPM",
        "060505104": "BAC",  "38141G104": "GS",
        "617446448": "MS",   "91324P102": "UNH",
        "532457108": "LLY",  "478160104": "JNJ",
        "931142103": "WMT",  "437076102": "HD",
        "580135101": "MCD",  "149123101": "CAT",
        "30231G102": "XOM",  "166764100": "CVX",
        "64110L106": "NFLX", "254687106": "DIS",
        "11135F101": "AVGO", "874039100": "TSM",
        "69608A108": "PLTR", "19260Q107": "COIN",
        "82509L107": "SHOP",
    }
    # Only keep entries pointing into the live universe.
    _CUSIP_TICKER_MAP.update({c: t for c, t in cusip_to_ticker.items()
                              if t in universe})
    # Stash name map on the module for downstream issuer fallback.
    global _ISSUER_NAME_TO_TICKER
    _ISSUER_NAME_TO_TICKER = {n: t for n, t in name_to_ticker.items()
                              if t in universe}
    _CUSIP_TICKER_MAP_LOADED = True
    return _CUSIP_TICKER_MAP


_ISSUER_NAME_TO_TICKER: Dict[str, str] = {}


def _resolve_ticker(cusip: str, issuer_name: Optional[str]) -> Optional[str]:
    cmap = _cusip_ticker_map()
    if cusip in cmap:
        return cmap[cusip]
    # Try a CUSIP prefix match (some filings drop the trailing check
    # digit; the 8-char prefix is the unique issuer key).
    if cusip and len(cusip) >= 8:
        prefix = cusip[:8]
        for ck, tk in cmap.items():
            if ck.startswith(prefix):
                return tk
    # Issuer-name fallback.
    if issuer_name:
        norm = re.sub(r"[^A-Z0-9& ]", "", issuer_name.upper()).strip()
        return _ISSUER_NAME_TO_TICKER.get(norm)
    return None


# ── persistence ───────────────────────────────────────────────────────


def _compute_change_from_prior(fund_cik: str, cusip: str,
                                       quarter_end_date: date,
                                       current_shares: Optional[float],
                                       s) -> Optional[float]:
    if current_shares is None:
        return None
    row = s.execute(
        select(FundHolding.shares)
        .where(FundHolding.fund_cik == fund_cik)
        .where(FundHolding.cusip == cusip)
        .where(FundHolding.quarter_end_date < quarter_end_date)
        .order_by(FundHolding.quarter_end_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        return current_shares - float(row)
    except Exception:
        return None


def write_fund_holdings(rows: List[FundHoldingRow]) -> int:
    """Persist 13F holdings, idempotent on
    ``(fund_cik, quarter_end_date, cusip)``.

    Uses SQLite's ``INSERT OR IGNORE`` (via SQLAlchemy core's
    ``prefix_with``) for new rows so a duplicate inside the same callback
    invocation — common when an amendment re-files the same quarter, or
    when two backfill processes race on the same fund — degrades to a
    no-op for that row instead of poisoning the entire session. Existing
    rows are updated via the ORM path so mutable fields (value, share
    count after amendment, etc.) refresh.

    The previous implementation called ``s.add()`` per row inside a
    ``try / except IntegrityError: s.rollback()`` block. SQLite's
    integrity error is raised at flush/commit time — by then the
    rollback had thrown away the WHOLE transaction's pending inserts,
    not just the offending row, which is why 13F was crashing the
    backfill with 0 new rows landed.
    """
    if not rows:
        return 0
    # Dedupe the input batch on the unique key. Amendments routinely
    # re-emit the prior quarter's positions; without this we'd insert
    # the same (fund, quarter, cusip) twice in a single transaction
    # and SQLite would reject the second.
    seen_keys: set = set()
    deduped: List[FundHoldingRow] = []
    for r in rows:
        k = (r.fund_cik, r.quarter_end_date, r.cusip)
        if k in seen_keys:
            continue
        seen_keys.add(k)
        deduped.append(r)
    inserted = 0
    try:
        with session_scope() as s:
            # Pre-load all existing rows for this batch in one shot.
            funds = {r.fund_cik for r in deduped}
            qtrs = {r.quarter_end_date for r in deduped}
            cusips = {r.cusip for r in deduped}
            existing_rows = s.execute(
                select(FundHolding)
                .where(FundHolding.fund_cik.in_(funds))
                .where(FundHolding.quarter_end_date.in_(qtrs))
                .where(FundHolding.cusip.in_(cusips))
            ).scalars().all()
            existing_map = {
                (h.fund_cik, h.quarter_end_date, h.cusip): h
                for h in existing_rows
            }
            new_payloads: List[Dict[str, Any]] = []
            now = datetime.utcnow()
            for r in deduped:
                ticker = _resolve_ticker(r.cusip, r.issuer_name)
                key = (r.fund_cik, r.quarter_end_date, r.cusip)
                existing = existing_map.get(key)
                if existing is not None:
                    existing.shares = r.shares
                    existing.value_usd = r.value_usd
                    existing.pct_of_portfolio = r.pct_of_portfolio
                    existing.ticker = ticker
                    existing.issuer_name = r.issuer_name
                    existing.change_from_prior_qtr = _compute_change_from_prior(
                        r.fund_cik, r.cusip, r.quarter_end_date,
                        r.shares, s)
                    existing.accession_number = r.accession_number
                    existing.filing_date = r.filing_date
                    existing.source_url = r.source_url
                    continue
                new_payloads.append({
                    "fund_cik": r.fund_cik,
                    "fund_name": r.fund_name[:200],
                    "ticker": ticker,
                    "cusip": r.cusip,
                    "issuer_name": r.issuer_name,
                    "quarter_end_date": r.quarter_end_date,
                    "shares": r.shares,
                    "value_usd": r.value_usd,
                    "pct_of_portfolio": r.pct_of_portfolio,
                    "change_from_prior_qtr": _compute_change_from_prior(
                        r.fund_cik, r.cusip, r.quarter_end_date,
                        r.shares, s),
                    "filing_date": r.filing_date,
                    "accession_number": r.accession_number,
                    "source_url": r.source_url,
                    "fetched_at": now,
                })
            if new_payloads:
                # SQLite-specific INSERT OR IGNORE — silently drops rows
                # that would violate the unique constraint. We target
                # the table directly (not the mapped class) so SQLAlchemy
                # doesn't auto-append a ``RETURNING id`` clause that
                # would force per-row execution and lose rowcount.
                bind = s.get_bind()
                dialect_name = getattr(bind.dialect, "name", "") if bind else ""
                if dialect_name == "sqlite":
                    stmt = (
                        sa_insert(FundHolding.__table__)
                        .prefix_with("OR IGNORE")
                    )
                    result = s.execute(stmt, new_payloads)
                    rc = int(result.rowcount or 0)
                    if rc < 0:
                        rc = len(new_payloads)
                    inserted += rc
                else:
                    for payload in new_payloads:
                        try:
                            s.execute(
                                sa_insert(FundHolding.__table__),
                                [payload],
                            )
                            inserted += 1
                        except IntegrityError:
                            s.rollback()
                            continue
    except Exception:
        logger.exception("edgar_13f: write_fund_holdings failed")
    return inserted


def write_13f_bronze(fund_cik: str, rows: List[FundHoldingRow],
                          *, chunk_start: date, chunk_end: date) -> None:
    if not rows:
        return
    try:
        from backend.bot.data import lake as _lake
        payload = []
        for r in rows:
            payload.append({
                "fund_cik": r.fund_cik, "fund_name": r.fund_name,
                "quarter_end_date": r.quarter_end_date.isoformat(),
                "cusip": r.cusip, "issuer_name": r.issuer_name,
                "ticker": r.ticker, "shares": r.shares,
                "value_usd": r.value_usd,
                "pct_of_portfolio": r.pct_of_portfolio,
                "filing_date": r.filing_date.isoformat(),
                "accession_number": r.accession_number,
                "source_url": r.source_url,
            })
        _lake.write_bronze(
            source="edgar", dtype="form13f",
            payload=payload, ticker=fund_cik,
            extra_tags={
                "chunk_start": chunk_start.isoformat(),
                "chunk_end": chunk_end.isoformat(),
            },
            request_url="edgar://Form13F-HR",
            source_version=__name__,
        )
    except Exception:
        logger.debug("edgar_13f: bronze write failed", exc_info=True)


# ── public fetch ──────────────────────────────────────────────────────


def fetch_13f_filings(cik: str, fund_name: str, since_date: date
                            ) -> List[FundQuarter]:
    """Return parsed 13F-HR filings for ``cik`` since ``since_date``.
    Each :class:`FundQuarter` carries its holdings list."""
    filings = list_13f_filings(cik, since_date)
    quarters: List[FundQuarter] = []
    for accession, filing_date, primary in filings:
        info_url = _find_information_table_url(cik, accession)
        if not info_url:
            continue
        status, body = _http_get(info_url)
        if status != 200:
            continue
        quarter_end = _quarter_end_from_filing(filing_date)
        holdings = parse_information_table(
            body, fund_cik=cik, fund_name=fund_name,
            accession=accession, filing_date=filing_date,
            quarter_end_date=quarter_end, source_url=info_url,
        )
        quarters.append(FundQuarter(
            fund_cik=cik, fund_name=fund_name,
            accession_number=accession, filing_date=filing_date,
            quarter_end_date=quarter_end, primary_doc=primary,
            holdings=holdings,
        ))
    return quarters


# ── orchestrator callback ─────────────────────────────────────────────


def edgar_13f_backfill_callback(fund_cik: str, chunk_start: date,
                                       chunk_end: date) -> CallbackResult:
    """The orchestrator treats each fund CIK as a "ticker". This
    callback walks every 13F-HR filing for the fund inside the chunk
    window and writes holdings."""
    if not _user_agent():
        raise RuntimeError(
            "edgar_13f: TB_SEC_USER_AGENT not set; cannot ingest")
    from backend.bot.data.watched_funds import lookup_fund_name
    name = lookup_fund_name(fund_cik) or f"CIK {fund_cik}"
    quarters = fetch_13f_filings(fund_cik, name, chunk_start)
    quarters = [q for q in quarters if q.filing_date <= chunk_end]
    if not quarters:
        return CallbackResult(
            last_completed_date=chunk_end, rows_written=0,
            metadata={"reason": "no_13f_filings_in_window"},
        )
    total_inserted = 0
    flat: List[FundHoldingRow] = []
    for q in quarters:
        if not q.holdings:
            continue
        total_inserted += write_fund_holdings(q.holdings)
        flat.extend(q.holdings)
    write_13f_bronze(fund_cik, flat,
                          chunk_start=chunk_start, chunk_end=chunk_end)
    last_dt = max((q.filing_date for q in quarters), default=chunk_end)
    return CallbackResult(
        last_completed_date=min(chunk_end, last_dt),
        rows_written=total_inserted,
        metadata={
            "filings": len(quarters),
            "holdings": sum(len(q.holdings) for q in quarters),
        },
    )


__all__ = [
    "FundHoldingRow",
    "FundQuarter",
    "list_13f_filings",
    "parse_information_table",
    "fetch_13f_filings",
    "write_fund_holdings",
    "write_13f_bronze",
    "edgar_13f_backfill_callback",
]
