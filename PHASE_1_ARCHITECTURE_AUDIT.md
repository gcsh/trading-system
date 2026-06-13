# Phase 1 Architecture Audit — 2026-06-02

Covers everything that landed in this session: ThetaData install + Phase
1.1-1.5 + 13 follow-ups + items #131-134 (verified pre-existing). Maps
the integration points, flags gaps, surfaces decisions that future
sessions will want to know.

---

## 1. The options data flow (top to bottom)

```
                                            ┌──────────────────────┐
                                            │  ThetaTerminal.jar   │
                                            │  systemd: thetadata  │
                                            │  Java 21 · 0.0.0.0   │
                                            │  :25503 REST + MCP   │
                                            │  :25520 FPSS stream  │
                                            └──────────┬───────────┘
                                                       │
                                              localhost HTTP
                                                       │
                                                       ▼
                ┌──────────────────────────────────────────────────────┐
                │  backend/bot/data/thetadata.py                       │
                │  • ThetaDataClient (typed wrapper)                   │
                │  • OptionQuote dataclass                             │
                │  • CACHED list_expirations (1h TTL)  ← P1.4-FU4      │
                │  • CACHED chain_snapshot (10s TTL)   ← P1.4 follow-up│
                │  • check_quote_sanity (P1.2)                         │
                │  • check_parity_sanity (P1.2-FU1)                    │
                │  • check_smile_sanity (P1.2-FU2)                     │
                │  • check_intraday_iv_sanity (P1.2-FU3)               │
                └──────────────────────┬───────────────────────────────┘
                                       │
                                       ▼
                ┌──────────────────────────────────────────────────────┐
                │  backend/bot/data/options.py — orchestrator          │
                │  • _selected_provider() reads OPTIONS_PROVIDER env   │
                │  • _provider_chain() → ordered list                  │
                │  • _atm_from_thetadata (P1.1)                        │
                │     - all 4 sanity gates                             │
                │     - Brenner-Subrahmanyam IV from straddle          │
                │     - data_confidence + sanity_flags surface         │
                │  • _atm_from_yfinance (fallback)                     │
                │  • _atm_from_cboe (last fallback)                    │
                │  • snap_strike (arithmetic, legacy)                  │
                │  • chain_strike (chain-aware) ← P1.4                 │
                │  • chain_strike_with_drift ← P1.4-FU2                │
                │  • chain_expiry / resolve_expiry_dte ← P1.4-FU1      │
                │  • _iv_rank_with_history ← P1.3                      │
                │  • _record_provider_hit / _record_sanity_flags       │
                └─────────────┬─────────────────────┬──────────────────┘
                              │                     │
            ┌─────────────────┘                     └─────────────┐
            │ side write                                          │ public
            ▼                                                     ▼
┌──────────────────────────┐                  ┌──────────────────────────────┐
│  iv_history table        │                  │  options_snapshot(ticker,    │
│  (SQLAlchemy)            │◄─ backfill ───┐  │  spot) → dict                │
│  EXTERNAL_CACHE; never   │  scheduler    │  │  Returns:                    │
│  wiped on fresh_start    │  17:00 ET cron│  │   has_options, iv_atm,       │
└──────────────┬───────────┘  ← P1.3-FU3   │  │   iv_rank, iv_rank_estimated,│
               │                            │  │   implied_move,              │
               │ percentile rank            │  │   data_confidence,           │
               │                            │  │   sanity_flags,              │
               └────────────────────────────┘  │   options_source, expiry,    │
                                               │   earnings_days              │
                                               └────────────┬─────────────────┘
                                                            │
                                                            ▼
                                ┌──────────────────────────────────────────┐
                                │  Consumers:                              │
                                │  • backend/bot/market_data.py:242        │
                                │    (engine cycle market snapshot)        │
                                │  • backend/bot/agents/* (council)        │
                                │  • backend/bot/strategies/all_*.py       │
                                │    (chain_strike+resolve_expiry_dte)     │
                                │  • backend/bot/engine.py:1530             │
                                │    (option plan strike selection)         │
                                │  • backend/bot/ai/brain.py:210            │
                                │    (Brain-decision strike)                │
                                │  • backend/api/routes/authority.py        │
                                │    (data-quality endpoint)                │
                                │  • frontend/.../DataQualityChip.jsx       │
                                │    (operator observability)               │
                                └──────────────────────────────────────────┘
```

