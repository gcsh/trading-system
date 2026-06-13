# Stage 1 — KPI and Label Contract

**Status**: Stage 1 core + 1.5 additions shipped 2026-05-29.
**Purpose**: A single human-readable artifact describing exactly what the
trading bot is being measured against. Every later stage is judged against
the metrics, labels, and gates defined here.

This document is the contract between the build team and the eventual canary
process. If a number on the Cockpit doesn't match a definition in this file,
the bug is on the build side.

---

## 1. Business KPI (primary)

The bot's primary success metric is **net P&L per trial day after realistic
costs**, evaluated over a rolling 30-day window. Sub-metrics inform why the
primary is what it is, but only the primary determines whether the system
should be promoted.

| KPI | Definition | Threshold | Where surfaced |
|---|---|---|---|
| **Primary** | Net P&L per trial day | > $0 on rolling 30 days | `/metrics/summary` `total_pnl` / days |
| Annualized Sharpe | `(mean(r) − rf/N) / stdev(r) · √N` over per-period returns | ≥ 1.2 | `/metrics/summary` `sharpe` |
| Sortino | Same as Sharpe but only downside deviation | track only | `/metrics/summary` `sortino` |
| Max drawdown | Peak-to-trough as fraction of running peak | ≤ 15% trailing 90d | `/metrics/summary` `max_drawdown_pct` |
| Profit factor | `Σ wins / |Σ losses|` | ≥ 1.5 | `/metrics/summary` `profit_factor` |
| Win rate | `wins / closed` (zero counts as loss) | ≥ 0.45 | `/metrics/summary` `win_rate` |
| Expectancy | `wr·avg_win + lr·avg_loss` ($ per trade) | > 0 | `/metrics/summary` `expectancy` |
| **Calibration: Brier** | `mean((p − y)²)` over (predicted_prob, win) pairs | ≤ 0.22 | `/metrics/summary` `brier` |
| **Calibration: ECE** | Population-weighted `|predicted − actual|` per probability bin | ≤ 0.05 | `/metrics/summary` `calibration_error` |

Annualization assumes 252 trading days. Risk-free rate defaults to 4.5%
(`TUNABLES.risk_free_rate`).

All numbers return **`None`** when the sample size is too thin to compute
honestly. The Cockpit shows `n/a`, never a fake 0. This is intentional and
non-negotiable.

---

## 2. Label contract (`TradeLabel`, schema v1)

A **label** is the immutable record of one trade's decision context plus its
realized outcome. Models train and evaluation runs against labels. Schema
version is bumped (`LABEL_SCHEMA`) when any field changes shape so older
artifacts can be detected.

```text
TradeLabel
├─ trade_id            int           PK; FK to Trade
├─ timestamp           ISO-8601 str  decision time, NOT close time
├─ ticker              str
├─ strategy            str           e.g. "macd_momentum"
├─ action              str           BUY_STOCK | BUY_CALL | SELL_CSP | ...
├─ instrument          str           stock | option | spread
│
│ ── decision context (what the system knew at decision time) ──
├─ regime_trend        str           bullish | bearish | choppy | unknown
├─ regime_volatility   str           high | normal | low
├─ regime_gamma        str           long_gamma | short_gamma | unknown
├─ grade               str           A+ | A | B | C | (empty)
├─ confidence          float         signal confidence at signal time
├─ win_probability     float | None  predicted win-prob at signal time
│
│ ── realized outcome (filled in only when closed) ──
├─ pnl                 float | None  realized $ P&L
├─ pnl_pct             float | None  pnl / entry_notional
├─ win                 0 | 1 | None  1 if pnl > 0, else 0, None if open
├─ exit_reason         str | None    take_profit | stop_loss | expiry | manual
├─ holding_minutes     int | None
├─ closed_at           str | None
│
└─ schema              int           LABEL_SCHEMA = 1
```

**Lookahead protection**: features and decision-context fields are pulled from
the matching DecisionLog row, which is written at signal time (before the
order fills). No field on the label may be sourced from data that wasn't
available at signal time.

**Entry notional convention** (used to compute `pnl_pct`):
- stock: `quantity × price`
- option/spread: `max($0.05, 0.03 × strike) × 100 × |contracts|` — matches the
  paper executor's premium model.

---

## 3. Label quality flags

The system refuses to silently report metrics on a degenerate dataset. These
warnings appear in `label_quality.warnings` and on the Cockpit MetricsCard:

