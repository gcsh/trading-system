# Trading Bot — Correction & Validation Plan

**Status of project:** Built thoroughly, never tested. North star: an auto-trading system whose edge is *proven* before a dollar of real capital touches it.

**The one non-negotiable rule of this plan:** Every phase has a **KILL criterion**. If a gate fails, you stop or pivot — you do not engineer your way around it. You sign up for this now, in writing, before you've seen any results. The gates exist specifically to overrule the part of you that says "I'm not backing off."

> This is not financial advice. Autonomous trading of real capital risks total loss. Most retail directional systems do not beat SPY net of costs. This plan is designed to find out which side of that line you're on as cheaply and honestly as possible.

---

## 0. Inventory: what survives, what gets pruned, what's on probation

| Component | Verdict | Reason |
|---|---|---|
| Provenance / replay (`decision_provenance`, drift=0.0) | **KEEP** | Genuinely valuable. Auditability is reusable as-is. |
| Policy engine (`policy.py`, declarative rules) | **KEEP** | Good. The gating framework is sound; it was just gating an unvalidated signal. |
| Exit manager (`exit_policy.py`) | **KEEP** | Reusable once entries are validated. |
| Risk manager (`risk.py`) | **KEEP** | Collateral / sizing logic is correct (the `naked_short_block` behavior was *right*). |
| Data lake + `outcome_linker.py` + `market_outcomes` | **KEEP — this is your test instrument** | The data to validate edge already lives here. |
| `quote_source.py` integrity contract | **KEEP** | Sound. |
| `PaperExecutor` fill model | **REBUILD** | Mid-fill is fiction. This is a Phase 2 rewrite. |
| 34 detectors | **PROBATION** | Phase 0 decides which (if any) survive. Default expectation: most get cut. |
| 10-agent council | **PROBATION** | Kept only if Phase 1 attribution shows it beats the surviving rule. Default: cut. |
| Claude brain-per-ticker | **PROBATION** | Same. It is the main cost sink ($300–400/mo). Must prove bps of added edge or it goes. |
| Learning loop (P18 tuning) | **FREEZE** | Nothing to learn from until there's a validated strategy and real sample. Safety flags stay OFF. |

---

## Phase 0 — Does any edge exist at all? (2–3 days, query-only, no new code)

**The answer is already in your database.** You will not run the engine, build a backtester, or place a trade in this phase.

**Objective:** For each detector, measure forward-return expectancy *net of realistic costs*, on the **stock leg only** (this isolates signal quality from the options-fill fiction), and compare it to a same-timestamp SPY baseline — *out of sample*.

