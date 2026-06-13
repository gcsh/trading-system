"""MITS Phase 8.1 — lake writer unit tests with a moto-mocked S3."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

BUCKET = "tradingbot-lake-157320905163"


@pytest.fixture()
def mocked_lake(monkeypatch):
    """Spin up a mocked S3 + flip the feature flag ON."""
    with mock_aws():
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        # Force the lake module to rebuild its client + executor with
        # the mocked credentials in scope.
        from backend.bot.data import lake
        from backend.config import TUNABLES
        prev = TUNABLES.lake_bronze_enabled
        TUNABLES.lake_bronze_enabled = True
        lake._client = None
        lake.shutdown_executor()
        yield lake, s3
        TUNABLES.lake_bronze_enabled = prev
        lake.shutdown_executor()
        lake._client = None


def _list(s3, prefix):
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return resp.get("Contents") or []


def test_write_bronze_sync_creates_parquet(mocked_lake):
    lake, s3 = mocked_lake
    uri = lake.write_bronze(
        "yfinance", "bars",
        [{"ticker": "SPY", "close": 500.0}],
        ts=datetime(2026, 6, 6, tzinfo=timezone.utc),
        ticker="SPY",
        sync=True,
    )
    assert uri is not None and uri.startswith("s3://")
    objs = _list(s3, "bronze/yfinance/bars/dt=2026-06-06/ticker=SPY/")
    assert len(objs) == 1
    assert objs[0]["Key"].endswith(".parquet")
    # Round-trip the parquet → DataFrame.
    df = lake.read_bronze("yfinance", "bars", "2026-06-06", ticker="SPY")
    assert len(df) == 1
    assert "ticker" in df.columns
    # Manifest invariants — every row carries the required columns.
    for col in ("fetch_ts", "source_version", "request_url", "row_count"):
        assert col in df.columns


def test_write_bronze_disabled_returns_none(monkeypatch, mocked_lake):
    lake, _s3 = mocked_lake
    from backend.config import TUNABLES
    TUNABLES.lake_bronze_enabled = False
    assert lake.write_bronze("yfinance", "bars",
                                 [{"ticker": "SPY"}], sync=True) is None


def test_partition_path_includes_ticker(mocked_lake):
    lake, s3 = mocked_lake
    lake.write_bronze("thetadata", "chain",
                        [{"strike": 500.0}], ticker="QQQ",
                        ts=datetime(2026, 6, 1), sync=True)
    keys = [o["Key"] for o in _list(s3, "bronze/thetadata/chain/dt=2026-06-01/")]
    assert any("ticker=QQQ" in k for k in keys)


def test_silver_partition_no_ticker(mocked_lake):
    lake, s3 = mocked_lake
    lake.write_silver("bars",
                        [{"ticker": "SPY", "close": 500.0}],
                        ts=datetime(2026, 6, 1), sync=True)
    keys = [o["Key"] for o in _list(s3, "silver/bars/dt=2026-06-01/")]
    assert len(keys) == 1
    # Silver MUST NOT partition by source — downstream readers don't care.
    assert "source=" not in keys[0]


def test_gold_snapshot_idempotent(mocked_lake):
    lake, s3 = mocked_lake
    uri1 = lake.write_gold("trades",
                              [{"id": 1, "pnl": 5.0}],
                              ts=datetime(2026, 6, 6), sync=True)
    uri2 = lake.write_gold("trades",
                              [{"id": 1, "pnl": 7.0}],   # overwrite
                              ts=datetime(2026, 6, 6), sync=True)
    assert uri1 == uri2  # idempotent key
    df = lake.read_gold("trades", "2026-06-06")
    assert len(df) == 1
    assert float(df.iloc[0]["pnl"]) == 7.0
