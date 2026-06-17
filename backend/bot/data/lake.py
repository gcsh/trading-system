"""MITS Phase 8 — S3 data lake (Bronze/Silver/Gold) writer + reader.

Three-layer design:

    bronze/ — raw vendor payloads captured at fetch time. Immutable.
              Partition: ``bronze/{source}/{dtype}/dt=YYYY-MM-DD/[ticker=X/]*.parquet``
    silver/ — normalized + schema-enforced canonical types (BarRow,
              QuoteRow, OptionContractRow, etc). Partition:
              ``silver/{canonical_type}/dt=YYYY-MM-DD/*.parquet``
    gold/   — nightly snapshots of bot SQLite tables. Partition:
              ``gold/{table}/dt=YYYY-MM-DD/snapshot.parquet``

The bronze writer is the load-bearing piece for Phase 8. It must NEVER
block the bot's primary cycle: every write is fire-and-forget on a
shared ThreadPoolExecutor. A queue-full or boto3-error is logged at
DEBUG and dropped — the in-process pipeline continues unaffected.

Manifest invariant: every parquet payload includes the columns
``fetch_ts`` (UTC ISO), ``source_version``, ``request_url``,
``row_count``. Lets us trace any silver row back to the exact bronze
object that produced it.

Feature flag: ``TUNABLES.lake_bronze_enabled`` gates the actual S3 put
call so the module imports cleanly even before the bucket / IAM are
provisioned. Local + CI runs leave the flag OFF; the EC2 deploy flips
it ON via env.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


# ── boto3 client cache + executor ─────────────────────────────────────


_client_lock = threading.Lock()
_client = None
_executor: Optional[ThreadPoolExecutor] = None


def lake_client():
    """Return a cached boto3 S3 client. Lazy-imports boto3 so the
    module can be imported on a box where boto3 isn't installed
    (graceful no-op path)."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        try:
            import boto3  # type: ignore
            _client = boto3.client("s3", region_name=TUNABLES.lake_region)
            return _client
        except Exception:
            logger.debug("lake_client: boto3 unavailable; lake disabled",
                          exc_info=True)
            return None


def _get_executor() -> Optional[ThreadPoolExecutor]:
    global _executor
    if _executor is not None:
        return _executor
    try:
        workers = max(1, int(TUNABLES.lake_async_workers))
    except Exception:
        workers = 4
    _executor = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="lake-write")
    return _executor


def shutdown_executor() -> None:
    """Idempotent shutdown — used by tests + clean process exit."""
    global _executor
    if _executor is not None:
        try:
            _executor.shutdown(wait=False)
        except Exception:
            pass
        _executor = None


# ── path helpers ──────────────────────────────────────────────────────


def _dt_str(ts: Optional[datetime]) -> str:
    if ts is None:
        ts = datetime.now(timezone.utc)
    if isinstance(ts, date) and not isinstance(ts, datetime):
        return ts.isoformat()
    return ts.date().isoformat()


def _bronze_key(source: str, dtype: str, dt: str, *,
                  ticker: Optional[str] = None,
                  filename: Optional[str] = None) -> str:
    parts = [
        "bronze",
        source.lower().strip(),
        dtype.lower().strip(),
        f"dt={dt}",
    ]
    if ticker:
        parts.append(f"ticker={ticker.upper().strip()}")
    parts.append(filename or f"{uuid.uuid4().hex}.parquet")
    return "/".join(parts)


def _silver_key(canonical_type: str, dt: str, *,
                  filename: Optional[str] = None) -> str:
    return "/".join([
        "silver",
        canonical_type.lower().strip(),
        f"dt={dt}",
        filename or f"{uuid.uuid4().hex}.parquet",
    ])


def _gold_key(table: str, dt: str) -> str:
    return f"gold/{table.lower().strip()}/dt={dt}/snapshot.parquet"


# ── payload normalization ─────────────────────────────────────────────