| Flag | Condition | What it means |
|---|---|---|
| `no closed trades` | `closed == 0` | every metric will be `n/a` |
| `only N closed trades — need ≥30 for stable metrics` | `0 < closed < 30` | numbers can be reported but should not drive promotion decisions |
| `all closed trades won` | `losses == 0` | likely sampling bias or overfitting |
| `all closed trades lost` | `wins == 0` | Brier and calibration are degenerate (`None`) |
| `no predicted probabilities` | `with_prediction == 0` | calibration metrics unavailable |

When `ok=False`, no later stage may use the dataset for promotion.

---

## 4. Walk-forward evaluation

The honest way to estimate out-of-sample edge.

- `walk_forward_split(labels, train_size, test_size)` — fixed-size sliding
  windows, non-overlapping test segments.
- `expanding_split(labels, initial_train, test_size)` — train set grows;
  used when total data is thin.
- `walk_forward_evaluate(...)` — runs the split and returns per-window
  metrics + cross-window summary (mean win rate, mean PF, mean ECE,
  cumulative P&L, stability count).

Labels MUST be sorted by `timestamp` ascending before splitting (the harness
sorts on entry).

---

## 5. Experiment tracking (Stage 1.5)

Every meaningful evaluation run gets persisted as an immutable
`ExperimentRecord`:

| Field | Purpose |
|---|---|
| `name` | e.g. `"walkforward"`, `"calibration_sweep"` |
| `kind` | `evaluation` \| `training` \| `calibration` |
| `dataset_hash` | SHA-256 over canonical label serialization — proves "same data" |
| `seed` | Random seed (relevant once ML enters) |
| `model_version` | String tag of the model artifact |
| `code_sha` | Git short-SHA of the checkout at run time |
| `params_json` | The knobs used (train/test size, etc.) |
| `metrics_json` | The metric snapshot at run time |
| `label_quality_json` | The quality flags at the time the metric was computed |

Endpoints:
- `GET /experiments` — list recent
- `GET /experiments/{id}` — one record
- `GET /experiments/compare/{a}/{b}` — matched metric deltas + `same_dataset`
  / `same_code` flags
- `POST /experiments/run/walkforward` — runs a walk-forward + persists the
  full provenance + returns the experiment id

Two runs over the same `dataset_hash` and `code_sha` MUST produce the same
metrics. If they don't, the system has nondeterminism — file a bug.

---

## 6. Numeric promotion gates (Stage 1.5)

The contract between paper and any future canary / scaled-live run. Each
gate is a single `GateCheck` with a numeric threshold and a clear verdict
(`pass` / `fail` / `insufficient_data`).

| Gate | Threshold | Direction | Min sample |
|---|---|---|---|
| `brier_ok` | ≤ 0.22 | lower-is-better | 100 closed |
| `calibration_error_ok` | ≤ 0.05 | lower-is-better | 100 closed |
| `sharpe_floor` | ≥ 1.2 | higher-is-better | 60 closed |
| `max_drawdown_ceiling` | ≤ 0.15 | lower-is-better | 30 closed |
| `win_rate_floor` | ≥ 0.45 | higher-is-better | 100 closed |
| `profit_factor_floor` | ≥ 1.5 | higher-is-better | 100 closed |
| `expectancy_positive` | > 0 | higher-is-better | 30 closed |

`/gates/catalog` returns the full list. `/gates/status` evaluates them all
against the live metrics and returns:

```json
{
  "gates": [{"name": "...", "verdict": "pass|fail|insufficient_data", ...}],
  "pass_count": int,
  "fail_count": int,
  "insufficient_count": int,
  "overall": "pass" | "fail" | "insufficient_data",
  "closed_trades": int
}
```

`overall = "pass"` only when every non-insufficient gate passes. Stage
transitions and canary promotions are gated by this verdict.

---

## 7. Stage 2 — Execution realism (added 2026-05-29)

Three components, all surfaced via `/execution/*` and applied automatically
inside `simulate_strategy(apply_realistic_costs=True)` (default).

### 7.1 Cost model

```
cost = commission(broker, side, qty) + spread_cost(snapshot, notional) + slippage(notional, ADV, vol)
```

| Component | Formula | Source |
|---|---|---|
| Commission | per-broker schedule (see catalog below) | `COMMISSION_CATALOG` in `bot/execution_costs/` |
| Spread (bps) | `max(floor, atr_pct × multiplier × 10000)`; default mult 0.5 | `estimate_spread_bps` |
| Slippage (bps) | `k × √(notional / ADV) × max(0.5, ann_vol/0.20)`; capped at 200 bps | `estimate_slippage_bps` |

Tunable knobs (env-overridable): `TB_SPREAD_BPS_FLOOR`, `TB_SPREAD_ATR_MULT`,
`TB_SLIPPAGE_K_BPS`, `TB_SLIPPAGE_BPS_CAP`, `TB_SLIPPAGE_DEFAULT_ADV`.

