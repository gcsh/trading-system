# Stage 8 — Ops Runbook

**Status**: Stage 8 shipped 2026-05-29.
**Purpose**: The operator's reference for moving the bot from paper to real
money, recognizing trouble, and stopping cleanly when things go wrong.

This document complements `STAGE1_KPI_CONTRACT.md` — that doc says *what
counts*; this one says *what to do*.

---

## 1. Canary state machine

Four states, persisted to `ml/canary/state.json`. Source of truth for
"how much real money is the bot authorized to lose right now?"

```
                 promote               promote
        PAPER  ─────────►  CANARY  ─────────►  SCALED
                                   ◄─────────
                                   rollback
                                       ▲                    ▲
                                       │ halt               │ halt
                                       ▼                    ▼
                                    HALTED  ◄──── halt
```

| State | Real-money capital | Trades execute | Notes |
|---|---|---|---|
| `paper` | $0 | yes, paper-only | default |
| `canary` | small (default $500) | yes, real broker | limited capital; gates monitored |
| `scaled` | scaled per allocation | yes, real broker | promotion only after canary clean run |
| `halted` | $0 | no | operator-set; safety stop |

**Promotion gates** — `POST /canary/promote` calls `/gates/status`
(Stage 1.5 contract) and refuses unless `overall == "pass"` OR `force=true`.
On promotion, `promoted_at` is recorded. On rollback, the state file
captures `rolled_back_at` + `rollback_reason` for the post-mortem.

```
POST /canary/promote
  body: {"target": "canary", "capital": 500, "force": false}
  → 200 OK with new state when gates pass
  → 422 with gates_summary when gates fail
```

---

## 2. Kill switch

Distinct from rollback. The kill switch is a single boolean read **every
engine cycle**:

- `kill_switch_active() == True` → engine emits `status="kill_switch"` for
  every BUY signal and skips placement. Exits + position management
  still run.
- Persisted to `ml/canary/kill_switch.json`.
- Survives process restart. Survives bug in the engine — the read is
  defensive (file missing or unparseable → not active).

**When to flip it:**
- Operator sees unexpected behaviour and wants to stop new entries NOW
- Vendor outage suspected (yfinance/Cboe down)
- Personal: stepping away and want zero new exposure

**Manual control:**
```
POST /canary/kill-switch  {"active": true, "reason": "..."}
```

---

## 3. Stress scenarios

Library lives in `bot/stress/`. Run any subset against a snapshot to verify
the bot handles adversarial inputs.

| Scenario | Mutates | Expected bot behaviour |
|---|---|---|
| `flash_crash` | price drops 10%, VIX × 2, ATR × 3 | refuse new entries; stop-losses fire |
| `halted` | price = 0; `halted: true` | snapshot validation rejects; no orders |
| `bad_quote` | negative price, NaN volume | upstream sanitization rejects |
| `illiquid_chain` | options chain empty | options strategies HOLD; equities unaffected |
| `vix_spike` | VIX × 2 | regime → high-vol; vol-target shrinks size |
| `wide_spread` | bid-ask × 10 | Stage-2 execution cost cap trims size |
| `stale_data` | timestamp 1 week old | monitoring SLO breach; cycle skipped |

**Pre-promotion check**: every scenario MUST pass when applied to today's
snapshot before any state transition past `paper`.

---

## 4. Replay

`/replay/{ticker}` re-runs a strategy bar-by-bar over historical candles.
Used to debug "why did the bot fire here?" without waiting for live bars.
Returns the actions count + the first N action-transition events.

Different from `/backtest/*` — replay gives the raw signal stream; backtest
simulates the P&L curve.

---

## 5. Secrets rotation

Sensitive keys: `ANTHROPIC_API_KEY`, `ALPACA_API_KEY` / `ALPACA_API_SECRET`,
`FLASHALPHA_API_KEY`, `FINNHUB_API_KEY`, `NEWS_API_KEY`,
`UNUSUAL_WHALES_API_KEY`.

**Storage**: env vars (`.env`) or the saved-config row in SQLite (Anthropic
only via UI). `/config` GET always masks values. `audit_account_write`
catches any "test"-tagged write attempt to the live account when
`TB_LOCK_ACCOUNT_WRITES=1`.

**Rotation procedure** (every 90 days, or immediately after any incident):
1. Generate new key with vendor
2. Set new env var on the host (`.env` reload OR `export` + restart)
3. Restart `uvicorn`
4. Verify with the relevant `/copilot/ai-status` (Anthropic) or feed-health
   endpoint
5. Revoke old key at the vendor

---

## 6. Incident response runbook

### Symptoms → first checks

| Symptom | First check | Likely Stage layer |
|---|---|---|
| Performance metrics show fake spike | `/audit/health` → reconciliation drift | Stage 1 audit |
| P&L diverges from positions | `/audit/health` → recent_trade_violations | Stage 1 audit |
| Trade row has weird strike | `/audit/health` → strike_not_snapped flags | Stage 1.5 invariants |
| Bot keeps trading through CPI/FOMC | `/event-risk/active` → check auto_hold | Stage 4 event-risk |
| Sharpe collapses overnight | `/drift/feature` + `/attribution/by-regime` | Stage 7 drift |
| Single feed (yfinance/cboe) flaky | `/monitoring/health` → breached_feeds | Stage 7 SLO |
| Suspicious large order | `/audit/health` → audit_blocked status | Stage 1.5 + Stage 6 |

### Stop sequence (severity-ordered)

1. **Quick** — `POST /canary/kill-switch {"active": true}` → blocks all new entries within one cycle (~30s)
2. **Reverse** — `POST /canary/rollback {"reason": "..."}` → returns to paper-only
3. **Hard** — `POST /canary/halt {"reason": "..."}` → state machine forbids re-entry until cleared
4. **Process** — kill the uvicorn PID and restart with `DISABLE_SCHEDULER=1` for forensics

### Resumption checklist

After ANY rollback / halt:
- [ ] Review `audit_health.recent_trade_violations` — clear or annotate
- [ ] Reconcile cash against expected via `/paper/state`
- [ ] Confirm `/monitoring/health.any_breach == false`
- [ ] Re-run stress suite via `POST /stress/apply`
- [ ] Verify gates with `/gates/status` (must be `pass` to re-promote)
- [ ] Clear kill switch: `POST /canary/kill-switch {"active": false}`
- [ ] Re-promote with `force=false`

---

## 7. Cron schedules (optional, future Stage 8.5)

These run via the existing APScheduler when configured — not yet
implemented as automatic jobs but documented here for reference:

| Job | Cadence | What it does |
|---|---|---|
| `retrain_model` | weekly | `POST /ml/train` if dataset > Stage-1.5 threshold |
| `replay_health_check` | nightly | replay today's session; flag if signal count diverges from live |
| `drift_check` | hourly | feature + prediction PSI vs training baseline |
| `secrets_age_audit` | weekly | flag keys older than 90 days |

---

**Owner**: Stage 8 build (2026-05-29).
**Review cadence**: every incident; every successful canary promotion.
