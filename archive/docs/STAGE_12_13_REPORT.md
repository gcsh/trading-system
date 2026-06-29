# Stage 12 + 13 — Self-Aware Trading OS

**Implementation date:** 2026-05-30
**Status:** all 9 items shipped, full test suite green
**Test delta:** 950 → 1029 (+79 net new tests across both stages)

---

## Quick reference: what changed and where

### Stage 12 — observability + self-evaluation

| Item | Module | Endpoints |
|---|---|---|
| A1 Agent Scorecards | `backend/bot/agents/scorecard.py` | `GET /agents/scorecard`, `GET /agents/weights` |
| A2 Devil's Advocate (8th agent) | `backend/bot/agents/__init__.py::agent_devils_advocate` | (in `/agents/list`, `/agents/consensus/preview`) |
| A3 Unified MarketState | `backend/bot/state/__init__.py` | `GET /state/current`, `POST /state/preview` |
| B4 Data Quality Score | `backend/bot/data_quality/__init__.py` | `POST /data-quality/score`, `GET /data-quality/current` |
| B6 AI Cost Telemetry | `backend/bot/ai_cost/__init__.py` | `GET /ai-cost/summary`, `GET /ai-cost/recent`, `GET /ai-cost/alpha-ratio` |
| C7 Abstain-as-Probability | `Consensus.probs` field on `bot/agents/__init__.py::aggregate` | (in `/agents/consensus/*` responses) |

### Stage 13 — research + selection

| Item | Module | Endpoints |
|---|---|---|
| C5 Regime Snapshot table + similarity | `backend/models/regime_episode.py`, `backend/bot/regime_similarity/__init__.py` | `POST /regimes/similar`, `GET /regimes/similar/current`, `POST /regimes/snapshot` |
| C9 Research Layer ("what changed today") | `backend/bot/research/__init__.py` | `GET /research/digest` |
| D10 Decision Marketplace (gated) | `backend/bot/marketplace/__init__.py` | `POST /marketplace/preview` |

---

## How to enable D10 in the engine

D10 ships as a fully-functional *evaluator* available via the endpoint. Engine integration ("collect candidates → score → select → execute") is **intentionally deferred** as a Stage-14 follow-up so the legacy per-ticker flow remains the runtime default. This matches the "no behavior change" commitment.

To use the marketplace today, POST to `/marketplace/preview` with a list of candidate signals + capital constraint. The endpoint returns the chosen subset with full rationale for each rejection.

When you're ready to put it in the engine: wire a new `run_cycle_marketplace()` path inside `bot/engine.py` that:
1. Iterates tickers and collects events *without executing*
2. Synthesizes each viable event into a `Candidate` via `marketplace.candidate_from(...)`
3. Calls `marketplace.select(candidates, capital_available=...)`
4. Only fires the selected subset (existing `_persist_trade` + executor path)

Gate it behind a config flag like `ai.marketplace_enabled` (default false) so it's opt-in per environment.

---

## Engine integration that DID land

The engine already consumes some of the new modules at decision time:

- `bot/state.set_latest(...)` is called in `_persist_trade` so `/state/current` reflects the most recent cycle's MarketState
- `market_state` is now persisted into `Trade.detail_json` alongside `consensus`, `memory`, `memo` — lineage surfaces it
- Anthropic-using modules (`memo`, `narrative`, `meta_ai`, `ai/brain`, `ai/chat`) now call `ai_cost.record_from_response(...)` after every successful API call — `/ai-cost/summary` accumulates spend automatically

---

## Safety invariants preserved

- Paper mode remains the default
- Server still bound to 127.0.0.1
- No destructive SQL — `regime_episode_snapshots` is new + additive; `_auto_migrate` handles column adds for existing tables
- Anthropic SDK is not monkey-patched — cost tracking is explicit at each call site
- All new gates / abstain logic ship behind config or threshold checks; existing behavior matches bit-for-bit when telemetry isn't acted on

---

## Bugs fixed along the way

1. **vol_phase claimed "compressing" on empty data** — added "no inputs → neutral" guard in `bot/state._vol_phase`
2. **Devil's Advocate voted HOLD on empty context, dropping the abstain ratio below threshold** — devils_advocate now abstains when no analytics exist at all (absence of evidence → red-team passes)
3. **Pre-existing 7-agent count assumptions in `test_stage11_agents.py`** — updated 4 assertions to expect 8 agents (`market`, `flow`, `options`, `macro`, `risk`, `portfolio`, `execution`, `devils_advocate`)

---

## TODOs recorded (for v2 / Stage 14)