### 7.2 Broker catalog

| Name | Stock comm | Option comm | Fractional? | Atomicity? | Notes |
|---|---|---|---|---|---|
| `local_paper` | $0 | $0 | yes | yes | dev default |
| `alpaca_paper` / `alpaca_live` | $0 | $0 | yes | yes | live capped at $200k/notional |
| `robinhood` | $0 | $0 | yes | **no** | spread legs are sequential — atomicity-failure risk |
| `ibkr_lite` | $0 | $0.65/ctr ($1 min) | yes | yes | |
| `ibkr_pro` | $0.0035/sh, $0.35 min, 1% cap | $0.65/ctr ($1 min) | **no** | yes | tiered |

### 7.3 Multi-leg atomicity

When a broker supports combo orders, all legs of a spread fill together.
When it doesn't (Robinhood), legs are submitted sequentially and each has
`TB_LEG_FAIL_PROB` (default 5%) of failing AFTER the previous already
filled. The simulator returns `atomic_failure=True` when the worst case —
some legs filled, others not — actually happens. Backtests + Stage-6
portfolio optimizer can model this as a risk premium against non-atomic
brokers.

### 7.4 Backtest integration

`simulate_strategy` now applies the full cost model to every entry + exit
when `apply_realistic_costs=True` (default). The returned summary adds:

```text
broker, realistic_costs_applied, total_costs_dollar,
net_win_rate, net_avg_win_pct, net_avg_loss_pct
```

Per trade, the dict adds:
```text
net_return_pct  — return after entry + exit costs
round_trip_cost — total cost in $ for the round trip
```

`return_pct` remains the gross number so historical comparisons keep working.

### 7.5 Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /execution/costs/preview` | Cost estimate for a hypothetical order |
| `GET /execution/brokers` | Full catalog (profiles + commissions) |
| `GET /execution/brokers/{name}` | One broker's complete profile |
| `POST /execution/validate-order` | Run constraint checks on a plan |
| `POST /execution/simulate-fill` | Partial-fill walk over supplied bars |
| `POST /execution/simulate-legs` | Multi-leg atomicity simulation |

---

## 8. Stage 3 — Options chain + IV surface + Greeks + assignment risk (added 2026-05-29)

Three pure-math modules + one chain fetcher; engine now picks strikes from a
real chain instead of the snap-to-interval workaround. Snap is still the
last-line fallback.

### 8.1 Greeks (`bot/greeks/`)

Black-Scholes with `math.erf`-based normal CDF (no SciPy dependency).
Returns a `Greeks` dataclass with delta / gamma / theta (daily) / vega (per
vol point) / rho / BS price.

Pinned against textbook examples + put-call parity in
`tests/unit/test_options_chain_and_greeks.py`. Implied vol is **bisection**
over [0.01, 5.0] so it always converges within tolerance when the price is
between intrinsic and worst-case σ.

### 8.2 Chain (`bot/options_chain/`)

| Function | Behaviour |
|---|---|
| `fetch_chain(ticker, expiration?, spot_hint?, prefer_synthetic?)` | Cache hit → yfinance → synthetic fallback. TTL controlled by `options_cache_ttl`. |
| `available_expirations(ticker)` | List of expiry dates from the chain. |
| `nearest_available_strike(ticker, target, kind, expiration?)` | Real chain strike; falls back to `snap_strike` when chain unavailable. Returns `(strike, source)` so callers know which tier they got. |
| `iv_surface(ticker)` | Pivots the chain into per-expiry IV ladders (smile + term structure). |
| `assignment_probability(spot, strike, dte, kind, ex_div_days?, side="SHORT")` | Heuristic [0, 1] + per-factor breakdown + reasons list. Monotonic in ITM-ness and inverse in DTE; ex-dividend bumps short-call risk. |

Sources, in order: `yfinance` → `synthetic` → `fallback`. The source tag is
threaded through every endpoint so the UI shows "real chain" vs "fallback".

### 8.3 Engine integration

`build_order_plan` now calls `nearest_available_strike` before falling back
to `snap_strike`. Wired in all three option branches:
  • `SINGLE_LEG_OPTIONS` (BUY_CALL / BUY_PUT)
  • `SINGLE_LEG_SHORT_OPTIONS` (SELL_CSP / SELL_COVERED_CALL)
  • `SPREAD_OPTIONS` (BULL_CALL_SPREAD / etc.)

When the chain is unavailable, behaviour is identical to Stage 2 (audit
invariants still hold).

