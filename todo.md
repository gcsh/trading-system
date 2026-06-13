# TODO — observe-first backlog

Items here are intentionally NOT in flight. We are observing the live paper
trial for a few days before changing more code. Pick up when we revisit.

Date filed: 2026-05-31 (Sunday). Today is Day 4 of the $5,000 paper trial
(trial started 2026-05-28). Do not start work without re-reading the
"Before touching anything" section first.

---

## 1. Memory-rich agent context — turn stateless reactors into stateless learners

### The idea (settled in conversation)

The 5-agent council is implemented as **pure functions** (agent_market,
agent_microstructure, agent_macro, agent_portfolio_risk,
agent_devils_advocate). They hold no instance state and that is correct.

But "stateless" must not mean "memoryless." Right now an agent sees the
present world (prices, internals, portfolio) but is **blind to its own
history**. Institutional agents must vote with prior lessons in hand.

The fix is NOT to reshape the agents — they stay pure. The fix is a new
`build_agent_context()` assembler in the engine that packs memory INTO
the context dict before each call. Same agent + same context = same vote.
Memory flows in, never trapped inside.

### Target context shape

```python
build_context(ticker, now) -> {
    # already wired today
    "market_internals_obj":   MarketInternalsScore,
    "portfolio_state":        positions/cash/risk-budget snapshot,
    "regime":                 trend/volatility/regime label,
    "risk_state":             drawdown, VIX, hedge pressure,

    # to add (this stage)
    "journal_lessons":        [Lesson, ...]   # 3-5 distilled rules
    "similar_trades":         [{ticker, regime, outcome}, ...]  # k=5
    "recent_performance":     {agent_name -> {calibration, win_rate, drift}}
}
```

### Where each piece lives today

| Field | Status | Source |
|---|---|---|
| Market snapshot / internals | wired | `backend/bot/agents/market_internals.py` → injected as `context["market_internals_obj"]` |
| Portfolio state | wired | `agent_portfolio_risk` + chairman both read it |
| Risk state | wired | drawdown / exposure caps already in portfolio_risk |
| **Journal lessons** | **built but OFF** | `backend/bot/journal/__init__.py` has `applicable_lessons(strategy=, regime_trend=, ...)`. Gated behind config flag `ai.use_journal_lessons` (default OFF). NOT plumbed into agent context. |
| **Similar trades** | **not built** | No retrieval layer. Need a new `journal.similar_trades(ticker, regime, k=5)` that queries closed trades by regime/ticker/strategy and returns outcomes. |
| **Recent performance per agent** | **partial** | `backend/bot/agents/scorecard.py::AgentScore` tracks per-agent calibration. It already feeds dynamic vote weights via `_apply_dynamic_weights` in `agents/__init__.py:1501`. But the **individual agent does not see its own scorecard** — the chairman uses it implicitly via weighting. We want each agent to see its own recent calibration so it can self-temper. |

### Concrete work when we pick this up

1. Flip `ai.use_journal_lessons` to ON (re-read the config tunables file
   to confirm exact key name) AFTER we have enough closed trades to
   mine lessons from. Journal mining needs sample size — `build_lessons`
   in `backend/bot/journal/__init__.py:309` weights by sample_size, so
   with < ~20 closed trades the lessons will be noisy. Wait until the
   trial accumulates real history.
2. Add `journal.similar_trades(ticker, regime, k=5)` — query
   `closed_trades` table by (regime_trend match) + (same strategy OR
   same ticker), order by recency, return outcome + size + reason.
3. Add `scorecard.recent_performance(agent_name, window=30)` returning
   {brier, calibration_error, win_rate, drift_flag}.
4. Add `engine.build_agent_context(ticker, now)` that bundles all of
   the above into the dict. Single call site; all agents read the same
   bundle (do NOT branch per-agent — keeps reproducibility easy).
5. Plumb the bundle through to `chairman_review()` too — chairman
   should also see lessons + analogs, especially when Stage 21 Claude
   Chairman lands (the LLM needs lessons in the prompt).
6. Tests: golden fixture test that proves `same context → same vote`
   still holds (agents must remain deterministic with the richer
   context). Add a fixture with lessons populated to confirm the
   weight on lesson-conflicting actions actually moves.

### Why we are deferring

- We need the paper trial to accumulate **closed trades** before lessons
  are statistically meaningful. Day 4 of 30. Mining lessons too early =
  noisy "lessons" the agents over-trust.
- This is also a prerequisite for Stage 21 (Claude Chairman) — the LLM
  prompt will want lessons + analogs baked in. Better to build once,
  with both consumers in mind, than build twice.

### Estimated size

~1 day of focused work. Not a big build; the journal mining and
scorecard layers already exist. This is plumbing + a new retrieval
function + a context assembler.

### Dependencies / what must be true before starting

- [ ] At least ~20 closed trades in `closed_trades` table (so
      `build_lessons` returns non-noisy output). Check via:
      `sqlite3 trading_bot.db "SELECT COUNT(*) FROM closed_trades WHERE
      status='closed'"`.
- [ ] Calendar-gate fix shipped (see item 2 below) — otherwise the
      "closed trades" pool will include weekend-opened positions that
      pollute lessons.
- [ ] Complex-MTM fix shipped (see item 3) — otherwise short-option
      outcomes won't have valid market_value at close and lessons mined
      from them will be wrong.

---

## 2. Calendar / market-hours gate in engine

### The observation (2026-05-31, Sunday)

The engine has **no market-hours gate**. APScheduler fires `run_cycle`
every `live_interval_sec` (30s) 24/7. Grep confirms zero references to
`is_market_open`, `market_hours`, `weekday`, `nyse`, or any trading-day
check in `backend/bot/engine.py`.

Tonight (Sun 6:17 PM PDT) the wheel strategy fired 4 SELL_CSP positions
on TSLA, NVDA, MSFT, AMD. Books balanced fine, but no fill should be
attributed to a closed session — it breaks regime detection, breaks
calibration, breaks any audit of "what happened on Monday."

### Concrete work

- Add `bot/calendar.py` with `is_us_market_open(now=None)` →
  weekday + 9:30-16:00 ET + NYSE holiday list. Use pandas-market-calendars
  if a dep is acceptable, otherwise a small static holiday list.
- Gate at the top of `engine.run_cycle`: if not open AND not in
  `force_trade` path → log INFO ("market closed, skipping cycle") and
  return early.
- Keep the scheduler running (we still want background data refresh).
- Add `TUNABLES.allow_after_hours_cycles` (default False) so simulation
  / backtesting paths can override.
- Tests: freeze time to Saturday → assert run_cycle no-ops. Freeze to
  Tue 10:00 ET → assert it proceeds.

### Why deferred

The user wants to OBSERVE the current behavior for a few days. Do not
fix this until we have a full week of paper-trial data and have agreed
that the weekend cycles are obscuring signal.

---

## 3. Complex-instrument mark-to-market in paper_executor

### The observation

`backend/bot/paper_executor.py` around lines 145-171 has branches for
`kind == "stock"` and `kind == "option"` only. Multi-leg / SELL_CSP
positions land as `kind == "complex"` and fall through both branches,
leaving `current_price`, `market_value`, `unrealized_pnl` unset. The
frontend then renders "$0.00" / "— mkt" / "+$0.00" for them.

Tonight this caused:
- 4 short puts showed `market_value=0` in /paper/positions
- CurrentlyHoldingStrip "invested" undercounted by exactly the premium
  received ($1,532.60), while topbar "invested" (equity − cash)
  included the short-option liability — two correct numbers, one
  inconsistent display.

### Concrete work

- Add `kind == "complex"` branch in `paper_executor.positions()` that:
  - For SELL_CSP / SELL_COVERED_CALL: synthetic mark = max(0,
    strike − current_underlying) for puts (or call analog) × multiplier
    × contracts, sign-flipped because we are short. Use Black-Scholes
    if you want time value too — but intrinsic-only is a defensible
    first cut for paper.
  - For IRON_CONDOR / multi-leg: sum the synthetic legs.
  - Always populate current_price, market_value, unrealized_pnl,
    unrealized_pnl_pct so frontend never sees nulls.
