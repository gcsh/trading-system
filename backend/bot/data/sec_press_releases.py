"""MITS Phase 11.2 — SEC 8-K press-release ingestor (broader than earnings).

The Finnhub Free tier caps company-news at ~1-2 years of history; the
operator wants 5 years. Rather than buying a paid feed, we pull every
8-K press release for each universe ticker from the SEC and write it
into ``news_articles`` as ``source="edgar_8k"``. Exhibit 99.1 (and 99.2
when present) is where companies park product launches, Reg-FD
disclosures, executive appointments, corporate actions, etc.

Differences from :mod:`backend.bot.data.sec_earnings_release`:

  * Earnings module filters to **Item 2.02** (earnings releases) and
    parses fiscal year / quarter. This module accepts **all 8-K items**
    (1.01, 2.01, 5.02, 7.01 etc.) and treats each as a news article.
  * Earnings module writes into ``earnings_transcripts`` +
    ``transcript_paragraphs``. This module writes into ``news_articles``
    so it slots into the existing news + sentiment + Brain-prompt
    pipelines without any downstream changes.
  * Sentiment scoring runs on the press-release headline + first ~2k
    chars of body via the existing FinBERT/VADER fallback in
    :mod:`backend.bot.data.sentiment`. Skipped on parse failure.

Idempotent on (ticker, article_id) where article_id is
``edgar_8k:{accession_no_dashes}:{exhibit_seq}``. Re-runs skip already
ingested filings via SQLite's UNIQUE constraint.

Window: pre-2025 (default). 2025-onward news is already covered by the
Finnhub Free path, and overlapping ingest is wasteful. The CLI accepts
an arbitrary start/end so the operator can shift the window later.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.bot.data.sync_orchestrator import CallbackResult
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.news_article import NewsArticle

# Reuse rate-limited HTTP + CIK resolution from Form 4.
from backend.bot.data.edgar_form4 import (
    SEC_BASE,
    _http_get,
    _resolve_cik,
)
# Reuse earnings-release HTML/PDF parsers + exhibit resolver.
from backend.bot.data.sec_earnings_release import (
    _accession_no_dashes,
    _filing_dir_url,
    _html_to_text,
    _pdf_to_text,
    _list_8k_filings,
)
# Sentiment scoring — same module Finnhub uses so the rows are
# directly comparable downstream.
try:
    from backend.bot.data.sentiment import score_text  # type: ignore
except Exception:  # noqa: BLE001 — fallback so module imports cleanly
    score_text = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# Max chars we'll persist as the article body. Most 8-K press releases
# fit comfortably; the big outliers (full annual proxy embedded) get
# truncated to keep storage + embedding cost bounded.
_MAX_BODY_CHARS = 12_000

# Soft cap on how many bytes of an exhibit we'll download before
# bailing — protects against rogue 100MB PDFs.
_MAX_FETCH_BYTES = 5 * 1024 * 1024


# ── shapes ────────────────────────────────────────────────────────────


@dataclass
class PressRelease:
    ticker: str
    cik: str
    accession_number: str
    filing_date: date
    item_codes: List[str]  # e.g. ["1.01", "7.01"] from the filing index
    exhibit_seq: int       # 1 for Exhibit 99.1, 2 for 99.2, etc.
    exhibit_url: str
    headline: str
    body: str
    parse_failed: bool = False
    sentiment_label: Optional[str] = None
    sentiment_score: Optional[float] = None
    sentiment_model: Optional[str] = None
    raw_meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def article_id(self) -> str:
        # Stable across re-runs (no clock or sequence numbers) so the
        # UNIQUE constraint actually dedupes.
        return f"edgar_8k:{_accession_no_dashes(self.accession_number)}:{self.exhibit_seq}"

    @property
    def published_at(self) -> datetime:
        # SEC filing dates are date-only; we anchor at 16:30 UTC which
        # is post-market close on the East Coast and matches when most
        # 8-Ks file. The exact intra-day stamp isn't load-bearing
        # downstream — the date partition is what the feature layer
        # uses to align with bars.
        return datetime(
            self.filing_date.year,
            self.filing_date.month,
            self.filing_date.day,
            16, 30, 0,
            tzinfo=timezone.utc,
        ).replace(tzinfo=None)


# ── parsing helpers ───────────────────────────────────────────────────


def _is_press_release_8k(filing: Dict[str, Any]) -> bool:
    """Filter heuristic — most 8-Ks ship a press release in Ex-99.1, but
    the empty 8-K/A amendments (no exhibits) waste a fetch. We accept
    any 8-K whose primary document is a press-release-shaped file or
    whose items aren't pure boilerplate (5.07 vote results, 9.01
    exhibit lists with no content)."""
    items = (filing.get("items") or "").lower()
    # 5.07 shareholder votes + 9.01 standalone exhibit lists almost
    # never have a stand-alone press release. Skip them.
    if items in ("5.07", "9.01") and ("press release" not in items):
        return False
    primary = (filing.get("primary_document") or "").lower()
    if "ex99" in primary or "exh99" in primary or "exhibit" in primary:
        return True
    # If the filing index explicitly lists items, it's probably a real
    # 8-K (vs. an /A correction). We'll trust it.
    return bool(items) or bool(primary.endswith(".htm") or primary.endswith(".html"))


def _resolve_press_release_exhibits(
    cik: str, accession_number: str
) -> List[tuple[int, str, str]]:
    """Walk the filing's index.json and return every Ex-99.X exhibit.

    Returns a list of ``(seq, type, url)`` tuples ordered by seq so the
    primary press release (Ex-99.1) is first.
    """
    import json as _json

    base = _filing_dir_url(cik, accession_number)
    try:
        status, body = _http_get(f"{base}index.json")
    except Exception:
        return []
    if status != 200 or not body:
        return []
    try:
        payload = _json.loads(body.decode("utf-8", errors="ignore"))
    except Exception:
        return []
    items = (payload.get("directory") or {}).get("item") or []
    exhibits: List[tuple[int, str, str]] = []
    for entry in items:
        name = (entry.get("name") or "").lower()
        # Look for "ex99-1.htm", "exhibit99-1.pdf", "exh99-2.htm" etc.
        m = re.search(r"ex(?:hibit)?[-_]?99[-_]?(\d+)", name)
        if not m:
            continue
        try:
            seq = int(m.group(1))
        except ValueError:
            continue
        exhibits.append((seq, name, f"{base}{entry.get('name')}"))
    exhibits.sort(key=lambda x: x[0])
    return exhibits


def _derive_headline(body: str) -> str:
    """First non-trivial line of the press-release body. Falls back to
    the first 140 chars if no line break is visible."""
    if not body:
        return "(no content)"
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if len(line) < 12:
            continue
        # Skip header boilerplate like "FOR IMMEDIATE RELEASE" or
        # company addresses; they tend to be ALL CAPS short tokens.
        if line.upper() == line and len(line) < 40:
            continue
        return line[:280]
    return body.strip()[:140]


def _fetch_exhibit_text(url: str) -> str:
    """Pull the exhibit body, HTML/PDF/plain-text auto-detect. Returns
    empty string on any failure."""
    try:
        status, body_bytes = _http_get(url)
    except Exception:
        return ""
    if status != 200 or not body_bytes:
        return ""
    if len(body_bytes) > _MAX_FETCH_BYTES:
        body_bytes = body_bytes[:_MAX_FETCH_BYTES]
    lower_url = url.lower()
    if lower_url.endswith(".pdf"):
        try:
            return _pdf_to_text(body_bytes)
        except Exception:
            return ""
    # Try HTML first; if it looks like raw text already, just decode.
    try:
        text_decoded = body_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    if "<html" in text_decoded.lower() or "<body" in text_decoded.lower():
        try:
            return _html_to_text(text_decoded)
        except Exception:
            return text_decoded
    return text_decoded


def _fetch_and_parse_release(
    filing: Dict[str, Any], *, ticker: str, cik: str
) -> List[PressRelease]:
    """For one 8-K filing, fetch every Ex-99 exhibit and turn each into
    a PressRelease row."""
    accession = filing.get("accession_number") or filing.get("accessionNumber") or ""
    if not accession:
        return []
    try:
        filing_date = datetime.strptime(
            filing.get("filing_date") or filing.get("filingDate"),
            "%Y-%m-%d",
        ).date()
    except Exception:
        return []
    items = [
        i.strip() for i in (filing.get("items") or "").split(",") if i.strip()
    ]
    exhibits = _resolve_press_release_exhibits(cik, accession)
    if not exhibits:
        return []
    releases: List[PressRelease] = []
    for seq, _, url in exhibits:
        body = _fetch_exhibit_text(url)
        if not body:
            releases.append(
                PressRelease(
                    ticker=ticker,
                    cik=cik,
                    accession_number=accession,
                    filing_date=filing_date,
                    item_codes=items,
                    exhibit_seq=seq,
                    exhibit_url=url,
                    headline="(parse failed)",
                    body="",
                    parse_failed=True,
                )
            )
            continue
        body = body.strip()
        if len(body) > _MAX_BODY_CHARS:
            body = body[:_MAX_BODY_CHARS]
        headline = _derive_headline(body)
        rel = PressRelease(
            ticker=ticker,
            cik=cik,
            accession_number=accession,
            filing_date=filing_date,
            item_codes=items,
            exhibit_seq=seq,
            exhibit_url=url,
            headline=headline,
            body=body,
        )
        if score_text is not None:
            try:
                label, score, model = score_text(headline + "\n\n" + body[:1800])
                rel.sentiment_label = label
                rel.sentiment_score = float(score) if score is not None else None
                rel.sentiment_model = model
            except Exception:
                pass
        releases.append(rel)
    return releases


# ── persistence ───────────────────────────────────────────────────────


def write_press_release_rows(releases: List[PressRelease]) -> int:
    """Insert one row per release into ``news_articles``. Skips rows
    that already exist via the UNIQUE constraint."""
    if not releases:
        return 0
    inserted = 0
    with session_scope() as s:
        for rel in releases:
            if rel.parse_failed:
                continue
            existing = s.execute(
                select(NewsArticle.id)
                .where(NewsArticle.ticker == rel.ticker)
                .where(NewsArticle.article_id == rel.article_id)
            ).first()
            if existing is not None:
                continue
            row = NewsArticle(
                article_id=rel.article_id,
                ticker=rel.ticker,
                headline=rel.headline,
                summary=rel.body[:2000] if rel.body else None,
                source="edgar_8k",
                published_at=rel.published_at,
                url=rel.exhibit_url,
                category="press_release_" + (",".join(rel.item_codes) or "unknown"),
                sentiment_label=rel.sentiment_label,
                sentiment_score=rel.sentiment_score,
                sentiment_model=rel.sentiment_model,
            )
            s.add(row)
            inserted += 1
    return inserted


def write_press_release_bronze(rel: PressRelease) -> None:
    """Write the raw exhibit body to the bronze lake. Best-effort —
    failures are logged but not raised."""
    if rel.parse_failed:
        return
    try:
        from backend.bot.data.lake import write_bronze
    except Exception:
        return
    try:
        ts = datetime(
            rel.filing_date.year, rel.filing_date.month, rel.filing_date.day
        )
        write_bronze(
            source="sec_press_releases",
            dtype="press_release",
            payload={
                "accession": rel.accession_number,
                "exhibit_seq": rel.exhibit_seq,
                "exhibit_url": rel.exhibit_url,
                "items": rel.item_codes,
                "headline": rel.headline,
                "body": rel.body,
                "filing_date": rel.filing_date.isoformat(),
                "sentiment_label": rel.sentiment_label,
                "sentiment_score": rel.sentiment_score,
                "sentiment_model": rel.sentiment_model,
                "ticker": rel.ticker,
            },
            ts=ts,
            ticker=rel.ticker,
            extra_tags={"exhibit_seq": rel.exhibit_seq},
            request_url=rel.exhibit_url,
        )
    except Exception:
        logger.debug("bronze write failed for %s", rel.article_id, exc_info=True)


# ── public API ────────────────────────────────────────────────────────


def fetch_press_releases(
    ticker: str, start_date: date, end_date: date
) -> List[PressRelease]:
    """Walk 8-K filings in [start_date, end_date] for ``ticker`` and
    return every Ex-99 press release we successfully parsed."""
    cik = _resolve_cik(ticker)
    if not cik:
        return []
    filings = _list_8k_filings(cik, start_date)
    out: List[PressRelease] = []
    for f in filings:
        try:
            fd = datetime.strptime(
                f.get("filing_date") or f.get("filingDate"), "%Y-%m-%d"
            ).date()
        except Exception:
            continue
        if fd < start_date or fd > end_date:
            continue
        if not _is_press_release_8k(f):
            continue
        try:
            releases = _fetch_and_parse_release(f, ticker=ticker, cik=cik)
        except Exception:
            logger.exception(
                "press-release parse failed ticker=%s accession=%s",
                ticker, f.get("accession_number") or f.get("accessionNumber"),
            )
            continue
        out.extend(releases)
    return out


def sec_press_releases_backfill_callback(
    ticker: str, chunk_start: date, chunk_end: date
) -> CallbackResult:
    """SyncOrchestrator-shaped callback. Idempotent on
    (ticker, article_id) via the news_articles UNIQUE constraint."""
    releases = fetch_press_releases(ticker, chunk_start, chunk_end)
    if not releases:
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=0,
            metadata={"reason": "no_filings_in_window"},
        )
    inserted = write_press_release_rows(releases)
    for rel in releases:
        write_press_release_bronze(rel)
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
    "PressRelease",
    "fetch_press_releases",
    "write_press_release_rows",
    "write_press_release_bronze",
    "sec_press_releases_backfill_callback",
]