def _payload_to_records(payload: Any) -> List[Dict[str, Any]]:
    """Best-effort coerce any vendor payload to a list of row dicts.

    Supported shapes:

      * pandas DataFrame → records (orient='records')
      * list[dict]       → as-is
      * dict              → wrap in one-element list
      * everything else   → JSON-serialize into a single
                            ``{"raw_json": "..."}`` row so the
                            bronze layer still captures it
    """
    if payload is None:
        return []
    try:
        import pandas as pd  # type: ignore
        if isinstance(payload, pd.DataFrame):
            df = payload.copy()
            # Reset index so a DatetimeIndex becomes a column.
            df = df.reset_index() if df.index.name else df
            records = df.to_dict(orient="records")
            # Convert any pd.Timestamp / numpy types to plain Python.
            for row in records:
                for k, v in list(row.items()):
                    try:
                        if hasattr(v, "isoformat"):
                            row[k] = v.isoformat()
                        elif hasattr(v, "item"):
                            row[k] = v.item()
                    except Exception:
                        row[k] = str(v)
            return records
    except Exception:
        pass
    if isinstance(payload, list):
        if all(isinstance(x, dict) for x in payload):
            return payload  # already row dicts
        # else fall through to wrap
    if isinstance(payload, dict):
        return [payload]
    # Fall-through: serialize to JSON string.
    try:
        return [{"raw_json": json.dumps(payload, default=str)}]
    except Exception:
        return [{"raw_json": str(payload)}]


def _records_to_parquet_bytes(records: List[Dict[str, Any]],
                                  *, manifest: Dict[str, Any]) -> bytes:
    """Serialize records + manifest to Parquet bytes.

    Every row carries the manifest columns (fetch_ts, source_version,
    request_url, row_count) so downstream readers don't need a
    sidecar.
    """
    import pyarrow as pa  # type: ignore
    import pyarrow.parquet as pq  # type: ignore

    rows = list(records or [])
    if not rows:
        rows = [{"_empty": True}]
    for row in rows:
        for k, v in list(manifest.items()):
            row.setdefault(k, v)
    # Coerce all values to string when arrow can't infer (mixed dicts).
    # Build a unified schema by taking the union of keys.
    cols: Dict[str, list] = {}
    keys: List[str] = []
    for row in rows:
        for k in row:
            if k not in cols:
                cols[k] = []
                keys.append(k)
    for row in rows:
        for k in keys:
            v = row.get(k)
            if isinstance(v, (dict, list)):
                cols[k].append(json.dumps(v, default=str))
            else:
                cols[k].append(v)
    table = pa.table(cols)
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="snappy")
    return sink.getvalue()


# ── manifest ──────────────────────────────────────────────────────────


def _build_manifest(*, source: str, dtype: str,
                       source_version: Optional[str],
                       request_url: Optional[str],
                       row_count: int) -> Dict[str, Any]:
    return {
        "fetch_ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "dtype": dtype,
        "source_version": source_version or "unknown",
        "request_url": request_url or "",
        "row_count": int(row_count),
    }


# ── bronze writers ────────────────────────────────────────────────────


def _put_object(bucket: str, key: str, body: bytes,
                  *, content_type: str = "application/octet-stream") -> bool:
    client = lake_client()
    if client is None:
        return False
    try:
        client.put_object(
            Bucket=bucket, Key=key, Body=body, ContentType=content_type,
            ServerSideEncryption="AES256",
        )
        return True
    except Exception:
        logger.debug("lake put_object failed: %s/%s", bucket, key, exc_info=True)
        return False


