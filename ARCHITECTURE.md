# Trading Bot — Architecture

_Last fully rewritten 2026-06-06 at the close of MITS Phase 6 (recursive learning loop)._

This document is the single authoritative description of the system as it stands. It is the operator's mental model and the next developer's onboarding doc. Where details would balloon (every TUNABLE, every detector), pointers are given to the source of truth instead.

---

## 1. One-paragraph thesis

The bot is a fully-autonomous, plain-English, options-and-equities paper-trading system that learns from itself. A nightly **End-of-Day (EOD) pass** scores tomorrow's setups by replaying every detector against a multi-year corpus of historical observations, weighing each setup by Bayesian-shrunk win rate. During the trading session, the **engine** uses that EOD bias to gate live trades, size positions by conviction, and a 7-agent **council** debates each trade. Every closed trade becomes a high-weight observation back in the corpus (the **recursive loop**), so the posterior shifts toward what's actually working in live trading — not just the historical replay. The operator sees this via a single **$5k Trial Scorecard** page that proves the bot works (or doesn't), plus a **Sunday weekly retrospective** and a **detector scorecard** that surfaces self-disabling suggestions. Everything is config-driven, audit-logged, and runs on a single AWS EC2 instance with ThetaData for real options pricing and Alpaca paper for execution.

---

## 2. The corpus → trade → reconcile loop

```
                  ┌────────────────────────────────────────────────────────┐
                  │            HISTORICAL CORPUS (years of bars)           │
                  └──┬─────────────────────────────────────────────────────┘
                     │ Detectors (40+)
                     ▼
              market_observations (raw pattern fires)
                     │ Outcome Linker (forward returns)
                     ▼
              market_outcomes (won?, return_pct, horizon)
                     │ Knowledge Aggregator (Bayesian shrinkage)
                     ▼
              knowledge_graph_cells  ←──────┐
              (per-cohort posteriors)        │ live-weight x5
                     │ EOD Pass (16:30 ET)   │
                     ▼                       │
              eod_analysis                   │
              (tomorrow's setups)            │
                     │ Engine cycle (EOD bias gate)
                     ▼                       │
                  Live trade                 │
                     │ Close                 │
                     ▼                       │
                  Trade row                  │
                     │ live_outcome_ingest (23:40 ET nightly)
                     │ → market_observation(source='live_trade')
                     │ → market_outcome
                     └───────────────────────┘   (the loop closes here)
```

Eight scheduled jobs drive this loop. See §10 for the full cron table.

---

## 3. Data layer

Nine integrated sources. Each lives under `backend/bot/data/` or is wired via `backend/bot/{breadth,flowintel,iv_regime,…}`.

| Source | Purpose | Where it lives |
| --- | --- | --- |
| **ThetaData (Standard tier)** | Real options chain pricing, intraday IV, historical greeks. EC2-resident terminal on :25503. | `backend/bot/data/thetadata_client.py` |
| **Alpaca paper** | Stock quotes (provider #2 + fallback) + paper execution. Account `PA32CBIKZPK2`. | `backend/bot/alpaca_executor.py` |
| **yfinance** | Daily bar fallback, equity history. Fragile but still the cheapest catch-all. | `backend/bot/data/bars_fetcher.py` |
| **FRED** | Macro panel (CPI, NFP, FOMC dates). | `backend/bot/data/fred.py` |
| **SEC EDGAR** | 8-K, 10-Q filings (earnings call intel). | `backend/bot/data/edgar.py` |
| **FINRA** | Daily short volume per ticker. | `backend/bot/data/finra.py` |
| **CFTC COT** | Weekly Commitment of Traders. | `backend/bot/data/cot.py` |
| **Market breadth** | Advance/decline + new highs/lows. | `backend/bot/breadth/` |
| **Cboe** | VIX term structure, GEX scalar inputs. | `backend/bot/iv_regime/`, `backend/bot/signals/gex.py` |

### Integrity layer

`backend/bot/data_quality/` validates every pricing read with put-call parity, IV-smile monotonicity, and self-regression intra-tick consistency checks before the data flows into signals. The **data-blame principle** (memory entry): vendors must be clean enough that losses are attributable to agent logic, not "the feed was bad." Pricing source provenance is stamped on every `Trade.pricing_source` so post-hoc audits can isolate which vendor priced what.

---

## 4. Detection layer

**40+ detectors** across 9 families, all registered in `backend/bot/detectors/__init__.py`.

| Family | Examples | Count |
| --- | --- | --- |
| candlesticks | hammer, doji, engulfing | 8 |
| price_action | bull_flag, bear_flag, breakout, breakdown | 6 |
| market_structure | hh_hl, lh_ll, swing_failure | 5 |
| liquidity | sweep, stop_run, fvg | 4 |
| vwap | vwap_reclaim, vwap_loss | 3 |
| volume_profile | poc_test, value_area_break | 3 |
| options_intel | gamma_pin, vanna_unwind | 3 |
| talib | macd_cross, rsi_extreme | 6 |
| **flow_intel** | sweep, block, darkpool | 3 |

Each detector inherits from `Detector` (`backend/bot/detectors/base.py`) and emits a `MarketObservation` row when its pattern fires. The detector's `default_params()` are overridable per-detector via the **detector control plane** (`/detectors` route + `DetectorSettings.jsx`).

### Knowledge graph + Bayesian shrinkage + walk-forward splits

The corpus aggregator (`backend/bot/corpus/knowledge_aggregator.py`) folds every (observation, outcome) tuple into a `knowledge_graph_cell` keyed by **(ticker, pattern, regime, vol_state, time_bucket, horizon, sample_split)**. The `sample_split` axis is one of:

- `in_sample` — historical replay observations only.
- `out_of_sample` — live trading observations (engine + live_trade).
- `combined` — every observation regardless of provenance, with **live observations weighted by `TUNABLES.live_outcome_weight_multiplier` (default 5x)** in the Beta-Binomial posterior. When live N ≥ `live_n_authoritative_floor` (default 30), the combined cell's primary posterior comes from live observations only; historical becomes a "secondary" reference shown in the UI.

Each cell carries:

- `sample_size`, `win_rate`, `posterior_win_rate` (Beta-Binomial shrinkage with the matching `pattern_prior` row).
- `avg_return_pct`, `avg_hold_minutes`.
- `confidence_lower` / `confidence_upper` (Wilson 95% CI).

A nightly snapshot at 23:50 ET captures every cell into `knowledge_graph_history` so the UI sparkline can render a true posterior-over-time line.

---

## 5. EOD analysis layer

**16:30 ET weekdays** (`scheduler._eod_analysis_pass`). The pass:

1. Walks the watchlist + benchmark ETFs.
2. Runs every enabled detector against today's bars (with ThetaData bar fallback per P4.3).
3. For each ticker, queries the knowledge graph for the top-N posteriors.
4. Computes `rank_score = posterior * log(1 + sample_size)`.
5. Calls Claude (when key present) for a one-paragraph thesis + suggested action JSON.
6. UPSERTs an `eod_analysis` row per (ticker, analysis_date).

The **/tomorrow** page renders the top-K rows for the operator's morning briefing.

**Sunday 10:00 ET + Monday 06:00 ET catch-up** (`_eod_catchup_pass`) handles weekends and post-holiday Mondays so the EOD pass never silently misses a day.

A **17:00 ET reconcile** (`_eod_prediction_reconcile`) walks the day's `eod_analysis` rows and writes `EodPredictionOutcome` rows tagged `traded_matched` / `traded_diverged` / `not_traded` / `pending`. This is how we know whether our predictions are converting to trades and whether those trades are winning.

---

## 6. Trading layer

`backend/bot/engine.py` runs the live cycle. The cycle, in order:

1. **Load EOD bias** (`backend/bot/eod_bias.py`) — pull today's `eod_analysis` rows, promote any high-conviction tickers into the priority candidate list.
2. **Build agent context** (`backend/bot/agent_context.py`) — knowledge-graph evidence, memory bias, gamma features, flow intel, narrative.
3. **Detect** — run every enabled detector on the cycle's bar window, persist live observations.
4. **Brain** (Claude-driven, `backend/bot/ai/brain.py`) — composite signal + confidence + suggested order.
5. **7-agent council** (`backend/bot/agents/`) — chairman + 6 specialists vote; consensus or chairman authority gate decides.
6. **Conviction sizing** — when `signal_source == eod_bias`, multiplier from rank: `rank_1` × 1.5, `rank_2_3` × 1.0, `rank_4_plus` × 0.5.
7. **Catalyst gate** — earnings within `catalyst_earnings_window_days` (default 5d) halves size; short-DTE options ≤ `catalyst_short_dte_threshold` (default 7d) into earnings abstain entirely. Same logic for FOMC.
8. **Thesis-health exit monitor** (MITS-5) — the 7th agent consults the winner trajectory profile and votes EXIT when the open position's trajectory no longer matches historical winners.
9. **Executor** (`PaperExecutor` / `AlpacaExecutor`) — places fills, stamps `pricing_source`, records to `trades` + `paper_positions`.
10. **Audit invariants** — `audit.py` enforces account + trade write contracts; never inject synthetic data into the live paper DB.

---

## 7. Outcome layer

`EodPredictionOutcome` is the prediction → trade → realized loop's outcome ledger. It survives `fresh_start` so prediction accuracy is auditable across paper-trial resets.

The **/trade-loop** page renders:

- Predictions made today.
- Which converted to trades, which were skipped (with the skip reason).
- Same-day outcomes once the reconcile fires.
- Multi-week prediction accuracy rollup.

---

## 8. Self-improvement layer (MITS Phase 6 — the recursive close)

Four shipped subsystems make the bot learn from itself:

### 8.1 Live outcome ingest (`backend/bot/corpus/live_outcome_ingest.py`)

Each closed `Trade` becomes a `MarketObservation` + `MarketOutcome` pair tagged `source='live_trade'`. The pattern is derived from `Trade.detail_json.eod_bias.top_pattern` (or strategy / signal_source fallbacks). Idempotent via the `IngestWatermark` row. Runs nightly at **23:40 ET**.

Live observations carry `live_outcome_weight_multiplier` × the weight of historical observations in the Beta-Binomial update, and at N ≥ `live_n_authoritative_floor` the combined cell becomes live-only. This is how the posterior shifts toward what's actually working in live trading.

### 8.2 Detector scorecard (`backend/bot/scorecard/detector_scorecard.py`)

Per-detector aggregate from closed Trade rows whose `eod_bias.top_pattern == name` (or whose detail_json.pattern matches): total trades, win rate, realized P&L, average hold, exponentially-decayed `attribution_score` (`detector_attribution_decay_half_life_days` default 14d).

Routes: `GET /detectors/{name}/scorecard?window=7|30|all` and `GET /detectors/scorecard` for the leaderboard. Surfaced in `DetectorSettings.jsx` as a 3-stat strip next to every enable toggle.

### 8.3 Self-disabling detector suggestions (`backend/bot/scorecard/suggestions.py`)

**23:55 ET nightly**. For each detector:

- Currently-enabled + out-of-sample posterior < 0.45 + N > 100 → `DetectorSuggestion(reason='low_posterior')`.
- Currently-disabled + recent live posterior > 0.60 + N ≥ 30 → `DetectorSuggestion(reason='recovered_posterior')`.

The operator sees a top banner in DetectorSettings with one-click accept/dismiss. Dismissed low_posterior suggestions enter a `detector_suggestion_cooldown_days` (default 14d) cooldown.

### 8.4 Sunday weekly retrospective (`backend/bot/retrospective.py`)

**Sunday 11:00 ET**. Walks the prior Mon-Fri trade history + EodPredictionOutcomes + catalyst gate skips, assembles a structured recap into `WeeklyRetrospective`:

- Realized P&L, win rate, average hold, trade count.
- Top winning + losing tickers, patterns, and detector families.
- Catalyst-gate saves count + estimated dollars avoided.
- Conviction-multiplier P&L effect (rank_1 / rank_2_3 / rank_4_plus split).
- Claude-composed summary paragraph (cached, with deterministic fallback when no key).

Surfaced via `/retrospective` route + `Retrospective.jsx` page (with prior-12-weeks selector).

### 8.5 $5k Trial Scorecard (`backend/api/routes/trial_scorecard.py`)

THE single page the operator can point at to prove the bot works. `GET /trial-scorecard` returns:

- Starting / current equity, total return %.
- Trading days elapsed / total, trial start/end dates.
- Weekly predicted-vs-realized P&L bars.
- High-conviction setups: total / taken / won, hit rate.
- Max drawdown (%, $), Sharpe estimate (annualized daily-return basis).
- Projection: `on_track` | `off_track` | `breached` (vs `trial_target_growth_pct` curve and `trial_breach_equity_floor_pct` floor).
- Narrative paragraph (Claude when key present, deterministic fallback).

Surfaced as the **Trial** nav entry between Today and Tomorrow.

---

## 9. UI surfaces

| Path | Page | Purpose |
| --- | --- | --- |
| `/` | Today.jsx | Live cockpit: open positions, today's P&L, plain-English activity feed, EvidencePanel for every position. |
| `/trial-scorecard` | TrialScorecard.jsx | The $5k 30-day trial proof page (P6.5). |
| `/tomorrow` | Tomorrow.jsx | Tomorrow's Setup digest (top-ranked EOD analyses). |
| `/trade-loop` | TradeLoop.jsx | Prediction → trade → outcome ledger. |
| `/analysis` | StockAnalysis.jsx | Per-stock annotated chart + corpus thesis. |
| `/trades` | TradesV2.jsx | Trade history with autopsy memos. |
| `/intel` | Intel.jsx | Tabbed view: GEX, flow, earnings, sources, AI, markets. |
| `/knowledge` | KnowledgeGraph.jsx | Cohort matrix + posterior sparklines. |
| `/retrospective` | Retrospective.jsx | Weekly retrospective + family attribution (P6.4). |
| `/council` | Council.jsx | 7-agent panel + chairman + dissent surface. |
| `/lab` | Lab.jsx | Strategy compare + backtest. |
| `/settings` | SettingsHub.jsx | Watchlist, detectors, risk, alerts, AI keys. Detector scorecard strip + suggestion banner live under "detectors". |

The `Layout.jsx` topbar carries the equity readout, snapshot quality chip, engine heartbeat badge, and chat copilot widget.

---

## 10. Scheduler jobs

All times America/New_York. See `backend/bot/scheduler.py`.

| Job | Cron | Purpose |
| --- | --- | --- |
| `_pre_market` | Mon-Fri 08:30 | Pre-open scan. |
| `_intraday` | Mon-Fri 09:00-15:55 / 5min | Live trading cycle. |
| `_swing_check` | Mon-Fri 09:35 | Swing-trade re-evaluate. |
| `_post_market` | Mon-Fri 16:15 | Daily counters reset. |
| `_gex_history` | Mon-Fri 09:00-16:00 / 15min | GEX regime snapshots. |
| `_regime_snapshot` | Mon-Fri 09:00-15:55 / 15min | System regime fingerprint. |
| `_regime_backfill` | Mon-Fri 18:00 | Forward-outcome backfill for regime similarity. |
| `_research_digest` | Mon-Fri 17:30 | Research finding alerts. |
| `_fred_refresh` | Mon-Fri 07:00 | Macro panel pull. |
| `_breadth_refresh` | Mon-Fri 16:45 | EOD breadth snapshot. |
| `_edgar_refresh` | Hourly + 10min | SEC filings. |
| `_finra_refresh` | Tue-Sat 04:00 | Short volume. |
| `_cot_refresh` | Sat 06:00 | Weekly COT. |
| `_iv_history_gap_fill` | Mon-Fri 17:00 | Iv_history gap-filler. |
| `_eod_analysis_pass` | Mon-Fri 16:30 | EOD pattern analysis → /tomorrow. |
| `_eod_catchup_pass` | Sun 10:00 + Mon 06:00 | Catch-up for missed EOD passes. |
| `_eod_prediction_reconcile` | Mon-Fri 17:00 | Prediction → outcome reconcile. |
| `_telegram_eod_digest` | Mon-Fri 16:30 | Operator-facing daily digest. |
| `_telegram_tomorrow_setup` | Mon-Fri 16:35 | Tomorrow's Setup push. |
| `_nightly_outcome_link` | Mon-Fri 19:00 | Corpus outcome linker. |
| `_nightly_recompute_cells` | Mon-Fri 19:30 | Knowledge graph recompute. |
| `_ingest_live_outcomes` | Mon-Fri,Sun 23:40 | **(P6.1)** Convert closed trades → corpus observations. |
| `_nightly_snapshot_cells` | Mon-Fri,Sun 23:50 | Snapshot graph to history (sparkline source). |
| `_detector_suggestions_pass` | Mon-Fri,Sun 23:55 | **(P6.3)** Disable/re-enable suggestions. |
| `_weekly_retrospective_pass` | Sun 11:00 | **(P6.4)** Build the weekly recap. |
| `_weekly_full_replay` | Sat 06:00 | Weekend corpus refresh. |
| `_telegram_drain_queue` | every 60s | Notifier retry queue. |

---

## 11. TUNABLES — pointer

Single source of truth: `backend/config.py:Tunables`. Categories:

- Market-data fallbacks (default IV, range, VIX).
- Cache TTLs.
- Data cross-validation tolerances.
- Backtest assumptions.
- IV rank → percentile mapping.
- Stage 9 abstain + cohort rules.
- Portfolio optimizer (Kelly fraction, vol targets, drawdown cuts).
- Execution realism (spread, slippage, fill share caps).
- Options strike intervals + auto-close DTE.
- Crypto profile.
- Heatseeker / Flowseeker windows + multipliers.
- IBKR commission baseline.
- Engine cycle timeout + autostart.
- EMA50 strategy cooldown.
- Option adaptive exit thresholds.
- Analytical layer regime + grade cutoffs.
- AI Brain + chat + meta + memo + narrative model + max-token settings.
- Stage 18a free data sources.
- Chairman authority flag.
- Notifier quiet hours + rate limits.
- Memory bias scale + bounds.
- Knowledge sparkline density.
- Thesis-health exit.
- Master agent contract (min confidence, agent quorum).
- **MITS Phase 5** — EOD high-conviction floors, conviction-size multipliers, catalyst gate windows, flow-intel conviction thresholds.
- **MITS Phase 6** — `live_outcome_weight_multiplier`, `live_n_authoritative_floor`, `detector_attribution_decay_half_life_days`, `detector_suggest_disable_posterior`, `detector_suggest_disable_min_n`, `detector_suggest_reenable_posterior`, `detector_suggest_reenable_min_n`, `detector_suggestion_cooldown_days`, `trial_starting_equity`, `trial_start_date`, `trial_duration_days`, `trial_target_growth_pct`, `trial_breach_equity_floor_pct`, `weekly_retrospective_top_n`.

Every TUNABLE is env-overridable via the matching `TB_*` env var. UI-saved overrides persist into the `bot_config` SQLite table and merge on top.

---

## 12. Deploy + observability

### Substrate

- **EC2**: `i-0426a45181d08adff` (t4g.small, us-east-1).
- **EIP**: 32.197.70.83 (SSM-only — no SSH).
- **Service**: `systemctl status trading-bot`.
- **Working dir**: `/opt/trading-bot`.
- **SQLite**: `/opt/trading-bot/trading_bot.db`.
- **Frontend dist**: `/opt/trading-bot/frontend/dist` (served by FastAPI at `/`).
- **User**: `tradingbot:tradingbot`.
- **ThetaTerminal**: own systemd unit (`thetaterminal.service`), listens on :25503.

### Deploy flow (operator-side)

1. Local: `cd frontend && npm run build` (EC2 has no node).
2. Local: `tar --exclude=node_modules --exclude=__pycache__ -czf trading-bot.tar.gz backend frontend/dist requirements.txt`.
3. SSM: upload + extract to `/opt/trading-bot/`, `pip install -r requirements.txt`, `systemctl restart trading-bot`.
4. Verify: `curl localhost:8000/trial-scorecard | jq .` from EC2; smoke pages from your laptop via SSM port-forward `aws ssm start-session --target i-0426a45181d08adff --document AWS-StartPortForwardingSession --parameters portNumber=8000,localPortNumber=8000`.

### Observability

- `journalctl -u trading-bot -f` for live log tail.
- `journalctl -u trading-bot -e --since "today" | grep -iE "live_outcome|retrospective|suggestion"` for Phase 6 jobs.
- `journalctl -u thetaterminal -e --since "today"` for the data terminal.
- The **engine heartbeat badge** in the topbar surfaces stalled-cycle bugs the systemd unit can't catch.
- The **snapshot quality chip** surfaces accounting_version + data_quality of the latest equity snapshot.
- The **warnings log** ring buffer is surfaced via `/diagnostics/warnings` for the UI to consume.

---

## 13. What's intentionally NOT in scope

- **Real-money brokerage routing.** The Alpaca live path exists but is gated behind explicit operator action and a $5k paper trial gate. Phase 6 closes the learning loop; the next gate is live-money promotion, which sits outside this codebase's responsibility.
- **Multi-account / multi-strategy fund management.** One paper account, one bot.
- **Order-book / Level-2 data.** ThetaData Standard tier does not provide it; the bot is calibrated for retail-quality fills.
- **Crypto perpetual / margin.** Crypto data is loaded for SPX correlation analysis only; we don't trade it.
- **Real-time news classification.** EDGAR + FRED + EODHD style scheduled refreshes only; no firehose subscription.

---

_End of architecture document._
