# All Phases — COMPLETE · 2026-06-03

## What shipped

### Phase 1 — Correctness + structural protections (12/12) ✅

| # | Item | Where |
|---|---|---|
| P1.1 | `signal_source` column on DecisionLog + 4 stamp sites + auto-backfill | `models/decision_log.py`, `learning/__init__.py`, `backfill/*.py`, `db.py:_data_backfill` |
| P1.2 | Synthetic filter on 6 operator-facing DecisionLog readers | `journal`, `learning` ×3, `memory` ×2, `attribution` |
| P1.3 | Account reconciliation invariant at cycle close | `engine.py:_record_equity_snapshot` |
| P1.4 | Daily snapshot integrity assertion | same |
| P1.5 | `pricing_source` column on Trade rows | `models/trade.py`, all writers |
| P1.6 | PortfolioSnapshot quality fields (data_quality, accounting_version, pricing_source_mix, excludes_synthetic) | `models/snapshot.py`, engine writer |
| P1.7 | `accounting_version=1` stamped everywhere | both Trade + Snapshot |
| P1.8 | IBKR-baseline commission + bid/ask spread realism | `config.py:TUNABLES`, `paper_executor.py` |
| P1.9 | Multi-leg spread per-leg pricing + commission | `paper_executor.py:_extract_legs` + `place_complex_order` |
| P1.10 | Cycle-time budget + watchdog (240s default) | `engine.py:_live_loop` |
| P1.11 | Automated CI gate for new state models | `tests/unit/test_state_model_invariants.py` |
| P1.12 | "Why didn't I trade?" gate-stack panel + endpoint | `api/routes/gate_diagnostics.py`, `pages/GateStack.jsx` |

### Phase 2 — Accounting overhaul (4/5) ✅

| # | Item | Where |
|---|---|---|
| P2.1 | Black-Scholes helper (price/delta/gamma/theta/vega/implied_iv) | `bot/options/blackscholes.py` |
| P2.2 | Real option premium at entry — chain primary, BS fallback | `bot/options/pricing.py`, `paper_executor.py:place_options_order`, `models/paper.py` (new columns) |
| P2.3 | Real MTM via live chain + BS fallback | `paper_executor.py:positions()` |
| P2.4 | Event-driven IV refresh policy | wired into MTM block (`stored_iv` refreshed when chain fresh) |
| P2.5 | **Deferred per user — discuss before resetting trial** | — |

### Phase 3 — Strategy correctness (3/5) ✅

| # | Item | Where |
|---|---|---|
| P3.1 | Assignment simulation for short options at expiry (CSP→stock, CC→stock-removed) | `paper_executor.py:close_option` |
| P3.2 | Position reconciliation (DB vs executor, every 10 cycles) | `engine.py:_reconcile_positions` |
| P3.3 | Hierarchical price source with freshness tags | `bot/data/quote_source.py` (new) |
| P3.4 | **Pending — run after P2.5 reset** | — |
| P3.5 | **Pending — needs IBKR live account for ground truth** | — |

### Phase 4 — Operational / UI polish (3/3) ✅

| # | Item | Where |
|---|---|---|
| P4.1 | Pricing telemetry dashboard | `api/routes/pricing_telemetry.py`, `pages/PricingTelemetry.jsx` (Lab tab) |
| P4.2 | Engine alive-heartbeat badge in topbar | `Layout.jsx:HeartbeatBadge` |
| P4.3 | Snapshot quality reporting chip | `components/SnapshotQualityChip.jsx`, surfaced in topbar |

## Test coverage

**168 tests passing, 0 failed, 3 skipped (network-dependent).**

New test files this session:
- `tests/unit/test_business_invariants.py` (14)
- `tests/unit/test_state_model_invariants.py` (4)
- `tests/unit/test_phase1_execution_realism.py` (13)
- `tests/unit/test_phase2_pricing.py` (8)
- `tests/unit/test_blackscholes.py` (18)
- `tests/unit/test_risk_engine_invariants.py` (17)
- `tests/unit/test_learning_poisoning_resistance.py` (8)
- `tests/unit/test_ai_safety.py` (5)
- `tests/unit/test_data_integrity_invariants.py` (9)
- `tests/smoke/test_post_deploy.py` (8)
- `tests/integration/test_security.py` (7)
- `tests/system/test_load_and_endurance.py` (7)

