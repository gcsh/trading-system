"""SQLite engine, session factory, and table creation helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import SETTINGS


# SQLite concurrency pragmas — applied to every new connection.
#
# - journal_mode=WAL      Allow readers and writers to coexist.
#                         Eliminates "database is locked" errors during
#                         the heavy concurrent writes from backfills +
#                         the live engine. Persists per-database; once
#                         set, every future connection inherits it.
# - busy_timeout=30000    Wait up to 30s on lock contention before
#                         raising; previously we accepted whatever the
#                         driver default was (~0–5s on different
#                         platforms), which manifested as spurious
#                         OperationalError under load.
# - synchronous=NORMAL    WAL mode is durable enough at NORMAL — full
#                         synchronous is only required for delete-mode
#                         journals and only protects against power
#                         loss. We accept the trade-off because the
#                         alternative is fsync-on-every-commit which
#                         halves write throughput.
# - foreign_keys=ON       SQLite ships FK enforcement *off* per
#                         connection. We have ORM foreign keys we
#                         actually want enforced.
# - temp_store=MEMORY     Internal scratch tables (joins, sorts) live
#                         in RAM instead of /tmp. Tiny memory cost,
#                         meaningful speedup on aggregation queries.
#
# This listener is registered against ``Engine`` (base class) once at
# module import time so it fires for whichever engine ``init_db``
# eventually creates. Idempotent — re-importing this module on the
# same process re-registers but SQLAlchemy dedupes identical
# event listeners by ``(target, identifier, fn)``.
@event.listens_for(Engine, "connect")
def _sqlite_set_pragmas(dbapi_connection, connection_record) -> None:  # noqa: D401
    """Apply WAL + 30s busy_timeout to every new SQLite connection."""
    # Only act on sqlite3 connections — guard against future engines.
    try:
        import sqlite3

        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
    except Exception:
        return
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()
    except Exception:
        # Never block a connection on pragma failure — the bot must
        # remain usable even if a particular pragma is rejected.
        pass


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""


_engine = None
_SessionLocal: sessionmaker | None = None


def _build_url(path: str | None = None) -> str:
    return f"sqlite:///{path or SETTINGS.db_path}"


def init_db(db_path: str | None = None) -> None:
    """Initialise the SQLite engine and create tables if missing."""
    global _engine, _SessionLocal
    _engine = create_engine(
        _build_url(db_path),
        connect_args={"check_same_thread": False},
        future=True,
    )
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    # Import models so their tables register on Base.metadata.
    from backend.models import config as _config_model  # noqa: F401
    from backend.models import decision_log as _decision_log_model  # noqa: F401
    from backend.models import execution_log as _execution_log_model  # noqa: F401
    from backend.models import flow_seen as _flow_seen_model  # noqa: F401
    from backend.models import gex_history as _gex_history_model  # noqa: F401
    from backend.models import breadth_snapshot as _breadth_snapshot_model  # noqa: F401
    from backend.models import cot_report as _cot_report_model  # noqa: F401
    from backend.models import earnings_intel as _earnings_intel_model  # noqa: F401
    from backend.models import edgar_filing as _edgar_filing_model  # noqa: F401
    from backend.models import fred_observation as _fred_observation_model  # noqa: F401
    from backend.models import iv_history as _iv_history_model  # noqa: F401
    from backend.models import short_interest as _short_interest_model  # noqa: F401
    from backend.models import paper as _paper_model  # noqa: F401
    from backend.models import regime_episode as _regime_episode_model  # noqa: F401
    from backend.models import snapshot as _snapshot_model  # noqa: F401
    from backend.models import trade as _trade_model  # noqa: F401
    from backend.models import watchlist as _watchlist_model  # noqa: F401
    # MITS Phase 0 — knowledge graph + pattern detection corpus tables.
    from backend.models import market_observation as _market_observation_model  # noqa: F401
    from backend.models import market_outcome as _market_outcome_model  # noqa: F401
    from backend.models import knowledge_graph_cell as _knowledge_graph_model  # noqa: F401
    from backend.models import pattern_prior as _pattern_prior_model  # noqa: F401
    from backend.models import corpus_status as _corpus_status_model  # noqa: F401
    # MITS Phase 1 — knowledge graph history (sparkline source).
    from backend.models import knowledge_graph_history as _knowledge_graph_history_model  # noqa: F401
    # MITS Phase 2 — intraday IV cache (ThetaData Standard workaround).
    from backend.models import intraday_iv_cache as _intraday_iv_cache_model  # noqa: F401
    # MITS Phase 3 — detector config (operator-facing toggle / params).
    from backend.models import detector_config as _detector_config_model  # noqa: F401
    # MITS Phase 3 — EOD analysis batch (tomorrow's setup digest).
    from backend.models import eod_analysis as _eod_analysis_model  # noqa: F401
    # MITS Phase 5 — prediction→outcome tracking (closing the loop).
    from backend.models import eod_prediction_outcome as _eod_pred_outcome_model  # noqa: F401
    # MITS Phase 14.D — broader brain prediction → outcome ledger.
    from backend.models import brain_prediction as _bp_model  # noqa: F401
    # MITS Phase 6 — recursive self-improvement layer.
    from backend.models import ingest_watermark as _ingest_watermark_model  # noqa: F401
    from backend.models import detector_suggestion as _detector_suggestion_model  # noqa: F401
    from backend.models import weekly_retrospective as _weekly_retrospective_model  # noqa: F401
    # MITS Phase 7 — intraday regime transition log (discretionary layer).
    from backend.models import intraday_regime_event as _intraday_regime_event_model  # noqa: F401
    # MITS Phase 8 — S3 lake sync watermark (cache-class; survives reset).
    from backend.models import lake_sync as _lake_sync_model  # noqa: F401
    # MITS Phase 9 — Theory Studio (operator drawings) + lake health monitor.
    from backend.models import saved_theory_annotation as _saved_theory_model  # noqa: F401
    from backend.models import lake_health_alert as _lake_health_alert_model  # noqa: F401
    # Telegram notifier — persistent retry queue.
    from backend.models import telegram_outbox as _telegram_outbox_model  # noqa: F401
    # MITS Phase 11.G — per-source sync watermark + chunked backfill ledger.
    from backend.models import data_watermark as _data_watermark_model  # noqa: F401
    from backend.models import backfill_progress as _backfill_progress_model  # noqa: F401
    # MITS Phase 11.B.1 — silver-layer stock bar rows.
    from backend.models import stock_bar as _stock_bar_model  # noqa: F401
    # MITS Phase 11.B.2 — silver-layer EOD option contract bar rows.
    from backend.models import option_contract_bar as _option_contract_bar_model  # noqa: F401
    # MITS Phase 11.C — Finnhub company-news cache + sentiment scores.
    from backend.models import news_article as _news_article_model  # noqa: F401
    # MITS Phase 11.D — AlphaVantage earnings-call transcript header +
    # per-speaker paragraph rows (paragraph rows are the embedding
    # grain for Agent 4's vector pipeline).
    from backend.models import earnings_transcript as _earnings_transcript_model  # noqa: F401
    from backend.models import transcript_paragraph as _transcript_paragraph_model  # noqa: F401
    # MITS Phase 11.E — Form 4 insider transactions + 13F fund holdings.
    from backend.models import insider_trade as _insider_trade_model  # noqa: F401
    from backend.models import fund_holding as _fund_holding_model  # noqa: F401
    # MITS Phase 11.J — cross-vendor parity audit ledger.
    from backend.models import parity_audit_history as _parity_audit_history_model  # noqa: F401
    # MITS Phase 11.I — per-source health snapshot ledger.
    from backend.models import data_source_health as _data_source_health_model  # noqa: F401
    # MITS Phase 16.A — declarative policy engine rule-evaluation ledger.
    from backend.models import policy_rule_evaluation as _policy_eval_model  # noqa: F401
    # MITS Phase 16.B — decision provenance ledger for deterministic replay.
    from backend.models import decision_provenance as _dp_model  # noqa: F401
    # MITS Phase 17.E — declarative exit policy rule-evaluation ledger.
    from backend.models import exit_rule_evaluation as _exit_eval_model  # noqa: F401
    # MITS Phase 18.A — Learned Hypothesis Attribution ledger.
    from backend.models import learned_attribution as _la_model  # noqa: F401
    # MITS Phase 18.B — Counterfactual replay cache.
    from backend.models import counterfactual_replay as _cfr_model  # noqa: F401
    # MITS Phase 18.C — Policy Auto-Tuning advisory recommendations.
    from backend.models import policy_tuning as _pt_model  # noqa: F401
    # MITS Phase 18.D — Online agent weight adaptation history.
    from backend.models import agent_weight_history as _awh_model  # noqa: F401
    # MITS Phase 18.E — Operator approve/rollback audit trail.
    from backend.models import learning_rollback_log as _lrl_model  # noqa: F401
    # MITS Phase 18-FU Stream A — Decision Funnel daily rollup.
    from backend.models import decision_funnel_daily as _dfd_model  # noqa: F401
    # MITS Phase 18-FU Stream D (Gap 6) — per-cycle weight application log.
    from backend.models import weight_application_log as _wal_model  # noqa: F401
    # MITS Phase 18-FU Stream D (Gap 10) — learning impact measurement.
    from backend.models import learning_impact as _li_model  # noqa: F401
    # Domain modules whose ORM models live alongside their logic register here
    # so init_db() picks up the table the first time the app boots.
    from backend.bot import experiments as _experiments_module  # noqa: F401

    Base.metadata.create_all(_engine)
    _auto_migrate(_engine)
    _data_backfill(_engine)


def _auto_migrate(engine) -> None:
    """Add any model columns missing from existing SQLite tables.

    SQLAlchemy's create_all only creates missing *tables*, not missing
    *columns*. For a long-lived dev DB we additively ALTER TABLE to add new
    columns so the app keeps working without a manual wipe. SQLite supports
    ADD COLUMN with a default; never drops or alters existing data.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            if table_name not in existing_tables:
                continue
            have = {c["name"] for c in inspector.get_columns(table_name)}
            for col in table.columns:
                if col.name in have:
                    continue
                # Build a minimal column DDL: type + nullable/default.
                coltype = col.type.compile(dialect=engine.dialect)
                ddl = f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {coltype}'
                if col.default is not None and getattr(col.default, "arg", None) is not None:
                    arg = col.default.arg
                    if isinstance(arg, str):
                        ddl += f" DEFAULT '{arg}'"
                    elif isinstance(arg, (int, float)):
                        ddl += f" DEFAULT {arg}"
                try:
                    conn.execute(text(ddl))
                except Exception:
                    # Best-effort; if a column can't be added we leave it.
                    pass

    # MITS Phase 1 — knowledge_graph constraint migration. The original
    # unique constraint did not include `sample_split`; Phase 1 needs 3
    # rows per cohort (in_sample / out_of_sample / combined). SQLite
    # can't ALTER a constraint in place, so we rebuild the table when we
    # detect the old uniqueness shape. Idempotent on already-migrated
    # tables.
    try:
        _migrate_kg_unique_constraint(engine)
    except Exception:
        # Migration is best-effort. If it fails the only side effect is
        # the new in/out-of-sample rows collide with the old constraint
        # and silently get rejected — same as the pre-Phase-1 behavior.
        pass


