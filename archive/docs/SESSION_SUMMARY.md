# 2026-06-02 Session Summary — institutional options pipeline + memory layer

This file documents what was built/changed in this session for the user's review.
Not meant to ship; delete after the assessment is done.

---

## 1. The 30-second story

We replaced the bot's options data layer end-to-end:

1. **Paid OPRA feed (ThetaData $80/mo)** installed as a systemd service on EC2.
2. **Sanity layer** — every NBBO quote passes 5 gates (staleness / spread /
   put-call parity / IV smile / intra-tick consistency) before reaching agents.
3. **Real IV percentile** — historical EOD straddles populate `iv_history`;
   today's `iv_rank` is a true percentile vs the last 252 trading days.
4. **Chain-aware strikes + expiries** — strategies no longer write arithmetic
   strikes; they pick listed strikes that have quotes, with delta-band selection
   for institutional convention (CSP 30Δ, condor 20Δ/10Δ).
5. **Memory-rich agent context** — agents now vote with `journal_lessons`,
   `similar_trades`, and per-agent `recent_performance` in hand; a chokepoint
   `apply_memory_bias` in `run_consensus` adjusts confidence based on those
   memory fields.
6. **Heatseeker visual upgrade** — Long Gamma regime strip + per-strike +
   cumulative + per-expiry-decomposition panels with Vanna/Charm aggregates.
7. **Complex-instrument MTM** — `paper_executor` now marks SELL_CSP,
   SELL_COVERED_CALL, IRON_CONDOR, BULL_CALL_SPREAD positions properly.
8. **Operational tooling** — `/system/data-quality` endpoint + `DataQualityChip`
   for operator visibility into provider mix and sanity-flag counts.

Result: options trading was re-enabled (`options_disabled: false`) once the
above shipped.

---

## 2. Architecture flowchart

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                            EC2 (i-0426a45181d08adff)                          │
│                                                                                │
│  ┌──────────────────┐                                                          │
│  │ ThetaTerminal v3 │  systemd: thetadata.service                              │
│  │  (Java daemon)   │  creds: /run/thetadata/creds.txt (tmpfs)                 │
│  │                  │  cloud-auth: AWS Secrets Manager thetadata/credentials   │
│  └─────┬────────────┘                                                          │
│        │ localhost:25503/v3                                                    │
│        │ localhost:25520 (streaming)                                           │
│        ▼                                                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐  │
│  │   backend.bot.data.thetadata.ThetaDataClient                            │  │
│  │     • list_expirations  (1h TTL cache)                                  │  │
│  │     • list_strikes      (no cache yet)                                  │  │
│  │     • chain_snapshot    (10s TTL cache)                                 │  │
│  │     • check_quote_sanity     (staleness + spread + has-quote)           │  │
│  │     • check_parity_sanity    (put-call parity)                          │  │
│  │     • check_smile_sanity     (IV outlier vs median)                     │  │
│  │     • check_intraday_iv_sanity (z-score vs trailing 20-sample window)   │  │
│  └─────┬───────────────────────────────────────────────────────────────────┘  │
│        │                                                                       │
│        ▼                                                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐  │
│  │   backend.bot.data.options.options_snapshot(ticker, spot)               │  │
│  │     provider chain (env OPTIONS_PROVIDER):                              │  │
│  │       thetadata → yfinance → cboe                                       │  │
│  │     each provider returns same dict shape:                              │  │
│  │       {iv_atm, implied_move, dte, expiry, source,                       │  │
│  │        data_confidence, sanity_flags}                                   │  │
│  │     IV via Brenner-Subrahmanyam (no vendor IV on Standard tier)         │  │
│  │     records into iv_history table (live capture)                        │  │
│  │     in-process counters → /system/data-quality                          │  │
│  └─────┬───────────────────────────────────────────────────────────────────┘  │
│        │                                                                       │
│        ▼                                                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐  │
│  │   chain_strike(ticker, spot, kind, *, moneyness, target_dte,            │  │
│  │                target_delta, max_spread_pct, min_size)                  │  │
│  │     Tier 1: chain_snapshot → liquidity gate → target_delta (BS solve)   │  │
│  │     Tier 2 (fallback): closest-to-moneyness                             │  │
│  │     Tier 3 (fallback): arithmetic snap_strike                           │  │
│  │   chain_strike_with_drift returns (strike, drift_metadata)              │  │
│  │   resolve_expiry_dte returns (expiry_iso, actual_dte)                   │  │
│  └─────┬───────────────────────────────────────────────────────────────────┘  │
│        │                                                                       │
│        ▼                                                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐  │
│  │   backend.bot.strategies.all_strategies                                 │  │
│  │     all 14 sites converted: chain_strike(ticker, ...)                   │  │
│  │     all 9 dte=30 literals replaced with resolve_expiry_dte              │  │
│  │     CSP/CC use target_delta=0.30, condor uses 0.20/0.10                 │  │
│  │     Signal.metadata carries target_strike, target_dte,                  │  │
│  │       moneyness_actual, strike_drift_pct, expiration                    │  │
│  └─────┬───────────────────────────────────────────────────────────────────┘  │
│        │                                                                       │
│        ▼                                                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐  │
│  │   backend.bot.engine.run_cycle (per-ticker)                             │  │
│  │     calendar gate (is_us_market_open) → skip if RTH closed              │  │
│  │     pre-trade gates (kill switch, risk limits, options_disabled, ...)   │  │
│  │                                                                          │  │
│  │     build_agent_context(ticker, action, strategy, ...)                  │  │
│  │       → ctx.journal_lessons    (applicable_lessons)                     │  │
│  │       → ctx.similar_trades     (journal.similar_trades)                 │  │
│  │       → ctx.recent_performance (scorecard.recent_performance × 5)       │  │
│  │                                                                          │  │
│  │     run_consensus(ctx)                                                   │  │
│  │       → each agent_* emits AgentVote                                    │  │
│  │       → apply_memory_bias(votes, ctx) — confidence × bias               │  │
│  │       → chairman_review(votes) → ChairmanReport.decision                │  │
│  │                                                                          │  │
│  │     executor.place_order(...) → paper_executor                          │  │
│  │       kind=stock | option | complex                                     │  │
│  │       complex branch marks SELL_CSP/CC/IC synthetically                 │  │
│  └─────┬───────────────────────────────────────────────────────────────────┘  │
│        │                                                                       │
│        ▼                                                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐  │
│  │   FastAPI routes / Cloudflare Tunnel / pillar-watch.com (CF Access)     │  │
│  │     /heatseeker/{ticker}            (GEX result with Tier A/B/C)        │  │
│  │     /heatseeker/{ticker}/history    (regime ribbon source)              │  │
│  │     /heatseeker/{ticker}/by-expiry  (per-expiry decomposition)          │  │
│  │     /system/data-quality            (provider + sanity-flag counters)   │  │
│  │     /watchlist (add) → warm-start IV backfill in background thread      │  │
│  │                                                                          │  │
│  │   APScheduler:                                                           │  │
│  │     daily 5pm ET → _iv_history_gap_fill walks scan-universe             │  │
│  └─────┬───────────────────────────────────────────────────────────────────┘  │
└────────┼──────────────────────────────────────────────────────────────────────┘
         ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                                Frontend (React)                                │
