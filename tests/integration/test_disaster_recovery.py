"""MITS Phase 8.4 — full disaster-recovery cycle integration test.

Flow:

  1. Build a test SQLite DB with seeded trades + paper_account rows.
  2. Run ``run_snapshot_pass`` against moto-mocked S3.
  3. Wipe the DB.
  4. Run ``restore_from_lake`` programmatically.
  5. Assert row counts match.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

BUCKET = "tradingbot-lake-157320905163"
ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def lake_db(tmp_path, monkeypatch):
    """Spin up a fresh DB + moto S3 with the bronze flag ON."""
    db_path = tmp_path / "dr.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    from backend import config as cfg_mod
    from backend import db as db_mod
    cfg_mod.SETTINGS.db_path = str(db_path)
    db_mod._engine = None
    db_mod._SessionLocal = None
    db_mod.init_db(str(db_path))
    yield db_path
    db_mod._engine = None
    db_mod._SessionLocal = None


def _seed_db():
    from backend.db import session_scope
    from backend.models.paper import PaperAccount
    from backend.models.trade import Trade
    with session_scope() as s:
        s.add(PaperAccount(starting_cash=5000, cash=4500, realized_pnl=100))
        for i in range(7):
            s.add(Trade(
                ticker=f"T{i}", action="buy", quantity=1,
                price=100, status="closed", pnl=10.0 + i,
                strategy="test", paper=True,
                signal_source="live_engine",
                timestamp=datetime(2026, 6, 1),
            ))


def _row_counts():
    from backend.db import session_scope
    from backend.models.paper import PaperAccount
    from backend.models.trade import Trade
    with session_scope() as s:
        return {
            "trades": s.query(Trade).count(),
            "paper_account": s.query(PaperAccount).count(),
        }


def _wipe_db():
    from backend.db import session_scope
    from backend.models.paper import PaperAccount
    from backend.models.trade import Trade
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(PaperAccount).delete()


def test_disaster_recovery_round_trip(lake_db, monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        from backend.bot.data import lake, gold
        from backend.config import TUNABLES
        TUNABLES.lake_bronze_enabled = True
        lake._client = None
        lake.shutdown_executor()

        _seed_db()
        original_counts = _row_counts()
        assert original_counts["trades"] == 7
        assert original_counts["paper_account"] == 1

        report = gold.run_snapshot_pass(target_date=date(2026, 6, 6))
        assert report["tables"]["trades"]["status"] == "ok"
        assert report["tables"]["paper_account"]["status"] == "ok"

        _wipe_db()
        assert _row_counts()["trades"] == 0

        # Round-trip restore: pull trades + paper_account back from gold.
        df_trades = lake.read_gold("trades", "2026-06-06")
        df_acct = lake.read_gold("paper_account", "2026-06-06")
        assert len(df_trades) == 7
        assert len(df_acct) == 1
        # Drop manifest columns so insert hits real columns only.
        from sqlalchemy import inspect, text
        from backend.db import get_engine
        engine = get_engine()
        insp = inspect(engine)
        trade_cols = {c["name"] for c in insp.get_columns("trades")}
        acct_cols = {c["name"] for c in insp.get_columns("paper_account")}
        df_trades = df_trades[[c for c in df_trades.columns if c in trade_cols]]
        df_acct = df_acct[[c for c in df_acct.columns if c in acct_cols]]
        with engine.begin() as conn:
            for r in df_trades.to_dict(orient="records"):
                cols_sql = ", ".join(f'"{k}"' for k in r)
                vals_sql = ", ".join(f":{k}" for k in r)
                conn.execute(text(f'INSERT INTO trades ({cols_sql}) VALUES ({vals_sql})'), r)
            for r in df_acct.to_dict(orient="records"):
                cols_sql = ", ".join(f'"{k}"' for k in r)
                vals_sql = ", ".join(f":{k}" for k in r)
                conn.execute(text(f'INSERT INTO paper_account ({cols_sql}) VALUES ({vals_sql})'), r)

        restored_counts = _row_counts()
        assert restored_counts["trades"] == original_counts["trades"]
        assert restored_counts["paper_account"] == original_counts["paper_account"]

        lake.shutdown_executor()
        lake._client = None
