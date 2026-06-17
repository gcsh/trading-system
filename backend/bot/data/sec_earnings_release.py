"""MITS Phase 11.1 — SEC 8-K earnings-release ingestor.

A 100% free public-source replacement for the (operator-blocked)
AlphaVantage Premium earnings transcripts path. For each universe
ticker:

  1. List every 8-K filing since ``since_date`` via the EDGAR
     submissions JSON (same path used by Form 4).
  2. Filter to **earnings 8-Ks** — those with ``Item 2.02 Results of
     Operations and Financial Condition`` in the filing index, OR an
     item header mentioning "earnings release" / "results of
     operations" / "financial results" / "quarterly results".
  3. Download Exhibit 99.1 (the press release with management
     commentary). HTML → plain text via BeautifulSoup. PDF fallback
     via pdfplumber.
  4. Parse: company + fiscal quarter (heuristic), filing date,
     management commentary, financial highlights. Persist to
     ``earnings_transcripts`` as one row per (ticker, fiscal_year,
     fiscal_quarter) — same schema as the AlphaVantage path so the
     downstream embed + brain prompts don't care which source filled
     the row.
  5. Bronze write to
     ``s3://<lake-bucket>/bronze/sec_8k_earnings/dt=YYYY-MM-DD/ticker=X/*.parquet``.
  6. The press release is split into 1+ paragraphs and written to
     ``transcript_paragraphs`` so the existing
     ``earnings_call_paragraph`` embedding namespace picks them up
     unmodified.

What we explicitly do NOT do (and why):

  * No Q&A reconstruction. 8-K exhibit 99.1 is the prepared press
    release only — there is no Q&A in the public-source path. We
    leave ``qa_section`` empty (and tag the metadata so the UI can
    explain).
  * No phone-call transcript. Companies do not file the call audio
    transcripts as 8-K exhibits — those live behind paywalls
    (AlphaVantage, Bloomberg, FactSet, IR-Connect). This module is
    the **legally-public real management commentary** for free.

Rate limit: SEC's 10 req/sec ceiling, throttled via the shared
``_edgar_bucket`` from :mod:`backend.bot.data.edgar_form4`.

Idempotent on (ticker, fiscal_year, fiscal_quarter). Re-runs skip
already-persisted quarters via the ``UniqueConstraint`` on
``EarningsTranscript``.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, insert as sa_insert
from sqlalchemy.exc import IntegrityError

from backend.bot.data.sync_orchestrator import CallbackResult
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.earnings_transcript import EarningsTranscript
from backend.models.transcript_paragraph import TranscriptParagraph

# Reuse the rate-limited HTTP helpers + CIK resolution from Form 4.
from backend.bot.data.edgar_form4 import (
    SEC_BASE, SEC_DATA_BASE,
    _http_get, _resolve_cik, _edgar_bucket,
)

logger = logging.getLogger(__name__)


# Item codes / keywords on the filing index that signal an earnings 8-K.
EARNINGS_ITEM_KEYWORDS = (
    "2.02",            # The canonical "Results of Operations"
    "results of operations",
    "earnings release",
    "financial results",
    "quarterly results",
    "earnings",
)


# ── shapes ────────────────────────────────────────────────────────────


@dataclass
class EarningsRelease:
    ticker: str
    cik: str
    accession_number: str
    filing_date: date
    fiscal_year: int
    fiscal_quarter: int
    company_name: str
    full_text: str
    prepared_remarks: str   # the management commentary section
    financial_highlights: str  # short structured-ish snippet
    paragraphs: List[str]   # split into ~paragraph chunks for embedding
    source_url: str
    exhibit_url: Optional[str]
    parse_failed: bool = False
    raw_meta: Dict[str, Any] = field(default_factory=dict)


# ── filings list (only 8-K, only earnings items) ──────────────────────


def _list_8k_filings(cik: str, since: date) -> List[Dict[str, Any]]:
    """Return ``[{accession, filing_date, primary_document, items}, ...]``
    for every 8-K filing for ``cik`` with ``filing_date >= since``.

    The ``items`` field is the EDGAR index-line text (e.g.
    ``"2.02,9.01"``) so the caller can pre-filter to earnings 8-Ks
    without fetching the index HTML.
    """
    url = f"{SEC_DATA_BASE}/submissions/CIK{cik}.json"
    status, body = _http_get(url)
    if status == 404:
        return []
    if status != 200:
        raise RuntimeError(
            f"sec_earnings_release: submissions fetch failed "
            f"status={status} cik={cik}")
    payload = json.loads(body.decode("utf-8"))
    out: List[Dict[str, Any]] = []

    def _ingest(block: Dict[str, Any]) -> None:
        accs = block.get("accessionNumber") or []
        forms = block.get("form") or []
        dates = block.get("filingDate") or []
        prims = block.get("primaryDocument") or []
        items = block.get("items") or []
        for i in range(len(accs)):
            form = forms[i] if i < len(forms) else ""
            if form != "8-K":
                continue
            try:
                fd = datetime.strptime(dates[i], "%Y-%m-%d").date()
            except Exception:
                continue
            if fd < since:
                continue
            out.append({
                "accession": accs[i],
                "filing_date": fd,
                "primary_document": prims[i] if i < len(prims) else "",
                "items": items[i] if i < len(items) else "",
            })

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
                "sec_earnings_release: failed to walk older submissions "
                "file %s for cik=%s", name, cik, exc_info=True,
            )
            continue
    return out


def _is_earnings_8k(filing: Dict[str, Any]) -> bool:
    """True iff the 8-K is the standard quarterly earnings release."""
    items = (filing.get("items") or "").lower()
    for kw in EARNINGS_ITEM_KEYWORDS:
        if kw in items:
            return True
    # Some companies don't include item codes in the JSON index. Fall
    # back to letting the caller fetch the index page and look for an
    # exhibit 99.x with "earnings" in the description — we DON'T do that
    # here because it would double our HTTP budget. The shed of false
    # negatives is the well-flagged "earnings" releases that someone
    # filed without item codes (rare but exists).
    return False


# ── filing index → exhibit 99.1 URL ───────────────────────────────────


def _accession_no_dashes(accession_number: str) -> str:
    return accession_number.replace("-", "")


def _filing_dir_url(cik: str, accession_number: str) -> str:
    cik_int = str(int(cik))
    return (
        f"{SEC_BASE}/Archives/edgar/data/{cik_int}/"
        f"{_accession_no_dashes(accession_number)}/"
    )


def _resolve_exhibit_99_url(cik: str, accession_number: str,
                                  primary_document: str
                                  ) -> Optional[str]:
    """Find Exhibit 99.1 inside the filing directory.

    Strategy:
      1. Probe the ``index.json`` file (EDGAR's modern manifest) —
         lists every file in the accession with their description.
      2. Filter to ``.htm`` / ``.html`` files whose description /
         filename hints at exhibit 99.1.
      3. Fall back to the primary document if no 99.1 is found
         (sometimes companies file the press release as the primary
         doc with item 2.02 inline).
    """
    base = _filing_dir_url(cik, accession_number)
    idx_url = f"{base}index.json"
    try:
        status, body = _http_get(idx_url)
    except Exception:
        return f"{base}{primary_document}" if primary_document else None
    if status != 200:
        return f"{base}{primary_document}" if primary_document else None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return f"{base}{primary_document}" if primary_document else None
    items = ((payload.get("directory") or {}).get("item") or [])

    candidates_99_1: List[str] = []
    candidates_99_x: List[str] = []
    candidates_press: List[str] = []
    for it in items:
        name = (it.get("name") or "").lower()
        if not name.endswith((".htm", ".html", ".txt", ".pdf")):
            continue
        # Filter out the index + the FilingSummary itself
        if name in ("0", "0.htm", "filingsummary.xml", "filingsummary.htm"):
            continue
        if name == primary_document.lower():
            # Sometimes the primary doc IS the press release.
            pass
        if "ex99-1" in name or "ex991" in name or "exhibit99-1" in name or \
           "exhibit991" in name or "ex-99-1" in name or "ex-99_1" in name:
            candidates_99_1.append(it.get("name"))
        elif name.startswith(("ex99", "exhibit99", "ex-99", "exh99")):
            candidates_99_x.append(it.get("name"))
        elif "press" in name or "release" in name or "earnings" in name:
            candidates_press.append(it.get("name"))

    chosen = (candidates_99_1[:1] or candidates_99_x[:1]
              or candidates_press[:1])
    if chosen:
        return f"{base}{chosen[0]}"
    if primary_document:
        return f"{base}{primary_document}"
    return None


# ── HTML / PDF → plain text ───────────────────────────────────────────


_WHITESPACE = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text using BeautifulSoup if available, else
    a regex fallback. Press releases generally use minimal markup so the
    fallback is usable — but bs4 is in our requirements already."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        # Drop boilerplate: scripts, styles, hidden divs.
        for tag in soup(["script", "style", "head", "title", "meta"]):
            tag.decompose()
        # Convert <br> + <p> to newlines for paragraph-grain.
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for p in soup.find_all(("p", "div", "li", "tr")):
            p.append("\n")
        text = soup.get_text(separator=" ")
    except Exception:
        # Regex fallback.
        text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
        text = re.sub(r"</(p|div|li|tr)>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        # Decode HTML entities (best effort).
        import html as _html_mod
        text = _html_mod.unescape(text)
    # Normalize whitespace per-line.
    out_lines: List[str] = []
    for ln in text.splitlines():
        ln = _WHITESPACE.sub(" ", ln).strip()
        if ln:
            out_lines.append(ln)
    return "\n".join(out_lines)


def _pdf_to_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF press release.

    pdfplumber is preferred (handles tables better) but it's a heavy
    dep — if it isn't installed we fall back to PyPDF2 which is in our
    requirements already.
    """
    try:
        import pdfplumber  # type: ignore
        import io
        out: List[str] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t:
                    out.append(t)
        return "\n".join(out)
    except ImportError:
        pass
    try:
        import PyPDF2  # type: ignore
        import io
        out: List[str] = []
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        for page in reader.pages:
            t = page.extract_text() or ""
            if t:
                out.append(t)
        return "\n".join(out)
    except Exception:
        logger.debug("PDF extract failed (no pdfplumber + PyPDF2)",
                       exc_info=True)
        return ""


# ── parsing: company / fiscal period heuristics ───────────────────────


# Catches "Q3 2025", "third quarter 2025", "Q3 fiscal 2025", etc.
_QUARTER_PATTERNS = [
    re.compile(r"\b(?:fiscal\s+)?Q([1-4])\s+(?:fiscal\s+)?(\d{4})\b", re.I),
    re.compile(r"\b(first|second|third|fourth)\s+quarter\s+(?:of\s+)?(?:fiscal\s+)?(\d{4})\b",
                  re.I),
    re.compile(r"\b(\d{4})\s+Q([1-4])\b", re.I),
    # Annual call (Q4 + year-end)
    re.compile(r"\bfull[- ]year\s+(?:results?\s+for\s+)?(?:fiscal\s+)?(\d{4})\b",
                  re.I),
]
_WORD_TO_QUARTER = {"first": 1, "second": 2, "third": 3, "fourth": 4}


def _guess_fiscal_period(text: str, filing_date: date
                              ) -> Tuple[int, int]:
    """Return (fiscal_year, fiscal_quarter). Falls back to deriving from
    the filing date if no explicit token is found in the text."""
    head = text[:4000]   # only scan the header — body text often
                         # repeats the period in misleading places
    for pat in _QUARTER_PATTERNS:
        m = pat.search(head)
        if not m:
            continue
        groups = m.groups()
        # Q\d patterns: groups = (quarter, year)
        if pat.pattern.startswith(r"\b(?:fiscal\s+)?Q"):
            q = int(groups[0]); y = int(groups[1])
            return (y, q)
        # "first/second/third/fourth quarter YYYY"
        if "quarter" in pat.pattern.lower():
            q = _WORD_TO_QUARTER.get(groups[0].lower(), 0)
            y = int(groups[1])
            if q:
                return (y, q)
        # "YYYY Q\d"
        if pat.pattern.startswith(r"\b(\d{4})"):
            y = int(groups[0]); q = int(groups[1])
            return (y, q)
        # Annual results
        if "full" in pat.pattern.lower():
            y = int(groups[0])
            return (y, 4)
    # Fallback: derive from filing date (calendar quarter ending in the
    # prior month). Most companies file 8-Ks for the quarter that ended
    # 4-6 weeks before — picking the *prior* calendar quarter is right
    # 80% of the time. Fiscal-year companies (NVDA, ADBE, MU, etc) will
    # be off by one quarter but the embedding pipeline doesn't care
    # about exact quarter alignment.
    prev_month = (filing_date.month - 2) % 12 + 1
    prev_year = filing_date.year if filing_date.month > 2 else filing_date.year - 1
    q = (prev_month - 1) // 3 + 1
    return (prev_year, q)


def _split_prepared_remarks(text: str) -> Tuple[str, str]:
    """Return (prepared_remarks, financial_highlights).

    Press releases tend to have the form:
        <opening 1-2 paragraphs: headline + revenue / EPS> <- highlights
        <CEO quote>
        <CFO quote>
        <2-5 paragraphs of management commentary> <- prepared remarks
        <forward-looking statements / boilerplate>

    We extract the first 2 paragraphs as "highlights" (revenue/EPS bullet
    points) and everything between the first ``"--"`` / financial table
    and the boilerplate as "prepared remarks". The split is heuristic;
    operator should treat both fields as best-effort.
    """
    if not text:
        return ("", "")
    # Strip the trailing boilerplate ("safe harbor" / "forward-looking"
    # statements) — these are noise for the embedding layer.
    boilerplate_markers = [
        "forward-looking statements",
        "private securities litigation reform act",
        "safe harbor",
        "non-gaap financial",
    ]
    lower = text.lower()
    cut = len(text)
    for m in boilerplate_markers:
        i = lower.find(m)
        if i > 0 and i < cut:
            cut = i
    body = text[:cut].strip()
    paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
    # The first 3-5 lines are usually the heading + revenue bullets.
    highlights = "\n".join(paragraphs[:5])
    # Remainder is "prepared remarks" — CEO/CFO quotes + outlook.
    remarks = "\n".join(paragraphs[5:])
    return (remarks, highlights)


def _split_paragraphs(text: str, *, max_chars: int = 1200,
                          min_chars: int = 50) -> List[str]:
    """Split ``text`` into ~1k-char paragraphs for paragraph-level
    embedding. Greedy line-pack — don't break mid-sentence.

    Returns paragraphs >= ``min_chars`` (cuts micro-bullets like
    "Q3 2025") and <= ``max_chars`` (caps over-long press releases at
    something the sentence-transformer can ingest in one pass)."""
    if not text:
        return []
    raw_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: List[str] = []
    buf: List[str] = []
    buflen = 0
    for ln in raw_lines:
        if buflen + len(ln) + 1 > max_chars and buflen > min_chars:
            joined = "\n".join(buf).strip()
            if len(joined) >= min_chars:
                out.append(joined)
            buf = [ln]
            buflen = len(ln)
        else:
            buf.append(ln)
            buflen += len(ln) + 1
    if buf:
        joined = "\n".join(buf).strip()
        if len(joined) >= min_chars:
            out.append(joined)
    return out


# ── one-filing fetch + parse ──────────────────────────────────────────


def _fetch_and_parse_release(filing: Dict[str, Any], *,
                                  ticker: str, cik: str
                                  ) -> Optional[EarningsRelease]:
    accession = filing["accession"]
    filing_date = filing["filing_date"]
    primary_doc = filing.get("primary_document") or ""
    exhibit_url = _resolve_exhibit_99_url(cik, accession, primary_doc)
    if not exhibit_url:
        logger.debug("sec_earnings_release: no exhibit url for %s/%s",
                        cik, accession)
        return None
    try:
        status, body = _http_get(exhibit_url)
    except Exception:
        logger.warning("sec_earnings_release: fetch exhibit failed %s",
                          exhibit_url, exc_info=True)
        return None
    if status != 200 or not body:
        logger.warning(
            "sec_earnings_release: exhibit status=%s ticker=%s acc=%s",
            status, ticker, accession,
        )
        return None
    ext = exhibit_url.rsplit(".", 1)[-1].lower()
    parse_failed = False
    text = ""
    if ext in ("htm", "html"):
        try:
            text = _html_to_text(body.decode("utf-8", errors="ignore"))
        except Exception:
            logger.warning(
                "sec_earnings_release: HTML decode failed %s", exhibit_url,
                exc_info=True,
            )
            parse_failed = True
    elif ext == "pdf":
        try:
            text = _pdf_to_text(body)
            if not text.strip():
                parse_failed = True
        except Exception:
            logger.warning(
                "sec_earnings_release: PDF parse failed %s", exhibit_url,
                exc_info=True,
            )
            parse_failed = True
    else:
        text = body.decode("utf-8", errors="ignore")
    # Trim runaway. Capping at 200KB keeps SQLite row sizes sane.
    if len(text) > 200_000:
        text = text[:200_000]
    if not text or len(text) < 200:
        parse_failed = True

    fiscal_year, fiscal_quarter = _guess_fiscal_period(text, filing_date)
    remarks, highlights = _split_prepared_remarks(text)
    paragraphs = _split_paragraphs(text)

    # Extract company name (heuristic — first big-fonted line).
    company_name = ""
    for ln in text.splitlines()[:8]:
        if 4 < len(ln) < 120 and not ln.lower().startswith(("q1", "q2", "q3", "q4", "fiscal")):
            company_name = ln
            break

    src_url = exhibit_url
    return EarningsRelease(
        ticker=ticker.upper(),
        cik=cik,
        accession_number=accession,
        filing_date=filing_date,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        company_name=company_name,
        full_text=text,
        prepared_remarks=remarks,
        financial_highlights=highlights,
        paragraphs=paragraphs,
        source_url=src_url,
        exhibit_url=exhibit_url,
        parse_failed=parse_failed,
        raw_meta={
            "items": filing.get("items"),
            "primary_document": primary_doc,
            "exhibit_url": exhibit_url,
            "exhibit_ext": ext,
            "byte_count": len(body),
            "text_len": len(text),
        },
    )


# ── persistence ───────────────────────────────────────────────────────


def write_release_rows(releases: List[EarningsRelease]) -> int:
    """Persist one EarningsTranscript per (ticker, fiscal_year,
    fiscal_quarter). Skip duplicates via the unique constraint."""
    if not releases:
        return 0
    inserted = 0
    for rel in releases:
        try:
            with session_scope() as s:
                # Skip if already present.
                existing = s.execute(
                    select(EarningsTranscript.id)
                    .where(EarningsTranscript.ticker == rel.ticker)
                    .where(EarningsTranscript.fiscal_year == rel.fiscal_year)
                    .where(EarningsTranscript.fiscal_quarter == rel.fiscal_quarter)
                ).first()
                if existing:
                    continue
                row = EarningsTranscript(
                    ticker=rel.ticker,
                    fiscal_year=rel.fiscal_year,
                    fiscal_quarter=rel.fiscal_quarter,
                    report_date=rel.filing_date,
                    full_text=(rel.full_text or "")[:2_000_000],
                    metadata_json=json.dumps({
                        "source": "sec_8k_earnings_release",
                        "accession_number": rel.accession_number,
                        "cik": rel.cik,
                        "company_name": rel.company_name,
                        "exhibit_url": rel.exhibit_url,
                        "source_url": rel.source_url,
                        "parse_failed": rel.parse_failed,
                        "raw_meta": rel.raw_meta,
                        "qa_section_note": (
                            "8-K Exhibit 99.1 is the prepared press "
                            "release only — there is no Q&A section in "
                            "this public-source path. See "
                            "alphavantage_transcripts for the full "
                            "Q&A path if subscribed."
                        ),
                    }),
                    paragraph_count=len(rel.paragraphs),
                    fetched_at=datetime.utcnow(),
                )
                s.add(row)
                s.flush()  # we need row.id for paragraph FK
                # Paragraph fan-out.
                for idx, para in enumerate(rel.paragraphs):
                    p = TranscriptParagraph(
                        transcript_id=row.id,
                        ticker=rel.ticker,
                        fiscal_year=rel.fiscal_year,
                        fiscal_quarter=rel.fiscal_quarter,
                        paragraph_index=idx,
                        speaker="prepared_remarks",
                        speaker_title=None,
                        content=para[:8000],
                    )
                    s.add(p)
                inserted += 1
        except IntegrityError:
            continue
        except Exception:
            logger.exception(
                "sec_earnings_release: write failed ticker=%s acc=%s",
                rel.ticker, rel.accession_number,
            )
    return inserted


def write_release_bronze(release: EarningsRelease) -> None:
    try:
        from backend.bot.data import lake as _lake
        payload = [{
            "ticker": release.ticker,
            "cik": release.cik,
            "accession_number": release.accession_number,
            "filing_date": release.filing_date.isoformat(),
            "fiscal_year": release.fiscal_year,
            "fiscal_quarter": release.fiscal_quarter,
            "company_name": release.company_name,
            "full_text": release.full_text,
            "prepared_remarks": release.prepared_remarks,
            "financial_highlights": release.financial_highlights,
            "paragraph_count": len(release.paragraphs),
            "source_url": release.source_url,
            "exhibit_url": release.exhibit_url,
            "parse_failed": release.parse_failed,
        }]
        _lake.write_bronze(
            source="sec_8k_earnings",
            dtype="earnings_release",
            payload=payload,
            ticker=release.ticker,
            extra_tags={
                "accession_number": release.accession_number,
                "fiscal_year": str(release.fiscal_year),
                "fiscal_quarter": str(release.fiscal_quarter),
                "filing_date": release.filing_date.isoformat(),
            },
            request_url=release.source_url,
            source_version=__name__,
        )
    except Exception:
        logger.debug("sec_earnings_release: bronze write failed",
                        exc_info=True)


# ── public API ────────────────────────────────────────────────────────


def fetch_earnings_releases(ticker: str, start_date: date,
                                  end_date: date,
                                  *,
                                  max_filings: Optional[int] = None
                                  ) -> List[EarningsRelease]:
    """Return parsed earnings releases for ``ticker`` filed in
    ``[start_date, end_date]``. Includes failed parses (with
    ``parse_failed=True``) so the operator can see what was skipped."""
    cik = _resolve_cik(ticker)
    if not cik:
        logger.warning("sec_earnings_release: no CIK for ticker=%s", ticker)
        return []
    filings = _list_8k_filings(cik, since=start_date)
    # Filter to earnings 8-Ks (items contain "2.02") + within window.
    earnings_filings = [
        f for f in filings
        if _is_earnings_8k(f) and f["filing_date"] <= end_date
    ]
    if max_filings:
        earnings_filings = earnings_filings[: max_filings]
    out: List[EarningsRelease] = []
    for f in earnings_filings:
        rel = _fetch_and_parse_release(f, ticker=ticker, cik=cik)
        if rel is not None:
            out.append(rel)
    return out


# ── orchestrator callback ─────────────────────────────────────────────


def sec_earnings_release_backfill_callback(ticker: str, chunk_start: date,
                                                  chunk_end: date
                                                  ) -> CallbackResult:
    """SyncOrchestrator-shaped callback. Each chunk pulls one window of
    8-Ks; the orchestrator already paces the outer iteration. Idempotent
    on (ticker, fiscal_year, fiscal_quarter)."""
    releases = fetch_earnings_releases(ticker, chunk_start, chunk_end)
    if not releases:
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=0,
            metadata={"reason": "no_filings_in_window"},
        )
    inserted = write_release_rows(releases)
    for rel in releases:
        write_release_bronze(rel)
    parse_failed = sum(1 for r in releases if r.parse_failed)
    return CallbackResult(
        last_completed_date=chunk_end,
        rows_written=inserted,
        metadata={
            "filings_seen": len(releases),
            "parse_failed": parse_failed,
        },
    )


__all__ = [
    "EarningsRelease",
    "fetch_earnings_releases",
    "write_release_rows",
    "write_release_bronze",
    "sec_earnings_release_backfill_callback",
]