**Tasks**
1. Split `market_observations` chronologically: **discovery set** = first 70% of history, **holdout set** = last 30%. Never look at holdout until step 4.
2. On the discovery set, for each `(detector, direction)` cell, join `market_observations → market_outcomes` and compute the distribution (mean, median, IQR, std) of `return_pct_20bars`. Subtract a brutal flat cost haircut (start at **30 bps round-trip** for stocks).
3. Compute the **SPY baseline the same way**: 20-bar forward return measured at the *same set of timestamps* as the signals (so you're comparing against the market regime the signals actually fired in, not a different one).
4. Rank cells by net mean return *minus* SPY baseline. Take the top candidates. **Only now** run the identical computation on the untouched holdout set.

**Test cases for the query itself**
- TC-0.1: A known random sample of timestamps must produce net expectancy ≈ 0 minus costs (sanity: random entry loses the haircut).
- TC-0.2: Reproduce one cell's mean by hand from raw rows to confirm the join isn't double-counting outcomes.
- TC-0.3: Confirm no look-ahead: every `market_outcome` timestamp is strictly *after* its `market_observation` timestamp.

**Acceptance / GO criteria**
- At least one `(detector, direction)` cell shows **positive net mean expectancy on the discovery set** with a **bootstrap 95% CI that excludes zero**, AND beats the SPY baseline, AND **survives on the holdout set** (still positive, still beats SPY, sign unchanged).
- Multiple-comparisons honesty: you are testing ~34 detectors × directions. The best-looking one *will* look good by chance. Out-of-sample survival on the holdout is your real protection — trust that over any in-sample p-value.

**KILL criterion**
- If **no cell** clears the GO bar — i.e., nothing beats SPY net of 30 bps out-of-sample — then your directional signal layer has no demonstrable edge. **Stop building.** Options: (a) pivot the thesis entirely (different asset class, different horizon, different data), or (b) shut it down and keep the infra as a learning asset. Adding ML, more agents, or a smarter brain to a 50% signal is forbidden by this plan.

**Deliverable:** A one-page results table — every cell, net expectancy discovery vs holdout vs SPY, CI. This single table decides whether Phases 1–4 happen at all.

---

## Phase 1 — Realistic backtest of the survivor(s) (1–2 weeks)

**Entry criteria:** Phase 0 produced ≥1 surviving cell.

**Objective:** Build an honest event-driven backtester for the 1–3 survivors (stock leg first) and get a real equity curve vs SPY buy-and-hold.

**Tasks**
1. Event-driven loop over historical bars. **No vectorized shortcuts that leak future data.**
2. Realistic fills: buys at the **ask (far touch)**, sells at the **bid**, plus slippage scaled by order-size / displayed-size. Commissions modeled.
3. **Walk-forward validation**: roll the train/test window forward across the full history (not a single split). Report each window.
4. Position sizing: fixed fractional first (e.g., 1–2% risk per trade). No Kelly yet.

**Test cases**
- TC-1.1 **Look-ahead guard (highest priority):** assert every signal at bar *t* uses only data with timestamp ≤ *t*'s close. One leaked future bar invalidates the entire backtest. Build an automated assertion, not a manual check.
- TC-1.2 **Deterministic replay:** same inputs → identical equity curve, twice.
- TC-1.3 **Fixture trades:** hand-crafted input where the correct P&L is known; backtester must match exactly.
- TC-1.4 **Cost sensitivity:** re-run at 0 / 30 / 60 bps. If edge only survives at 0 bps, it's not real.
- TC-1.5 **Regime split:** report performance separately in up / down / chop markets. A strategy that only works in one regime needs a regime filter or a smaller claim.

**Acceptance / GO criteria (judgment calls — adjust before you start, not after)**
- Out-of-sample (walk-forward) **Sharpe ≥ 1.0**, AND
- Beats **SPY's Sharpe** over the same windows (SPY long-run ≈ 0.5–0.6), AND
- **Max drawdown ≤ 25%**, AND
- Positive net per-trade expectancy with CI excluding zero, AND
- Holds up at the 30 bps cost level (TC-1.4).

**KILL criterion**
- Fails the bar net of realistic fills out-of-sample → stop. Do **not** loosen the cost assumption or cherry-pick the best walk-forward window to pass. That's the original sin in a new costume.

**Deliverable:** Equity curve + metrics table (CAGR, Sharpe, Sortino, max DD, win rate, payoff ratio, per-trade expectancy CI) vs SPY, per walk-forward window.

---

## Phase 2 — Rebuild the executor + honest paper trading (2–4 weeks running)

**Entry criteria:** Phase 1 produced a strategy that beats SPY out-of-sample net of realistic costs.

**Objective:** Replace the fantasy executor, run the validated strategy live-paper, and confirm reality matches the backtest.

**Tasks**
1. Rewrite `PaperExecutor`: far-touch fills, size-scaled slippage, an explicit **adverse-selection haircut** (worse fills when the signal is right). For options, model NBBO width that widens for low-OI / far-OTM strikes. Stop pretending mid exists outside SPY/QQQ.
2. Run the **stock** strategy in paper with the honest executor. (Add options only after the stock leg validates live-paper — one variable at a time.)
3. Log realized paper fills next to the backtest's *assumed* fills. Track the gap.
4. **Pre-register** the success criteria and required sample size *before* the run starts. Write them in this doc. Do not move them after seeing results.

**Test cases**
- TC-2.1: Executor regression — given a known order book snapshot, fill price matches the documented model exactly.
- TC-2.2: Adverse-selection check — on winning signals, average fill is measurably worse than mid (proves the penalty is active).
- TC-2.3: Backtest-vs-paper drift — paper expectancy stays within tolerance (e.g., ±25%) of backtest expectancy.

**Acceptance / GO criteria**
- ≥ ~50–100 closed paper trades (compute the real required n from Phase 1's per-trade volatility — don't eyeball).
- Live-paper net expectancy positive with CI excluding zero.
- Within tolerance of the backtest (TC-2.3). Large negative divergence = the backtest is still lying.

**KILL criterion**
- Paper expectancy materially negative or far below backtest → your model is wrong somewhere (fills, look-ahead you missed, regime change). Back to Phase 1, do not advance.

---

## Phase 3 — Pilot with real capital you can lose entirely (4+ weeks)

**Entry criteria:** Phase 2 paper matched backtest with positive expectancy over a pre-registered sample.

**Objective:** Real fills are the final arbiter. Find out if your *actual* broker fills match paper.

**Tasks**
1. Fund a pilot account with money whose total loss changes nothing in your life. Small.
2. Run the validated strategy with hard risk limits and a working kill switch (your policy engine + `kill_switch_active` rule already does this — now it earns its keep).
3. Log real fills vs paper fills. This is the only number that has ever mattered.

**Acceptance / GO:** Real fills track paper; net expectancy stays positive over a meaningful sample.
**KILL:** Real fills systematically worse than paper such that edge disappears → the strategy doesn't survive real microstructure. Stop.

---

## Phase 4 — Automation, sizing, and "the brain" (only after Phase 3 holds)

**Entry criteria:** Real-capital pilot confirmed edge survives real fills.

Now — and only now — you build toward "it trades automatically for me," and you re-test the expensive components as *hypotheses*:

1. **Automated execution + monitoring:** scheduler, alerting, automated kill switches, daily P&L reconciliation. Your existing infra slots in here.
2. **Sizing:** introduce fractional Kelly (cap ≤ 0.25) only now, with the validated edge as input.
3. **Does the brain/council earn its cost?** Run `learned_attribution` and `counterfactual_replays` (the tables you built and never queried): compare *rule alone* vs *rule + council* vs *rule + Claude brain*. Keep a component **only if it adds positive bps net of its dollar cost.** Default expectation: the rule alone wins, and you delete the rest and save $300–400/mo.
4. Re-enable the learning loop's safety flags **one at a time**, each behind its own before/after A/B, with auto-rollback armed.

**KILL/PRUNE:** Any component that doesn't add net-of-cost edge gets cut, no matter how much work it was.

---

## Consolidated kill-criteria table (print this; tape it to the wall)

| Phase | Gate | If it fails |
|---|---|---|
| 0 | No detector beats SPY net of 30bps out-of-sample | **Stop or pivot the thesis.** No more model-building. |
| 1 | OOS Sharpe < 1.0 or doesn't beat SPY net of costs | **Stop.** Don't loosen costs or cherry-pick windows. |
| 2 | Honest-executor paper diverges negative from backtest | Back to Phase 1; fix the model. |
| 3 | Real fills kill the edge | **Stop.** Strategy doesn't survive microstructure. |
| 4 | A component adds no net-of-cost edge | Delete it. |

---

## Statistical guardrails (the things that make backtests lie)

1. **Look-ahead bias** is the #1 killer. Automate the assertion (TC-1.1). Most "profitable" backtests die here.
2. **Out-of-sample beats p-values.** With 34 detectors, the best one looks good by luck. Holdout survival (Phase 0 step 4) is your real defense.
3. **Pre-register criteria** before each run. Moving the bar after seeing results is self-deception with extra steps.
4. **Costs are not optional.** If edge needs 0 bps to survive, there is no edge.
5. **Regime-aware.** Report up/down/chop separately. A bull-only strategy is a bull-only claim.

---

## Metric definitions (so "beats SPY" is unambiguous)

- **Per-trade expectancy:** mean(net return per trade), with bootstrap 95% CI.
- **Sharpe:** annualized (mean − rf) / std of returns, net of all costs.
- **Baseline:** SPY buy-and-hold over the *identical* date range and, where relevant, same-timestamp forward windows.
- **Edge claim is valid only if:** expectancy CI excludes zero AND risk-adjusted return exceeds SPY's over the same period, out of sample.

---

## What this plan refuses to let you do
- Add detectors, agents, or a smarter brain to a signal that hasn't cleared Phase 0.
- Trust paper trades from the old mid-fill executor.
- Move a gate after seeing results.
- Flip learning safety flags before there's a validated strategy and real sample.
- Confuse "the machine is auditable" with "the machine is right."

**First action, today:** Phase 0, step 1 — split `market_observations` 70/30 by time and run the expectancy query. Nothing downstream is worth doing until that one table exists.

---

## Pre-commitment (signed before seeing results)

If Phase 0 step 1 returns no `(detector, direction)` cell that:
- has positive net mean expectancy on the discovery set with bootstrap 95% CI excluding zero,
- beats same-timestamp SPY baseline net of 30 bps round-trip,
- and survives sign-unchanged on the held-out 30%,

then the response is: **"no edge demonstrated; recommend pivot or shutdown."** No additional ML layer, no smarter brain, no Phase 0.5 remediation, no refactor of agents. Stop.

Signed: pre-commitment dated 2026-06-16 (before query execution).