### 8.4 Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /options/expirations/{ticker}` | List of expiry dates |
| `GET /options/chain/{ticker}` | Full chain with bid / ask / mid / IV / volume / OI |
| `GET /options/iv-surface/{ticker}` | Pivoted (strike × dte) → IV |
| `GET /options/strike-suggest` | Chain-aware strike picker; returns source tag |
| `GET /options/greeks` | Greeks for one contract from supplied σ |
| `GET /options/implied-vol` | Recover σ from a market price (bisection) |
| `GET /options/assignment-risk` | Short-contract assignment probability |

### 8.5 Adverse paths covered by tests

| Scenario | Test |
|---|---|
| Empty chain (ticker with no options) | `test_synthetic_without_spot_safe` |
| yfinance unreachable / flaky | synthetic fallback path |
| Strike target outside the listed ladder | `test_offmarket_target_picks_nearest` |
| Option price below intrinsic | `test_below_intrinsic_returns_none` |
| Degenerate inputs (T≤0, σ≤0, S≤0) | `test_degenerate_safe` |

---

## 9. Stage 4 — Microstructure + cross-asset + event-risk (added 2026-05-29)

Three new modules + an engine gate. Together they answer "is right now a
sensible time for the bot to enter, given what the broader market and the
macro calendar are doing?"

### 9.1 Microstructure proxies (`bot/microstructure/`)

| Metric | Source proxy | Honest about |
|---|---|---|
| `spread_bps` | yfinance bid/ask | best-effort; real L2 needed for tick-by-tick |
| `bid_ask_imbalance` ∈ [-1, +1] | bid_size vs ask_size when available | 0 when no size data |
| `aggressive_flow` ∈ [-1, +1] | up-volume vs down-volume across recent bars | tape-side approximation |
| `sweep_probability` ∈ [0, 1] | volume × range expansion vs ADV-derived expectation | not real-time tick detection |
| `absorption_probability` ∈ [0, 1] | high volume + small body | reversal precursor heuristic |
| `urgency` ∈ [0, 1] | recent 5-bar pace vs typical | crude but stable |

Hard limit: spoof / iceberg / hidden-liquidity detection require Level-2.
Stage 7 (data-source health) will add a paid feed; until then `source="proxy"`
is stamped on every snapshot.

### 9.2 Cross-asset intelligence (`bot/cross_asset/`)

Pulls **14 instruments** in one call (5-min cache):
- equities: SPY, QQQ, IWM
- volatility: ^VIX
- yields: ^TNX (10Y), ^FVX (5Y)
- dollar: DX=F
- commodities: GLD, USO
- crypto: BTC-USD
- sectors: XLK, XLF, XLE, XLU

Reduces them to a `CrossAssetState` with one label per axis:
| Axis | Possible values |
|---|---|
| `equities` | risk_on / risk_off / mixed (vote across SPY+QQQ+IWM) |
| `volatility` | compressed / elevated / spiking (VIX bands + 5d delta) |
| `yields` | rising / falling / stable (^TNX 5d delta > ±2%) |
| `dollar` | rising / falling / stable (DX=F 5d delta > ±1%) |
| `commodities` | inflationary / disinflationary / mixed (GLD+USO joint) |
| `crypto` | bullish / bearish / choppy (BTC trend) |
| `breadth` | broad / narrow / unknown (equity vote concordance) |
| `regime_label` | combined headline: `risk_on_compressed_vol`, `risk_off_high_vol`, `rally_with_fear`, `tighten_pressure`, `ease_pulse`, `mixed` |

`alignment_for(ticker_regime_trend, state)` returns whether a per-ticker
setup aligns with the cross-asset state. `hedge_suggestion(state, net_beta)`
returns a sizing fraction + instruments list (VXX / SH / SQQQ / TLT puts).

### 9.3 Event-risk calendar (`bot/event_risk/`)

Hardcoded 2026 reference calendar covering CPI, PPI, FOMC statements,
Powell pressers, NFP. Plus auto-generated OPEX dates (3rd Friday of every
month). Per-ticker earnings pulled from `yfinance.Ticker.calendar` when
available.

`can_trade(ticker)` is the gate the engine asks every cycle:
| Rule | Default window |
|---|---|
| High-impact macro print | ±30 min around the print |
| Per-ticker earnings | ±1 day around the date |
| OPEX | medium impact — warn, do not block |

Returns `TradePermission` with `can_trade`, `reason`, `blocking_events`,
`next_window`. The engine logs a `status="event_hold"` event and skips
opening new positions; exits + management still run.

### 9.4 Engine integration

`run_cycle` consults `event_risk.can_trade(ticker)` BEFORE any BUY signal
fires the executor. Default behaviour configurable via
`config.event_risk.enabled` (default `True`).