def _migrate_kg_unique_constraint(engine) -> None:
    """Rebuild the knowledge_graph table so the unique constraint includes
    `sample_split`. SQLite stores table-level UniqueConstraints as
    `sqlite_autoindex_*` which CANNOT be dropped without rebuilding the
    table. This migration:

      1. Detects whether a 6-axis (legacy) autoindex is still active.
      2. If yes, rebuilds the table: CREATE knowledge_graph_new with the
         correct 7-axis UniqueConstraint, INSERT SELECT all rows over,
         DROP the old table, RENAME the new one, recreate the secondary
         indexes that SQLAlchemy didn't auto-create.

    Idempotent: when the table already has only the 7-axis constraint
    (post-rebuild), this is a no-op.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "knowledge_graph" not in inspector.get_table_names():
        return
    indexes = inspector.get_indexes("knowledge_graph")
    has_split_idx = False
    has_legacy_6axis = False
    for idx in indexes:
        cols = idx.get("column_names") or []
        if not idx.get("unique"):
            continue
        if "sample_split" in cols and {
            "ticker", "pattern", "regime", "vol_state",
            "time_bucket", "horizon", "sample_split"
        }.issubset(set(cols)):
            has_split_idx = True
        elif set(cols) == {
            "ticker", "pattern", "regime", "vol_state",
            "time_bucket", "horizon"
        }:
            has_legacy_6axis = True

    # If both exist, the legacy autoindex still blocks 3-row-per-cohort
    # writes. Rebuild the table to strip it. If only the legacy exists,
    # the new constraint hasn't been added yet — rebuild handles both.
    if not has_legacy_6axis:
        if not has_split_idx:
            # Edge case: neither constraint — table was created some
            # other way. Best-effort add the 7-axis index.
            with engine.begin() as conn:
                conn.execute(text(
                    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_kg_cohort" '
                    'ON "knowledge_graph" '
                    '("ticker", "pattern", "regime", "vol_state", '
                    '"time_bucket", "horizon", "sample_split")'
                ))
        return  # table-rebuild not needed

    # Legacy autoindex present — do a full table rebuild.
    import logging as _logging
    _log = _logging.getLogger(__name__)
    _log.warning(
        "knowledge_graph: rebuilding table to strip legacy 6-axis "
        "UNIQUE autoindex (blocks 3-row-per-cohort writes)."
    )
    with engine.begin() as conn:
        # Get the live column list so the INSERT SELECT picks up any
        # columns added since the table was originally created (e.g.
        # sample_split itself, which was added by _auto_migrate before
        # this runs).
        col_rows = conn.execute(text(
            "PRAGMA table_info(knowledge_graph)"
        )).fetchall()
        col_names = [r[1] for r in col_rows]
        if not col_names:
            return
        cols_sql = ", ".join(f'"{c}"' for c in col_names)

        # Default sample_split for any nulls before the new constraint
        # tries to enforce non-null cohort tuples.
        if "sample_split" in col_names:
            conn.execute(text(
                "UPDATE knowledge_graph SET sample_split = 'combined' "
                "WHERE sample_split IS NULL OR sample_split = ''"
            ))

        # Build the new table DDL from the live model so SQLAlchemy puts
        # the 7-axis constraint inline (as an autoindex this time, in
        # the right shape). Easiest path: temporarily rename the live
        # table, let create_all rebuild from metadata, then INSERT
        # SELECT back, then drop the renamed legacy.
        conn.execute(text(
            'ALTER TABLE knowledge_graph '
            'RENAME TO knowledge_graph_legacy'
        ))

        # Drop the secondary named indexes that came along with the
        # legacy table (they got renamed automatically by SQLite, but
        # let's be defensive and remove any that survived).
        for name in ("uq_kg_cohort", "ix_kg_horizon", "ix_kg_ticker_pattern",
                     "ix_knowledge_graph_ticker",
                     "ix_knowledge_graph_pattern"):
            try:
                conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
            except Exception:
                pass

    # Re-create the table fresh using SQLAlchemy metadata (which holds
    # the correct 7-axis UniqueConstraint definition). This emits the
    # autoindex with the right shape.
    from sqlalchemy import inspect as _inspect
    Base.metadata.tables["knowledge_graph"].create(bind=engine)

    with engine.begin() as conn:
        # Copy rows over by column list — schema may have evolved but
        # the column names match between legacy and new.
        conn.execute(text(
            f'INSERT OR IGNORE INTO knowledge_graph ({cols_sql}) '
            f'SELECT {cols_sql} FROM knowledge_graph_legacy'
        ))
        conn.execute(text('DROP TABLE knowledge_graph_legacy'))
        _log.warning(
            "knowledge_graph: rebuild complete. Legacy autoindex removed; "
            "3-row-per-cohort writes now permitted."
        )


def _data_backfill(engine) -> None:
    """One-shot data backfills that must run after ``_auto_migrate``.

    These are idempotent: each statement is a no-op once the targeted
    rows have already been corrected. Safe to run on every boot.

    Why this is needed: ``_auto_migrate`` adds new columns with the
    table-level default (e.g. ``signal_source='live_engine'``). But
    rows inserted before the column existed have no business defaulting
    to "live_engine" — synthetic-replay rows should be tagged as such.
    This backfill recovers the correct value from sibling columns.
    """
    from sqlalchemy import text
    statements = [
        # P1.1 — synthetic-replay DecisionLog rows must be tagged
        # historical_replay so live-only analytics filter cleanly.
        # Distinguishing marker: status starts with the replay constant.
        """
        UPDATE decision_log
        SET signal_source = 'historical_replay'
        WHERE signal_source IN ('live_engine', '')
          AND status LIKE 'historical_replay%'
        """,
        # Decisions with a trade_id inherit signal_source from Trade
        # when Trade has the (older) signal_source column populated.
        """
        UPDATE decision_log
        SET signal_source = (
            SELECT trades.signal_source FROM trades
            WHERE trades.id = decision_log.trade_id
        )
        WHERE signal_source IN ('live_engine', '')
          AND trade_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM trades
            WHERE trades.id = decision_log.trade_id
              AND trades.signal_source IS NOT NULL
              AND trades.signal_source <> ''
          )
        """,
        # MITS Phase 2 — backfill `net_gex_scalar` from existing regime
        # rows. Formula: sign from dealer_regime
        # (long_gamma → +1, short_gamma → -1, unknown → 0) multiplied by
        # |spot - gamma_flip| * 1e9 (distance-to-flip proxy scaled to
        # match real net-GEX magnitudes in the billions). Idempotent —
        # NULL guard means a row only gets a scalar once. Updates only
        # NULL rows so a real net-GEX value (from a future Pro vendor)
        # is never overwritten.
        """
        UPDATE gex_regime_history
        SET net_gex_scalar = (
            CASE dealer_regime
                WHEN 'long_gamma'  THEN  1.0
                WHEN 'short_gamma' THEN -1.0
                ELSE 0.0
            END
        ) * (
            CASE
                WHEN gamma_flip IS NOT NULL AND spot_price IS NOT NULL
                THEN ABS(spot_price - gamma_flip) * 1000000000.0
                ELSE 0.0
            END
        )
        WHERE net_gex_scalar IS NULL
          AND dealer_regime IS NOT NULL
        """,
    ]
    try:
        with engine.begin() as conn:
            for stmt in statements:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    # Best-effort. Column may not yet exist on a brand-new
                    # DB where _auto_migrate just added it — that's OK,
                    # the next boot will catch up.
                    pass
    except Exception:
        pass


def get_engine():
    if _engine is None:
        init_db()
    return _engine


def get_sessionmaker() -> sessionmaker:
    if _SessionLocal is None:
        init_db()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a SQLAlchemy session and commit/rollback automatically."""
    factory = get_sessionmaker()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