## 2. Sanity gates execution order (inside `_atm_from_thetadata`)

```
chain_snapshot ──► ATM strike pick
                   │
                   ▼
           check_quote_sanity (call leg) ──── reject ──► fall through to yfinance
                   │ pass
                   ▼
           check_quote_sanity (put leg)  ──── reject ──► fall through to yfinance
                   │ pass
                   ▼
           check_parity_sanity            ──── reject ──► fall through to yfinance
                   │ pass
                   ▼
           check_smile_sanity (chain)     ──── reject ──► fall through to yfinance
                   │ pass
                   ▼
           Compute iv_atm (Brenner-Subrahmanyam)
                   │
                   ▼
           check_intraday_iv_sanity       ──── reject ──► fall through to yfinance
                   │ pass
                   ▼
           record_today(iv_history)
                   │
                   ▼
           Return atm dict with confidence label
```

## 3. Strategy → engine → executor data path

```
Strategy.analyze(ticker, data)
    │
    │  resolve_expiry_dte(ticker, target_dte=30)
    │  chain_strike_with_drift(ticker, spot, kind, moneyness, target_dte, target_delta)
    ▼
Signal(
   action=…, ticker=…, strike=<real listed>,
   dte=<chain-resolved>,
   metadata={
     strike, expiration, dte, target_dte, target_strike,
     target_moneyness, moneyness_actual, strike_drift_pct,
     target_delta, …
   }
)
    │
    ▼
engine.run_cycle → _build_plan(signal) → _chain_strike() fallback chain
    │
    ▼
PaperExecutor.execute_order → paper_positions row
    │
    ▼
PaperExecutor.positions() → kind=="complex" branch (#131)
    │  computes synthetic mark per leg (intrinsic)
    │  reports market_value, unrealized_pnl, unrealized_pnl_pct
    ▼
/paper/positions endpoint → CurrentlyHoldingStrip + topbar EquityReadout
```

## 4. Agent council context flow (#132)

```
engine.run_cycle (per signal)
    │
    │ build_agent_context(
    │     ticker, action, strategy, snapshot,
    │     analytics, portfolio_risk, optimizer,
    │     cross_asset, config
    │ )
    ▼
agents_ctx = {
    legacy: market_internals_obj, portfolio_state, regime, risk_state, …
    new (#132):
      journal_lessons      ← journal.applicable_lessons(strategy, regime)
      similar_trades       ← journal.similar_trades(ticker, regime, k=5)
      recent_performance   ← scorecard.recent_performance(agent_name, window=30)
}
    │
    ▼
run_consensus(agents_ctx, use_dynamic_weights=True)
    │  5 agents: market, microstructure, macro, portfolio_risk, devils_advocate
    │  each reads its slice of context, returns vote + confidence
    ▼
Chairman aggregates → final decision
```

## 5. GEX Tier A pipeline (#133)

```
backend/bot/signals/gex.py
    │
    │ ── Inputs: option chain (via options_chain package),
    │           spot, dealer assumption
    ▼
Compute fields → GammaProfile dataclass:
    • net_gex_total           ← P1 of Tier A
    • call_gex_total / put_gex_total
    • max_gamma_strike / max_gamma_value
    • vol_trigger             ← "destabilizing below"
    • dealer_flow_intensity   ← |net_gex_total| regime strength
    • pin_risk_strike + distance + dte_weighted
    • zero_dte_net_gex        ← 0DTE separated
    • call_wall / put_wall / gamma_flip
    ▼
Profile serialized via to_dict() → /gex/* endpoints
    ▼
Frontend Heatseeker.jsx consumes:
    • Cards row: Net Gamma, Call Gamma, Put Gamma, 1-Day Expected Move
    • LongGammaStrip (regime banner)         ← #134
    • GexTable (per-strike)                  ← #134 panel 1
    • CumulativeGexPanel (running cumsum)    ← #134 panel 2
    • ExpiryDecompositionPanel               ← #134 panel 3
```