### 9.5 Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /microstructure/{ticker}` | Order-book proxies + interpretation |
| `GET /cross-asset/state` | Full 14-asset state snapshot |
| `GET /cross-asset/alignment/{trend}` | Does a per-ticker trend align? |
| `GET /cross-asset/hedge?net_beta=` | Hedge sizing + instrument suggestions |
| `GET /event-risk/calendar?within_days=` | Upcoming events |
| `GET /event-risk/active` | Events inside the auto-hold window now |
| `GET /event-risk/can-trade/{ticker}` | Engine's gate question |

### 9.6 Promotion-gate addition

A new gate is implied by Stage 4 but not yet codified: **no trades should
fire during ``event-risk active`` windows in any backtest replay or
canary**. Adding to the gate catalog in Stage 7 when monitoring lands.

---

## 10. Stage 5 — ML upgrade (added 2026-05-29)

Five submodules in `backend/bot/ml/`:

| Submodule | Responsibility |
|---|---|
| `feature_store` | Materialize DecisionLog rows as `(X_df, y, meta)` for training; refuses to return data below `min_closed=30` (default) |
| `models` | sklearn factory — `logistic` (Stage 1.5 baseline) + `hist_gb` (HistGradientBoostingClassifier, ~LightGBM-equivalent algorithm). Both share a uniform preprocessor (median impute + scale + one-hot) |
| `calibration` | `calibrate_model(pipeline, X, y, method="sigmoid"\|"isotonic", cv=3)` via sklearn's CalibratedClassifierCV |
| `registry` | Versioned `.pkl` artifacts + JSON metadata + `active.json` pointer. `register_model` / `list_models` / `set_active` / `active_model` |
| `ab` | Deterministic SHA-1(split_name + ticker) bucketing. Persists splits to `ml/registry/ab_splits.json` |

### 10.1 Model lifecycle

```
DecisionLog → feature_store.build_dataset → models.create_model("hist_gb")
  → calibration.calibrate_model(method="isotonic", cv=3)
  → registry.register_model → registry.set_active
```

Every artifact's metadata records: `version`, `model_type`, `calibration`,
`trained_at`, `rows_trained`, `cv_brier`, `cv_calibration_error`, `notes`,
`artifact_path`. The `cv_brier` and `cv_calibration_error` are evaluated
against the same Stage-1 contract — promotion to active stays gated by the
numeric thresholds in section 6.

**Live-verified Stage-5 lifecycle** (seeded synthetic data):
- 60 labelled rows → trained → `cv_brier=0.16` (≤ 0.22 ✓), `cv_calibration_error=0.05` (= gate)
- Active pointer set → `/ml/active` returned the new version
- A/B split with 30% candidate share over 10 tickers → 6 candidate, 4 control (within sampling variance of 30%)

### 10.2 Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /ml/feature-store/stats` | Counts + class balance |
| `GET /ml/models` | Every registered model + supported types |
| `GET /ml/active` | Currently active model + metadata |
| `POST /ml/train` | Train + optionally `set_active=true`; returns 422 with feature-store warnings when below threshold |
| `POST /ml/set-active` | Switch active version |
| `GET /ml/ab` | List splits |
| `POST /ml/ab` | Register split (name, control_version, candidate_version, candidate_share, notes) |
| `GET /ml/ab/{name}/route/{ticker}` | Returns the arm + version for a (split, ticker) |

### 10.3 What's NOT in Stage 5 (deliberate)

- **LightGBM / XGBoost / CatBoost backends** — the venv has none of those.
  sklearn's HistGradientBoostingClassifier is the closest substitute (same
  algorithm family, similar speed). Adding the alternatives in Stage 5.5
  when the environment provides them is a one-line factory entry.
- **Online / continual training as a daemon** — the trainer is a one-shot
  `POST /ml/train` for now. Cron-scheduled retrain happens in Stage 7
  monitoring; the lifecycle infrastructure is here.
- **Engine integration in run_cycle** — the active model is registered and
  inspectable but the engine still uses the legacy `bot/predictive/`
  surface. Switching the engine to consume `ml.active_model()` will land
  alongside Stage-7 drift detection so a model decay is caught before it
  affects production decisions.

---

## 11. Stage 6 — Portfolio optimizer (added 2026-05-29)

Four submodules in `backend/bot/portfolio_optimizer/`:

### 11.1 Sizing primitives (`sizing.py`)

Four pure functions; the combiner takes the **MIN** so no single algorithm
overrides a more conservative cap.

