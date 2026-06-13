# MITS Phase 8.6 — Athena query cookbook

Workgroup: `tradingbot-research`
Glue database: `tradingbot_lake`
Result location: `s3://tradingbot-lake-157320905163/athena/`

These tables are CREATEd via the Glue catalog from the parquet partitions
the bot writes. Use them as starting points; the schema columns mirror
the Pydantic types in `backend/bot/data/silver.py` and the SQLite columns
snapshotted to gold.

## 1. Cross-vendor parity audit

Show SPY bars where yfinance close differed from ThetaData close by
more than 0.5%. Bronze stores the raw fetch payload per vendor, so this
quickly surfaces whether a regression is "vendor noise" vs "real signal."

```sql
WITH yf AS (
  SELECT date_format(from_iso8601_timestamp(ts), '%Y-%m-%d %H:%i') AS bucket,
         AVG(close)  AS yf_close
  FROM "tradingbot_lake"."bars"
  WHERE source = 'yfinance' AND ticker = 'SPY' AND dt >= '2026-06-01'
  GROUP BY 1
), td AS (
  SELECT date_format(from_iso8601_timestamp(ts), '%Y-%m-%d %H:%i') AS bucket,
         AVG(close)  AS td_close
  FROM "tradingbot_lake"."bars"
  WHERE source = 'thetadata' AND ticker = 'SPY' AND dt >= '2026-06-01'
  GROUP BY 1
)
SELECT yf.bucket, yf.yf_close, td.td_close,
       ABS(yf.yf_close - td.td_close) / NULLIF(td.td_close, 0) * 100 AS pct_diff
FROM yf JOIN td ON yf.bucket = td.bucket
WHERE ABS(yf.yf_close - td.td_close) / NULLIF(td.td_close, 0) > 0.005
ORDER BY pct_diff DESC
LIMIT 200;
```

## 2. Historical panic-day backtest screen

Find every dt where VIX changed > 20% (close-to-close) AND breadth (pct
above 50-day) was < 0.20. These are the regimes that drive the Phase 7
discretionary opportunism layer — gold answers in seconds.

```sql
WITH vix AS (
  SELECT dt, MAX(value) AS vix_close
  FROM "tradingbot_lake"."fred_observations"
  WHERE series_id = 'VIXCLS'
  GROUP BY dt
), bread AS (
  SELECT dt, MAX(pct_above_50dma) AS breadth_50
  FROM "tradingbot_lake"."breadth_snapshots"
  GROUP BY dt
)
SELECT v.dt, v.vix_close,
       LAG(v.vix_close) OVER (ORDER BY v.dt) AS prev_close,
       (v.vix_close - LAG(v.vix_close) OVER (ORDER BY v.dt))
         / LAG(v.vix_close) OVER (ORDER BY v.dt) * 100 AS vix_change_pct,
       b.breadth_50
FROM vix v JOIN bread b USING (dt)
QUALIFY ABS(vix_change_pct) > 20 AND breadth_50 < 0.20
ORDER BY v.dt DESC
LIMIT 100;
```

## 3. Detector P&L attribution

Join closed trades to EOD analysis predictions, group by the top
detector pattern, and sum realized PnL. This is the gold-standard way
to answer "which detector actually made money in production."

```sql
WITH trades AS (
  SELECT id AS trade_id, ticker, pnl, opened_at,
         json_extract_scalar(detail_json, '$.top_pattern') AS top_pattern
  FROM "tradingbot_lake"."trades"
  WHERE status IN ('closed') AND pnl IS NOT NULL
), eod AS (
  SELECT ticker, analysis_date, top_pattern AS predicted_pattern,
         posterior, recommended_action
  FROM "tradingbot_lake"."eod_analysis"
)
SELECT t.top_pattern,
       COUNT(*)        AS n_trades,
       SUM(t.pnl)      AS realized_pnl,
       AVG(t.pnl)      AS avg_pnl,
       AVG(CAST(t.pnl > 0 AS INTEGER)) AS win_rate
FROM trades t LEFT JOIN eod e
  ON t.ticker = e.ticker
 AND date_format(from_iso8601_timestamp(t.opened_at), '%Y-%m-%d') = CAST(e.analysis_date AS VARCHAR)
WHERE t.top_pattern IS NOT NULL
GROUP BY t.top_pattern
ORDER BY realized_pnl DESC;
```

## 4. Vendor freshness map

Sanity check: how recent is each silver canonical type per source? Helps
catch silent feed staleness even before the warnings UI rings.

```sql
SELECT canonical_type, source,
       MAX(silver_ts) AS most_recent,
       COUNT(*)       AS rows
FROM (
  SELECT 'bars'        AS canonical_type, source, silver_ts FROM "tradingbot_lake"."bars"        UNION ALL
  SELECT 'quotes'      AS canonical_type, source, silver_ts FROM "tradingbot_lake"."quotes"      UNION ALL
  SELECT 'options'     AS canonical_type, source, silver_ts FROM "tradingbot_lake"."options"     UNION ALL
  SELECT 'macro'       AS canonical_type, source, silver_ts FROM "tradingbot_lake"."macro"
)
GROUP BY 1, 2
ORDER BY 1, 2;
```

## 5. Regulator-audit ledger

Every trade with full lineage — opportunity hypothesis, gate decisions,
fill, exit — in one CSV-friendly result. Drop into a notebook or hand to
a compliance reviewer.

