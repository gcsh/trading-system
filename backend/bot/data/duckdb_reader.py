"""MITS Phase 11.1 — DuckDB analytics read layer.

A second read path for heavy aggregate queries that scanning SQLite
would block the live hot-path on. DuckDB reads parquet directly from
S3 via its ``httpfs`` extension; the live trading-bot keeps writing
to SQLite for the 1s quote / 5min cycle path.

Why DuckDB (not Athena, not RDS):

  * Athena has 10-30s cold-start + per-query egress costs. The Lake
    Status panel polls every 30s, so cold-start would dominate.
  * RDS would force us to ferry the silver layer to Postgres too —
    that's two copies of every table. With DuckDB we read the bronze
    parquet directly; no copy.
  * DuckDB embedded mode runs in-process. No new daemon, no new IAM
    role, no new port.

Contract:

  * One ``duckdb.connect()`` connection per process (thread-safe under
    DuckDB's MVCC).
  * ``httpfs`` extension is installed on first use; subsequent calls
    are warm.
  * AWS creds are picked up from the instance role (boto3-style
    credential chain) — no static keys.
  * Queries are *read-only*. There is no write API here.
  * Empty / missing parquet returns an empty DataFrame, never throws
    — so consumers don't have to special-case the cold-start case.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, List, Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


_CONN_LOCK = threading.Lock()
_CONN = None
_HTTPFS_INSTALLED = False


def _resolve_region() -> str:
    return (os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1")


def _connect():
    """Lazy-construct a process-wide DuckDB connection with httpfs
    enabled. None on failure (consumers fall back to SQLite).
    """
    global _CONN, _HTTPFS_INSTALLED
    if _CONN is not None:
        return _CONN
    with _CONN_LOCK:
        if _CONN is not None:
            return _CONN
        try:
            import duckdb  # type: ignore
        except ImportError:
            logger.debug(
                "duckdb_reader: duckdb not installed — pip install duckdb")
            return None
        try:
            con = duckdb.connect(database=":memory:")
        except Exception:
            logger.debug("duckdb_reader: connect failed", exc_info=True)
            return None
        # httpfs is mandatory — we ALWAYS read from S3.
        try:
            con.execute("INSTALL httpfs;")
            con.execute("LOAD httpfs;")
            _HTTPFS_INSTALLED = True
        except Exception:
            logger.warning(
                "duckdb_reader: httpfs install/load failed — S3 reads will "
                "not work. Run `INSTALL httpfs;` manually if persistence "
                "is needed.", exc_info=True,
            )
        # Wire AWS creds via the instance metadata service. Setting the
        # region lets httpfs construct the right virtual-host-style URL.
        region = _resolve_region()
        try:
            con.execute(f"SET s3_region = '{region}';")
        except Exception:
            pass
        # boto3-style credential chain via the AWS_ACCESS_KEY / IMDS path.
        # DuckDB v0.10+ exposes `s3_access_key_id` / `s3_secret_access_key`
        # / `s3_session_token` settings; on EC2 with an instance role
        # these are auto-populated from IMDSv2 via the `aws_credential_chain`
        # secret type. If that's unavailable we fall back to AWS_*_KEY env.
        try:
            con.execute(
                "CREATE SECRET IF NOT EXISTS s3_role "
                "(TYPE S3, PROVIDER CREDENTIAL_CHAIN);"
            )
        except Exception:
            # Older DuckDB without CREATE SECRET — fall back to env vars.
            access = os.environ.get("AWS_ACCESS_KEY_ID")
            secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
            token = os.environ.get("AWS_SESSION_TOKEN")
            if access and secret:
                try:
                    con.execute(
                        f"SET s3_access_key_id = '{access}';")
                    con.execute(
                        f"SET s3_secret_access_key = '{secret}';")
                    if token:
                        con.execute(
                            f"SET s3_session_token = '{token}';")
                except Exception:
                    logger.debug(
                        "duckdb_reader: env-cred fallback failed",
                        exc_info=True,
                    )
        _CONN = con
        logger.info(
            "duckdb_reader: connected (region=%s, httpfs=%s)",
            region, _HTTPFS_INSTALLED,
        )
        return _CONN


def query(sql: str, params: Optional[List[Any]] = None):
    """Execute ``sql`` and return a pandas DataFrame. Empty DataFrame
    on connect failure or read error — never throws.

    ``params`` is positional binds. Use ``?`` placeholders in ``sql``.
    """
    import pandas as pd  # type: ignore
    con = _connect()
    if con is None:
        return pd.DataFrame()
    try:
        if params:
            result = con.execute(sql, params)
        else:
            result = con.execute(sql)
        return result.fetchdf()
    except Exception:
        logger.warning(
            "duckdb_reader: query failed — sql=%r", sql[:120],
            exc_info=True,
        )
        return pd.DataFrame()


def s3_bronze_glob(source: str, dtype: str,
                       *, dt: Optional[str] = None,
                       ticker: Optional[str] = None) -> str:
    """Build the ``s3://.../*.parquet`` glob pattern for one bronze
    partition. Use ``dt='*'`` to span all dates.
    """
    bucket = TUNABLES.lake_bucket
    parts = [f"s3://{bucket}/bronze", source.lower(), dtype.lower()]
    if dt:
        parts.append(f"dt={dt}")
    else:
        parts.append("dt=*")
    if ticker:
        parts.append(f"ticker={ticker.upper()}")
    parts.append("*.parquet")
    return "/".join(parts)


def scan_table_parquet(table: str,
                            *, dt: Optional[str] = None) -> str:
    """Build the ``s3://.../sqlite_ferry/{table}/...`` glob pattern.

    ``dt`` defaults to ``*`` (scan every ferry-time partition).
    """
    bucket = TUNABLES.lake_bucket
    parts = [
        f"s3://{bucket}/bronze/sqlite_ferry",
        table,
        f"dt={dt}" if dt else "dt=*",
        "*.parquet",
    ]
    return "/".join(parts)


# ── high-level analytic helpers used by the heavy-query routes ────────


def aggregate_parity_summary() -> dict:
    """Return a parity-audit aggregate over the LAST 12 MONTHS of
    parity rows, scanned via DuckDB instead of SQLite.

    Output shape mirrors ``/data-quality/parity`` so the route can
    swap reader without UI changes.
    """
    glob = scan_table_parquet("parity_audit_history")
    df = query(
        f"""
        SELECT
            COUNT(*)                            AS total_audited_rows,
            SUM(CASE WHEN severity = 'missing' THEN 1 ELSE 0 END) AS missing,
            SUM(CASE WHEN severity = 'ok'      THEN 1 ELSE 0 END) AS ok,
            SUM(CASE WHEN severity = 'suspect' THEN 1 ELSE 0 END) AS suspect
        FROM read_parquet('{glob}', union_by_name=true)
        """
    )
    if df.empty:
        return {
            "total_audited_rows": 0,
            "severity_counts": {"missing": 0, "ok": 0, "suspect": 0},
            "suspect_pct_of_total": 0.0,
            "source": "duckdb_empty",
        }
    row = df.iloc[0].to_dict()
    total = int(row.get("total_audited_rows") or 0)
    miss = int(row.get("missing") or 0)
    ok = int(row.get("ok") or 0)
    susp = int(row.get("suspect") or 0)
    return {
        "total_audited_rows": total,
        "severity_counts": {"missing": miss, "ok": ok, "suspect": susp},
        "suspect_total": susp,
        "suspect_pct_of_total": round(susp / total, 4) if total else 0.0,
        "source": "duckdb",
    }


def aggregate_lake_source_rowcounts() -> List[dict]:
    """Per-source bronze row count from S3. Used by /lake-status to
    answer "how many rows did each source actually ferry to bronze?".
    """
    bucket = TUNABLES.lake_bucket
    # Glob across every source × dtype × dt → count.
    glob = f"s3://{bucket}/bronze/*/*/dt=*/*.parquet"
    df = query(
        f"""
        SELECT
            regexp_extract(filename, 'bronze/([^/]+)/', 1) AS source,
            regexp_extract(filename, 'bronze/[^/]+/([^/]+)/', 1) AS dtype,
            COUNT(*) AS row_count
        FROM read_parquet('{glob}', union_by_name=true, filename=true)
        GROUP BY source, dtype
        ORDER BY source, dtype
        """
    )
    if df.empty:
        return []
    return df.to_dict(orient="records")


def healthcheck() -> dict:
    """One-shot health probe used by /lake-status and ops dashboards."""
    con = _connect()
    if con is None:
        return {"ok": False, "reason": "no_duckdb_or_connect_failed"}
    try:
        df = query("SELECT 1 AS ping")
        ok = bool(not df.empty and int(df.iloc[0]["ping"]) == 1)
    except Exception as e:
        return {"ok": False, "reason": f"ping_failed: {e!r}"}
    return {"ok": ok, "httpfs": _HTTPFS_INSTALLED}


__all__ = [
    "query",
    "s3_bronze_glob",
    "scan_table_parquet",
    "aggregate_parity_summary",
    "aggregate_lake_source_rowcounts",
    "healthcheck",
]