| Function | Inputs | Returns |
|---|---|---|
| `kelly_fraction(win_rate, avg_win, avg_loss)` | strategy edge | `f* × fraction` (default quarter-Kelly = 0.25 ×) |
| `cvar_size_fraction(equity, daily_loss_budget, sigma_pct, confidence=0.95)` | risk budget + vol | fraction such that tail-VaR ≤ budget |
| `vol_target_fraction(target_vol, asset_vol)` | targets | `target / asset` clipped to [0, 1] |
| `drawdown_size_multiplier(current_drawdown_pct)` | dd state | 1.0 below cut threshold (default 5%); linearly to floor (default 0.25×) by 4× cut |

### 11.2 Capital allocator (`allocator.py`)

`allocate_capital(by_strategy_metrics)` returns shares per strategy.

Rules:
- Floor: `strategy_min_allocation` (default 5%) — every active strategy still samples
- Cap: `strategy_max_allocation` (default 40%) — no single strategy dominates
- Scoring: `log1p(expectancy) × win_rate × min(3, profit_factor) × confidence(closed/30)`
- Negative expectancy → floor (5%); below 5 closed trades → neutral 1.0 score
- Sum of shares ≤ 1.0; remainder = cash reserve

### 11.3 Correlation-aware caps (`correlation.py`)

Cluster exposure built from `portfolio_intel.themes_for(ticker)`. Each
ticker contributes to EVERY theme it belongs to (NVDA → Mag7 + AI infra +
Semis), keeping the caps conservative.

`check_cluster_cap(ticker, new_value, positions, equity)` returns the
most-binding theme + `blocked` flag + `allowed_value` (max $ that can be
added without breaching). Default cap = 50% (`cluster_max_exposure`).

### 11.4 Top-level optimizer (`optimizer.py`)

`optimize_size(ticker, strategy, requested_dollar, equity, drawdown_pct,
positions, by_strategy_metrics, asset_volatility, daily_loss_budget)`
runs the full pipeline:

1. Look up per-strategy allocation share
2. Compute Kelly / CVaR / vol-target / DD-multiplier
3. Combine into the most-binding cap
4. Apply drawdown multiplier
5. Cluster check on the resulting size
6. Return `OptimizerDecision` with `recommended_dollar`, binding cap name,
   reasoning trail

**Hard invariant**: `recommended ≤ requested` always. The optimizer
never increases size.

### 11.5 Live-verified behaviour

```
sizing primitives @ win_rate=0.6, win=200, loss=-100, σ=20%, equity=$10k:
  kelly=0.1000  cvar=0.0608  vol_target=0.7500  dd_mult=1.00 (no DD)

drawdown ramp:
  DD=0%   → 1.000
  DD=5%   → 1.000   (at cut threshold)
  DD=10%  → 0.750
  DD=15%  → 0.500
  DD=20%  → 0.250   (floor)

live cluster exposures (paper $4,371 portfolio):
  Mag7 (MSFT,TSLA)  46.2%  ← approaching cap
  Cloud (MSFT)      23.7%
  AI infra (AMD)    22.8%

cluster-check: add $1000 NVDA → would push Mag7 to 69.1% → BLOCKED
  allowed_value = $165.19

preview: $2000 NVDA request, 8% drawdown, win_rate=0.65, σ=25%, budget=$200
  recommended = $413.52
  binding cap = CVaR (9.73%)
  dd_multiplier = 0.85
  strategy_share = 40%
```

### 11.6 Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /portfolio/optimizer/sizing/primitives` | One-shot diagnostic for every sizing rule |
| `GET /portfolio/optimizer/clusters` | Live exposure breakdown |
| `GET /portfolio/optimizer/cluster-check` | Would adding $X to ticker breach a cap? |
| `GET /portfolio/optimizer/allocation` | Per-strategy capital shares from live metrics |
| `POST /portfolio/optimizer/preview` | Full pipeline preview for hypothetical plan |

### 11.7 Engine integration (deferred)

The endpoints are live but the engine still uses Stage-1.5's risk evaluator
for per-trade sizing. Swapping `run_cycle` to consult `optimize_size`
lands in Stage 7 alongside drift detection so a sudden size change is
observable in monitoring before it affects production paper trading.

---

## 12. Stage 7 — Drift + monitoring + attribution + explainability (added 2026-05-29)

Four submodules + a unified endpoint surface:

### 12.1 Drift (`bot/drift/`)

Population Stability Index (PSI) is the institutional standard for
distribution-shift detection:

```
PSI = Σ (p_cur − p_base) × ln(p_cur / p_base)
```

| PSI band | Severity | Action |
|---|---|---|
| < 0.10 | `ok` | no significant change |
| 0.10 – 0.25 | `watch` | monitor, do not retrain yet |
| ≥ 0.25 | `critical` | retrain or investigate |