## 6. Observability infrastructure

```
                  ┌─────────────────────────┐
                  │  WarningsChip (existing) │
                  │  ─ /system/warnings       │
                  └─────────────────────────┘
                  ┌─────────────────────────┐
                  │  DataQualityChip (P1.5)  │ ← new this session
                  │  ─ /system/data-quality  │
                  │    GET + POST /reset     │
                  └─────────────────────────┘
                              │
                              ▼
                  ┌─────────────────────────┐
                  │  options.py in-process  │
                  │   _PROVIDER_HITS        │
                  │   _SANITY_FLAG_HITS     │
                  │   (locked dicts)        │
                  └─────────────────────────┘
```

## 7. Identified gaps (HONEST)

1. **5 watchlist tickers return 0 expirations from ThetaData**
   (META, CRWD, SNOW, NFLX, WULF). Confirmed vendor-side, not client.
   They degrade to the linear `iv_rank_estimate` — bot still
   functional but IV rank is less calibrated. Action: contact
   ThetaData support OR cross-check with Cboe.
2. **The engine loop is `running: false`.** Operator hasn't started
   it. Until started, the DataQualityChip will read empty counters
   because nothing is firing options_snapshot in the bot process.
3. **No integration test for the full sanity cascade.** Unit tests
   per check exist. A test that walks ThetaData → 5 gates → result
   for a synthetic chain with one bad strike would catch regressions
   if any gate is silently disabled.
4. **`target_delta` in chain_strike_with_drift uses BS implied vol
   per-strike via bisection.** Slow when chain is large — currently
   ~5 bisection solves per chain_strike call. OK at our cycle rate
   but worth profiling if cycle time grows.
5. **`options_chain/__init__.py` still uses yfinance as primary**
   when constructing OptionChain objects (different path from
   `_atm_from_thetadata`). Two parallel chain-source code paths
   exist. Probably worth unifying in a future session — see
   `nearest_available_strike` vs `chain_strike`.
6. **`_dividend_yield` caches forever.** True yields update at most
   quarterly; infinite TTL is fine for production but could go stale
   over a multi-week trial. Acceptable for now.
7. **No end-to-end test of `iv_history.backfill`.** It's tested
   manually (the runs that populated 7 tickers). A unit test that
   stubs the client and asserts row counts would catch regressions.
8. **Frontend bundle re-fetches `/system/data-quality` every 30s
   even when the chip modal is closed.** Tiny cost; could be paused
   when document.hidden.

## 8. Decisions worth remembering

  - **OPRA NBBO computed locally is the trust anchor.** ThetaData
    Standard tier doesn't expose IV/greeks; we compute them. This
    is actually GOOD for the data-integrity story — vendor IV can't
    be silently wrong because we never used it.
  - **Provider chain default `thetadata_first`** with yfinance
    fallback. After 1-2 weeks of clean operation, consider switching
    to `thetadata` (no fallback) so degraded vendor data doesn't
    quietly slip through.
  - **Sanity gate philosophy: hard-reject on staleness, parity,
    smile outlier, intraday IV jump.** Soft-warn on warn-spread,
    no-timestamp. The hard rejects fall back to yfinance, so we
    never trade on what we don't trust.
  - **Delta-band selection is institutional convention** (30Δ for
    CSP/CC, 20Δ short condor, 10Δ long condor). The arithmetic
    moneyness is the fallback when delta math fails on illiquid
    strikes.
  - **iv_history is EXTERNAL_CACHE** — never wiped on fresh_start.
    Eight years of vendor history is too expensive to rebuild.

---

*Companion documents: `futref` for session-by-session memos,
`PHASE_1_UI_AUDIT.md` for the page-by-page render check,
`PHASE_1_FINAL_SUMMARY.md` for the executive briefing.*
