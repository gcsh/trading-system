#!/usr/bin/env python
"""MITS Phase 8.6 — Idempotent Glue catalog registration.

Creates / refreshes the AWS Glue database ``tradingbot_lake`` and the
external tables that point at the bronze/silver/gold parquet partitions
the bot writes. Re-runnable: every CREATE is preceded by a DELETE so a
fresh schema replaces an old one. Safe to invoke from cron or on
manual deploys.

Tables registered:

  bronze layer (raw fetcher payloads):
    bronze_yf_bars         — yfinance bars
    bronze_thetadata_bars  — ThetaData bars
    bronze_thetadata_chain — option chains
    bronze_fred            — FRED macro series
    bronze_edgar           — EDGAR filings
    bronze_finra           — FINRA short-interest + dark-pool
    bronze_cot             — CFTC COT reports
    bronze_breadth         — NYSE advancers/decliners
    bronze_cboe            — Cboe put/call ratio
    bronze_alpaca_ticks    — sampled Alpaca tick stream

  silver layer (canonical schemas in silver.py):
    bars, quotes, options, observations, macro, filings

  gold layer (nightly SQLite snapshots):
    trades, paper_positions, paper_account, decision_log,
    market_observations, knowledge_graph, eod_analysis,
    portfolio_snapshots, regime_episode_snapshots, etc.

Run:

    AWS_PROFILE=lm-arbiter-poc python bin/register_glue_catalog.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s glue: %(message)s",
)
log = logging.getLogger("glue")


DB_NAME = "tradingbot_lake"
LAKE_BUCKET = "tradingbot-lake-157320905163"
REGION = "us-east-1"


def _client():
    import boto3
    return boto3.client("glue", region_name=REGION)


def _ensure_db(glue) -> None:
    try:
        glue.get_database(Name=DB_NAME)
        log.info("database %s exists", DB_NAME)
    except glue.exceptions.EntityNotFoundException:
        glue.create_database(DatabaseInput={
            "Name": DB_NAME,
            "Description": "MITS Phase 8 lake catalog",
        })
        log.info("database %s created", DB_NAME)


def _drop_table(glue, name: str) -> None:
    try:
        glue.delete_table(DatabaseName=DB_NAME, Name=name)
        log.info("dropped pre-existing table %s", name)
    except glue.exceptions.EntityNotFoundException:
        pass


def _parquet_storage(location: str, columns: List[Dict[str, str]],
                       partition_keys: Optional[List[Dict[str, str]]] = None,
                       ) -> Dict[str, Any]:
    return {
        "Name": "",  # filled in below
        "StorageDescriptor": {
            "Columns": columns,
            "Location": location,
            "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
            "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
            "SerdeInfo": {
                "SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
                "Parameters": {"serialization.format": "1"},
            },
            "Compressed": True,
            "StoredAsSubDirectories": False,
        },
        "PartitionKeys": partition_keys or [],
        "TableType": "EXTERNAL_TABLE",
        "Parameters": {
            "classification": "parquet",
            "compressionType": "snappy",
            "typeOfData": "file",
            "projection.enabled": "true",
        },
    }


def _bronze_ticker_table(table: str, source: str, dtype: str,
                            columns: List[Dict[str, str]]) -> Dict[str, Any]:
    loc = f"s3://{LAKE_BUCKET}/bronze/{source}/{dtype}/"
    t = _parquet_storage(loc, columns, partition_keys=[
        {"Name": "dt", "Type": "string"},
        {"Name": "ticker", "Type": "string"},
    ])
    t["Name"] = table
    t["Parameters"].update({
        "projection.dt.type": "date",
        "projection.dt.format": "yyyy-MM-dd",
        "projection.dt.range": "2024-01-01,NOW",
        "projection.ticker.type": "injected",
        "storage.location.template":
            f"s3://{LAKE_BUCKET}/bronze/{source}/{dtype}/dt=${{dt}}/ticker=${{ticker}}/",
    })
    return t


def _bronze_flat_table(table: str, source: str, dtype: str,
                         columns: List[Dict[str, str]]) -> Dict[str, Any]:
    loc = f"s3://{LAKE_BUCKET}/bronze/{source}/{dtype}/"
    t = _parquet_storage(loc, columns, partition_keys=[
        {"Name": "dt", "Type": "string"},
    ])
    t["Name"] = table
    t["Parameters"].update({
        "projection.dt.type": "date",
        "projection.dt.format": "yyyy-MM-dd",
        "projection.dt.range": "2024-01-01,NOW",
        "storage.location.template":
            f"s3://{LAKE_BUCKET}/bronze/{source}/{dtype}/dt=${{dt}}/",
    })
    return t


def _silver_table(canonical: str, columns: List[Dict[str, str]]) -> Dict[str, Any]:
    loc = f"s3://{LAKE_BUCKET}/silver/{canonical}/"
    t = _parquet_storage(loc, columns, partition_keys=[
        {"Name": "dt", "Type": "string"},
    ])
    t["Name"] = canonical
    t["Parameters"].update({
        "projection.dt.type": "date",
        "projection.dt.format": "yyyy-MM-dd",
        "projection.dt.range": "2024-01-01,NOW",
        "storage.location.template": f"s3://{LAKE_BUCKET}/silver/{canonical}/dt=${{dt}}/",
    })
    return t


def _gold_table(table: str, columns: List[Dict[str, str]]) -> Dict[str, Any]:
    loc = f"s3://{LAKE_BUCKET}/gold/{table}/"
    t = _parquet_storage(loc, columns, partition_keys=[
        {"Name": "dt", "Type": "string"},
    ])
    t["Name"] = table
    t["Parameters"].update({
        "projection.dt.type": "date",
        "projection.dt.format": "yyyy-MM-dd",
        "projection.dt.range": "2024-01-01,NOW",
        "storage.location.template": f"s3://{LAKE_BUCKET}/gold/{table}/dt=${{dt}}/",
    })
    return t


# Common bronze columns + manifest.
BAR_COLS = [
    {"Name": "ts", "Type": "string"},
    {"Name": "open", "Type": "double"},
    {"Name": "high", "Type": "double"},
    {"Name": "low", "Type": "double"},
    {"Name": "close", "Type": "double"},
    {"Name": "volume", "Type": "double"},
    {"Name": "fetch_ts", "Type": "string"},
    {"Name": "source", "Type": "string"},
    {"Name": "source_version", "Type": "string"},
    {"Name": "request_url", "Type": "string"},
    {"Name": "row_count", "Type": "int"},
]
OPTION_COLS = [
    {"Name": "strike", "Type": "double"},
    {"Name": "expiry", "Type": "string"},
    {"Name": "right", "Type": "string"},
    {"Name": "bid", "Type": "double"},
    {"Name": "ask", "Type": "double"},
    {"Name": "mid", "Type": "double"},
    {"Name": "iv", "Type": "double"},
    {"Name": "delta", "Type": "double"},
    {"Name": "gamma", "Type": "double"},
    {"Name": "vega", "Type": "double"},
    {"Name": "theta", "Type": "double"},
    {"Name": "oi", "Type": "double"},
    {"Name": "volume", "Type": "double"},
    {"Name": "ts", "Type": "string"},
    {"Name": "fetch_ts", "Type": "string"},
    {"Name": "source", "Type": "string"},
    {"Name": "source_version", "Type": "string"},
    {"Name": "request_url", "Type": "string"},
    {"Name": "row_count", "Type": "int"},
]
FRED_COLS = [
    {"Name": "series_id", "Type": "string"},
    {"Name": "date", "Type": "string"},
    {"Name": "value", "Type": "double"},
    {"Name": "fetch_ts", "Type": "string"},
    {"Name": "source", "Type": "string"},
    {"Name": "source_version", "Type": "string"},
    {"Name": "request_url", "Type": "string"},
    {"Name": "row_count", "Type": "int"},
]
EDGAR_COLS = [
    {"Name": "cik", "Type": "string"},
    {"Name": "ticker", "Type": "string"},
    {"Name": "form", "Type": "string"},
    {"Name": "filed_at", "Type": "string"},
    {"Name": "raw_json", "Type": "string"},
    {"Name": "fetch_ts", "Type": "string"},
    {"Name": "source", "Type": "string"},
]
FINRA_COLS = [
    {"Name": "ts", "Type": "string"},
    {"Name": "ticker", "Type": "string"},
    {"Name": "metric", "Type": "string"},
    {"Name": "value", "Type": "double"},
    {"Name": "raw_json", "Type": "string"},
    {"Name": "fetch_ts", "Type": "string"},
    {"Name": "source", "Type": "string"},
]
COT_COLS = [
    {"Name": "report_date", "Type": "string"},
    {"Name": "category", "Type": "string"},
    {"Name": "value", "Type": "double"},
    {"Name": "raw_json", "Type": "string"},
    {"Name": "fetch_ts", "Type": "string"},
    {"Name": "source", "Type": "string"},
]
BREADTH_COLS = [
    {"Name": "ts", "Type": "string"},
    {"Name": "advancers", "Type": "int"},
    {"Name": "decliners", "Type": "int"},
    {"Name": "pct_above_50dma", "Type": "double"},
    {"Name": "pct_above_200dma", "Type": "double"},
    {"Name": "fetch_ts", "Type": "string"},
    {"Name": "source", "Type": "string"},
]
CBOE_COLS = [
    {"Name": "ts", "Type": "string"},
    {"Name": "put_call_ratio", "Type": "double"},
    {"Name": "fetch_ts", "Type": "string"},
    {"Name": "source", "Type": "string"},
]
TICK_COLS = [
    {"Name": "ts", "Type": "string"},
    {"Name": "bid", "Type": "double"},
    {"Name": "ask", "Type": "double"},
    {"Name": "last", "Type": "double"},
    {"Name": "bid_size", "Type": "double"},
    {"Name": "ask_size", "Type": "double"},
    {"Name": "fetch_ts", "Type": "string"},
    {"Name": "source", "Type": "string"},
]


def _silver_bar_cols() -> List[Dict[str, str]]:
    return BAR_COLS + [
        {"Name": "ticker", "Type": "string"},
        {"Name": "vwap", "Type": "double"},
        {"Name": "integrity_status", "Type": "string"},
        {"Name": "lineage_bronze_uri", "Type": "string"},
        {"Name": "silver_ts", "Type": "string"},
    ]


def _silver_option_cols() -> List[Dict[str, str]]:
    return OPTION_COLS + [
        {"Name": "ticker", "Type": "string"},
        {"Name": "integrity_status", "Type": "string"},
        {"Name": "lineage_bronze_uri", "Type": "string"},
        {"Name": "silver_ts", "Type": "string"},
    ]


def _silver_quote_cols() -> List[Dict[str, str]]:
    return [
        {"Name": "ticker", "Type": "string"},
        {"Name": "ts", "Type": "string"},
        {"Name": "bid", "Type": "double"},
        {"Name": "ask", "Type": "double"},
        {"Name": "bid_size", "Type": "double"},
        {"Name": "ask_size", "Type": "double"},
        {"Name": "last", "Type": "double"},
        {"Name": "source", "Type": "string"},
        {"Name": "source_version", "Type": "string"},
        {"Name": "integrity_status", "Type": "string"},
        {"Name": "lineage_bronze_uri", "Type": "string"},
        {"Name": "silver_ts", "Type": "string"},
    ]


def _silver_obs_cols() -> List[Dict[str, str]]:
    return [
        {"Name": "observation_id", "Type": "string"},
        {"Name": "ticker", "Type": "string"},
        {"Name": "pattern", "Type": "string"},
        {"Name": "ts", "Type": "string"},
        {"Name": "regime", "Type": "string"},
        {"Name": "features_json", "Type": "string"},
        {"Name": "source", "Type": "string"},
        {"Name": "source_version", "Type": "string"},
        {"Name": "integrity_status", "Type": "string"},
        {"Name": "lineage_bronze_uri", "Type": "string"},
        {"Name": "silver_ts", "Type": "string"},
    ]


def _silver_macro_cols() -> List[Dict[str, str]]:
    return [
        {"Name": "series_id", "Type": "string"},
        {"Name": "ts", "Type": "string"},
        {"Name": "value", "Type": "double"},
        {"Name": "source", "Type": "string"},
        {"Name": "source_version", "Type": "string"},
        {"Name": "integrity_status", "Type": "string"},
        {"Name": "lineage_bronze_uri", "Type": "string"},
        {"Name": "silver_ts", "Type": "string"},
    ]


def _silver_filing_cols() -> List[Dict[str, str]]:
    return [
        {"Name": "cik", "Type": "string"},
        {"Name": "ticker", "Type": "string"},
        {"Name": "filing_type", "Type": "string"},
        {"Name": "filing_date", "Type": "string"},
        {"Name": "accession_number", "Type": "string"},
        {"Name": "content_url", "Type": "string"},
        {"Name": "source", "Type": "string"},
        {"Name": "source_version", "Type": "string"},
        {"Name": "integrity_status", "Type": "string"},
        {"Name": "lineage_bronze_uri", "Type": "string"},
        {"Name": "silver_ts", "Type": "string"},
    ]


def _gold_trades_cols() -> List[Dict[str, str]]:
    return [
        {"Name": "id", "Type": "int"},
        {"Name": "ticker", "Type": "string"},
        {"Name": "strategy", "Type": "string"},
        {"Name": "regime", "Type": "string"},
        {"Name": "status", "Type": "string"},
        {"Name": "opened_at", "Type": "string"},
        {"Name": "closed_at", "Type": "string"},
        {"Name": "qty", "Type": "double"},
        {"Name": "entry_price", "Type": "double"},
        {"Name": "exit_price", "Type": "double"},
        {"Name": "pnl", "Type": "double"},
        {"Name": "detail_json", "Type": "string"},
        {"Name": "fetch_ts", "Type": "string"},
    ]


GENERIC_GOLD_COLS = [
    {"Name": "id", "Type": "int"},
    {"Name": "ts", "Type": "string"},
    {"Name": "fetch_ts", "Type": "string"},
    {"Name": "raw_json", "Type": "string"},
]


def register_all() -> Dict[str, int]:
    glue = _client()
    _ensure_db(glue)

    tables: List[Dict[str, Any]] = []

    # Bronze — per-ticker partitioned.
    tables.append(_bronze_ticker_table("bronze_yf_bars", "yfinance", "bars", BAR_COLS + [{"Name": "ticker", "Type": "string"}]))
    tables.append(_bronze_ticker_table("bronze_thetadata_bars", "thetadata", "bars", BAR_COLS + [{"Name": "ticker", "Type": "string"}]))
    tables.append(_bronze_ticker_table("bronze_thetadata_chain", "thetadata", "chain", OPTION_COLS + [{"Name": "ticker", "Type": "string"}]))
    tables.append(_bronze_ticker_table("bronze_thetadata_iv", "thetadata", "iv_snapshot", OPTION_COLS + [{"Name": "ticker", "Type": "string"}]))
    tables.append(_bronze_ticker_table("bronze_alpaca_ticks", "alpaca_stream", "ticks", TICK_COLS + [{"Name": "ticker", "Type": "string"}]))

    # Bronze — flat (no ticker dim).
    tables.append(_bronze_flat_table("bronze_fred", "fred", "series", FRED_COLS))
    tables.append(_bronze_flat_table("bronze_edgar", "edgar", "filings", EDGAR_COLS))
    tables.append(_bronze_flat_table("bronze_finra_si", "finra", "short_interest", FINRA_COLS))
    tables.append(_bronze_flat_table("bronze_finra_dp", "finra", "dark_pool", FINRA_COLS))
    tables.append(_bronze_flat_table("bronze_cot", "cot", "reports", COT_COLS))
    tables.append(_bronze_flat_table("bronze_breadth", "breadth", "snapshot", BREADTH_COLS))
    tables.append(_bronze_flat_table("bronze_cboe", "cboe", "put_call", CBOE_COLS))

    # Silver — canonical.
    tables.append(_silver_table("bars", _silver_bar_cols()))
    tables.append(_silver_table("quotes", _silver_quote_cols()))
    tables.append(_silver_table("options", _silver_option_cols()))
    tables.append(_silver_table("observations", _silver_obs_cols()))
    tables.append(_silver_table("macro", _silver_macro_cols()))
    tables.append(_silver_table("filings", _silver_filing_cols()))

    # Gold — bot SQLite snapshots. We register the high-value ones with
    # explicit columns; the rest are catch-alls.
    tables.append(_gold_table("trades", _gold_trades_cols()))
    for catch_all in [
        "paper_positions", "paper_account", "decision_log",
        "portfolio_snapshots", "market_observations", "market_outcomes",
        "knowledge_graph", "knowledge_graph_history",
        "pattern_priors", "corpus_status", "iv_history", "intraday_iv_cache",
        "gex_regime_history", "eod_analysis", "eod_prediction_outcomes",
        "detector_config", "detector_suggestions", "weekly_retrospectives",
        "intraday_regime_events", "regime_episode_snapshots",
        "ingest_watermarks", "fred_observations", "edgar_filings",
        "short_interest", "cot_reports", "breadth_snapshots",
        "earnings_call_intel", "watchlist_items", "seen_flow_alerts",
        "bot_config", "experiment_record", "lake_sync_watermark",
        "execution_log",
    ]:
        tables.append(_gold_table(catch_all, GENERIC_GOLD_COLS))

    written = 0
    for t in tables:
        _drop_table(glue, t["Name"])
        try:
            glue.create_table(DatabaseName=DB_NAME, TableInput=t)
            log.info("created table %s", t["Name"])
            written += 1
        except Exception as exc:
            log.error("create_table %s failed: %s", t["Name"], exc)
    return {"tables_registered": written, "total_attempted": len(tables)}


def main() -> int:
    stats = register_all()
    print(json.dumps(stats, indent=2))
    return 0 if stats["tables_registered"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