`assess_feature_drift({baseline_numeric, current_numeric, baseline_categorical, current_categorical})` returns a `DriftReport` with per-feature PSI + severity + overall verdict (worst-severity propagates).

`assess_prediction_drift(baseline_preds, current_preds)` is the same math
applied to the ML model's predicted-probability distribution.

### 12.2 Monitoring (`bot/monitoring/`)

Per-feed rolling buffers (200 samples each) tracking p50/p95/p99 latency +
success/failure counts + last-success timestamp.

| Feed | SLO max stale (min) |
|---|---|
| `flashalpha` | 15 |
| `cboe` | 20 |
| `yfinance` | 30 |
| `finnhub` | 30 |
| `anthropic` | 60 |
| `newsapi` | 60 |

`with timing("yfinance"):` is the canonical instrumentation pattern —
records latency on success, increments failure on exception. `feed_summary()`
returns `{feeds, any_breach, breached_feeds, tracked_feeds}` consumed by
`/monitoring/health`.

### 12.3 Attribution (`bot/attribution/`)

P&L slicing by strategy / regime / grade using the same labels Stage 1
uses. Each bucket reports `closed`, `wins`, `losses`, `total_pnl`,
`win_rate`, `expectancy`, `profit_factor`, **`pnl_contribution_pct`** (signed
share of overall total). Buckets sort by `|total_pnl|` so the top movers
are first.

### 12.4 Explainability (`bot/attribution.explain_trade`)

Composes existing artefacts into a structured rationale:
- `trade` (Trade row)
- `decision_context` (regime, grade, win_probability, features at signal time)
- `execution_quality` (slippage_bps, is_adverse, side)
- `outcome` (status, pnl, instrument)
- `headline` + `why[]` bullets the UI renders as a clean card

### 12.5 Live-verified behaviour

```
PSI sanity:
  identical samples           PSI=0.0       severity=ok
  shifted by +5               PSI=9.84      severity=critical
  totally new range           PSI=12.02     severity=critical

Multi-feature drift report:
  rsi (unchanged)             PSI=0.0       severity=ok
  atr (4× shift)              PSI=40.06     severity=critical
  overall                     critical

Monitoring latencies (just recorded):
  yfinance    35 ms p50   not breached
  cboe       120 ms p50   not breached
  anthropic  850 ms p50   not breached

Attribution by strategy (live trade history):
  exit_manager      closed=1  wins=0/1  pnl=-$646.05  contrib=1.0
  macd_momentum     closed=0  wins=0/0  pnl=$0.0

Explainability (real NVDA option close):
  headline: "SELL_STOCK NVDA (unranked setup)"
  why:      "signal reason: stop-loss hit: -83.3% ≤ -50%"
  outcome:  status=closed  pnl=-$646.05  instrument=option
```

### 12.6 Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /drift/feature` | Multi-feature PSI report |
| `POST /drift/prediction` | Prediction-distribution drift |
| `GET /drift/psi` | Quick inline PSI between two series |
| `GET /monitoring/health` | All-feeds health snapshot |
| `GET /monitoring/feed/{name}` | One feed's detail |
| `POST /monitoring/record` | Manual recorder (tests + callers w/o timing CM) |
| `GET /attribution/by-strategy` | P&L attribution per strategy |
| `GET /attribution/by-regime` | P&L attribution per regime |
| `GET /attribution/by-grade` | P&L attribution per grade cohort |
| `GET /explain/trade/{trade_id}` | Per-trade rationale card |

### 12.7 New promotion gates (informational)

Stage 7 surfaces the data to enforce two new gates (will codify in Stage 8
canary):

1. **No feed should be in SLO breach** when canary is active
2. **No feature should show PSI ≥ 0.25** vs training baseline for > 24h

---

## 13. Stage 8 — Adversarial + simulation + canary + ops (added 2026-05-29)

The production-readiness gate. Three modules + one doc artifact +
engine integration of the kill-switch.

### 13.1 Stress scenario library (`bot/stress/`)

| Scenario | Mutation | Expected bot response |
|---|---|---|
| `flash_crash` | price drop 10%, VIX × 2, ATR × 3 | refuse entry; stops fire |
| `halted` | price = 0, halted flag | snapshot validation rejects |
| `bad_quote` | negative price, NaN volume | upstream sanitization rejects |
| `illiquid_chain` | options chain empty | options strategies HOLD |
| `vix_spike` | VIX × 2 | regime → high-vol; sizing cuts |
| `wide_spread` | bid-ask × 10 | TCA gate trims size |
| `stale_data` | snapshot timestamp 1 week old | SLO breach; cycle skipped |