- **Dynamic vote weighting in `aggregate()`** — `vote_weights()` already derives per-agent weights from the scorecard. Wire them into the consensus engine once we have ≥30 closed trades to validate.
- **Regime snapshot scheduled job** — `snapshot_current()` is hand-callable; add a 15-min cron in `BotScheduler` to auto-capture during market hours.
- **Forward-outcome backfill** — `RegimeEpisodeSnapshot.fwd_1d_return` / `fwd_trades_*` columns are written as 0 and need a daily backfill job that walks closed trades and credits the matching snapshot.
- **Research digest scheduled job + push** — `/research/digest` is pull-only today; add a daily cron + optional Slack/email push.
- **D10 engine integration** — wire `marketplace.select()` into `run_cycle` behind `ai.marketplace_enabled`. Architectural change; ship in isolation.
- **Confidence decomposition surface** — the data exists (agent votes, probability.components); add a unified `/confidence/decompose/{trade_id}` endpoint and a Mission Control panel.
- **Mission Control UI panels for new surfaces** — Agent Scorecards, Data Quality, AI Cost, Research Digest — endpoints are live, UI panels not yet built.

---

## What you can do right now to validate

```bash
# All endpoints respond on the running backend
curl http://127.0.0.1:8000/agents/scorecard | jq
curl http://127.0.0.1:8000/agents/list | jq          # 8 agents now
curl http://127.0.0.1:8000/state/current | jq
curl http://127.0.0.1:8000/data-quality/current | jq
curl http://127.0.0.1:8000/ai-cost/summary | jq
curl 'http://127.0.0.1:8000/regimes/similar/current?k=5' | jq
curl http://127.0.0.1:8000/research/digest | jq

# Try the marketplace
curl -X POST http://127.0.0.1:8000/marketplace/preview \
  -H 'Content-Type: application/json' -d '{
    "candidates":[
      {"ticker":"NVDA","stop_pct":3.0,"take_profit_pct":10.0,"probability":0.65,"capital_required":500},
      {"ticker":"AMD","stop_pct":5.0,"take_profit_pct":4.0,"probability":0.50,"capital_required":500}
    ],
    "capital_available":750,"max_positions":5
  }' | jq
```

Mission Control (`/mission-control`) already shows agent consensus including the new `devils_advocate` agent + the `probs` three-way distribution. The Trade Memo, Memory Recall, Feature Attribution, and Decision Lineage panels render with the new MarketState stage in the lineage when the engine has run a cycle.

---

## Test growth by stage

| Sweep checkpoint | Test count | Delta |
|---|---|---|
| End of Stage 11.8 | 950 | — |
| End of Stage 12 (A1-A3, B4, B6, C7) | 1007 | +57 |
| End of Stage 13 (C5, C9, D10) | 1029 | +22 |

All sweeps exit clean (exit code 0).

---

## File-by-file diff summary

**New modules:**
- `backend/bot/agents/scorecard.py`
- `backend/bot/state/__init__.py`
- `backend/bot/data_quality/__init__.py`
- `backend/bot/ai_cost/__init__.py`
- `backend/bot/regime_similarity/__init__.py`
- `backend/bot/research/__init__.py`
- `backend/bot/marketplace/__init__.py`
- `backend/models/regime_episode.py`

**New routes:**
- `backend/api/routes/state.py`
- `backend/api/routes/data_quality.py`
- `backend/api/routes/ai_cost.py`
- `backend/api/routes/regime_similarity.py`
- `backend/api/routes/research.py`
- `backend/api/routes/marketplace.py`

**Edited (additive only):**
- `backend/bot/agents/__init__.py` — added Devil's Advocate, `probs` field, `_three_way_probs()`
- `backend/api/routes/agents.py` — added scorecard + weights routes
- `backend/bot/memo/__init__.py` — added `record_from_response()` call
- `backend/bot/narrative/__init__.py` — added `record_from_response()` call
- `backend/bot/meta_ai/__init__.py` — added `record_from_response()` call
- `backend/bot/ai/brain.py` — added `record_from_response()` call
- `backend/bot/ai/chat.py` — added `record_from_response()` call
- `backend/bot/engine.py` — `build_market_state()` + `set_latest()` per cycle; `market_state` in `detail_json`
- `backend/bot/lineage/__init__.py` — surfaces `market_state` as a new stage
- `backend/main.py` — registers 6 new routers
- `backend/db.py` — registers `regime_episode` model
- `tests/integration/test_route_coverage.py` — added 12 new endpoints
- `tests/unit/test_stage11_agents.py` — updated 4 assertions for 7→8 agents

**New tests:**
- `tests/unit/test_stage12_scorecard.py` (21)
- `tests/unit/test_stage12_devils_advocate.py` (7)
- `tests/unit/test_stage12_state.py` (12)
- `tests/unit/test_stage12_data_quality.py` (8)
- `tests/unit/test_stage12_ai_cost.py` (15)
- `tests/unit/test_stage12_three_way_probs.py` (8)
- `tests/unit/test_stage13_regime_similarity.py` (16)
- `tests/unit/test_stage13_research.py` (4)
- `tests/unit/test_stage13_marketplace.py` (12)

---

That's all 9. Welcome back.
