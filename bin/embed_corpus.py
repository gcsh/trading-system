#!/usr/bin/env python
"""MITS Phase 11.K — paragraph-level vector embedding walker.

Walks every Phase 11 text/structured table and embeds rows that don't
yet have a corresponding pgvector entry. Idempotent: re-running skips
already-embedded rows by checking ``vector_entries`` for the same key.

Namespaces touched:

  * ``news_paragraph``           — one vec per NewsArticle row
  * ``earnings_call_paragraph``  — one vec per TranscriptParagraph row
  * ``insider_form4_narrative``  — one vec per InsiderTrade row
  * ``fund_holding_change``      — one vec per FundHolding row
  * ``regime_snapshot_v2``       — daily macro+regime fingerprint built
                                       from the new 50-series FRED panel

Usage:

    python bin/embed_corpus.py --kinds all
    python bin/embed_corpus.py --kinds insider,fund_holdings
    python bin/embed_corpus.py --kinds regime --start 2021-01-01

The script is read-only against silver/raw tables — it only writes to
pgvector. Safe to run alongside backfills.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s — %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout)


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


# ── existing-keys cache ───────────────────────────────────────────────


def _existing_keys(namespace: str) -> Set[str]:
    """Return the set of pgvector keys already in ``namespace``. Empty
    set when pgvector is unreachable (in which case the walker silently
    no-ops upserts that would otherwise re-embed)."""
    out: Set[str] = set()
    try:
        from backend.bot.ai.vector_store import _conn_handle
        conn = _conn_handle()
        if conn is None:
            return out
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key FROM vector_entries WHERE namespace = %s",
                (namespace,),
            )
            for row in cur.fetchall():
                out.add(str(row[0]))
    except Exception:
        logging.getLogger(__name__).debug(
            "_existing_keys(%s) failed", namespace, exc_info=True,
        )
    return out


# ── per-kind walkers ──────────────────────────────────────────────────


def _walk_news(logger: logging.Logger) -> Dict[str, int]:
    stats = {"seen": 0, "embedded": 0, "skipped": 0, "errors": 0}
    try:
        from sqlalchemy import select
        from backend.db import session_scope
        from backend.models.news_article import NewsArticle
        from backend.bot.ai.vector_store import index_news_paragraph
    except Exception:
        return stats
    existing = _existing_keys("news_paragraph")
    with session_scope() as s:
        rows = s.execute(select(NewsArticle)).scalars().all()
        # Detach into plain dicts so we can close the session before
        # the slow per-row embed loop.
        records = []
        for r in rows:
            records.append({
                "article_id": r.article_id, "ticker": r.ticker,
                "headline": r.headline, "summary": r.summary or "",
                "published_at": (r.published_at.isoformat()
                                       if r.published_at else ""),
                "sentiment_label": r.sentiment_label,
                "sentiment_score": r.sentiment_score,
            })
    for rec in records:
        stats["seen"] += 1
        key = f"{rec['ticker']}:{rec['article_id']}"
        if key in existing:
            stats["skipped"] += 1
            continue
        ok = False
        try:
            ok = index_news_paragraph(
                article_id=rec["article_id"], ticker=rec["ticker"],
                headline=rec["headline"], summary=rec["summary"],
                published_iso=rec["published_at"],
                sentiment_label=rec["sentiment_label"],
                sentiment_score=rec["sentiment_score"],
            )
        except Exception:
            logger.debug("news embed failed", exc_info=True)
            stats["errors"] += 1
            continue
        if ok:
            stats["embedded"] += 1
        else:
            stats["errors"] += 1
    return stats


def _walk_transcripts(logger: logging.Logger) -> Dict[str, int]:
    stats = {"seen": 0, "embedded": 0, "skipped": 0, "errors": 0}
    try:
        from sqlalchemy import select
        from backend.db import session_scope
        from backend.models.transcript_paragraph import TranscriptParagraph
        from backend.bot.ai.vector_store import index_earnings_call_paragraph
    except Exception:
        return stats
    existing = _existing_keys("earnings_call_paragraph")
    with session_scope() as s:
        rows = s.execute(select(TranscriptParagraph)).scalars().all()
        records = []
        for r in rows:
            records.append({
                "id": r.id, "ticker": r.ticker,
                "fiscal_year": r.fiscal_year,
                "fiscal_quarter": r.fiscal_quarter,
                "paragraph_index": r.paragraph_index,
                "speaker": r.speaker,
                "speaker_title": r.speaker_title,
                "content": r.content or "",
            })
    for rec in records:
        stats["seen"] += 1
        key = (f"{rec['ticker']}:{rec['fiscal_year']}Q"
                  f"{rec['fiscal_quarter']}:{rec['paragraph_index']}")
        if key in existing:
            stats["skipped"] += 1
            continue
        ok = False
        try:
            ok = index_earnings_call_paragraph(
                paragraph_id=rec["id"], ticker=rec["ticker"],
                fiscal_year=rec["fiscal_year"],
                fiscal_quarter=rec["fiscal_quarter"],
                paragraph_index=rec["paragraph_index"],
                speaker=rec["speaker"],
                speaker_title=rec["speaker_title"],
                content=rec["content"],
            )
        except Exception:
            logger.debug("transcript embed failed", exc_info=True)
            stats["errors"] += 1
            continue
        if ok:
            stats["embedded"] += 1
        else:
            stats["errors"] += 1
    return stats


def _walk_insider(logger: logging.Logger) -> Dict[str, int]:
    stats = {"seen": 0, "embedded": 0, "skipped": 0, "errors": 0}
    try:
        from sqlalchemy import select
        from backend.db import session_scope
        from backend.models.insider_trade import InsiderTrade
        from backend.bot.ai.vector_store import index_insider_form4_narrative
    except Exception:
        return stats
    existing = _existing_keys("insider_form4_narrative")
    with session_scope() as s:
        rows = s.execute(select(InsiderTrade)).scalars().all()
        records = []
        for r in rows:
            records.append({
                "id": r.id, "ticker": r.ticker,
                "insider_name": r.insider_name,
                "insider_role": r.insider_role,
                "transaction_code": r.transaction_code,
                "shares": r.shares, "price": r.price,
                "total_value": r.total_value,
                "transaction_date": (r.transaction_date.isoformat()
                                            if r.transaction_date else ""),
            })
    for rec in records:
        stats["seen"] += 1
        key = str(rec["id"])
        if key in existing:
            stats["skipped"] += 1
            continue
        ok = False
        try:
            ok = index_insider_form4_narrative(
                trade_id=rec["id"], ticker=rec["ticker"],
                insider_name=rec["insider_name"],
                insider_role=rec["insider_role"],
                transaction_code=rec["transaction_code"],
                shares=rec["shares"], price=rec["price"],
                total_value=rec["total_value"],
                transaction_date_iso=rec["transaction_date"],
            )
        except Exception:
            logger.debug("insider embed failed", exc_info=True)
            stats["errors"] += 1
            continue
        if ok:
            stats["embedded"] += 1
        else:
            stats["errors"] += 1
    return stats


def _walk_fund_holdings(logger: logging.Logger) -> Dict[str, int]:
    stats = {"seen": 0, "embedded": 0, "skipped": 0, "errors": 0}
    try:
        from sqlalchemy import select
        from backend.db import session_scope
        from backend.models.fund_holding import FundHolding
        from backend.bot.ai.vector_store import index_fund_holding_change
    except Exception:
        return stats
    existing = _existing_keys("fund_holding_change")
    with session_scope() as s:
        rows = s.execute(select(FundHolding)).scalars().all()
        records = []
        for r in rows:
            # Only embed rows that actually represent a change. The
            # vector space gets polluted by zero-delta "held the same"
            # rows otherwise.
            if not r.ticker:
                continue
            records.append({
                "id": r.id, "fund_name": r.fund_name,
                "fund_cik": r.fund_cik, "ticker": r.ticker,
                "quarter_end": (r.quarter_end_date.isoformat()
                                     if r.quarter_end_date else ""),
                "shares": r.shares,
                "change_from_prior_qtr": r.change_from_prior_qtr,
                "pct_of_portfolio": r.pct_of_portfolio,
                "value_usd": r.value_usd,
            })
    for rec in records:
        stats["seen"] += 1
        key = str(rec["id"])
        if key in existing:
            stats["skipped"] += 1
            continue
        ok = False
        try:
            ok = index_fund_holding_change(
                holding_id=rec["id"], fund_name=rec["fund_name"],
                fund_cik=rec["fund_cik"], ticker=rec["ticker"],
                quarter_end_iso=rec["quarter_end"],
                shares=rec["shares"],
                change_from_prior_qtr=rec["change_from_prior_qtr"],
                pct_of_portfolio=rec["pct_of_portfolio"],
                value_usd=rec["value_usd"],
            )
        except Exception:
            logger.debug("fund_holding embed failed", exc_info=True)
            stats["errors"] += 1
            continue
        if ok:
            stats["embedded"] += 1
        else:
            stats["errors"] += 1
    return stats


# ── regime snapshots ──────────────────────────────────────────────────


# Series we use to build the daily regime fingerprint. Picked from the
# Phase 11.F 50-series panel — yield curve, vol, breadth proxies.
_REGIME_SERIES = [
    "DGS10", "DGS2", "T10Y2Y", "BAMLH0A0HYM2", "VIXCLS",
    "DTWEXBGS", "DCOILWTICO", "UNRATE", "PAYEMS", "CPIAUCSL",
]


def _build_regime_snapshot_for_date(d: date,
                                                fred_lookup: Dict[Tuple[str, date], float]
                                                ) -> Optional[Tuple[str, Dict]]:
    """Return (summary_text, metadata) for a regime snapshot on date ``d``.

    None when no observations landed on/before ``d`` (the loop
    skips ahead).
    """
    parts = [f"date={d.isoformat()}"]
    meta: Dict[str, float] = {}
    have_any = False
    for series_id in _REGIME_SERIES:
        # Carry-forward lookup: take the latest value at or before ``d``.
        cand: Optional[float] = None
        for off in range(0, 14):
            key = (series_id, d - timedelta(days=off))
            if key in fred_lookup:
                cand = fred_lookup[key]
                break
        if cand is None:
            continue
        meta[series_id] = float(cand)
        parts.append(f"{series_id}={cand:.4f}")
        have_any = True
    if not have_any:
        return None
    return (" | ".join(parts), meta)


def _walk_regime(logger: logging.Logger, start: Optional[date],
                      end: Optional[date]) -> Dict[str, int]:
    stats = {"seen": 0, "embedded": 0, "skipped": 0, "errors": 0}
    try:
        from sqlalchemy import select
        from backend.db import session_scope
        from backend.models.fred_observation import FredObservation
        from backend.bot.ai.vector_store import index_regime_snapshot_v2
    except Exception:
        return stats
    existing = _existing_keys("regime_snapshot_v2")
    fred_lookup: Dict[Tuple[str, date], float] = {}
    with session_scope() as s:
        q = select(FredObservation).where(
            FredObservation.series_id.in_(_REGIME_SERIES),
        )
        if start is not None:
            q = q.where(FredObservation.observation_date >=
                            datetime.combine(start - timedelta(days=14),
                                                 datetime.min.time()))
        if end is not None:
            q = q.where(FredObservation.observation_date <=
                            datetime.combine(end, datetime.max.time()))
        for row in s.execute(q).scalars().all():
            try:
                d = (row.observation_date.date()
                          if hasattr(row.observation_date, "date")
                          else row.observation_date)
                v = float(row.value)
                fred_lookup[(row.series_id, d)] = v
            except Exception:
                continue
    if not fred_lookup:
        logger.info("regime walker: no FRED rows in scope, skipping")
        return stats

    # Build per-trading-day snapshots. We index daily across
    # (start, end), defaulting to the FRED window when start/end aren't
    # supplied.
    fred_dates = sorted({k[1] for k in fred_lookup.keys()})
    real_start = start or fred_dates[0]
    real_end = end or fred_dates[-1]
    d = real_start
    while d <= real_end:
        # Weekends — skip.
        if d.weekday() < 5:
            stats["seen"] += 1
            key = d.isoformat()
            if key in existing:
                stats["skipped"] += 1
            else:
                pair = _build_regime_snapshot_for_date(d, fred_lookup)
                if pair is None:
                    stats["skipped"] += 1
                else:
                    text, meta = pair
                    ok = False
                    try:
                        ok = index_regime_snapshot_v2(
                            key=key, date_iso=key,
                            summary_text=text, metadata=meta,
                        )
                    except Exception:
                        logger.debug("regime embed failed", exc_info=True)
                        stats["errors"] += 1
                    if ok:
                        stats["embedded"] += 1
                    else:
                        stats["errors"] += 1
        d += timedelta(days=1)
    return stats


# ── dispatch ──────────────────────────────────────────────────────────


_WALKERS = {
    "news": _walk_news,
    "transcripts": _walk_transcripts,
    "insider": _walk_insider,
    "fund_holdings": _walk_fund_holdings,
    "regime": None,  # special-cased — takes date range
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=("MITS Phase 11.K — vector embedding walker for "
                       "Phase 11 paragraph-level + structured rows."),
    )
    parser.add_argument(
        "--kinds", default="all",
        help=("Comma list of kinds OR 'all' (default). Choices: "
              "news, transcripts, insider, fund_holdings, regime."),
    )
    parser.add_argument(
        "--start", default=None,
        help=("YYYY-MM-DD inclusive start for the regime walker only."),
    )
    parser.add_argument(
        "--end", default=None,
        help=("YYYY-MM-DD inclusive end for the regime walker only."),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    logger = logging.getLogger("embed_corpus")

    from backend.db import init_db
    init_db()
    from backend.bot.ai.vector_store import ensure_schema
    if not ensure_schema():
        logger.warning(
            "ensure_schema returned False — pgvector likely unreachable. "
            "Embedding walker will report 0 hits."
        )

    kinds_arg = (args.kinds or "all").strip().lower()
    if kinds_arg == "all":
        kinds = list(_WALKERS.keys())
    else:
        kinds = [k.strip() for k in kinds_arg.split(",") if k.strip()]
        for k in kinds:
            if k not in _WALKERS:
                logger.error("unknown kind: %s (valid: %s)",
                                  k, list(_WALKERS.keys()))
                return 2

    start = _parse_date(args.start) if args.start else None
    end = _parse_date(args.end) if args.end else None

    grand: Dict[str, Dict[str, int]] = {}
    for kind in kinds:
        t0 = datetime.utcnow()
        if kind == "regime":
            stats = _walk_regime(logger, start, end)
        else:
            stats = _WALKERS[kind](logger)
        dur = (datetime.utcnow() - t0).total_seconds()
        logger.info("kind=%s stats=%s dur=%.1fs", kind, stats, dur)
        grand[kind] = stats
    logger.info("embed_corpus GRAND TOTAL: %s", grand)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