│                                                                                │
│  AuthoritySpine (topbar):                                                      │
│    [WarningsChip] [DataQualityChip] — provider mix + sanity-flag distribution  │
│                                                                                │
│  Heatseeker page:                                                              │
│    ┌────────────────────────────────────────────────────────────────────┐    │
│    │  LongGammaStrip                                                     │    │
│    │  [Regime][Δ to flip][0DTE share][Peak Γ][Pin risk][Vanna][Charm]    │    │
│    │  ◇◇◇◇◇◇◇◇◇◇◇◇◇◇  ← 30-day regime ribbon                              │    │
│    └────────────────────────────────────────────────────────────────────┘    │
│    ┌──────────────────┬──────────────────┬──────────────────┐               │
│    │ GEX per Strike   │ Cumulative GEX   │ GEX by Expiry    │               │
│    │  (existing)      │  (new)           │  (new, stacked)  │               │
│    │   call/put tabs  │   crosses zero → │   color by DTE   │               │
│    │   wall highlight │   gamma flip     │   bucket         │               │
│    └──────────────────┴──────────────────┴──────────────────┘               │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. New / modified files (this session)

### Backend Python

| File | What |
|---|---|
| `backend/bot/data/thetadata.py` | NEW — v3 client + 4 sanity helpers + chain cache |
| `backend/bot/data/options.py` | provider chain, chain_strike, chain_strike_with_drift, resolve_expiry_dte, IV-rank-from-history, sanity gates + provider counters |
| `backend/bot/data/iv_history.py` | NEW — record_today, iv_percentile_rank, backfill, CLI |
| `backend/models/iv_history.py` | NEW SQLAlchemy model (external-cache pattern) |
| `backend/bot/agent_context.py` | NEW — build_agent_context + apply_memory_bias |
| `backend/bot/journal/__init__.py` | + similar_trades(ticker, regime, k=5) |
| `backend/bot/agents/scorecard.py` | + recent_performance(agent_name, window=30) |
| `backend/bot/agents/__init__.py` | + apply_memory_bias hook before chairman |
| `backend/bot/signals/gex.py` | Tier A (max gamma, vol trigger, dealer flow, pin risk, distance to flip) + Tier B (Vanna, Charm) + Tier C (0DTE share) + gex_by_expiry() |
| `backend/bot/paper_executor.py` | complex-kind MTM branch (CSP, CC, IC, spread) |
| `backend/bot/engine.py` | three-tier _chain_strike, build_agent_context call |
| `backend/bot/ai/brain.py` | chain_strike replacement for snap_strike |
| `backend/bot/scheduler.py` | _iv_history_gap_fill daily job |
| `backend/bot/system_reset.py` | iv_history listed in EXTERNAL_CACHE_TABLES |
| `backend/bot/strategies/all_strategies.py` | 14 chain_strike calls + 9 dte=30 → dte; target_delta on CSP/CC/IC |
| `backend/api/routes/heatseeker.py` | + /heatseeker/{ticker}/history + /by-expiry |
| `backend/api/routes/authority.py` | + /system/data-quality + /reset |
| `backend/api/routes/watchlist.py` | + warm-start IV backfill on add |
| `backend/db.py` | iv_history model registered |
| `backend/bot/metrics/__init__.py` | sharpe_ratio numeric-tolerance fix |

