"""MITS Phase 11.E — Form 4 (insider transaction) parser + backfill.

Pulls every Form 4 filing for each universe ticker since the start of
the requested window, fetches the XML primary document, and emits one
:class:`backend.models.insider_trade.InsiderTrade` row per
``<nonDerivativeTransaction>`` line.

EDGAR endpoints used:
  - ``GET https://data.sec.gov/submissions/CIK{cik}.json``
       → list of recent filings (recent[] + files[].json for older).
  - ``GET https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_doc}``
       → the Form 4 XML/HTML primary document. Form 4 instance XML is
       reliably named ``*.xml`` inside the index folder.

Rate limit: SEC EDGAR caps at 10 req/sec. We share the
SyncOrchestrator's ``edgar`` family token bucket (default 4 calls/sec
via ``TUNABLES.sync_max_calls_per_second_edgar``).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select, insert as sa_insert
from sqlalchemy.exc import IntegrityError

from backend.bot.data.sync_orchestrator import CallbackResult
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.insider_trade import InsiderTrade

logger = logging.getLogger(__name__)


SEC_BASE = "https://www.sec.gov"
SEC_DATA_BASE = "https://data.sec.gov"


# ── shared per-process token bucket for ALL EDGAR calls ───────────────
#
# SEC enforces a HARD 10 req/sec ceiling per IP; exceeding it earns a
# 10-minute IP ban that returns an HTML "rate limit exceeded" page. Our
# orchestrator paces OUTER chunk calls but each chunk fans out into
# many inner filings + primary docs, so the inner rate easily exceeds
# 10 req/sec without throttling here. The bucket lives at module scope
# and is shared with :mod:`backend.bot.data.edgar_13f` via a thin
# helper in :func:`_edgar_bucket`.


class _EdgarBucket:
    def __init__(self, per_second: float) -> None:
        self.rate = max(0.1, float(per_second))
        self.capacity = max(1.0, float(per_second))
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(
                    self.capacity,
                    self.tokens + (now - self.last_refill) * self.rate,
                )
                self.last_refill = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = max(0.05, (1.0 - self.tokens) / self.rate)
            time.sleep(min(wait, 1.0))


_EDGAR_BUCKET: Optional[_EdgarBucket] = None
_EDGAR_BUCKET_LOCK = threading.Lock()


def _edgar_bucket() -> _EdgarBucket:
    global _EDGAR_BUCKET
    if _EDGAR_BUCKET is not None:
        return _EDGAR_BUCKET
    with _EDGAR_BUCKET_LOCK:
        if _EDGAR_BUCKET is None:
            # Default ceiling: TUNABLES.sync_max_calls_per_second_edgar
            # (4.0). Stays well clear of SEC's 10 r/s and leaves head-
            # room for the other crons sharing the IP.
            rate = float(getattr(TUNABLES,
                                   "sync_max_calls_per_second_edgar", 4.0))
            _EDGAR_BUCKET = _EdgarBucket(rate)
        return _EDGAR_BUCKET


# Form 4 transaction codes worth tracking. "P"=open-market purchase,
# "S"=open-market sale, "M"=exercise of derivatives, "F"=tax-withhold,
# "A"=grant, "G"=gift, "C"=conversion. The feature layer decides what
# counts as "buy" vs "sell" — we just persist them all.
TRACKED_CODES = ("P", "S", "M", "F", "A", "G", "C")


# ── shapes ────────────────────────────────────────────────────────────


@dataclass
class Form4Transaction:
    ticker: str
    cik: str
    accession_number: str
    filing_date: date
    transaction_date: date
    insider_name: str
    insider_role: Optional[str]
    transaction_code: str
    shares: Optional[float]
    price: Optional[float]
    total_value: Optional[float]
    is_director: bool
    is_officer: bool
    is_10pct_owner: bool
    source_url: str


# ── HTTP (uses the existing EDGAR client UA contract) ─────────────────


def _user_agent() -> str:
    ua = getattr(TUNABLES, "sec_user_agent", "") or ""
    return ua.strip()


def _http_get(url: str) -> Tuple[int, bytes]:
    """SEC HTTP GET with the operator's UA. Returns (status_code, body).
    Raises on transport errors. Token-bucket throttled at the module
    level so we stay under SEC's 10 req/sec hard ceiling regardless of
    how many filings a single chunk fans out into.

    Also defensively guards against the SEC's HTML "rate limit
    exceeded" response. SEC returns HTTP 200 with an HTML body in that
    case, NOT a 429, so naive callers parse the HTML as XML and emit
    a "mismatched tag" error far from the source. We sniff for the
    sentinel string and surface a synthetic 429.
    """
    import requests
    ua = _user_agent()
    if not ua:
        raise RuntimeError(
            "edgar_form4: TB_SEC_USER_AGENT not set; SEC requires a "
            "User-Agent header with an operator contact email")
    _edgar_bucket().acquire()
    timeout = float(getattr(TUNABLES, "edgar_http_timeout_sec", 30.0))
    resp = requests.get(
        url, headers={
            "User-Agent": ua,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/xml, */*",
        },
        timeout=timeout,
    )
    body = resp.content
    if resp.status_code == 200 and body and b"Request Rate Threshold Exceeded" in body[:2000]:
        # Back off — the surrounding token bucket should have prevented
        # this but the SEC counter is per-IP and shared across all
        # backfills on this box; another module may have just spent
        # our budget. Sleep + return synthetic 429 so the orchestrator
        # retries with exponential backoff.
        logger.warning("edgar_form4: SEC rate-limit page received; backing off 30s")
        time.sleep(30.0)
        return (429, body)
    return (resp.status_code, body)


# ── ticker → CIK resolution (reuses EdgarClient cache) ────────────────


_CIK_CACHE: Dict[str, str] = {}
_CIK_CACHE_LOCK = threading.Lock()
_CIK_CACHE_HYDRATED = False


def _hydrate_cik_cache() -> None:
    """Pull SEC's full ticker→CIK map ONCE per process and stash it in
    :data:`_CIK_CACHE`. This eliminates the 40-parallel-fetch storm that
    was getting our IP rate-limit-banned at the start of every Form 4
    backfill (each call to ``EdgarClient.ticker_to_cik`` re-downloads
    the full 6MB JSON, so 40 cold callers = 40 fetches in <1s and SEC
    bans the IP for 10 minutes).
    """
    global _CIK_CACHE_HYDRATED
    if _CIK_CACHE_HYDRATED:
        return
    with _CIK_CACHE_LOCK:
        if _CIK_CACHE_HYDRATED:
            return
        # Throttle the SINGLE hydration call through the EDGAR bucket so
        # we play nice with any concurrent module fetches.
        _edgar_bucket().acquire()
        max_attempts = int(getattr(TUNABLES,
                                     "sec_ticker_map_retry_attempts", 4))
        backoff_base = float(getattr(TUNABLES,
                                       "sec_ticker_map_retry_base_sec", 5.0))
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                status, body = _http_get(
                    "https://www.sec.gov/files/company_tickers.json")
                if status == 200:
                    import json as _json
                    data = _json.loads(body.decode("utf-8"))
                    new_map: Dict[str, str] = {}
                    for _, entry in (data.items()
                                       if isinstance(data, dict) else []):
                        t = str(entry.get("ticker") or "").upper()
                        cik = entry.get("cik_str")
                        if t and cik is not None:
                            new_map[t] = str(cik).zfill(10)
                    _CIK_CACHE.update(new_map)
                    _CIK_CACHE_HYDRATED = True
                    logger.info(
                        "edgar_form4: ticker→CIK map hydrated entries=%d",
                        len(_CIK_CACHE),
                    )
                    return
                last_exc = RuntimeError(
                    f"company_tickers.json status={status}")
            except Exception as exc:
                last_exc = exc
            if attempt < max_attempts:
                wait = backoff_base * (2 ** (attempt - 1))
                logger.warning(
                    "edgar_form4: ticker-map hydration attempt %d/%d failed: "
                    "%s — backing off %.1fs",
                    attempt, max_attempts, last_exc, wait,
                )
                time.sleep(wait)
        logger.warning(
            "edgar_form4: ticker-map hydration exhausted retries; "
            "will fall back to per-call resolution. last_err=%s", last_exc,
        )


def _resolve_cik(ticker: str) -> Optional[str]:
    """Resolve ``ticker`` to a zero-padded 10-digit CIK.

    Strategy:
      1. Local cache (populated by :func:`_hydrate_cik_cache`, called
         lazily before the first lookup).
      2. ``EdgarClient.ticker_to_cik`` as a per-call fallback when the
         bulk hydration was blocked by an SEC ban / network blip. This
         path is the historical behaviour — preserved so an unlucky
         startup window doesn't permanently disable Form 4 ingest.
    """
    tk = (ticker or "").upper().strip()
    if not tk:
        return None
    if not _CIK_CACHE_HYDRATED:
        _hydrate_cik_cache()
    if tk in _CIK_CACHE:
        return _CIK_CACHE[tk]
    # Strip class-share suffix (BRK.B → BRK·B → BRK-B is SEC's form;
    # the company_tickers map uses a dash, not a dot, so we try both).
    if "." in tk:
        alt = tk.replace(".", "-")
        if alt in _CIK_CACHE:
            return _CIK_CACHE[alt]
    from backend.bot.data.edgar import EdgarClient
    cl = EdgarClient()
    cik = cl.ticker_to_cik(tk)
    if cik:
        _CIK_CACHE[tk] = cik
    return cik


# ── filings index walker ──────────────────────────────────────────────


def _list_form4_filings(cik: str, since: date
                          ) -> List[Tuple[str, date, str]]:
    """Return ``[(accession_number, filing_date, primary_document), ...]``
    for every Form 4 filing for ``cik`` with ``filing_date >= since``.

    Walks both the ``recent`` block in ``submissions/CIK{cik}.json`` AND
    every additional file referenced in ``files[].name`` (used by
    high-volume CIKs like AAPL/MSFT where the recent block holds only
    the last ~1000 filings; older filings live in
    ``CIK{cik}-submissions-001.json`` etc).
    """
    import json
    url = f"{SEC_DATA_BASE}/submissions/CIK{cik}.json"
    status, body = _http_get(url)
    if status == 404:
        return []
    if status != 200:
        raise RuntimeError(
            f"edgar_form4: submissions fetch failed status={status} "
            f"cik={cik}")
    payload = json.loads(body.decode("utf-8"))
    out: List[Tuple[str, date, str]] = []

    def _ingest(block: Dict[str, Any]) -> None:
        accs = block.get("accessionNumber") or []
        forms = block.get("form") or []
        dates = block.get("filingDate") or []
        prims = block.get("primaryDocument") or []
        for i in range(len(accs)):
            form = forms[i] if i < len(forms) else ""
            if form != "4":
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
                "edgar_form4: failed to walk older submissions file %s "
                "for cik=%s", name, cik, exc_info=True,
            )
            continue
    return out


# ── XML parser ────────────────────────────────────────────────────────


def _accession_no_dashes(accession_number: str) -> str:
    return accession_number.replace("-", "")


def _form4_url(cik: str, accession_number: str,
                  primary_doc: str) -> str:
    cik_int = str(int(cik))  # strip zero pad
    return (
        f"{SEC_BASE}/Archives/edgar/data/{cik_int}/"
        f"{_accession_no_dashes(accession_number)}/{primary_doc}"
    )


def _index_url(cik: str, accession_number: str) -> str:
    cik_int = str(int(cik))
    return (
        f"{SEC_BASE}/Archives/edgar/data/{cik_int}/"
        f"{_accession_no_dashes(accession_number)}/"
    )


def _text(elt) -> str:
    return (elt.text or "").strip() if elt is not None else ""


def _parse_form4_xml(xml_bytes: bytes, *,
                          ticker: str, cik: str,
                          accession_number: str,
                          filing_date: date,
                          source_url: str
                          ) -> List[Form4Transaction]:
    """Parse the Form 4 instance XML into a flat list of transactions.

    Robust to missing fields — every field defaults to None / safe
    sentinel so a malformed (but valid) Form 4 doesn't crash the
    backfill.
    """
    import xml.etree.ElementTree as ET
    root = None
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        # SEC frequently wraps the Form 4 ownershipDocument inside a
        # ``<DOCUMENT><TYPE>4</TYPE>...<XML>...</XML></DOCUMENT>`` envelope
        # which isn't well-formed XML at the top level. Carve out the
        # bare ``<ownershipDocument>...</ownershipDocument>`` slice and
        # retry — recovers ~95% of the previously-failing filings.
        text = xml_bytes.decode("utf-8", errors="ignore")
        m_open = text.find("<ownershipDocument")
        m_close = text.rfind("</ownershipDocument>")
        if m_open != -1 and m_close != -1 and m_close > m_open:
            slice_xml = text[m_open: m_close + len("</ownershipDocument>")]
            try:
                root = ET.fromstring(slice_xml)
            except Exception:
                logger.warning(
                    "edgar_form4: XML slice retry failed for %s %s",
                    ticker, accession_number, exc_info=True,
                )
        if root is None:
            logger.debug(
                "edgar_form4: failed to parse XML for %s %s",
                ticker, accession_number,
            )
            return []

    # Reporter (the insider) — Form 4 supports multiple <reportingOwner>
    # blocks. Pull the first one's identity + flag set; further blocks
    # are rare on retail-sized filings.
    def _find(elt, path: str):
        return elt.find(path) if elt is not None else None

    insider_name = ""
    is_director = is_officer = is_10pct = False
    insider_role: Optional[str] = None
    reporters = root.findall(".//reportingOwner") or []
    if reporters:
        first = reporters[0]
        insider_name = _text(_find(first, ".//rptOwnerName"))
        rel = _find(first, ".//reportingOwnerRelationship")
        if rel is not None:
            is_director = _text(_find(rel, "isDirector")) in ("1", "true", "True")
            is_officer = _text(_find(rel, "isOfficer")) in ("1", "true", "True")
            is_10pct = _text(_find(rel, "isTenPercentOwner")) in ("1", "true", "True")
            insider_role = _text(_find(rel, "officerTitle")) or None
            if not insider_role:
                if is_director:
                    insider_role = "Director"
                elif is_10pct:
                    insider_role = "10% Owner"

    out: List[Form4Transaction] = []
    blocks = (root.findall(".//nonDerivativeTransaction") +
              root.findall(".//derivativeTransaction"))
    for blk in blocks:
        code = _text(_find(blk, ".//transactionCode")) or \
            _text(_find(blk, ".//transactionCoding/transactionCode"))
        if not code:
            continue
        # Some filings put fields with the value inside <value>
        def _val(path: str) -> Optional[str]:
            v = _find(blk, path)
            if v is None:
                return None
            inner = _find(v, "value")
            return _text(inner) or _text(v) or None

        tx_date_raw = _val(".//transactionDate")
        try:
            tx_date = datetime.strptime((tx_date_raw or "")[:10],
                                          "%Y-%m-%d").date()
        except Exception:
            tx_date = filing_date  # best-effort fallback
        shares_raw = _val(".//transactionShares")
        price_raw = _val(".//transactionPricePerShare")
        try:
            shares = float(shares_raw) if shares_raw else None
        except Exception:
            shares = None
        try:
            price = float(price_raw) if price_raw else None
        except Exception:
            price = None
        total_value = None
        if shares is not None and price is not None:
            total_value = round(shares * price, 2)
        out.append(Form4Transaction(
            ticker=ticker.upper(),
            cik=cik,
            accession_number=accession_number,
            filing_date=filing_date,
            transaction_date=tx_date,
            insider_name=insider_name or "UNKNOWN",
            insider_role=insider_role,
            transaction_code=code,
            shares=shares,
            price=price,
            total_value=total_value,
            is_director=is_director,
            is_officer=is_officer,
            is_10pct_owner=is_10pct,
            source_url=source_url,
        ))
    return out


def _looks_like_form4_xml(body: bytes) -> bool:
    """Cheap sniff: a Form 4 instance XML always contains an
    ``<ownershipDocument`` tag near the top. Reject HTML wrappers and
    FilingSummary docs without paying the cost of a full ET.fromstring
    parse (which raises a noisy ParseError on the SEC's HTML rate-limit
    pages)."""
    if not body:
        return False
    head = body[:4096]
    # SEC's HTML wrappers and the rate-limit page lack the marker.
    return b"<ownershipDocument" in head or b"ownershipDocument" in head[:1024]


def _fetch_primary_doc(cik: str, accession_number: str,
                            primary_doc: str) -> Optional[bytes]:
    """Try the recorded ``primary_document`` first; if it's an HTML
    wrapper OR a non-Form-4 XML (FilingSummary etc), walk the index
    folder and find an XML that actually contains the ownership block.

    Why the change: SEC sometimes lists a non-XML primary document or
    a wrapper XML that contains ``<DOCUMENT>`` envelope chrome instead
    of the bare ``<ownershipDocument>`` root. The previous code
    eagerly returned the first 200 it got, which made
    ``ET.fromstring`` crash on the wrapper for ~12% of filings. The
    content-sniff filters those out and forces a walk to the correct
    bare XML."""
    candidates: List[Tuple[str, bytes]] = []
    if primary_doc and primary_doc.lower().endswith(".xml"):
        url = _form4_url(cik, accession_number, primary_doc)
        status, body = _http_get(url)
        if status == 200 and body:
            if _looks_like_form4_xml(body):
                return body
            candidates.append((url, body))
    # Walk the index folder for an .xml file.
    idx_status, idx_body = _http_get(_index_url(cik, accession_number))
    if idx_status != 200:
        # If we already collected a body via primary_doc, hand it back —
        # better than nothing for downstream debugging, even if it
        # didn't pass the sniff.
        return candidates[0][1] if candidates else None
    text = idx_body.decode("utf-8", errors="ignore")
    matches = re.findall(r'href="([^"]+\.xml)"', text)
    # De-dupe + put primary_doc-pointed file last so we try fresh ones
    # first (the primary_doc body is already in ``candidates``).
    seen_urls: set = set()
    for href in matches:
        if href.endswith("FilingSummary.xml"):
            continue
        if href.startswith("/"):
            url = f"{SEC_BASE}{href}"
        else:
            url = _index_url(cik, accession_number) + href
        if url in seen_urls:
            continue
        seen_urls.add(url)
        status, body = _http_get(url)
        if status != 200 or not body:
            continue
        if _looks_like_form4_xml(body):
            return body
        candidates.append((url, body))
    # No file passed the sniff — return the best candidate we saw so
    # the parser logs which file it choked on (rather than silently
    # returning None and looking like an empty filing).
    return candidates[0][1] if candidates else None


# ── public fetch ──────────────────────────────────────────────────────


def fetch_form4_filings(ticker: str, since_date: date
                            ) -> List[Form4Transaction]:
    """Return every Form 4 transaction for ``ticker`` with
    ``filing_date >= since_date``. Empty list when SEC has no Form 4s
    or when the CIK cannot be resolved."""
    cik = _resolve_cik(ticker)
    if not cik:
        return []
    filings = _list_form4_filings(cik, since_date)
    if not filings:
        return []
    out: List[Form4Transaction] = []
    for accession, filing_date, primary_doc in filings:
        doc = _fetch_primary_doc(cik, accession, primary_doc)
        if not doc:
            continue
        url = _form4_url(cik, accession,
                           primary_doc or "form4.xml")
        try:
            rows = _parse_form4_xml(
                doc, ticker=ticker, cik=cik,
                accession_number=accession,
                filing_date=filing_date,
                source_url=url,
            )
        except Exception:
            logger.exception(
                "edgar_form4: parse crashed ticker=%s accession=%s",
                ticker, accession,
            )
            continue
        out.extend(rows)
    return out


# ── persistence ───────────────────────────────────────────────────────


def write_insider_trades(rows: List[Form4Transaction]) -> int:
    """Persist Form 4 transactions, idempotent on the row's unique key
    (``cik, accession, insider_name, code, transaction_date, shares,
    price``). Uses SQLite's ``INSERT OR IGNORE`` to skip duplicates
    silently instead of letting the per-row ``IntegrityError`` poison
    the surrounding transaction (the original ORM-add + rollback pattern
    threw away ALL pending inserts on the first dup, so 100% of batches
    landed 0 rows on EC2 reruns)."""
    if not rows:
        return 0
    inserted = 0
    try:
        with session_scope() as s:
            ciks = {r.cik for r in rows}
            accs = {r.accession_number for r in rows}
            existing_rows = s.execute(
                select(
                    InsiderTrade.cik,
                    InsiderTrade.accession_number,
                    InsiderTrade.insider_name,
                    InsiderTrade.transaction_code,
                    InsiderTrade.transaction_date,
                    InsiderTrade.shares,
                    InsiderTrade.price,
                ).where(InsiderTrade.cik.in_(ciks))
                 .where(InsiderTrade.accession_number.in_(accs))
            ).all()
            existing = {tuple(r) for r in existing_rows}
            # Dedupe inside the batch first — the same Form 4 amendment
            # frequently lists the same line twice (correction + add).
            seen_batch_keys: set = set()
            new_payloads: List[Dict[str, Any]] = []
            now = datetime.utcnow()
            for r in rows:
                key = (r.cik, r.accession_number, r.insider_name,
                       r.transaction_code, r.transaction_date,
                       r.shares, r.price)
                if key in existing or key in seen_batch_keys:
                    continue
                seen_batch_keys.add(key)
                new_payloads.append({
                    "ticker": r.ticker, "cik": r.cik,
                    "accession_number": r.accession_number,
                    "filing_date": r.filing_date,
                    "transaction_date": r.transaction_date,
                    "insider_name": r.insider_name[:200],
                    "insider_role": r.insider_role,
                    "transaction_code": r.transaction_code,
                    "shares": r.shares, "price": r.price,
                    "total_value": r.total_value,
                    "is_director": r.is_director,
                    "is_officer": r.is_officer,
                    "is_10pct_owner": r.is_10pct_owner,
                    "source_url": r.source_url,
                    "fetched_at": now,
                })
            if new_payloads:
                bind = s.get_bind()
                dialect_name = getattr(bind.dialect, "name", "") if bind else ""
                if dialect_name == "sqlite":
                    stmt = (
                        sa_insert(InsiderTrade.__table__)
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
                                sa_insert(InsiderTrade.__table__),
                                [payload],
                            )
                            inserted += 1
                        except IntegrityError:
                            s.rollback()
                            continue
    except Exception:
        logger.exception("edgar_form4: write_insider_trades failed")
    return inserted


def write_form4_bronze(ticker: str, rows: List[Form4Transaction],
                            *, chunk_start: date, chunk_end: date) -> None:
    if not rows:
        return
    try:
        from backend.bot.data import lake as _lake
        payload = []
        for r in rows:
            payload.append({
                "ticker": r.ticker, "cik": r.cik,
                "accession_number": r.accession_number,
                "filing_date": r.filing_date.isoformat(),
                "transaction_date": r.transaction_date.isoformat(),
                "insider_name": r.insider_name,
                "insider_role": r.insider_role,
                "transaction_code": r.transaction_code,
                "shares": r.shares, "price": r.price,
                "total_value": r.total_value,
                "is_director": r.is_director,
                "is_officer": r.is_officer,
                "is_10pct_owner": r.is_10pct_owner,
                "source_url": r.source_url,
            })
        _lake.write_bronze(
            source="edgar", dtype="form4",
            payload=payload, ticker=ticker,
            extra_tags={
                "chunk_start": chunk_start.isoformat(),
                "chunk_end": chunk_end.isoformat(),
            },
            request_url="edgar://Form4",
            source_version=__name__,
        )
    except Exception:
        logger.debug("edgar_form4: bronze write failed", exc_info=True)


# ── orchestrator callback ─────────────────────────────────────────────


def edgar_form4_backfill_callback(ticker: str, chunk_start: date,
                                          chunk_end: date) -> CallbackResult:
    if not _user_agent():
        raise RuntimeError(
            "edgar_form4: TB_SEC_USER_AGENT not set; cannot ingest")
    rows = fetch_form4_filings(ticker, chunk_start)
    # Filter to the chunk window — we asked SEC for everything since
    # chunk_start; cap on the upper side here.
    rows = [r for r in rows if r.filing_date <= chunk_end]
    if not rows:
        return CallbackResult(
            last_completed_date=chunk_end, rows_written=0,
            metadata={"reason": "no_filings_in_window"},
        )
    inserted = write_insider_trades(rows)
    write_form4_bronze(ticker, rows,
                          chunk_start=chunk_start, chunk_end=chunk_end)
    last_dt = max(r.filing_date for r in rows)
    return CallbackResult(
        last_completed_date=min(chunk_end, last_dt),
        rows_written=inserted,
        metadata={"transactions_parsed": len(rows)},
    )


__all__ = [
    "Form4Transaction",
    "fetch_form4_filings",
    "write_insider_trades",
    "write_form4_bronze",
    "edgar_form4_backfill_callback",
]