- Unify the "invested" definition across topbar EquityReadout and
  CurrentlyHoldingStrip. Two options:
  - (a) Both use `equity − cash` (matches conservative risk view).
  - (b) Both sum market_value across all kinds (matches "what is on
       the book").
  Recommend (a) — it's what an operator means by "how much is at risk."
  Then add a separate "premium received" pill for short options so the
  credit is visible without being confused for invested capital.
- Frontend: when a ticker has both a stock and an option position,
  group them visually (stack the cards, label one "stock" one "short
  put"). Right now MSFT and AMD appear twice with no instrument-kind
  badge — looks like a duplicate bug.

### Why deferred

Same reason — observation period. The numbers are confusing but
correct; the books balance. Wait for the user to decide whether short
premium credits should display as "invested" (no), "cash" (yes, they
are, but that's how cash went up), or as their own line item (best).

---

## Before touching anything in this file

1. Re-read this file end-to-end. Context from this conversation may
   not be in your context window next session.
2. Check the trial progress: how many days into the 30-day trial?
   How many closed trades? What does /authority/status say about
   the 6 pillars (data, model, council, risk, execution, learning)?
3. Confirm the user still wants to proceed. They explicitly said
   "let's observe for few days I don't want to build anything now"
   when this file was created. The only active item is **#8
   Observation period** — keep adding to its observation log until
   user signals "ok let's build."
4. Sequencing rule: items that clear the data corpus must ship
   before items that learn FROM the corpus. Lessons mined from
   weekend-opened complex-MTM-broken positions will be wrong.
5. **Recommended order of work when we resume:**
   - **Wave A — clean the substrate (fixes):**
     - #3 Complex-MTM (clears display + fixes closed-trade outcomes)
     - #2 Calendar gate (clears off-hours pollution from the corpus)
     - #7 Tier 1 Options confidence (daily IV logging, chain-aware
       strike selection, document closing logic)
   - **Wave A.5 — infrastructure (parallelizable with Wave A):**
     - #9 AWS migration (EC2 + systemd + nginx + S3 backup + monitoring)
     - #10 Free data-source upgrades (Alpaca bars, Stooq fallback,
       Finnhub fundamentals, EDGAR XBRL extractor)
     - 1-week parallel paper trial on AWS before retiring laptop
   - **Wave B — build the memory layer:**
     - #1 Memory-rich agent context (passive memory)
     - #5 Per-ticker × strategy edge map (data-backed intuition)
   - **Wave C — build the learning layer:**
     - #4 Autopsy → binding lesson with deadlock guards (active
       prohibitions ride on top of #1's passive memory)
   - **Wave D — extend the perception layer:**
     - #6 Tier A gamma features (foundational + mechanics)
     - #7 Tier 2 Polygon data (gated on user approval + budget)
     - #6 Tier B/C gamma features (Vanna, Charm, 0DTE — requires #7 Tier 2)
6. Items #7 Tier 2 and #6 Tier B/C are **paid-data-gated**. Don't
   start them without an explicit Polygon (or equivalent) approval
   from the user. ~$50-200/month.
7. Every wave produces a real artifact the user can see in the UI
   before the next wave starts. Don't batch a multi-wave PR.

---

## 4. Autopsy → binding lesson (with deadlock prevention)

### The idea (user's framing)

When a trade loses badly, the autopsy shouldn't just sit in a log
file. It should become a **binding lesson** that:
- Is acknowledged (the system "owns" the mistake explicitly).
- Is always referenced — every future vote in a similar setup must
  pass through it.
- Makes the agents demonstrably more **cautious, careful, and
  responsible** — not just "trained" once and forgotten.

Quote: "the bad trades when we are doing the autopsy it should be like
acknowledgement it should be learning and always be referred so we do
not make the same mistake or learn and improve agents needs to be
cautious careful and more responsible."

User-confirmed design constraint: **strong autopsy strategy that is
ideal and not resulting in a deadlock.** The system must learn from
losses without paralysis. The framework below is designed around that
constraint — every learning mechanism has a paired un-learning
mechanism.

### The five-piece extraction loop (settled in conversation)

This is the chain that turns loss → behavior change. Every step that's
missing is where autopsy value leaks out.

```
Loss → Autopsy → Pattern signature → Hypothesis →
Backtest hypothesis against history → Validated rule →
Enforced in agent context → Measured rule-contribution →
Retired when stops paying
```

**Piece 1: Structured output, not narrative.** Each autopsy must emit:
```
pattern_signature: hash(strategy, regime_band, vix_band, dte_band, action, ticker_class)
deviation_from_base: "lost 18pp more than this signature's historical avg"
candidate_rule:      "size 0.5×" OR "block" OR "require N agent concurrence"
predicted_impact:    "if rule had been active, last 30 days +$340"
status:              draft | acknowledged | active | dormant | retired
```
Prose-only autopsies are therapy. Hashable signatures are learning.

**Piece 2: Mandatory backtest before promotion.** Before a candidate
rule becomes binding, replay it against the last N matching trades
in `closed_trades`. Promote only if the rule passes:
- Improves outcomes in ≥ 60% of replays, AND
- Sample size ≥ 5 matching historical trades, AND
- Doesn't block more than 30% of historical trades in that signature
  (the **anti-deadlock guard** — see deadlock prevention below).
Reject single-trade rules. Reject rules that would have blocked the
majority of a healthy strategy's history.

**Piece 3: Enforcement, not advisory.** Active rules go into the
agent context dict (see item 1). Each agent vote must include
`lessons_consulted: [rule_id, ...]`. If a relevant rule was active
and the agent ignored it → chairman vetoes the vote OR requires
typed `lesson_override_reason`. Three operator-tunable enforcement
levels:
- `advisory`: rule visible in context, no veto
- `soft`: rule triggers position-size modifier (0.5×, 0.7×)
- `hard`: rule blocks the action entirely
New rules start at `advisory` for 2 weeks before promoting up.

**Piece 4: Shadow A/B every rule.** When a rule activates, the engine
also computes the counterfactual ("what would we have done without
this rule?") and tracks rule contribution weekly:
- Rule helps (positive contribution, statistically) → keep
- Rule neutral → mark `dormant`, candidate for retirement
- Rule HURTS (negative contribution) → **auto-retire** and write a
  meta-lesson: "this signature didn't predict what we thought"

**Piece 5: Autopsy → feature engineering loop.** Every autopsy that
concludes "we missed X" should propose a *feature* that would have
detected X. Example: lost on a gap-down → propose `pre_market_gap`
feature in the context dict. Rules constrain behavior; features
expand perception. **This is how autopsies make agents smarter,
not just more cautious.** Critical asymmetry.

### Two contrarian additions

**Autopsy WINS too, not just losses.** Almost every retail bot only
autopsies losses → asymmetric learning → system drifts conservative
because positive feedback never registers. If a trade wins 30% above
predicted, that's also a signal. Was it the agent or luck? If
repeatable, *that's* an edge worth amplifying. Same five-piece loop,
opposite sign: produces "concentration rules" ("when X, size UP")
not just "avoidance rules."

**Operator-in-the-loop for the first ~30 days.** Auto-mining lessons
is safe (soft, decay over time). Auto-creating *binding* rules from
autopsies during a new trial is dangerous — a single anomalous loss
in week one could create a rule that hurts for months. Every
binding-rule promotion requires one click of "I agree this is real"
from the operator until ~30 closed trades. After that, autopilot
with weekly digest.

### Deadlock prevention (the user-mandated design constraint)

The single biggest failure mode of a learning system is **lesson
collapse** — every loss generates a rule, rules accumulate, system
stops trading. Five safeguards are built into the framework above
specifically to prevent this:

1. **Sample size floor (N ≥ 5).** No rule from a single loss.
2. **Coverage cap (≤ 30%).** A rule cannot block more than 30% of
   the historical trades in its signature. If it would, the signature
   is too coarse — refuse promotion, refine the signature.
3. **Tiered enforcement (advisory → soft → hard).** Every rule starts
   advisory. Promotion to `soft` requires 2 weeks of positive shadow
   A/B contribution. Promotion to `hard` requires 4 more weeks.
   Mechanical, not vibes.
4. **Auto-retirement on negative contribution.** If a rule's shadow
   A/B goes negative for 30 days → retired without operator action.
   Build the retirement path FIRST, then the creation path.
5. **Active rule budget.** Hard cap on simultaneously-`hard` rules
   (initial: 10). Once at cap, new promotions require an existing
   rule to retire first. Forces the system to prioritize.

Plus a **global circuit breaker:** if active-rule blocks ever exceed
40% of considered trades in any 7-day window → the system flags
"learning paralysis" and disables `hard` enforcement (all rules drop
to `soft`) until operator review. Prevents quiet death by
over-learning.

### Concrete schema changes (when we build)

### What we already have

- `backend/bot/autopsy/__init__.py` — runs an autopsy per closed
  losing trade. Lives in DB. Output is mostly narrative.
- `backend/bot/journal/__init__.py::build_lessons()` — mines lessons
  across closed trades by strategy × regime. Gated behind
  `ai.use_journal_lessons` (default OFF).
- `applicable_lessons(strategy, regime_trend, ...)` retrieval exists
  but is not wired into agent context (see item 1 above).
- `/autopsy` page exists in the UI.

### What we DON'T have (the gaps)

- Pattern signatures (hashable, queryable).
- Candidate rule schema with status enum.
- Backtest-before-promote infrastructure.
- Mandatory consultation field on AgentVote.
- Shadow A/B counterfactual measurement.
- Auto-retirement on negative contribution.
- Tiered enforcement (advisory → soft → hard).
- Coverage cap or active-rule budget.
- Global circuit breaker for over-learning.
- Win autopsy (only loss autopsy today).
- Autopsy → feature engineering loop.

### What to cut from the standard autopsy playbook

- **Five whys.** In practice it's "we were overconfident" five times.
  Pattern signatures beat introspection.
- **Blameless framing.** Borrowed from SRE; mostly noise here since
  no humans are pulling triggers. The agents have no feelings.
- **Long write-ups.** A 3-paragraph autopsy of every loss = nobody
  reads them. Keep them to the structured schema in Piece 1.

### Schema (when we build)

- `autopsy_acknowledgement(trade_id, pattern_signature, deviation_pp,
  candidate_rule_json, status, sample_size, last_seen, created_at)`
- `binding_rules(rule_id, pattern_signature, action_taken, enforcement
  ∈ {advisory, soft, hard}, predicted_impact_dollars, status ∈
  {draft, acknowledged, active, dormant, retired},
  shadow_contribution_dollars, blocks_count, last_blocked_at,
  promoted_at, retired_at, retired_reason)`
- `agent_vote` adds `lessons_consulted: List[rule_id]` and optional
  `lesson_override_reason: str`.
- Pattern signature function: stable hash of
  (strategy, regime_trend, vix_band, dte_band, action, ticker_class).

### Effort & order

**Effort:** Medium-large — ~3-4 days. Infrastructure exists
(autopsy table, journal mining). New work: pattern signature
function, backtest-before-promote logic, tiered enforcement at
chairman level, shadow A/B counterfactual measurement, auto-retire,
circuit breaker, UI for lessons & rules + operator promotion clicks.

**Order:** Build AFTER item 1 (memory-rich context). Item 1 gives
agents passive memory; item 4 gives them active prohibitions plus
the safety mechanisms that prevent the prohibitions from killing
the system. Passive memory must exist and be validated first.

---

## 5. Per-ticker per-strategy win-rate intelligence

### The idea (user's framing)

Some strategies likely work 80-90%+ on specific tickers (e.g., maybe
"momentum_breakout works 85% on NVDA but 40% on KO"). That knowledge
needs to be:
- Captured automatically as trades close.
- Tracked on the UI — which ticker × strategy wins most, which loses
  most.
- Used as input to the agent context so future votes weight by it.

Quote: "There might be some Strategies that are 80 to 90+% working
for a specific stock that knowledge also needs to be gathered and used
and should be tracked on UI which stock which strategy worked the
most and which got failed so it will help us very much in long run."

### What we already have

- Per-strategy aggregate stats (across all tickers) — yes, in
  `closed_trades` + journal mining.
- Cohort matrix from Stage 9 — strategy × regime cells, NOT per
  ticker.
- AgentScore tracks per-agent calibration — also not per ticker.

We do **not** have a per-(ticker, strategy) breakdown anywhere in
the DB or UI today.

### Concrete work

- New view (SQL view, not a table) `ticker_strategy_perf`:
  ```sql
  SELECT ticker, strategy,
         COUNT(*) as trades,
         SUM(pnl > 0) as wins,
         AVG(pnl) as avg_pnl,
         AVG(pnl / size_dollars) as avg_roi,
         MAX(closed_at) as last_seen
  FROM closed_trades
  WHERE status = 'closed'
  GROUP BY ticker, strategy
  HAVING trades >= 5;  -- sample-size floor
  ```
- API: `/intel/ticker_strategy?ticker=NVDA` returns ranked list,
  `/intel/strategy_ticker?strategy=momentum_breakout` returns the
  inverse view.
- UI: new page "Edge Map" — a heatmap, rows = tickers, columns =
  strategies, cell color = win rate, cell opacity = sample size
  (low samples = washed out). Click a cell → drill into the specific
  trades behind it. Filter for "only show cells with N ≥ 10".
- Wire into agent context (item 1): if a ticker × strategy has
  N ≥ 10 and win-rate ≥ 65% → emit a "specialist edge" hint;
  if win-rate ≤ 35% → emit a "specialist anti-edge" hint. The
  agent decides what to do with it. The chairman uses it for
  position-size modifier.

### My opinion (assistant)

**This is the highest-leverage of the three items.** Honest take:

(a) **Most realistic to ship.** No new ML. No new data sources. Pure
SQL aggregation over data we already have. Maybe a day's work end to
end, mostly in the UI.

(b) **But sample size will bite you.** With a 30-day trial at maybe
5-15 trades/day, you'll have ~150-400 trades total. Spread across
~7 tickers × ~10 strategies = 70 cells, most with single-digit
samples. The heatmap will look mostly empty / mostly noise for
months. Plan for that visually — wash out low-N cells aggressively,
maybe require N ≥ 10 before a cell even renders, otherwise users
will see "AAPL × momentum at 100% win-rate" based on 2 trades and
trust it.

(c) **Watch for ticker mean-reversion in performance.** A strategy
that wins 85% on NVDA in a strong-trend regime can flip to 30% the
moment the trend breaks. Always slice by (ticker, strategy, regime)
not just (ticker, strategy). The 3-dimensional cube is the right
data model — 2D is misleading.

(d) **Survivorship bias hazard.** If you remove or stop running a
strategy mid-trial, its early sample becomes uninformative. Track
"would have fired" decisions too, not just "fired and closed." We
already log decisions, so this is plumbing.

**Order:** This can be done in parallel with items 1 and 4 — they
share the same context-assembler plumbing. If you only do ONE thing,
do this one. The trader's mental model ("X strategy works on Y stock")
finally becomes data-backed.

---

## 6. Dealer positioning / options-flow knowledge layer (gamma map)

### The idea (user's framing)

The user provided a curated list of 18 foundational + 6 advanced + 8
cutting-edge concepts from the SpotGamma / MenthorQ world:

**Foundational (1-5):** Gamma Walls, Zero Gamma Flip, Call Wall,
Put Wall, Major GEX Levels.

**Mechanics (6-10):** Positive Gamma, Negative Gamma, GEX, Dealer
Hedging Flow, Unfinished Auction / Reaction at Gamma Levels.

**Advanced Greeks (11-15):** Vanna, Charm (Delta Decay), Max Gamma /
Peak Gamma, Net GEX, Dynamic Delta Hedging.

**Regime & Context (16-18):** Vol Trigger, Gamma Profile, 0DTE Flow
Impact.

**More advanced:** VEX, CEX, Zomma, DDOI, HIRO, Speed, Volatility
Surface / Skew, Color, Ultima, Vomma, Seek-and-Destroy, CHEX, DEX,
Pin Risk, Gamma Scalping, Vol Term Structure, Cross-Asset Gamma,
OI vs GEX Clustering, Variance Swaps / Vega Hedging.

Quote: "I want to check if we are using them or make a note that we
need to embed them to the knowledge base and train the models on these
before stock execution."

### What we already have (audited 2026-05-31)

Looked in `backend/bot/signals/gex.py` and confirmed:

| Concept | Status |
|---|---|
| **Call Wall** | ✅ computed (`call_wall`) |
| **Put Wall** | ✅ computed (`put_wall`) |
| **Zero Gamma Flip** | ✅ computed (`gamma_flip`) |
| **Major GEX Levels** | ✅ partial (`gex_by_strike` aggregation) |
| **Positive / Negative Gamma regime** | ✅ computed (`dealer_regime`) |
| **GEX (per-strike)** | ✅ computed |
| **Net GEX** | ⚠️ derivable from gex_by_strike but not surfaced as one number |
| **Gamma Walls (general)** | ⚠️ implicit via gex_by_strike + walls |
| **Dealer Hedging Flow** | ❌ not computed |
| **Vol Trigger** | ❌ not computed |
| **Vanna / Charm** | ❌ not computed |
| **Max Gamma / Peak Gamma** | ⚠️ derivable, not labeled |
| **Gamma Profile** | ⚠️ chart exists, not a labeled feature |
| **0DTE Flow Impact** | ❌ not computed |
| **VEX / CEX / DDOI / HIRO** | ❌ none computed |
| **Cutting-edge (Color, Ultima, Vomma, Speed, etc.)** | ❌ none |
| **Pin Risk** | ❌ not computed |
| **Cross-asset gamma (SPY ↔ SPX)** | ❌ not modeled |

Frontend page `/heatseeker` shows what we have. `/flowseeker` covers
the flow side.

### Concrete work — tiered

**Important nuance:** the user said "train the models on these." My
correction: we don't train ON definitions. We use them AS INPUTS.
Agents read these as numbers in the context dict, the same way they
read the price. The agent then learns (via calibration / lessons)
which inputs predict outcomes for which setups. So this work is
"compute and expose," not "fine-tune a model on PDF pages."

**Tier A — finish the foundational + mechanics layer (1-10):**
- Surface Net GEX as a labeled scalar (sum of GEX across chain).
- Compute Max Gamma / Peak Gamma (strike with absolute highest
  gamma concentration) as a labeled output.
- Compute Vol Trigger — SpotGamma defines it loosely as the level
  below which dealer hedging flips destabilizing. Reasonable proxy:
  current spot − distance to gamma flip × volatility scalar.
- Compute Dealer Hedging Flow as a derived feature — at minimum,
  flag direction (buying / selling / neutral) based on
  (positive gamma + price up) vs (negative gamma + price down).
- Pin Risk near expiry — distance to highest-OI strike weighted by
  DTE.

**Tier B — second-order Greeks (11-15):**
- Vanna and Charm require an IV surface. We can approximate:
  Vanna ≈ ∂(Gamma)/∂σ at each strike; Charm ≈ ∂(Delta)/∂t. Both
  need full chain IV which is the fragile bit (yfinance is the
  weak link in our data pipeline — see memory:
  [[project_trading_bot]]).
- Dynamic Delta Hedging — model the hedging flow estimate (delta
  shift × dollar gamma × estimated dealer net position).

**Tier C — regime & 0DTE (16-18):**
- Gamma Profile as a labeled curve output (we have the data;
  expose it as a vector feature, not just a chart).
- 0DTE Flow Impact — filter the chain to today's expiry, compute
  same GEX/gamma metrics, surface separately. This matters a lot
  intraday.

**Tier D — advanced (VEX, CEX, DDOI, HIRO, etc.):**
- Skip for now. SpotGamma's HIRO is proprietary and we'd need to
  build a real-time replica. VEX / CEX / DDOI are computable but
  with diminishing returns vs. the Tier A gaps. Park.

**Tier E — cutting-edge (Color, Ultima, Vomma, Speed, etc.):**
- Skip. Third-order Greeks on a yfinance-fed data layer is a
  data-quality joke. We'd be computing precision artifacts on
  imprecise inputs.

### Where it plugs in

- Each computed feature becomes a key in the `context["gex"]` /
  `context["options_flow"]` sub-dict the engine assembles before
  agent calls (item 1).
- `agent_microstructure` is the natural consumer — it should
  weight gamma walls and vol triggers heavily. `agent_market`
  uses dealer regime as a state input.
- The chairman uses gamma walls as soft stop / target hints when
  sizing.
- UI: enrich `/heatseeker` with the Tier A additions; label every
  metric with its definition (operator education).

### My opinion (assistant)

**Right direction, wrong scope.** Honest take:

(a) **The list is overwhelming on purpose.** 40+ concepts.
SpotGamma's value isn't in the count — it's in 5-6 things they
do well. Tier A covers 80% of the actionable edge for retail-scale
paper trading. Tiers D and E are noise unless we're trading SPX
0DTE size, which we are not.

(b) **Data layer is the bottleneck, not the math.** yfinance
options chains are slow, sometimes incomplete, and have stale
IVs. Computing Vanna correctly requires a clean IV surface across
the chain. Before adding Tier B/C, we need to decide: do we pay
for a real options data feed (CBOE, Polygon, ORATS), or accept
that our Greeks are approximations? **This decision gates how
deep we can go.**

(c) **Don't "train the models on these."** Use them as inputs.
The agents are already calibrated against outcomes — feed them
gamma features and the calibration will surface which features
matter for which setups. No fine-tuning needed. The LLM Chairman
(Stage 21) might want gamma context in its prompt — but that's
prompt engineering, not training.

(d) **Risk: feature explosion.** Adding 30 new features to the
agent context can overwhelm the simpler heuristic agents
(`agent_market`, `agent_microstructure`) — they'll either ignore
them or over-fit. Add features incrementally, watch which ones
actually move the chairman's vote, drop the dead ones.

**Effort:**
- Tier A: ~2 days. Mostly extending `signals/gex.py` + UI labels.
- Tier B (Vanna/Charm): ~3-5 days, GATED on data quality decision.
- Tier C (0DTE separation): ~1 day, high value intraday.
- Tier D + E: skip indefinitely.

**Order:** Do Tier A right after items 1 + 5 (memory + edge map),
because the gamma features then plug into the same context bundle.
Tier B/C are gated on the data-feed decision (see "Open questions"
below).

---

## 7. Options-trading confidence is uncalibrated (escalated gap)

### What I found auditing the live system (2026-05-31)

The 4 SELL_CSP positions tonight fired through this confidence stack:

```
Strategy confidence (e.g. 0.70 → "high")
    ↑ derived from
estimated_iv_rank  (e.g. 50)
    ↑ derived from
ATM_IV_today  (from yfinance; sometimes stale or missing)
    ↑ if missing →
hardcoded defaults: iv_rank=50, implied_move=0.07
```

Every layer hides an honest disclaimer. The dict literally returns
`iv_rank_estimated: True`. The file docstring states: *"true IV-rank
needs a year of IV history we don't have, so iv_rank here is an
estimate from the live ATM IV level."* But downstream strategies,
agents, and chairman consume the estimate as if it were the percentile.

### The specific gaps (each is a TODO)

1. **No true IV rank** (`backend/bot/data/options.py:160-165`).
   Current: linear scaling of today's ATM IV. Needed: stored
   1-year IV history + actual percentile rank. Requires either:
   - Polygon options (paid) — clean historical IV per ticker, OR
   - Persisting our own yfinance IV reads daily for a year (slow, but free).

2. **No chain-aware strike selection**
   (`backend/bot/strategies/all_strategies.py:391`). Current:
   `snap_strike(price, "put", moneyness=-0.05)` — picks 5% OTM by
   arithmetic. Doesn't read the chain to confirm the strike exists
   or has acceptable bid/ask spread. Needed: read available strikes,
   pick one with delta in target band (~0.30 for typical CSP), reject
   if spread > 5% mid.

3. **No DTE-aware expiry selection** (same file). Current: hardcoded
   `dte=30`. Doesn't query chain to find an actual 30-day expiry.
   Needed: read available expiries, pick closest to target DTE that
   meets liquidity floor.

4. **No backtest path for options.** From `backend/bot/data/options.py`
   docstring: *"historical option chains [are not available], so
   options strategies still can't be backtested — they fire live,
   not in the 1-year backtest."* So we have **zero counterfactual**
   for SELL_CSP, SELL_COVERED_CALL, IRON_CONDOR in our regime. The
   30-day trial is the first time we'll see real outcomes.

5. **No mark-to-market for `kind="complex"`** (see item 3 above —
   same root cause, surfacing again). Until fixed, we can't tell
   intraday if a short put is winning or losing.

6. **No documented closing logic** for short options. They have
   `dte=30` and a strike. Who closes them? At 50% profit? At
   200% loss? At expiry? Need to read the exit logic and confirm,
   then surface in UI.

7. **Confidence formula is uncalibrated arithmetic**:
   `conf = min(0.85, 0.6 + (iv_rank - 30) / 200)`. Linear in an
   estimate. Not anchored to historical SELL_CSP win rate, brier
   score, or per-ticker performance. A 0.70 confidence currently
   means "the strategy thinks IV is somewhat elevated," not "70%
   of comparable trades won."

### Why this matters in plain terms

For the 30-day paper trial, the system can show 4-for-4 winning short
puts and that result would tell us **almost nothing** — we wouldn't
know if we won because the strategy has edge or because IV happened
to mean-revert in our favor. Without calibrated confidence + backtest
context, every options outcome is an anecdote.

### Resolution path

**Tier 1 (free, our existing data):**
- Start logging daily ATM IV per ticker so that in 1 year we have
  real IV history. Cheap, just a cron job.
- Persist closing logic + actual exit reason for every options trade.
- Read the chain when selecting strike (use, don't ignore, available
  data).

**Tier 2 (Polygon options, paid):**
- Replace the IV rank estimator with true percentile.
- Replace the implied-move default with chain-derived value.
- Unlock options backtest (1+ year of historical chains).
- Compute Vanna/Charm honestly (item 6, Tier B).

This is now the **#1 prerequisite blocker** for trusting any option
strategy's confidence. Until Tier 1 ships, options confidence is
decoration. Until Tier 2 ships, options Greeks are approximations.

### Operator decision (2026-05-31)

User has chosen to **let options strategies run during the paper
trial** for now:

> "let it run for now as it's paper testing longer we run better we
> get to know"

> "I would observe the system as how it is doing for couple of days
> and pause the options if that is hitting hard"

Rationale: paper trial is for learning system behavior, not for proof
of edge. Even uncalibrated outcomes inform what to fix.

**Abort trigger (when to pause options mid-trial):**
- If options strategies account for ≥ 30% drawdown of the $5,000
  trial bankroll, OR
- If any single options position loses more than $500 (10% of
  bankroll), OR
- If complex-MTM stays broken AND we hit ≥ 5 short positions (then
  we genuinely don't know what we own — pause to assess), OR
- If the user judges by sight that options trades look reckless.

When any trigger fires: pause via config flag (no need to delete
strategies), document the loss, and revisit Tier 1 fixes before
re-enabling.

### Effort

- Tier 1: ~1-2 days, no new dependencies.
- Tier 2: ~3-5 days + monthly Polygon cost, GATED on user approval.

---

## 8. Observation period — moved to AWS substrate (2026-06-01)

**SUPERSEDED by operator decision on 2026-06-01:**

> "let's ship the application as we have today and observe from the
> aws and fix .. let's move out of this laptop"
>
> "I want to retire the laptop today .. maybe we readjust the clock
> starting tomorrow with same 5k and start from there"

The original 7-day local-observation gate is **dropped.** Local
substrate (MacBook) was degrading under load (bot + chat + IDE +
browser) — observation value collapses on an unstable host. The
pivot: ship as-is to AWS, restart the trial clock fresh tomorrow,
fix bugs in parallel on AWS.

### What changed

- **Old plan:** observe locally through 2026-06-07, then build Wave A.
- **New plan:** ship to AWS today (no code changes first), reset
  trial to $5,000 on AWS, observe on AWS while Wave A fixes ship
  incrementally.

### What stays

- All 10 todo items are still tracked here.
- Abort triggers (item #7) still apply.
- Operator-in-the-loop discipline still applies.
- Memory-rich context (#1), autopsy (#4), edge map (#5), gamma (#6)
  still wait for sample size.

### Day 0 on AWS (2026-06-01)

- AWS infra: bootstrap + Phase 1 + Phase 2 applied (S3, IAM, secrets,
  EC2 t4g.small + EIP).
- App: zipped, uploaded to S3, deployed via SSM, systemd unit
  installed, FastAPI on 127.0.0.1:8000.
- Access: `aws ssm start-session ... --document-name
  AWS-StartPortForwardingSession --parameters '{"portNumber":
  ["8000"],"localPortNumber":["8000"]}'` then http://localhost:8000.
- Trial clock: restarts tomorrow (2026-06-02) with fresh
  `system_reset.fresh_start()` to $5,000.

### What to check at the 1-week revisit (still on schedule for 2026-06-08)

Same checklist as before:

1. **Trial scoreboard.** Closed trade count, win rate, total P&L,
   max drawdown. If fewer than ~10 closed trades → too early to
   draw conclusions, extend observation.
2. **Pillar history.** Did any pillar drop to `mid` or `bad` during
   the week? Which one most often?
3. **Abort trigger status.** Did any of the 4 options-abort
   conditions from item #7 fire? If yes — pause options first.
4. **yfinance reliability on AWS.** Different network than home WiFi;
   may behave differently. Track failures.
5. **EC2 health.** Is t4g.small enough? Memory? CPU? journalctl
   noise? Daily systemd status.
6. **Re-rank the todo.** Re-rank before building.

### What to track each day during the observation

1. **Books balance.** Cash + Σ(stock_market_value) +
   Σ(option_mark_or_liability) should equal equity, every minute.
   We already know complex-MTM is broken — so during observation,
   audit by hand from the DB rather than trusting the UI.

2. **Weekend cycles.** Note how many cycles run on Sat/Sun and what
   actions they take. Confirms calendar gate is the right next fix
   (item 2).

3. **Options strategy hit-rate.** Track each SELL_CSP / SELL_COVERED_CALL
   open-to-close outcome. Note the iv_rank claimed at entry vs actual
   IV move during hold. First real-world data on whether the estimator
   produces sane edge.

4. **Abort triggers (see item 7).** Watch for the four conditions
   that would pause options.

5. **Pillars drift.** Daily snapshot of `/authority/status` —
   DATA / MODEL / COUNCIL / RISK / EXECUTION / LEARNING. Track which
   ones move tier most often. Tells us which pillar is most fragile
   in live conditions.

6. **Closed trades count.** This number gates item 1 (lesson mining)
   and item 5 (edge map). When it crosses ~20, we can start mining
   meaningful patterns.

### What NOT to do during observation

- Don't fix things mid-trial. Note in this file, resume on a clean
  build day.
- Don't reset state to "look better" — outcomes including the messy
  ones are the data.
- Don't add features. Every new feature changes the regime; we'd be
  observing a moving target.
- Don't intervene in trades manually unless an abort trigger fires.
  Manual interventions pollute the trial data.

### Daily 30-second check (suggested)

Open Command Center. Read three numbers:
1. Equity vs starting $5,000.
2. Number of open positions (sanity check vs cash + invested split).
3. Authority Confidence (CONFIDENT / WATCHING / RESTRICTED) and
   which pillar is lowest.

If all three look sane → close the page. If anything looks weird →
note it here under "Observation log" below.

### Observation log

(append entries here as we observe; date-stamp them)

- **2026-05-31 (Sun, Day 4):** Engine running cycles 24/7 (no calendar
  gate). 4 SELL_CSPs opened tonight (TSLA, NVDA, MSFT, AMD) — short
  premium credited cash $1,532.60. Complex-MTM showing $0 / "— mkt"
  for all 4. Books still balance to the penny on `/paper/state`
  ($5,000.00 equity). User decisions:
  - Let options run during trial; abort triggers from item #7 apply.
  - Committed to AWS migration (item #9).
  - Backlog revisit scheduled on or after 2026-06-07 (1 week).
  - During observation week, ONLY Stooq fallback (item #10) is
    candidate work — everything else pollutes trial signal.
- **2026-06-01 (Mon, Day 5):** **AWS PIVOT — bot moved off laptop.**
  Local Mac was degrading (4 stacked uvicorn processes, slowdown).
  User decision: ship as-is to AWS, retire laptop today, restart
  trial clock at $5,000 tomorrow on AWS. Wave A code fixes happen
  in parallel on AWS while observing.
  - AWS infra: bootstrap + Phase 1 + Phase 2 applied via Terraform.
  - EC2 i-0426a45181d08adff (t4g.small, ARM, 30 GB) running at EIP 32.197.70.83.
  - App deployed via SSM, systemd unit `trading-bot.service` active.
  - Secrets in Secrets Manager: anthropic-api-key, fred-api-key
    populated; alpaca-* placeholders empty.
  - `system_reset.fresh_start(5000)` ran on AWS — DB clean, cash
    $5,000, no positions.
  - Local engine processes killed; port 8000 free locally; laptop
    no longer hosts the bot.
  - Trial clock starts fresh on **2026-06-02 (Tue)** when operator
    hits Start in UI.
  - Connect to UI: **https://pillar-watch.com** (Cloudflare Tunnel +
    Access; magic-link PIN to gmail; 24h session).
  - Domain: pillar-watch.com on Cloudflare Registrar (~$10/yr).
  - Tunnel: cloudflared on EC2, outbound only, no inbound ports.
  - Access policy: only srikant.parimi@gmail.com.
  - Fallback if Cloudflare is down: SSM port-forward (plugin
    pre-installed at `/usr/local/bin/session-manager-plugin`).

### What to check at the 1-week revisit (2026-06-07)

When we resume after observation:

1. **Trial scoreboard.** Closed trade count, win rate, total P&L,
   max drawdown. If fewer than ~10 closed trades → too early to
   draw conclusions, extend observation.
2. **Pillar history.** Did any pillar drop to `mid` or `bad` during
   the week? Which one most often? Tells us which area to fix first.
3. **Abort trigger status.** Did any of the 4 options-abort
   conditions from item #7 fire? If yes — pause options first,
   discuss before further build.
4. **yfinance reliability.** How often did data fetches fail or
   return stale? Justifies (or doesn't) the Stooq fallback work.
5. **User comfort.** Is the operator (you) confident the system is
   doing sensible things? Or are there moments of "what is it
   doing?" that need explanation before we add complexity?
6. **Re-rank the todo.** Some items may have moved up/down in
   priority based on what we saw. Re-rank before building.

Only after that review → decide whether to start Wave A code fixes,
Wave A.5 infrastructure, or extend observation another week.

---

## 9. AWS migration — go-live infrastructure (committed)

### Operator decision (2026-05-31)

User committed to migrating to AWS for live trading:

> "ok I will create a AWS account and we can move there"

This replaces the local-MacBook substrate which is unsuitable for
real-money trading. The architecture itself stays the same — only
the host changes.

### Why this matters

A laptop is wrong for live money: lid closes → sleeps → no trading;
WiFi blip → no fills; macOS update reboots → silent loss of running
cycle; no backup; no failover. None of this matters in paper. All of
it matters live.

### What changes (substrate only)

- Host: MacBook → AWS EC2 (or Lightsail; cheaper)
- Process supervisor: foreground shell → systemd unit (auto-restart on crash)
- Reverse proxy: none → nginx or Caddy (HTTPS + auth on `127.0.0.1:8000`)
- Backup: none → daily SQLite snapshot to S3 (~$0.50/mo for the bucket)
- Monitoring: none → UptimeRobot health probe (free) + Telegram webhook for alerts
- Remote access: physical only → Tailscale or SSH key-only

### What does NOT change

- FastAPI + SQLite + APScheduler — fine at our scale
- Agent council architecture — fine
- React frontend — fine, served via nginx
- Single process — emphatically fine; don't pre-build microservices

### Recommended AWS shape

- **EC2 t4g.small** (2 vCPU ARM, 2 GB RAM, ~$12/mo) OR Lightsail equivalent (~$10/mo).
  ARM/Graviton is cheaper and our workload doesn't care about arch.
- **EBS gp3 20 GB** (~$1.60/mo) — plenty for SQLite + logs + 6 months of equity curves.
- **S3 standard bucket** for daily backup (~$0.50/mo at our data size).
- **Elastic IP** ($3.60/mo when attached) so the broker IP allowlist doesn't churn.
- **CloudWatch logs** (free tier covers our volume).
- Region: us-east-1 (lowest latency to most broker APIs).

**Total infrastructure cost when live: ~$15-20/month.** Data costs
(Polygon if/when we add it) will dwarf this — and that's the correct
ratio.

### Migration sequence (when we resume)

1. Spin up EC2 instance + base Python 3.11 environment.
2. `git clone` (or rsync if not git-tracked — repo is local-only per
   memory).
3. Install dependencies, run the existing test suite on the VPS to
   confirm parity (1,228 backend tests + Playwright E2E).
4. Migrate `.env` (with new ANTHROPIC + FRED keys; do NOT reuse old
   ones if exposed) + `trading_bot.db` via scp.
5. systemd unit for backend + frontend (frontend can be `npm run build`
   served by nginx static).
6. nginx reverse proxy with basic auth + Cloudflare tunnel OR direct
   HTTPS with Let's Encrypt.
7. Cron job: daily `sqlite3 .backup` + S3 sync.
8. UptimeRobot probe on `/system/warnings` (200 OK → green; non-200 → page).
9. Telegram bot for alerts (webhook from `warnings_log` + daily P&L digest).
10. **Live-fire test:** run paper trial for 1 week on AWS in parallel
    with local before turning off the laptop. Verify outcomes match.

### What to NOT migrate

- The current SQLite database state if we're starting fresh on AWS.
  Use `system_reset.fresh_start()` on the new host so trial accounting
  is clean from day 1 on the new substrate.
- Local cached options/breadth/FRED data — let the new host repopulate
  from sources. Smaller is cleaner.

### Security checklist (before going live)

- [ ] `ANTHROPIC_API_KEY` rotated (current key was set in conversation)
- [ ] `FRED_API_KEY` confirmed in `.env`, not committed
- [ ] Broker API keys in `.env`, not committed, IAM-restricted in AWS
      Secrets Manager when we go real-money
- [ ] FastAPI `bind = 127.0.0.1` enforced; nginx is the only public listener
- [ ] nginx basic auth OR Cloudflare access OR Tailscale-only — no
      open-to-internet trading UI
- [ ] AWS root account MFA on; daily-use IAM user with limited perms
- [ ] EC2 security group: port 443 only (or Tailscale)

### Order

This is **Wave A.5** — after Wave A code fixes (#2, #3, #7 Tier 1)
but before turning on real money. The VPS migration unlocks 24/7
operation; the code fixes ensure what's running is correct.

### Effort

- Initial migration + parity test: ~1 day
- Monitoring + alerts + backup wiring: ~half day
- 1-week parallel paper-trial on AWS: passive, no work

---

## 10. Free data-source upgrades (yfinance is not the only option)

### Audited reality (2026-05-31)

When I said "yfinance is the weakest link" I overstated it. We
actually have **9 data sources wired**:

| Source | What it gives | Status |
|---|---|---|
| yfinance | Prices + options + fundamentals | Primary (fragile) |
| Cboe delayed | Options Greeks | Fallback to yfinance |
| **FRED** | Fed macro data | ✅ fully wired |
| **SEC EDGAR** | 10-K/10-Q/8-K filings + transcripts | ✅ fully wired |
| **FINRA** | Short interest | ✅ wired |
| **CFTC COT** | Futures positioning | ✅ wired |
| Market Breadth | Advance/decline (derived) | ✅ wired (built on yfinance) |
| Finnhub | Alt prices/fundamentals | ⚠️ stub, mostly unused |
| Alpaca stream | Real-time prices | ⚠️ only when broker = Alpaca |

Macro (FRED), filings (EDGAR), positioning (FINRA/COT) are top-tier
free sources and we use them well. **yfinance is specifically the
weak link for options chains and equity fundamentals**, not for
everything.

### Free upgrade paths (before paying for Polygon)

| Replace | With | Cost | Win |
|---|---|---|---|
| yfinance OHLC | Alpaca historical bars | Free w/ Alpaca account | Cleaner data, broker-grade. Code partly exists in `alpaca_stream.py` |
| yfinance OHLC backup | Stooq | Free, no key, no rate limit | Reliable daily bars when yfinance flakes |
| yfinance fundamentals | Finnhub free tier (60/min) | Free with key | Stub exists in `data/finnhub.py`; just needs population |
| yfinance fundamentals (alt) | SEC EDGAR XBRL financials | Free, no key | We already pull filings; extract structured financials |
| yfinance OHLC (alt) | Tiingo free tier | Free with key | 50 calls/hour, good for daily |

### Concrete work (when we resume)

**Critical classification — pollution risk during the live trial:**

| Upgrade | Changes primary signal? | Observable in trial? | Pollutes trial? | Mid-trial safe? |
|---|---|---|---|---|
| Alpaca bars as PRIMARY OHLC | Yes — different bars reach regime detection | Tail events only | **Yes** — can't compare before/after if input data changed | ❌ NO |
| Stooq as OHLC backup | No — only activates when yfinance fails | Only if yfinance fails (need counter) | No — pure defensive fallback | ✅ **YES** |
| Finnhub fundamentals | Yes — different P/E → SELL_CSP gates differently | Yes — could change which trades fire | **Yes** — directly changes outcomes | ❌ NO |
| EDGAR XBRL extractor | No — but nothing downstream consumes it yet | **No** — produces data nothing reads | No, but no value until consumers wire | ⚠️ POINTLESS without consumers |

**The discipline:** the trial's value depends on holding the system
constant. Changing the primary data source mid-trial means in 2 weeks
we cannot answer "did behavior change because of the data swap or
because of market conditions?" The trial loses signal.

**Wave A.5 add-on (do during trial — defensive only):**
1. ✅ Add Stooq as a second backup for yfinance daily bars. ~2 hours.
   Pure reliability win, doesn't touch signal when yfinance works.

**Wave A.5 add-on (DEFER until trial closes — pollutes signal):**
2. Finish wiring Alpaca historical bars as primary OHLC source when
   broker is Alpaca; keep yfinance as fallback. Already half-wired.
   **Do not ship during trial** — changes bars feeding regime
   detection.
3. Populate `finnhub.py` to fill the fundamentals gap. ~half day.
   **Do not ship during trial** — changes which trades fire (SELL_CSP
   reads `pe_ratio` from fundamentals).
4. Build EDGAR XBRL extractor for P/E, EPS, revenue trend — replaces
   yfinance fundamentals for the heavy lifting. ~1 day. **Pointless
   without simultaneously wiring downstream consumers** — agents and
   strategies read simplified P/E/EPS only; full financial statements
   have no reader today. Bundle with item #1 (memory-rich context) so
   the consumer exists when the data lands.

**Where free runs out:**
- ❌ **Real-time options chain with full IV surface** — no good free
  source. Polygon ($30-200/mo) or ORATS or IEX Cloud are the realistic
  paid options. This is the only data we'd actually pay for.
- ❌ **Historical options chains for backtest** — same problem, same
  solution. Paid only.

### Architecture pattern (already in place)

`backend/bot/data/pipeline.py` already has the Clean → Normalize →
Validate → Enrich shape. Adding new sources is plumbing, not redesign.
Each source has a `provider` tag → fallback chain → cached → consumed
as a uniform dict by strategies.

The pattern is right. The pipe just needs more reliable input streams
on the fundamentals + options sides.

### Order

Bundle as Wave A.5 with the AWS migration (#9). Both are
infrastructure-grade work that doesn't change trading logic and
strengthens the foundation. Do them in the same week.

### Effort

- All free upgrades combined: ~2-3 days.
- No new dependencies the user has to approve (we already have
  Finnhub stub, EDGAR access, etc.).
- Polygon decision: parked until after first 30-day trial closes
  and we have real data on options performance.

---

## 11. Feature-agent enrichment (TA-Lib + FinGPT) — wait for context layer

### Operator decision (2026-06-02)

User proposed a three-agent enrichment architecture (TA-Lib for
technicals, Kronos for forecasts, FinGPT for narrative). Discussion
settled on:

- **TA-Lib swap** — yes, when we get there. Mechanical win: faster,
  more correct, replaces hand-rolled indicators in `bot/features` and
  `bot/signals`. ~1 day of work, low risk.
- **FinGPT routing for narrow tasks** — yes, when we get there. Replace
  Claude calls in news / earnings / EDGAR sentiment scoring with
  FinGPT (open-source, financial-domain fine-tune). Cost win on a
  well-defined task. ~2 days, needs CPU sidecar or batching.
- **Kronos forecast model** — **skip for now.** Worth a research spike
  later but only after the rest of the foundation is solid.

User quote: "I will find how to get good option data .. make note in
todo that we should plan and do TA-Lib swap + FinGPT routing later."

### Sequencing — do these AFTER

1. **Polygon (or equivalent) options data feed** — see item #7 Tier 2.
   Top priority. Bad option data is the actual bleeding; better agents
   on bad data don't help.
2. **Memory-rich agent context (#1)** — the `build_agent_context()`
   scaffold. TA-Lib outputs and FinGPT sentiment scores slot in as
   additive context fields, not as a new architectural layer.

Doing TA-Lib + FinGPT *before* the memory context exists would be
churn — we'd integrate them once, then refactor when the context
layer lands. Build the foundation first.

### When we get there

**TA-Lib swap (~1 day):**
- Replace `bot/features/__init__.py` indicator computations with
  TA-Lib equivalents
- Verify regression-test: same input → same output (within
  rounding) for current snapshot fixtures
- Drop hand-rolled smoothing variants

**FinGPT routing (~2 days):**
- Add `bot/ai/fingpt.py` wrapper exposing `score_sentiment(text)` →
  float in [-1, 1] and `summarize_filing(text)` → str
- Route in `bot/earnings_intel/` and `bot/data/edgar/` paths:
  swap any Claude calls for narrow tasks to FinGPT
- Keep Claude for: AI Brain (full reasoning), Meta-AI veto, council
  enrichment. Reserve Claude for tasks that need general reasoning;
  use FinGPT for domain-specific scoring
- Infrastructure: CPU is too slow for inline (~5-30s per call) —
  need either a small sidecar service with GPU, or batched offline
  scoring. Start with sidecar on the same EC2 (t4g.small has limited
  RAM; may need to bump to t4g.medium ~$24/mo) or external hosted
  inference (Hugging Face Inference API ~$0.06/hr GPU).

### What we are NOT doing

- ❌ Rebuilding the agent stack (proposed diagram would have been a
  parallel pipeline displacing existing council/chairman/risk)
- ❌ Kronos integration — research spike only, not committed
- ❌ TA-Lib + FinGPT in parallel with current code (replacing, not
  augmenting)

### Estimated impact

- TA-Lib: cleaner code, slightly faster cycles, possibly improved
  signal correctness in edge cases. No expected change in trade
  outcomes for typical setups.
- FinGPT: ~30-60% reduction in Claude API spend on news/earnings/
  EDGAR paths. No expected change in decision quality for those
  narrow tasks.
- Kronos (if pursued): unknown until backtested.

### Decision review trigger

Revisit this item AFTER:
- [ ] Polygon options data is live (or equivalent fix for option
      pricing reliability)
- [ ] Memory-rich context (`build_agent_context`) is shipped
- [ ] Trial has run ≥ 2 weeks on the new substrate without major
      data-layer fires

---

## 12. Position Management Agent — institutional-grade exit logic

### Operator proposal (2026-06-02)

User proposed replacing the current rules-only exit logic with an AI-
driven Position Management Agent that runs on every open position
each cycle. Verbatim summary of their design:

> Current model: Buy = AI, Sell = fixed rules (10%, -50%)
> Proposed: Buy = AI, Hold = AI, Exit = AI, Rules = only emergency brakes
>
> Position Management Agent runs three sub-checks per cycle:
>   - Thesis check (still valid? news aligned? forecast ok?)
>   - Trend check (momentum? regime? volatility?)
>   - Portfolio check (exposure? correlation? risk limits?)
>
> Outputs: ADD / HOLD / TRIM / EXIT — not just OPEN/CLOSE.
> Hard safety rules ONLY override on catastrophe:
>   - Stock: -18% to -25% → force exit (gap protection)
>   - Option: -25% to -30% → force exit
>   - Data anomaly → exit
>
> Feedback loop after every exit/trim:
>   trade_result → performance attribution → update Chairman weights
>                                          → update agent confidence

### Why this matters

The current `_manage_exits` in engine.py uses fixed thresholds
(5% stop / 10% take-profit for stocks, 50%/50% for options) regardless
of regime, volatility, or thesis state. Real PMs operate at the level
of "is my reason for owning this still true?" — not "did it move ±X%?".

The proposal correctly identifies three retail-bot defects:
- Static thresholds that get whipsawed by noise
- All-or-nothing position sizing (no scale-in / trim)
- Entry thesis never re-evaluated

### My pushback / design refinements

**1. Cost discipline is critical.** Calling Claude every cycle on every
position = ~$700/day at 5 positions / 30s cycles. Needs:
- Tiered cadence: every cycle for fresh positions (first 24h);
  every 5-10 cycles for stable; immediate-trigger on news / >2%
  move / regime change.
- Tiered models: Haiku 4.5 or FinGPT for routine "thesis still
  intact?" checks; Sonnet only when something has changed.
- Caching: 30-60s verdict cache to avoid re-calling on identical state.

**2. Decision instability (the "flicker" problem).** AI agents looking
at noisy 30s snapshots can oscillate ADD/HOLD/TRIM/EXIT. Mitigations:
- Minimum-hold-period after a decision (lock for N minutes).
- Confidence threshold — only act above bar.
- Smoothing — require same recommendation across 2-3 consecutive
  cycles before acting.

**3. Three layers, not two.** User proposed AI + emergency-brake. I'd
argue 3 layers is more robust:
- **Layer 1 (catastrophe):** hard rules. Data anomaly, -25%/-30%,
  0-DTE on options. No AI input.
- **Layer 2 (deterministic intelligence):** trailing stops (3% from
  peak), volatility-scaled thresholds (5% in low-vol, 12% in
  high-vol), max-hold guard (close stocks after 30 days of chop).
  Rules-aware-of-context. Fires before Layer 3.
- **Layer 3 (AI Position Management Agent):** thesis/trend/portfolio
  judgment. Fires only when L2 hasn't already triggered.

L2 covers ~80% of routine decisions cheaply and debuggably. L3
focuses expensive cycles on the ~20% where judgment matters.

**4. Schema additions REQUIRED (not optional).** To do thesis-check
the position must store the entry thesis at open-time:
```
position.entry_thesis: {
  catalyst: "Computex N1X announcement",
  expected_holding: "3-5 days",
  invalidation: "close below $215 OR Computex narrative reverses",
  features_at_entry: {RSI: 67, MACD: 4.2, regime: "...", ...},
  expected_move_pct: 5.5,
}
```
Without this, the thesis-check agent has nothing to compare against.

**5. Feedback loop is the crown jewel.** This is what turns the system
from "fancier exits" into a learning system. Without it: marginal.
With it: per-agent calibration tunes over time based on exit
outcomes. Hooks into existing AgentScore + cohort_matrix machinery.

### Sequencing — strict order

This is a 2-3 week project done right. Skip steps at your peril:

| Wave | Item | Why this order |
|---|---|---|
| 0 | Polygon options data | GIGO — AI on bad option pricing perpetuates the AAPL CALL disaster |
| 1 | Memory-rich context (#1) + entry-thesis schema | Position-management-agent reads this. Build substrate first. |
| 2 | Layer 2: trailing stops + vol-scaled thresholds (deterministic) | Captures 80% of value, deterministic, easy to debug. Ship and live with it 1 week. |
| 3 | Layer 1: tighten catastrophe rules to -18%/-25% etc. | Rebuilds safety net UNDER the AI layer. |
| 4 | Layer 3: Position Management Agent | Thesis-check + tiered cadence + minimum-hold + confidence threshold. Fire only on L2-survivors. |
| 5 | Feedback loop into Chairman weights + agent confidence | Now there are exit outcomes to learn from. Loop closes. |

### Estimated impact (when shipped, in order)

| Wave | Direct impact |
|---|---|
| 0 (Polygon) | Eliminates phantom option losses like the AAPL CALL -$711 |
| 1 (context + schema) | Foundation only — no behavior change |
| 2 (L2 rules) | ~30-50% reduction in stop-out frequency from noise. Better trail capture on winners. |
| 3 (L1 catastrophe) | Negligible day-to-day; protects against tail gap events |
| 4 (L3 agent) | Real PM-style trim/add decisions. Bigger wins held longer; broken theses cut earlier. Hard to quantify pre-trial. |
| 5 (feedback) | Compounding — agent quality improves with each closed trade |

### Schema additions (when we get there)

- `paper_positions.entry_thesis JSON` — catalyst, expected_holding,
  invalidation, features_at_entry, expected_move_pct
- New table `position_decision_log` — per-cycle ADD/HOLD/TRIM/EXIT
  decisions with reason + features + outcome (tagged later by
  exit result)
- `chairman_weights` — tunable weights for thesis-check / trend-check /
  portfolio-check sub-agents; updated by feedback loop

### What we are explicitly NOT doing

- ❌ Building this before Polygon options data lands
- ❌ Replacing rules with AI-only (3-layer hybrid is the right answer)
- ❌ Running AI on every cycle for every position without cadence discipline
- ❌ Shipping without the feedback loop (the loop IS the institutional-grade part)

### Decision review trigger

Revisit this item ONLY when:
- [ ] Polygon options data is live AND trial has 2+ weeks of clean
      data
- [ ] Memory-rich context (#1) is shipped with `build_agent_context()`
- [ ] User has reviewed Anthropic spend trajectory and confirmed
      budget for L3 agent (tiered cadence costs ~$30-100/month
      depending on position count + market activity)

### ⚠️ Critical design tie-in with #13 (Claude dependency reduction)

**This item MUST be designed together with #13. They are coupled.**

The Position Management Agent (#12 L3) is potentially the single
biggest new Claude caller in the system. If we ship #12 with Claude
hard-wired into the L3 agent, then turn around and try to do #13
(Claude dependency reduction), we'll refactor #12.

Design rule for #12 L3:
- **Model-agnostic interface.** The L3 agent gets a `model` parameter
  (string or callable) — Claude Sonnet today, FinGPT/Haiku/local tomorrow.
- **Structured I/O.** Input = canonical context dict (from #1). Output =
  one of {ADD, HOLD, TRIM, EXIT} with confidence + reason. No
  free-form prose dependency.
- **Replaceable per cadence tier.** Routine cycles can use cheap local
  model; "thesis changed" cycles use Sonnet; catastrophic-risk cycles
  could use Opus or just Layer-1 rules.
- **Tests written against fixtures, not against Claude.** So #13 can
  swap the model without breaking the test suite.

If #12 ships before this discipline lands, the cost trajectory makes
#13 hard. Design accordingly from day one.

---

## 13. Claude dependency reduction — move from "AI for everything" to "Claude for the 1-5% that matters"

### Operator decision (2026-06-02)

User confirmed: "we need to do this and move towards independent
system less relying on external LLM's." Goal is to reduce Claude API
calls from ~5,000+/day (current ceiling) to ~5-30/day. Spend from
$20-100/month → ~$3-10/month. **More importantly:** architectural
independence from a single external LLM vendor.

### Why this matters (not just cost)

| Concern | Today (Claude-heavy) | Target (1-5%) |
|---|---|---|
| **Vendor dependency** | Anthropic outage = bot offline | Local rules + models keep running |
| **Rate limits** | Hit on burst cycles | Not applicable to local models |
| **Latency** | 1-5s per call | 50-300ms for FinGPT / instant for rules |
| **Determinism for backtest** | Claude is non-deterministic | Rules + calibration reproduce |
| **Privacy** | Data leaves AWS to Anthropic | All processing local on EC2 |
| **Debuggability** | "Read prose, guess intent" | Step through rules, audit calibration |
| **Cost predictability** | Spiky ($20-700/month) | Flat ($5-15/month) |

This is **architectural maturity**, not just cost optimization.
Trading systems that depend on a single external LLM for core
decisions are fragile in ways that only show up at scale or during
edge events.

### Target end-state

Claude reserved for the **1-5% of cases where breadth of reasoning
genuinely beats specialized models**:

✅ **KEEP Claude for:**
- Chairman authority on high-stakes decisions (Stage 21, eval-gated)
- Hard thesis re-evaluation (#12 L3 agent, when thesis-state has
  flipped or features are wildly off baseline)
- Autopsy generation on losses ≥ $X (operator-tunable)
- Edge cases the rule-based system flags as "no good answer"
- Chat copilot (operator-facing, on-demand)

❌ **REMOVE / REPLACE Claude in:**
- AI Brain autonomous trader (already toggle-able OFF; default OFF;
  retire fully once council + rules prove out)
- Meta-AI veto (council ABSTAIN gate + heuristics replace it)
- Council Claude enrichment (already optional, off by default)
- Sentiment scoring (news, earnings, EDGAR) → FinGPT
- Research digest summarization → FinGPT or template-based
- Trade memo generation → template + structured data from features
- Routine position management → Layer 1 + Layer 2 rules from #12

### Replacement layer

| Replacing Claude with... | For these tasks | Why |
|---|---|---|
| **Rule strategies + 5-agent council** | Routine entry decisions | Already exists; works; just needs calibration trust |
| **TA-Lib** | Technical indicators (replaces hand-rolled + any Claude analysis of price action) | Canonical, fast, correct |
| **FinGPT** | Sentiment, narrative parsing, earnings analysis | Financial-domain fine-tune; CPU-runnable |
| **Calibrated probability + ranker** | Win probability estimation | Already exists in `bot/probability/`, `bot/ranker/` |
| **Cohort matrix + scorecard** | Per-strategy/per-regime confidence | Already exists in `bot/cohort_matrix.py`, `bot/agents/scorecard.py` |
| **Deterministic L1/L2 rules** | 80% of exit decisions | See #12 |
| **Local LLM (optional Llama 3.1 / Mistral)** | Chat copilot if we want full LLM behavior locally | Operational cost vs convenience trade-off |

### Sequencing — strict order

This is NOT a "rip out Claude" sprint. It's incremental displacement
where each prerequisite makes the next replacement viable.

| Wave | Item | Why this order |
|---|---|---|
| 0 | **Polygon options data** (#7 Tier 2) | Foundation. Without real data, no model — Claude or otherwise — can decide well. |
| 1 | **Memory-rich context (#1)** | Gives rules/council richer features so they can plausibly replace Claude reasoning. |
| 2 | **TA-Lib swap + FinGPT routing (#11)** | Move narrow tasks off Claude. ~30-60% reduction in Claude calls on news/earnings paths. |
| 3 | **L2 deterministic exits (#12 Wave 2)** | Captures 80% of exit decisions without Claude. |
| 4 | **Audit Claude callers + retire AI Brain default-on** | Brain stays available but `brain_enabled=false` is the default. Operator opt-in only for experimentation. |
| 5 | **Retire Meta-AI** | The 5-agent council + grade floor + IV-rank gate today already cover what Meta-AI checks. Remove redundancy. |
| 6 | **Wrap remaining Claude callers in a "claude_budget" governor** | Hard cap on daily Claude spend (e.g., $5/day). Above that → fall back to rules. Safety net against runaway. |
| 7 | **Stage 21 Chairman Claude** | When we add Claude Chairman, design as **opt-in per-trade**, not per-cycle. Operator can flag a trade for Claude review; otherwise the council decides. |

### Design discipline (apply NOW, before more Claude callers ship)

To make #13 actually achievable:

1. **Every new Claude caller must be wrapped behind a "reasoner"
   interface** — `Reasoner.evaluate(context) -> StructuredOutput`.
   Reasoner can be Claude, FinGPT, local Llama, or rules. Swap by
   config, not by code.

2. **All structured outputs.** No new code path consumes Claude prose
   directly. Use JSON schema. Validate. This is what makes the model
   swap mechanical.

3. **Per-caller budget.** Each Claude caller declares its budget
   (calls/cycle, calls/day). Aggregate at engine level. Hit cap →
   fall back to non-Claude path.

4. **Backtests must run without Claude.** No test depends on a live
   Claude call. Use fixtures or deterministic stub reasoners.

5. **`ai_brain_disabled` should mean disabled.** Today there are still
   Claude code paths that fire when `brain_enabled=false` (meta,
   chat copilot, research). Clean up so the flag actually does what
   the operator expects.

### What we are explicitly NOT doing

- ❌ Removing Claude entirely. It stays for the 1-5% where it earns
  its keep.
- ❌ Pre-buying GPU compute for local LLMs. CPU FinGPT first; GPU
  only if needed.
- ❌ Refactoring callers preemptively. Wait until prerequisites are
  in place; refactor as part of each Wave.
- ❌ Compromising decision quality for cost. If FinGPT does worse on
  a task than Claude, keep Claude. The goal is "Claude where it
  matters," not "Claude never."

### Estimated impact

| Wave | Claude calls/day | Claude $/month | Notes |
|---|---|---|---|
| Today | ~5,000 (cap) | $20-100 | AI Brain on, Meta on |
| After Wave 2 (FinGPT routing) | ~3,500 | $15-60 | Narrative tasks off |
| After Wave 3 (L2 exits) | ~2,500 | $10-40 | Routine exits off |
| After Wave 4 (Brain default-off) | ~500 | $3-15 | Only Meta + chat + memo |
| After Wave 5 (Meta retired) | ~100 | $2-8 | Only chat + memo + edge cases |
| After Wave 7 (Chairman opt-in) | ~5-30 | $1-5 | Just high-stakes + autopsy |

### ⚠️ Critical design tie-in with #12 (Position Management Agent)

**This item and #12 MUST be designed together.**

#12 introduces what could be the largest single new Claude caller in
the system (L3 Position Management Agent). If #12 ships without the
model-agnostic interface discipline from #13, we'll refactor #12
later.

Design rules for #12 from day one (mirrored here for visibility):
- L3 agent uses the **reasoner interface** (#13 design rule 1)
- Output is **structured** (ADD/HOLD/TRIM/EXIT + confidence + reason),
  not prose (#13 design rule 2)
- Different cadence tiers can use different reasoners (Claude for
  thesis changes, FinGPT/Haiku for routine checks)
- Tests run on fixtures, not live Claude (#13 design rule 4)

In short: **build #12 in a way that makes #13 cheap.** Don't hardcode
Claude into any new caller.

### Decision review trigger

Revisit this item AFTER:
- [ ] Polygon options data is live
- [ ] Memory-rich context (#1) is shipped
- [ ] TA-Lib + FinGPT routing (#11) is shipped (Wave 2 of this item)

This unlocks Wave 3-7 of the displacement plan.

---

## 14. Heatseeker visual upgrade — dual-panel + per-expiry + long-gamma regime strip

### Operator decision (2026-06-02)

Reference artifact: `~/Desktop/gex_heatmap.py` (FlashAlpha matplotlib
replicator) + accompanying multi-source mockup screenshot. Operator
confirmed direction: **the Heatseeker page should look like that, with
a Long Gamma regime view alongside.**

This item is VISUALS ONLY. The data feed swap (yfinance → ThetaData)
is item-gated by the ThetaData subscription decision; this is the
React/UI work that lifts the institutional-style layout once clean
inputs are flowing.

### Scope — six visual pieces to add to Heatseeker

1. **Dual-panel layout: per-strike (left) + cumulative GEX (right).**
   Today we surface per-strike only. Cumulative makes the **gamma
   flip level** — the strike where running cumsum crosses zero —
   visually obvious. That level is the canonical institutional read
   on "where does dealer hedging regime invert."

2. **Per-expiry decomposition panel (third column).** Stacked bars
   per strike, color-coded by expiry bucket: 0DTE / 1d / 3d / weekly
   / monthly OPEX. Separates 0DTE chaos from term-structure
   positioning. Aligned with the pin-risk math we already have.
   Legend: colored swatch per bucket bottom-right.

3. **Long Gamma regime strip (header above the three panels).**
   Single horizontal panel, NOT another heatmap. Contains:
   - **Regime label:** `LONG GAMMA` (mean-reverting / dealers absorb
     moves) or `SHORT GAMMA` (trending / dealers amplify moves), with
     a confidence number.
   - **Distance to gamma flip** in points (positive/negative from
     spot).
   - **0DTE gamma share %** — how much of total gamma sits in
     today's expiry.
   - **30-day regime ribbon** — sparkline showing whether we've been
     persistently long-gamma, persistently short-gamma, or whipping.
   - **Vanna + Charm cards** — numeric values, not visuals. Surfaces
     second-order dealer-hedging pressure from IV moves / time
     decay.

4. **Wall + current-price highlights.**
   - Largest abs(GEX) strike → brightest yellow bar with thin red
     outline, bold value label.
   - Strike row matching current underlying → white box around the
     row, bold strike label.
   - Both panels show the same highlights for visual alignment.

5. **Color encoding + compact value labels.**
   - Positive GEX: teal → green → yellow gradient by abs(value) /
     max(abs(value)) in current view.
   - Negative GEX: dark blue → purple gradient (same intensity
     mapping).
   - Per-bar value labels formatted compact: `B / M / K` (e.g.
     `105.14M`, `1.23B`, `596K`). Bold on the wall strike.
   - Vertical zero-line at x=0 thin neutral.

6. **Two-source compare toggle + data lineage footer.**
   - Toggle at top of page: **Primary (ThetaData)** /
     **Compare (IBKR)**. Hidden when only one source is configured.
   - **NOT** the 6-source toggle from the screenshot — that's
     visually appealing but operationally absurd for our setup.
     Cap is 2: primary + one optional secondary.
   - Footer line below the panels: `Sources: A + B | Symbol: X |
     Price: $P | Wall: K | Fetched: ISO timestamp`. Same lineage
     stamp as our existing data-quality badges elsewhere.

### What we are explicitly NOT doing from the reference artifact

- ❌ **Using FlashAlpha as the data source.** Same trap as ORATS —
  derived-analytics vendor for math we compute ourselves. GEX
  compute stays in `backend/bot/heatseeker`; vendor just supplies
  clean raw chains. See [[reference_options_data_vendors]].
- ❌ **The 6-source data toggle.** Six vendors = six bills, six
  reconciliation paths, no interpretable disagreement signal. Cap
  at 2 real sources max.
- ❌ **Static PNG output** (matplotlib). We render in our existing
  React chart stack — same library Heatseeker already uses.
- ❌ **Hardcoded credentials** of any kind. The reference script
  has a FlashAlpha key in plaintext. All vendor keys go through AWS
  Secrets Manager.

### Sequencing — strict order

This work is **gated behind ThetaData being live.** Polishing
visualizations on top of yfinance-sourced GEX is anti-value: a
prettier chart of unreliable numbers increases trust in something
we shouldn't trust yet.

1. ThetaData Options Standard ($80/mo) wired into
   `backend/bot/data/options.py` (env flag, yfinance fallback).
2. Internal sanity layer (staleness gate, parity check, IV smile,
   self-regression) — the [[feedback_data_integrity_layer]] piece.
3. Heatseeker GEX recomputes on the cleaner inputs. Same backend
   code, better source.
4. **THEN** this item — add cumulative panel, per-expiry
   decomposition, long-gamma strip, highlights, color encoding,
   2-source toggle, lineage footer.

Doing 4 before 1-3 means we spend a week making the visual gorgeous
and still hit silent failures.

### Effort estimate

- Backend (compute additions): ~1.5 days
  - cumulative GEX (already trivial — cumsum on existing per-strike)
  - per-expiry decomposition (re-shape the chain reduction to
    preserve expiry axis instead of collapsing)
  - long-gamma regime calculator (distance-to-flip, 0DTE share,
    30-day regime trail, Vanna + Charm aggregates)
- Frontend (Heatseeker page rewrite): ~2 days
  - three-panel responsive layout
  - regime strip component
  - highlight + color + label work
  - 2-source toggle
- Tests: ~0.5 day (compute correctness + visual regression)

**Total: ~4 days of focused work, post-ThetaData.**

### Decision review trigger

Revisit when:
- [ ] ThetaData Standard is live + sanity layer is catching anomalies
- [ ] Heatseeker backend confirmed to recompute cleanly on new inputs

Then this becomes the next visible UX win — the page where the
operator looks first in the morning to read the dealer-positioning
weather.

---

## Open questions to settle before resuming this work

These are not action items — they're decisions we need before items
4/5/6 can be sized properly. Bring them up when we revisit.

1. **Options data feed.** Stay on yfinance (free, fragile, partial)
   or move to a paid feed (Polygon, ORATS, CBOE)? Gates Tier B/C of
   item 6. Estimate: $50-$200/month for Polygon options.
2. **Binding rule retirement policy.** Who/what retires a hard rule
   when markets change? Time-decay? Operator approval? Reverse-evidence
   gate? All three? (See item 4.)
3. **Heatmap sample-size floor.** What N is enough for a ticker × strategy
   cell to be trusted? 5? 10? 20? Affects both the Edge Map UI and
   how the agent context uses it. (See item 5.)
4. **Cross-ticker generalization.** If NVDA × momentum wins 85%,
   does that transfer to AMD? Same sector, different beta. We could
   add a "sector edge" view as a fallback when ticker sample is low.

---

## Out of scope here

These were discussed but are not on this list — they live elsewhere:

- Stage 21 Claude Chairman (eval-gated, needs ≥30 closed trades +
  brier_ok + ≥14 days clean shadow). Lives in the Stage roadmap, not
  this file. Item 1 above is a prerequisite for it.
- Pipeline visibility v2 (WebSocket stage events).
- Authority Level operator-managed control plane with auto-demote.
- Cohort Matrix viewer / Drift mgmt UI / Gates Catalog / Portfolio
  Optimizer tuning / Execution Costs breakdown.