def write_bronze(source: str, dtype: str, payload: Any,
                    ts: Optional[datetime] = None, *,
                    ticker: Optional[str] = None,
                    extra_tags: Optional[Dict[str, Any]] = None,
                    source_version: Optional[str] = None,
                    request_url: Optional[str] = None,
                    sync: bool = False) -> Optional[str]:
    """Capture a raw vendor payload to the bronze layer.

    Returns the S3 URI on a successful sync write, the queued key
    (best-effort) on async, or ``None`` when the lake is disabled.
    """
    if not getattr(TUNABLES, "lake_bronze_enabled", False):
        return None
    records = _payload_to_records(payload)
    manifest = _build_manifest(
        source=source, dtype=dtype, source_version=source_version,
        request_url=request_url, row_count=len(records),
    )
    if extra_tags:
        manifest.update({f"tag_{k}": v for k, v in extra_tags.items()})
    dt = _dt_str(ts)
    key = _bronze_key(source, dtype, dt, ticker=ticker)
    bucket = TUNABLES.lake_bucket

    def _do_write() -> bool:
        try:
            body = _records_to_parquet_bytes(records, manifest=manifest)
        except Exception:
            logger.debug("bronze serialize failed", exc_info=True)
            return False
        return _put_object(bucket, key, body)

    if sync:
        ok = _do_write()
        return f"s3://{bucket}/{key}" if ok else None
    executor = _get_executor()
    if executor is None:
        return None
    try:
        executor.submit(_do_write)
    except Exception:
        logger.debug("bronze submit failed", exc_info=True)
        return None
    return f"s3://{bucket}/{key}"


def read_bronze(source: str, dtype: str, dt: str,
                  *, ticker: Optional[str] = None):
    """Return a pandas DataFrame of every bronze parquet under the
    matching prefix. Empty DataFrame if nothing matches."""
    import pandas as pd  # type: ignore

    client = lake_client()
    bucket = TUNABLES.lake_bucket
    if client is None:
        return pd.DataFrame()
    prefix_parts = [
        "bronze", source.lower(), dtype.lower(), f"dt={dt}",
    ]
    if ticker:
        prefix_parts.append(f"ticker={ticker.upper()}")
    prefix = "/".join(prefix_parts) + "/"
    try:
        resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    except Exception:
        return pd.DataFrame()
    frames: List[Any] = []
    for obj in resp.get("Contents") or []:
        try:
            payload = client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            import pyarrow.parquet as pq  # type: ignore
            buf = io.BytesIO(payload)
            df = pq.read_table(buf).to_pandas()
            frames.append(df)
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ── silver writers/readers ────────────────────────────────────────────


def write_silver(canonical_type: str, records: List[Dict[str, Any]],
                    ts: Optional[datetime] = None, *,
                    source_version: Optional[str] = None,
                    request_url: Optional[str] = None,
                    sync: bool = False) -> Optional[str]:
    if not getattr(TUNABLES, "lake_bronze_enabled", False):
        return None
    manifest = _build_manifest(
        source="silver", dtype=canonical_type,
        source_version=source_version, request_url=request_url,
        row_count=len(records),
    )
    dt = _dt_str(ts)
    key = _silver_key(canonical_type, dt)
    bucket = TUNABLES.lake_bucket

    def _do_write() -> bool:
        try:
            body = _records_to_parquet_bytes(records, manifest=manifest)
        except Exception:
            return False
        return _put_object(bucket, key, body)

    if sync:
        ok = _do_write()
        return f"s3://{bucket}/{key}" if ok else None
    executor = _get_executor()
    if executor is None:
        return None
    executor.submit(_do_write)
    return f"s3://{bucket}/{key}"


def read_silver(canonical_type: str, dt: str):
    """Return a pandas DataFrame for the silver partition."""
    import pandas as pd  # type: ignore
    client = lake_client()
    bucket = TUNABLES.lake_bucket
    if client is None:
        return pd.DataFrame()
    prefix = f"silver/{canonical_type.lower()}/dt={dt}/"
    try:
        resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    except Exception:
        return pd.DataFrame()
    frames: List[Any] = []
    for obj in resp.get("Contents") or []:
        try:
            payload = client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            import pyarrow.parquet as pq  # type: ignore
            frames.append(pq.read_table(io.BytesIO(payload)).to_pandas())
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ── gold writers/readers ──────────────────────────────────────────────


def write_gold(table: str, records: List[Dict[str, Any]],
                  ts: Optional[datetime] = None, *,
                  sync: bool = True) -> Optional[str]:
    """Gold writes are typically SYNCHRONOUS — the nightly snapshot job
    wants the I/O to complete before it logs success."""
    if not getattr(TUNABLES, "lake_bronze_enabled", False):
        return None
    manifest = _build_manifest(
        source="gold", dtype=table,
        source_version="snapshot",
        request_url="sqlite://trading_bot.db", row_count=len(records),
    )
    dt = _dt_str(ts)
    key = _gold_key(table, dt)
    bucket = TUNABLES.lake_bucket
    try:
        body = _records_to_parquet_bytes(records, manifest=manifest)
    except Exception:
        return None
    ok = _put_object(bucket, key, body)
    return f"s3://{bucket}/{key}" if ok else None