```sql
SELECT t.id AS trade_id, t.ticker, t.strategy, t.opened_at, t.closed_at,
       t.pnl, t.status,
       json_extract_scalar(t.detail_json, '$.opportunity_hypothesis.thesis')   AS opp_thesis,
       json_extract_scalar(t.detail_json, '$.opportunity_hypothesis.conviction') AS opp_conviction,
       json_extract_scalar(t.detail_json, '$.gate_decisions')                  AS gate_log
FROM "tradingbot_lake"."trades" t
WHERE t.closed_at IS NOT NULL
ORDER BY t.closed_at DESC
LIMIT 500;
```

## 6. SPY 1-min bars on a specific date

Pull every SPY 1-minute bar the bot recorded on 2026-06-05. Partition
pruning means this scans only the SPY × 2026-06-05 partition, not the
full corpus.

```sql
SELECT ts, open, high, low, close, volume, source
FROM "tradingbot_lake"."bars"
WHERE dt = '2026-06-05'
  AND ticker = 'SPY'
ORDER BY ts;
```

## 7. Top-10 highest-IV days for AAPL (last year)

Surface the days the corpus saw AAPL implied vol explode — the
fingerprints of pre-earnings setups + macro shocks. The silver options
layer carries IV per contract, so we average the ATM contracts for the
ranking.

```sql
SELECT dt,
       AVG(iv)   AS avg_iv,
       MAX(iv)   AS peak_iv,
       COUNT(*)  AS contracts
FROM "tradingbot_lake"."options"
WHERE ticker = 'AAPL'
  AND dt >= date_format(current_date - interval '365' day, '%Y-%m-%d')
  AND iv > 0
  AND ABS(delta) BETWEEN 0.35 AND 0.65
GROUP BY dt
ORDER BY avg_iv DESC
LIMIT 10;
```

## 8. EOD predictions that were not traded — group by skip_reason

The EOD analysis layer logs a predicted setup and the live engine logs
why each was skipped. Join + group_by to see whether the bot is filtering
out winners.

```sql
SELECT json_extract_scalar(d.detail_json, '$.skip_reason') AS skip_reason,
       COUNT(*)                                            AS skipped_count,
       SUM(CASE WHEN o.realized_return > 0 THEN 1 ELSE 0 END) AS would_have_won,
       AVG(o.realized_return)                              AS avg_realized
FROM "tradingbot_lake"."decision_log" d
LEFT JOIN "tradingbot_lake"."eod_prediction_outcomes" o
  ON o.eod_id = CAST(json_extract_scalar(d.detail_json, '$.eod_id') AS INTEGER)
WHERE d.dt >= date_format(current_date - interval '30' day, '%Y-%m-%d')
  AND json_extract_scalar(d.detail_json, '$.skip_reason') IS NOT NULL
GROUP BY 1
ORDER BY skipped_count DESC;
```

## 9. Detector hit-rate by family across the corpus

Each MarketObservation carries the pattern that fired. Joined to
MarketOutcomes (realized P&L proxy), you get the live-corpus hit rate
per detector family — the same shape Phase 6 publishes in the scorecard,
queryable here without touching SQLite.

```sql
SELECT regexp_extract(o.pattern, '^([a-zA-Z]+)') AS detector_family,
       COUNT(*)                                  AS observations,
       SUM(CASE WHEN m.win = TRUE THEN 1 ELSE 0 END) AS wins,
       CAST(SUM(CASE WHEN m.win = TRUE THEN 1 ELSE 0 END) AS DOUBLE)
         / NULLIF(COUNT(*), 0)                   AS hit_rate
FROM "tradingbot_lake"."market_observations" o
LEFT JOIN "tradingbot_lake"."market_outcomes" m
  ON m.observation_id = o.id
WHERE o.dt >= date_format(current_date - interval '90' day, '%Y-%m-%d')
GROUP BY 1
ORDER BY observations DESC;
```

## 10. All FOMC events with subsequent 5-day SPY return

Pull every FRED FOMC announcement timestamp from the macro layer and
join the next-5-trading-day SPY return. Use the answer to drive event
sizing in the catalyst gate.

```sql
WITH fomc AS (
  SELECT ts AS fomc_date
  FROM "tradingbot_lake"."macro"
  WHERE series_id = 'DFEDTARU'
    AND dt >= '2025-01-01'
), spy_close AS (
  SELECT dt, MAX(close) AS spy_close
  FROM "tradingbot_lake"."bars"
  WHERE ticker = 'SPY'
  GROUP BY dt
)
SELECT f.fomc_date,
       sc0.spy_close AS spy_on_event,
       sc5.spy_close AS spy_5d_after,
       (sc5.spy_close - sc0.spy_close) / NULLIF(sc0.spy_close, 0) * 100 AS pct_5d
FROM fomc f
LEFT JOIN spy_close sc0 ON sc0.dt = substr(f.fomc_date, 1, 10)
LEFT JOIN spy_close sc5
  ON sc5.dt = date_format(
       date_parse(substr(f.fomc_date, 1, 10), '%Y-%m-%d') + interval '7' day,
       '%Y-%m-%d')
ORDER BY f.fomc_date DESC
LIMIT 50;
```

## 11. Knowledge graph cells with N > 50 ranked by posterior

The Bayesian posterior is the load-bearing number the AI Brain reads.
This query surfaces the cells with enough sample size to be trustworthy
and ranks them so the operator can spot the most-confident (regime,
pattern) pairs in seconds.

```sql
SELECT pattern, regime, vol_state, time_bucket,
       n_observations, posterior_win_rate, shrinkage_alpha
FROM "tradingbot_lake"."knowledge_graph"
WHERE n_observations > 50
ORDER BY posterior_win_rate DESC
LIMIT 50;
```