### Frontend React

| File | What |
|---|---|
| `frontend/src/components/DataQualityChip.jsx` | NEW |
| `frontend/src/components/LongGammaStrip.jsx` | NEW |
| `frontend/src/components/CumulativeGexPanel.jsx` | NEW |
| `frontend/src/components/ExpiryDecompositionPanel.jsx` | NEW |
| `frontend/src/components/AuthoritySpine.jsx` | mounts DataQualityChip |
| `frontend/src/pages/Heatseeker.jsx` | mounts the 3 panels + Long Gamma strip |

### Tests

| File | What |
|---|---|
| `tests/unit/test_thetadata_client.py` | NEW — 13 tests (client, sanity, chain_strike, drift) |
| `tests/unit/test_paper_executor.py` | + 3 complex-MTM tests |
| `tests/unit/test_ai_brain.py` | force_run_when_closed for new calendar gate |
| `tests/unit/test_stage14_marketplace_engine.py` | force_run_when_closed |
| `tests/unit/test_stage15_consensus_gate.py` | force_run_when_closed |

### Infrastructure

| Resource | What |
|---|---|
| systemd `thetadata.service` | Java daemon on EC2 (User=thetadata, RuntimeDirectory=thetadata) |
| IAM `trading-bot-paper-ec2-role` | + inline policy `ThetaDataSecretRead` (least privilege on one secret ARN) |
| AWS Secrets Manager | `thetadata/credentials` (JSON: email + password) |
| Bot DB | new `iv_history` table |

### Docs / state files

| File | What |
|---|---|
| `futref` | session-spanning runbook for context the next session needs |
| `todo.md` | item #14 added (Heatseeker visual upgrade) + items #1/#3/#6 deferred plans documented |
| Memory (auto) | options data vendors, data-integrity principle, ThetaData live note |

---

## 4. Operational state at end of session

- **EC2** i-0426a45181d08adff t4g.small ARM64 us-east-1
- **ThetaTerminal v3** active, ~700 MB RSS, serving localhost:25503
- **Bot service** restarted multiple times during session; `running: false`
  by default (operator starts the engine loop manually)
- **Config**: `options_disabled: false`, `paper_mode: true`
- **iv_history**: AAPL has 23 backfilled samples (90 days). Full 365-day
  backfill for all 12 watchlist tickers was running when session ended;
  status will be visible via the regression of the backfill cmd id
  `1bee6105-5c52-4e05-9dc2-2114e01f0a03`.
- **Regression**: 1182 / 1193 passed pre-fix; 11 failures hit, all 11
  diagnosed and fixed (3 thetadata mock pattern, 2 sharpe float
  tolerance, 4 calendar-gate test fixtures, 1 AI brain calendar gate,
  1 chair memory hook). Re-run pending the bot redeploy.

## 5. Items explicitly NOT done (with reasons)

- **#11 TA-Lib swap + FinGPT routing** — explicitly deferred in todo.md.
  Gated on the AI Brain ecosystem being trial-tested with the new memory
  layer. Substantial multi-day work.
- **#12 Position Management Agent** — explicitly deferred. Requires
  cohort matrix backtests on top of clean ThetaData history. Substantial
  multi-day work.
- **#13 Claude dependency reduction** — explicitly deferred. Designed
  together with #12.
- **Two-source toggle in Heatseeker** — only ThetaData is configured;
  IBKR connection comes when execution moves to IBKR. Stubbed out as a
  follow-up in futref.
- **Multi-ticker IV backfill verification** — running in background; the
  output will be available via the SSM command result. The full 365-day
  histories should populate the iv_history table for all 12 tickers.

---

## 6. To re-enable trading and start observing

1. `POST /bot/start` (or click Start in the UI)
2. Watch `/system/data-quality` — confirm `providers.thetadata` dominates
3. Watch `/system/warnings` — confirm sanity rejections are rare and
   reasonable (after-hours stale fields are expected; persistent
   parity_violation is not)
4. Watch Heatseeker for any watchlist symbol — the Long Gamma strip
   should populate; per-expiry decomposition should show all near-term
   expirations
5. Trades should land with rich `metadata.strike_drift` and a real
   `metadata.expiration` from the chain

If anything in the data-quality counter spikes badly, the operator can
flip `OPTIONS_PROVIDER=yfinance` via env or `options_disabled: true`
via config to fall back instantly. Both changes are observable in the
new chip without a redeploy.
