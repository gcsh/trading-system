# MITS Phase 11 — Corpus Depth + Breadth (the foundational rebuild)

**Created:** 2026-06-09
**Operator approval:** 2026-06-09 (40 tickers, free-tier news + transcripts, 5y news history, no pause during backfill, 100 watched funds for 13F, embed every paragraph, CLEAN corpus rebuild)
**Status:** in flight

## Operator-locked decisions

1. Universe: 40 tickers per `universe.json` (megacap tech + quality + financials + healthcare + consumer + industrial + comms + semis + growth + index/sector ETFs)
2. Budget: pick FREE tier alternatives where possible (Finnhub News Free, AlphaVantage Earnings Free)
3. News history: 5 years
4. Earnings transcripts: 5 years (~800 calls × 40 tickers)
5. Backfill cadence: 5-day background run, trading continues
6. 13F scope: 100 watched funds
7. Vector embedding: paragraph-level granularity for all text
8. Corpus disposition: clean rebuild (drop existing `market_observations`, `market_outcomes`, `knowledge_graph`)

## Budget alternatives chosen

| Source | Original | Alternative | Cost |
|---|---|---|---|
| Stock bars + options + IV | yfinance + ThetaData $40 | ThetaData Stocks + Options ($100 already paid) | $0 delta |
| News | Finnhub Pro $79/mo | **Finnhub Free** (60 req/min, 5y history) | $0 |
| Earnings transcripts | AlphaVantage Premium $50/mo | **AlphaVantage Free** (25 req/day → 32-day backfill) | $0 |
| Macro | FRED (free) | FRED (free, expand to 50 series) | $0 |
| Insider Form 4 + 13F | EDGAR (free) | EDGAR (free, build parser) | $0 |

**Total new monthly recurring: $0.** (Only existing $100/mo ThetaData.)

## Sub-phases

| # | Name | Effort | Runtime |
|---|---|---|---|
| 11.A | Universe + ticker config | 1 day | — |
| 11.G | Watermark + sync orchestrator | 2 days | — |
| 11.B.1 | ThetaData stock bars 20y daily + 5y intraday | 1 day code | 5 days background |
| 11.B.2 | ThetaData options EOD chains 5y | 1 day code | 3-5 days background |
| 11.B.3 | ThetaData IV history 5y | 0.5 days | 0.5 days |
| 11.C | Finnhub news 5y backfill | 2 days | 1 day |
| 11.D | AlphaVantage earnings transcripts 5y | 2 days | 32 days (rate-limited) |
| 11.E | Form 4 (insider) + 13F (100 funds) parsers | 3 days | 1 day backfill |
| 11.F | FRED macro expansion (~50 series) | 1 day | 1 day |
| 11.H | Detector replay on full corpus | 1 day code | 2 days runtime |
| 11.I | Per-source health monitoring | 1 day | — |
| 11.J | Cross-vendor parity audit | 1 day | 1 day |
| 11.K | Vector layer rebuild (text embedding) | 1 day | 1 day |

**Total elapsed: 17-21 days** with parallel execution.

## Agent execution plan

| Agent | Phases | Goal |
|---|---|---|
| 1 | 11.A + 11.G + 11.B.1 + 11.B.3 + 11.F | Foundation + sync arch + start stock+IV+macro backfills |
| 2 | 11.C + 11.D + 11.E | Text + alt-data backfills (news, transcripts, insider, 13F) |
| 3 | 11.B.2 | Options EOD chains backfill (the big one) |
| 4 | 11.H + 11.J + 11.K | Detector replay + parity audit + vector rebuild |
| 5 | 11.I + UI surfaces + downstream wire-up | Health monitoring + UI + Brain integration |

## Verification (operator gate skipped — runs after Agent 5)

- `SELECT COUNT(*) FROM market_observations` ≥ 1.5M (target 3M)
- `options_observations` ≥ 500k
- `knowledge_graph` cells ≥ 30k
- `news_articles` ≥ 100k across 40 tickers
- `earnings_transcripts` = 800
- `insider_trades` populated
- `fund_holdings` for 100 funds
- `fred_observations` covering 50 series
- pgvector embeddings ≥ 2M
- Lake Status page shows 9-source health grid green

## Status

- **Agent 1 launching:** 2026-06-09