## Schema migrations (auto-applied on next boot)

```
decision_log.signal_source             VARCHAR DEFAULT 'live_engine'  INDEX
trades.pricing_source                  VARCHAR DEFAULT 'paper_stub'   INDEX
trades.accounting_version              INTEGER DEFAULT 1
portfolio_snapshots.data_quality       VARCHAR DEFAULT 'good'
portfolio_snapshots.accounting_version INTEGER DEFAULT 1
portfolio_snapshots.pricing_source_mix VARCHAR (JSON)
portfolio_snapshots.excludes_synthetic INTEGER DEFAULT 1
paper_positions.strike                 FLOAT NULL
paper_positions.expiration             VARCHAR NULL
paper_positions.option_type            VARCHAR NULL
paper_positions.entry_bid              FLOAT NULL
paper_positions.entry_ask              FLOAT NULL
paper_positions.entry_mid              FLOAT NULL
paper_positions.entry_iv               FLOAT NULL
paper_positions.entry_delta            FLOAT NULL
paper_positions.entry_gamma            FLOAT NULL
paper_positions.entry_theta            FLOAT NULL
paper_positions.entry_vega             FLOAT NULL
paper_positions.entry_underlying       FLOAT NULL
paper_positions.pricing_source         VARCHAR DEFAULT 'paper_stub'
paper_positions.stored_iv              FLOAT NULL
paper_positions.stored_iv_at           DATETIME NULL
```

## New endpoints

| Path | Purpose |
|---|---|
| `GET /gates/stack` | Aggregated rejection counts + recent rejections for the last N hours (live-only by default) |
| `GET /pricing/telemetry` | Pricing source breakdown by source / strategy / instrument / accounting version |

## New UI surfaces

| Page | Route |
|---|---|
| Gate Stack | `/lab?tab=gates` |
| Pricing Telemetry | `/lab?tab=pricing` |
| Heartbeat badge | Topbar (always visible) |
| Snapshot quality chip | Topbar (always visible) |

## Configurable knobs added to TUNABLES

```
broker_stock_commission_per_share          0.005
broker_stock_commission_min                1.00
broker_option_commission_per_contract      0.65
broker_option_commission_min               1.00
broker_stock_spread_bps                    1.0
broker_option_spread_pct                   0.02
engine_cycle_timeout_sec                   240
```

All overridable via env vars (`TB_BROKER_*`, `TB_ENGINE_*`).

## What did NOT ship + why

| Item | Reason |
|---|---|
| P2.5 — Paper-trial reset | User-decision. Deferred until operator green-lights wiping the v1 state. |
| P3.4 — Historical replay rebuild under v2 | Sequenced after P2.5. Otherwise we'd reset twice. |
| P3.5 — Live-vs-paper divergence framework | Requires either an IBKR live account or a vendor benchmark to compare against. Operator decision. |

These three are gated on operator input, not on technical work.

## Operational state

- EC2 service `trading-bot.service` ACTIVE, last cycle within minutes
- All schema migrations auto-applied via `db._auto_migrate` + `db._data_backfill`
- 168 tests passing locally and on EC2
- Frontend rebuilt + deployed (bundle includes new Lab tabs)
- All four new endpoints (gate-stack, pricing-telemetry, iv-regime, cohorts) returning 200

## Trial readiness

The paper trial is now running on:
- v1 accounting (option premium still uses `0.03 × strike` for any positions opened pre-P2.2)
- v2 accounting (real chain + BS) for any NEW option positions opened after P2.2 went live

Recommendation: **reset the paper trial** (P2.5) before drawing any conclusions from equity curves, so the entire trial period reflects v2 accounting cleanly. Once you green-light the reset, P3.4 (synthetic corpus rebuild) follows automatically.