def read_gold(table: str, dt: str):
    """Return a pandas DataFrame for the gold snapshot."""
    import pandas as pd  # type: ignore
    client = lake_client()
    bucket = TUNABLES.lake_bucket
    if client is None:
        return pd.DataFrame()
    key = _gold_key(table, dt)
    try:
        payload = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        import pyarrow.parquet as pq  # type: ignore
        return pq.read_table(io.BytesIO(payload)).to_pandas()
    except Exception:
        return pd.DataFrame()


# ── status helpers ────────────────────────────────────────────────────


@dataclass
class LakeLayerStat:
    layer: str
    bytes: int = 0
    object_count: int = 0
    last_modified: Optional[str] = None


def stat_layer(prefix: str) -> LakeLayerStat:
    """Cheap walk of a top-level prefix for status display."""
    client = lake_client()
    if client is None:
        return LakeLayerStat(layer=prefix)
    bucket = TUNABLES.lake_bucket
    total_bytes = 0
    count = 0
    latest: Optional[datetime] = None
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/"):
            for obj in page.get("Contents") or []:
                total_bytes += int(obj.get("Size") or 0)
                count += 1
                lm = obj.get("LastModified")
                if lm and (latest is None or lm > latest):
                    latest = lm
    except Exception:
        logger.debug("stat_layer failed for %s", prefix, exc_info=True)
    return LakeLayerStat(
        layer=prefix,
        bytes=total_bytes,
        object_count=count,
        last_modified=latest.isoformat() if latest else None,
    )


# ── bronze decorator: 1-line opt-in for fetcher sites ─────────────────


def bronze_capture(source: str, dtype: str, *,
                      ticker_from: Optional[str] = None,
                      url_template: Optional[str] = None):
    """Decorator: capture the function's return value into the bronze
    layer, then return it unchanged.

    Usage:

        @bronze_capture("yfinance", "bars", ticker_from="ticker")
        def fetch_yfinance_bars(ticker: str, ...) -> pd.DataFrame:
            ...

    Failures in the bronze path are SWALLOWED — the wrapped function's
    return is always passed through.
    """
    def _decorate(fn):
        def _wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            try:
                if getattr(TUNABLES, "lake_bronze_enabled", False):
                    tk = None
                    if ticker_from:
                        tk = kwargs.get(ticker_from)
                        if tk is None and args:
                            # Best-effort positional ticker resolution.
                            tk = args[0] if isinstance(args[0], str) else None
                    url = url_template or fn.__module__
                    write_bronze(
                        source, dtype, result,
                        ticker=tk if isinstance(tk, str) else None,
                        request_url=url,
                        source_version=fn.__module__,
                    )
            except Exception:
                logger.debug("bronze_capture wrapper failed", exc_info=True)
            return result
        _wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        _wrapper.__name__ = fn.__name__
        _wrapper.__doc__ = fn.__doc__
        return _wrapper
    return _decorate


# ── disaster-recovery helper: list available gold snapshot dates ──────


def list_gold_dates(table: str) -> List[str]:
    client = lake_client()
    if client is None:
        return []
    bucket = TUNABLES.lake_bucket
    prefix = f"gold/{table.lower()}/"
    out: List[str] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj.get("Key") or ""
                # gold/<table>/dt=YYYY-MM-DD/snapshot.parquet
                parts = key.split("/")
                for p in parts:
                    if p.startswith("dt="):
                        out.append(p.split("=", 1)[1])
    except Exception:
        return []
    return sorted(set(out))


__all__ = [
    "lake_client",
    "write_bronze", "read_bronze",
    "write_silver", "read_silver",
    "write_gold", "read_gold",
    "stat_layer", "LakeLayerStat",
    "bronze_capture", "list_gold_dates", "shutdown_executor",
]