`apply_scenario(name, snapshot)` returns the mutated snapshot + the
expected behaviour string. `run_suite(snapshot)` applies every scenario for
a pre-promotion check.

Test contract: every strategy must survive every scenario without
crashing AND must not emit a BUY against a degenerate snapshot.

### 13.2 Replay (`bot/replay/`)

`replay_session(strategy, ticker, period, interval)` runs the strategy
bar-by-bar over historical candles and returns the action stream + action
counts + first N transition events. Different from `/backtest/*`:
- Backtest → simulated P&L curve over the period
- Replay → raw signal stream, for "why did the bot fire here?" debugging

### 13.3 Canary state machine (`bot/canary/`)

Persisted to `ml/canary/state.json`. Forward-only by default:
`paper → canary → scaled` with `rollback` always available; `halt` is the
operator hard-stop.

`POST /canary/promote {target, capital, force}`:
- Reads `/gates/status` (Stage 1.5 numeric contract)
- Refuses with HTTP 422 unless `overall == "pass"` OR `force=true`
- On success, records `promoted_at` + capital

`POST /canary/rollback {reason}`:
- Always succeeds; returns state to `paper`
- Records `rolled_back_at` + `rollback_reason` for post-mortem

`POST /canary/halt {reason}`:
- Hard stop; state machine forbids re-entry until operator clears

### 13.4 Kill switch

Single-boolean operator override. Persisted to `ml/canary/kill_switch.json`.

`kill_switch_active()` is read **every engine cycle**. When True, every
BUY signal becomes `status="kill_switch"` and is skipped; exits + position
management still run. Survives process restart. Defensive read (missing /
unparseable file → not active).

Endpoints:
- `GET /canary/kill-switch` — current status + reason + set timestamp
- `POST /canary/kill-switch {active, reason}` — toggle

### 13.5 Engine integration

`run_cycle()` now consults kill-switch FIRST (before event-risk, audit,
analytics) and short-circuits BUYs with the `kill_switch` status. This is
the lowest-latency safety control in the entire system — flipping it
takes effect within one cycle (~30s).

### 13.6 Ops runbook

`docs/STAGE8_OPS_RUNBOOK.md` — operator's reference covering:
- Canary state-machine diagram
- Symptoms → diagnostic endpoint mapping
- Stop-sequence severity ladder (kill → rollback → halt → process kill)
- Post-incident resumption checklist
- Secrets rotation procedure (90-day cadence)

### 13.7 Live-verified behaviour

```
/stress/scenarios → 7 registered scenarios
/stress/apply → 7 mutated snapshots, each with expected_behaviour

Canary state machine:
  initial: paper
  promote without gates → HTTP 422 (refused) ✓
  promote with force=true → canary, $500 capital ✓
  rollback → paper, rollback_reason recorded ✓

Kill switch:
  inactive → active "stage-8 demo" → cleared ✓

Replay SPY 1mo:
  21 bars, 21 signals (all HOLD: below 50MA)
  first events show real close prices + reasons
```

### 13.8 Promotion gates closed by Stage 8

| Gate | Status |
|---|---|
| Reconciliation drift ≤ $0.50 | enforced by `/audit/health` (Stage 1.5) |
| Brier ≤ 0.22, ECE ≤ 0.05 | enforced by `/gates/status` (Stage 1.5) |
| No event-risk active windows during trade | enforced in engine (Stage 4) |
| No feed in SLO breach | enforced by `/monitoring/health` (Stage 7) |
| No critical drift > 24h | enforced via `/drift/feature` (Stage 7) |
| All stress scenarios pass | enforced via `/stress/apply` (Stage 8) |
| Kill switch inactive | enforced by engine consult (Stage 8) |

All 7 gates must be green for `POST /canary/promote {force: false}` to
succeed. The contract is now complete.

---

## 14. Roadmap is closed

The 8-stage institutional roadmap is fully shipped as of 2026-05-29. Further
work falls into refinement categories rather than new stages:

- LightGBM / XGBoost backends (Stage 5.5)
- Real L2 / dxFeed / Polygon integration (Stage 4 upgrade)
- Multi-account access control (cross-cutting)
- CI/CD for model artifacts (cross-cutting)
- Cron-scheduled retrain + drift checks (Stage 7.5)
- Cockpit hero redesign + Portfolio Risk drill-down (deferred from earlier)

Each is a one-stage-or-less effort that can be done in isolation now that
the institutional contract is in place.

---

**Owner**: Stage-1 measurement build (2026-05-29).
**Review cadence**: Every stage gate; bump version number on any change.
