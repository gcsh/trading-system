"""MITS Phase 8.5 — Vector store on pgvector (Postgres) + embeddings.

Why pgvector on the same EC2 box (instead of pinecone / RDS): the
operator's substrate is a single EC2 + a SQLite DB. A second managed
service adds an extra network hop, an extra failure mode, and an extra
bill. pgvector covers our scale (millions of rows) on a single t4g.small
without breaking a sweat, and the embedding pipeline is the only
producer.

Namespaces (one row per logical entity):

  * ``regime_snapshots``     — embed regime + key features per
                                  IntradayRegimeEvent.
  * ``market_observations``  — embed (pattern, ticker, regime,
                                  features summary) per MarketObservation.
  * ``eod_theses``            — embed thesis text per EodAnalysis.
  * ``closed_trades``         — embed (context + outcome) per closed Trade.

Behavior on missing deps (psycopg2 / sentence-transformers / pgvector
running):  graceful no-op. Every public function returns an empty list
or ``False``. Logs at DEBUG. The rest of the bot keeps running.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


# ── embedding model cache ─────────────────────────────────────────────


_model_lock = threading.Lock()
_model = None


def _load_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except Exception:
            logger.debug("sentence-transformers unavailable", exc_info=True)
            return None
        try:
            cache_dir = TUNABLES.vector_embedding_cache_dir
            os.makedirs(cache_dir, exist_ok=True)
            _model = SentenceTransformer(
                TUNABLES.vector_embedding_model,
                cache_folder=cache_dir,
            )
            return _model
        except Exception:
            logger.warning("vector model load failed", exc_info=True)
            return None


def embed(text: str) -> List[float]:
    """Return a 384-dim embedding vector for ``text``. Empty on error."""
    text = (text or "").strip()
    if not text:
        return []
    model = _load_model()
    if model is None:
        return []
    try:
        vec = model.encode(text, normalize_embeddings=True)
        return [float(x) for x in vec]
    except Exception:
        logger.debug("embed failed", exc_info=True)
        return []


# ── pgvector client ───────────────────────────────────────────────────


_conn_lock = threading.Lock()
_conn = None


def _conn_handle():
    """Get a process-shared psycopg2 connection. None on error."""
    global _conn
    if _conn is not None:
        return _conn
    with _conn_lock:
        if _conn is not None:
            return _conn
        try:
            import psycopg2  # type: ignore
        except Exception:
            logger.debug("psycopg2 unavailable; vector store inert",
                            exc_info=True)
            return None
        try:
            _conn = psycopg2.connect(TUNABLES.vector_db_dsn)
            _conn.autocommit = True
            return _conn
        except Exception:
            logger.debug("pgvector connect failed", exc_info=True)
            return None


def reset_conn() -> None:
    """Force-close the shared connection — used by tests + restarts."""
    global _conn
    with _conn_lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None


def ensure_schema() -> bool:
    """Idempotent CREATE EXTENSION + CREATE TABLE for vector_entries."""
    conn = _conn_handle()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS vector_entries ("
                "  id BIGSERIAL PRIMARY KEY,"
                "  namespace VARCHAR(64) NOT NULL,"
                "  key VARCHAR(256) NOT NULL,"
                f" vector vector({TUNABLES.vector_dim}) NOT NULL,"
                "  metadata JSONB NOT NULL,"
                "  created_at TIMESTAMPTZ DEFAULT NOW(),"
                "  UNIQUE(namespace, key)"
                ");"
            )
            # Vector cosine index — created best-effort. lists=100 is a
            # sane default; tune in TUNABLES.vector_ivfflat_lists for
            # corpora > 1M rows.
            try:
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS vector_entries_cosine_idx "
                    f"ON vector_entries USING ivfflat "
                    f"(vector vector_cosine_ops) WITH (lists = {int(TUNABLES.vector_ivfflat_lists)});"
                )
            except Exception:
                pass
            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS vector_entries_namespace_idx "
                    "ON vector_entries (namespace);"
                )
            except Exception:
                pass
        return True
    except Exception:
        logger.debug("ensure_schema failed", exc_info=True)
        return False


def _vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(x):.7f}" for x in vec) + "]"


def upsert(namespace: str, key: str, vector: Sequence[float],
              metadata: Dict[str, Any]) -> bool:
    if not vector:
        return False
    conn = _conn_handle()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO vector_entries (namespace, key, vector, metadata) "
                "VALUES (%s, %s, %s::vector, %s::jsonb) "
                "ON CONFLICT (namespace, key) DO UPDATE SET "
                "  vector = EXCLUDED.vector, metadata = EXCLUDED.metadata;",
                (namespace, key, _vector_literal(vector),
                  json.dumps(metadata, default=str)),
            )
        return True
    except Exception:
        logger.debug("vector upsert failed (%s/%s)", namespace, key,
                        exc_info=True)
        return False


@dataclass
class SimilarityHit:
    namespace: str
    key: str
    cosine: float
    metadata: Dict[str, Any]


def similarity_search(namespace: str, query_vector: Sequence[float],
                        *, k: Optional[int] = None,
                        min_cosine: Optional[float] = None,
                        ) -> List[SimilarityHit]:
    """Top-K cosine-similarity search in ``namespace``.

    Cosine distance in pgvector is ``vector <=> vector`` (0 = identical,
    1 = orthogonal, 2 = opposite). Cosine *similarity* = 1 - distance.
    We return rows with similarity ≥ ``min_cosine`` (default
    ``TUNABLES.analog_min_cosine``).
    """
    if not query_vector:
        return []
    k = k or int(TUNABLES.analog_top_k)
    min_cos = float(min_cosine if min_cosine is not None
                       else TUNABLES.analog_min_cosine)
    conn = _conn_handle()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, metadata, (1.0 - (vector <=> %s::vector)) AS cosine "
                "FROM vector_entries WHERE namespace = %s "
                "ORDER BY vector <=> %s::vector ASC LIMIT %s;",
                (_vector_literal(query_vector), namespace,
                  _vector_literal(query_vector), int(k)),
            )
            hits: List[SimilarityHit] = []
            for row in cur.fetchall():
                key, meta, cosine = row
                cos_val = float(cosine or 0.0)
                if cos_val < min_cos:
                    continue
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {"raw": meta}
                hits.append(SimilarityHit(
                    namespace=namespace, key=str(key), cosine=cos_val,
                    metadata=meta or {},
                ))
            return hits
    except Exception:
        logger.debug("similarity_search failed", exc_info=True)
        return []


def namespace_stats() -> Dict[str, Dict[str, Any]]:
    """Return per-namespace count + most-recent created_at for status UI."""
    conn = _conn_handle()
    if conn is None:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT namespace, COUNT(*), MAX(created_at) "
                "FROM vector_entries GROUP BY namespace;"
            )
            for ns, count, latest in cur.fetchall():
                out[ns] = {
                    "count": int(count or 0),
                    "last_created_at": latest.isoformat() if latest else None,
                }
    except Exception:
        logger.debug("namespace_stats failed", exc_info=True)
    return out


# ── indexing helpers — called from cron + backfill CLI ────────────────


def _safe_features_summary(features_json: str, limit: int = 400) -> str:
    if not features_json:
        return ""
    try:
        obj = json.loads(features_json)
    except Exception:
        return str(features_json)[:limit]
    parts = []
    for k, v in obj.items():
        parts.append(f"{k}={v}")
    return " ".join(parts)[:limit]


def index_regime_snapshot(*, key: str, regime_state: str,
                              spy_30m: Optional[float],
                              vix_level: Optional[float],
                              breadth: Optional[float],
                              put_call: Optional[float],
                              sector_dispersion: Optional[float],
                              top_flow_summary: str,
                              date_iso: str,
                              ) -> bool:
    """Embed a regime fingerprint and upsert under ``regime_snapshots``."""
    text_parts = [
        f"date={date_iso}",
        f"regime={regime_state or 'unknown'}",
        f"spy_30m={spy_30m if spy_30m is not None else 'na'}",
        f"vix={vix_level if vix_level is not None else 'na'}",
        f"breadth={breadth if breadth is not None else 'na'}",
        f"put_call={put_call if put_call is not None else 'na'}",
        f"sector_dispersion={sector_dispersion if sector_dispersion is not None else 'na'}",
        f"flow={top_flow_summary[:200]}",
    ]
    text = " | ".join(text_parts)
    vec = embed(text)
    if not vec:
        return False
    metadata = {
        "date": date_iso,
        "regime": regime_state,
        "spy_30m": spy_30m,
        "vix": vix_level,
        "breadth": breadth,
        "put_call": put_call,
        "sector_dispersion": sector_dispersion,
        "flow_summary": top_flow_summary[:1000],
    }
    return upsert("regime_snapshots", key, vec, metadata)


def index_market_observation(*, observation_id: str, ticker: str,
                                  pattern: str, regime: str,
                                  features_json: str,
                                  date_iso: str) -> bool:
    text = (
        f"ticker={ticker} pattern={pattern} regime={regime} "
        f"features={_safe_features_summary(features_json)} "
        f"date={date_iso}"
    )
    vec = embed(text)
    if not vec:
        return False
    return upsert("market_observations", str(observation_id), vec, {
        "observation_id": str(observation_id),
        "ticker": ticker,
        "pattern": pattern,
        "regime": regime,
        "features_json": features_json,
        "date": date_iso,
    })


def index_eod_thesis(*, analysis_id: str, ticker: str,
                          analysis_date: str, thesis_text: str,
                          regime: str,
                          ) -> bool:
    text = (
        f"ticker={ticker} regime={regime} date={analysis_date} "
        f"thesis={thesis_text[:1500]}"
    )
    vec = embed(text)
    if not vec:
        return False
    return upsert("eod_theses", str(analysis_id), vec, {
        "analysis_id": str(analysis_id),
        "ticker": ticker,
        "regime": regime,
        "date": analysis_date,
        "thesis": thesis_text[:2000],
    })


def index_closed_trade(*, trade_id: str, ticker: str,
                          strategy: str, regime: Optional[str],
                          outcome: str, pnl: float, entry_iso: str,
                          context_summary: str,
                          ) -> bool:
    text = (
        f"trade={trade_id} ticker={ticker} strategy={strategy} "
        f"regime={regime or 'na'} outcome={outcome} pnl={pnl:.2f} "
        f"entry={entry_iso} context={context_summary[:800]}"
    )
    vec = embed(text)
    if not vec:
        return False
    return upsert("closed_trades", str(trade_id), vec, {
        "trade_id": str(trade_id),
        "ticker": ticker,
        "strategy": strategy,
        "regime": regime,
        "outcome": outcome,
        "pnl": pnl,
        "entry_iso": entry_iso,
        "context": context_summary[:1500],
    })


# ── MITS Phase 11.K — paragraph-level + Phase 11 namespace indexers ───


def index_news_paragraph(*, article_id: str, ticker: str,
                                 headline: str, summary: str,
                                 published_iso: str,
                                 sentiment_label: Optional[str] = None,
                                 sentiment_score: Optional[float] = None,
                                 ) -> bool:
    """Embed a single news article. One vector per (ticker, article_id).

    Namespace: ``news_paragraph``. Key: ``{ticker}:{article_id}``.
    """
    text_parts = [f"ticker={ticker}", f"date={published_iso}",
                       f"headline={headline[:300]}"]
    if summary:
        text_parts.append(f"summary={summary[:1200]}")
    if sentiment_label:
        text_parts.append(f"sentiment={sentiment_label}")
    text = " | ".join(text_parts)
    vec = embed(text)
    if not vec:
        return False
    key = f"{ticker}:{article_id}"
    return upsert("news_paragraph", key, vec, {
        "article_id": str(article_id),
        "ticker": ticker,
        "headline": headline[:500],
        "summary": (summary or "")[:1500],
        "published": published_iso,
        "sentiment_label": sentiment_label,
        "sentiment_score": sentiment_score,
    })


def index_earnings_call_paragraph(*, paragraph_id: str, ticker: str,
                                            fiscal_year: int,
                                            fiscal_quarter: int,
                                            paragraph_index: int,
                                            speaker: Optional[str],
                                            speaker_title: Optional[str],
                                            content: str) -> bool:
    """Embed one paragraph of an earnings call. Namespace:
    ``earnings_call_paragraph``. Key: ``{ticker}:{fy}Q{fq}:{idx}``.
    """
    text_parts = [
        f"ticker={ticker}",
        f"period={fiscal_year}Q{fiscal_quarter}",
    ]
    if speaker:
        text_parts.append(f"speaker={speaker}")
    if speaker_title:
        text_parts.append(f"title={speaker_title}")
    text_parts.append(f"content={content[:1800]}")
    text = " | ".join(text_parts)
    vec = embed(text)
    if not vec:
        return False
    key = f"{ticker}:{fiscal_year}Q{fiscal_quarter}:{paragraph_index}"
    return upsert("earnings_call_paragraph", key, vec, {
        "paragraph_id": str(paragraph_id),
        "ticker": ticker,
        "fiscal_year": fiscal_year,
        "fiscal_quarter": fiscal_quarter,
        "paragraph_index": paragraph_index,
        "speaker": speaker,
        "speaker_title": speaker_title,
        "content": content[:2000],
    })


def index_insider_form4_narrative(*, trade_id: str, ticker: str,
                                              insider_name: str,
                                              insider_role: Optional[str],
                                              transaction_code: str,
                                              shares: Optional[float],
                                              price: Optional[float],
                                              total_value: Optional[float],
                                              transaction_date_iso: str,
                                              ) -> bool:
    """Embed a Form 4 transaction narrative. Namespace:
    ``insider_form4_narrative``. Key: ``{trade_id}``.
    """
    parts = [
        f"ticker={ticker}", f"insider={insider_name}",
        f"role={insider_role or 'na'}",
        f"code={transaction_code}",
        f"shares={int(shares) if shares else 'na'}",
        f"price={price or 'na'}",
        f"value={int(total_value) if total_value else 'na'}",
        f"date={transaction_date_iso}",
    ]
    text = " | ".join(parts)
    vec = embed(text)
    if not vec:
        return False
    return upsert("insider_form4_narrative", str(trade_id), vec, {
        "trade_id": str(trade_id),
        "ticker": ticker,
        "insider_name": insider_name,
        "insider_role": insider_role,
        "transaction_code": transaction_code,
        "shares": shares,
        "price": price,
        "total_value": total_value,
        "transaction_date": transaction_date_iso,
    })


def index_fund_holding_change(*, holding_id: str, fund_name: str,
                                       fund_cik: str, ticker: str,
                                       quarter_end_iso: str,
                                       shares: Optional[float],
                                       change_from_prior_qtr: Optional[float],
                                       pct_of_portfolio: Optional[float],
                                       value_usd: Optional[float],
                                       ) -> bool:
    """Embed a 13F position-delta narrative. Namespace:
    ``fund_holding_change``. Key: ``{holding_id}``.

    The narrative is what an analyst would write looking at the delta:
    "{Fund}: +500k shares of {ticker}, now 12% of portfolio."
    """
    direction = "added" if (change_from_prior_qtr or 0) > 0 else "trimmed"
    if change_from_prior_qtr is None or change_from_prior_qtr == 0:
        direction = "held"
    parts = [
        f"ticker={ticker}", f"fund={fund_name}",
        f"cik={fund_cik}", f"quarter={quarter_end_iso}",
        f"action={direction}",
        f"shares={int(shares) if shares else 'na'}",
        f"change={int(change_from_prior_qtr) if change_from_prior_qtr else 'na'}",
        f"pct={pct_of_portfolio or 'na'}",
        f"value={int(value_usd) if value_usd else 'na'}",
    ]
    text = " | ".join(parts)
    vec = embed(text)
    if not vec:
        return False
    return upsert("fund_holding_change", str(holding_id), vec, {
        "holding_id": str(holding_id),
        "ticker": ticker,
        "fund_name": fund_name,
        "fund_cik": fund_cik,
        "quarter_end": quarter_end_iso,
        "shares": shares,
        "change_from_prior_qtr": change_from_prior_qtr,
        "pct_of_portfolio": pct_of_portfolio,
        "value_usd": value_usd,
    })


def index_regime_snapshot_v2(*, key: str, date_iso: str,
                                       summary_text: str,
                                       metadata: Dict[str, Any]) -> bool:
    """MITS Phase 11.K — regime snapshot v2.

    Daily fingerprint built from the new 50-series FRED panel + bar
    summary. ``summary_text`` is the embed payload; ``metadata`` is the
    structured fields used by downstream queries.
    """
    vec = embed(summary_text)
    if not vec:
        return False
    return upsert("regime_snapshot_v2", key, vec, {
        "date": date_iso,
        **metadata,
    })


__all__ = [
    "embed", "upsert", "similarity_search", "ensure_schema",
    "namespace_stats", "SimilarityHit", "reset_conn",
    "index_regime_snapshot", "index_market_observation",
    "index_eod_thesis", "index_closed_trade",
    # MITS Phase 11.K — paragraph-level + Phase 11 namespaces.
    "index_news_paragraph", "index_earnings_call_paragraph",
    "index_insider_form4_narrative", "index_fund_holding_change",
    "index_regime_snapshot_v2",
]
