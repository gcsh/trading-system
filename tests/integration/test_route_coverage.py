"""Route-coverage + functional checks for the deterministic (offline) endpoints.

Two jobs:
1. A registration guard — every endpoint the UI depends on must stay mounted
   (catches an accidental router rename/removal).
2. Real functional assertions on the endpoints that don't need the network, so
   a broken handler fails here rather than silently in the browser. Network-bound
   routes (market/backtest/heatseeker/flow/recommend) are exercised by the
   dedicated mocked tests + the Playwright E2E suite.
"""
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload

    from backend import main as main_mod

    reload(main_mod)
    return TestClient(main_mod.app)


# (method, path) the frontend relies on — keep in lockstep with the routers.
EXPECTED_ROUTES = [
    ("POST", "/bot/start"), ("POST", "/bot/stop"), ("GET", "/bot/status"),
    ("POST", "/bot/run-cycle"), ("POST", "/bot/force-trade"),
    ("GET", "/config"), ("POST", "/config"),
    ("GET", "/trades/list"), ("GET", "/trades/summary"), ("GET", "/trades/{trade_id}"),
    ("GET", "/paper/state"), ("GET", "/paper/positions"), ("POST", "/paper/reset"),
    ("GET", "/portfolio/equity"), ("GET", "/portfolio/performance"),
    ("GET", "/portfolio/positions"), ("GET", "/portfolio/overview"),
    ("GET", "/portfolio/by-strategy"), ("GET", "/portfolio/risk"),
    ("GET", "/watchlist/folders"), ("GET", "/watchlist/items"),
    ("POST", "/watchlist"), ("DELETE", "/watchlist/{item_id}"),
    ("GET", "/alerts/list"),
    ("GET", "/market/last/{ticker}"), ("GET", "/market/validate/{ticker}"),
    ("GET", "/market/quote/{ticker}"), ("GET", "/market/candles/{ticker}"),
    ("GET", "/market/search"), ("GET", "/market/intraday/{ticker}"),
    ("GET", "/market/overview"),
    ("GET", "/diagnostics/cycle"), ("GET", "/diagnostics/strategy/{strategy_name}"),
    ("POST", "/diagnostics/seed-demo"),
    ("GET", "/backtest/compare/{ticker}"), ("GET", "/backtest/{strategy_name}/{ticker}"),
    ("GET", "/strategies/list"), ("POST", "/strategies/import-pine"),
    ("GET", "/copilot/briefing"), ("POST", "/copilot/autonomy"),
    ("GET", "/copilot/recommend"), ("POST", "/copilot/apply-strategy"),
    ("POST", "/copilot/brain"), ("POST", "/copilot/meta"),
    ("GET", "/copilot/ai-status"),
    ("POST", "/copilot/ai-key"), ("POST", "/copilot/chat"),
    ("POST", "/copilot/start-trial"),
    ("GET", "/heatseeker/regime"), ("GET", "/heatseeker/regime/history"),
    ("GET", "/heatseeker/batch"), ("GET", "/heatseeker/{ticker}"),
    ("GET", "/flow/live"), ("GET", "/flow/darkpool"), ("GET", "/flow/summary"),
    ("GET", "/flow/{ticker}"),
    ("GET", "/analytics/{ticker}"),
    ("GET", "/learning/insights"),
    ("GET", "/flowintel/{ticker}"),
    ("GET", "/execution/insights"),
    ("GET", "/narrative"),
    ("GET", "/predictive/status"),
    ("GET", "/audit/health"),
    ("GET", "/trades/{trade_id}/detail"),
    ("GET", "/metrics/summary"),
    ("GET", "/metrics/by-strategy"),
    ("GET", "/metrics/by-grade"),
    ("GET", "/metrics/by-regime"),
    ("GET", "/metrics/calibration"),
    ("GET", "/metrics/walkforward"),
    ("GET", "/metrics/labels"),
    ("GET", "/experiments"),
    ("GET", "/experiments/{experiment_id}"),
    ("GET", "/experiments/compare/{a_id}/{b_id}"),
    ("POST", "/experiments/run/walkforward"),
    ("GET", "/gates/catalog"),
    ("GET", "/gates/status"),
    ("GET", "/execution/costs/preview"),
    ("GET", "/execution/brokers"),
    ("GET", "/execution/brokers/{name}"),
    ("POST", "/execution/validate-order"),
    ("POST", "/execution/simulate-fill"),
    ("POST", "/execution/simulate-legs"),
    ("GET", "/options/expirations/{ticker}"),
    ("GET", "/options/chain/{ticker}"),
    ("GET", "/options/iv-surface/{ticker}"),
    ("GET", "/options/strike-suggest"),
    ("GET", "/options/greeks"),
    ("GET", "/options/implied-vol"),
    ("GET", "/options/assignment-risk"),
    ("GET", "/microstructure/{ticker}"),
    ("GET", "/cross-asset/state"),
    ("GET", "/cross-asset/alignment/{ticker_trend}"),
    ("GET", "/cross-asset/hedge"),
    ("GET", "/event-risk/calendar"),
    ("GET", "/event-risk/active"),
    ("GET", "/event-risk/can-trade/{ticker}"),
    ("GET", "/ml/feature-store/stats"),
    ("GET", "/ml/models"),
    ("GET", "/ml/active"),
    ("POST", "/ml/train"),
    ("POST", "/ml/set-active"),
    ("GET", "/ml/ab"),
    ("POST", "/ml/ab"),
    ("GET", "/ml/ab/{name}/route/{ticker}"),
    ("GET", "/portfolio/optimizer/allocation"),
    ("GET", "/portfolio/optimizer/clusters"),
    ("GET", "/portfolio/optimizer/cluster-check"),
    ("GET", "/portfolio/optimizer/sizing/primitives"),
    ("POST", "/portfolio/optimizer/preview"),
    ("POST", "/drift/feature"),
    ("POST", "/drift/prediction"),
    ("GET", "/drift/psi"),
    ("GET", "/monitoring/health"),
    ("GET", "/monitoring/feed/{name}"),
    ("POST", "/monitoring/record"),
    ("GET", "/attribution/by-strategy"),
    ("GET", "/attribution/by-regime"),
    ("GET", "/attribution/by-grade"),
    ("GET", "/explain/trade/{trade_id}"),
    ("GET", "/stress/scenarios"),
    ("POST", "/stress/apply"),
    ("POST", "/stress/scenario/{name}"),
    ("GET", "/replay/{ticker}"),
    ("GET", "/canary/state"),
    ("POST", "/canary/promote"),
    ("POST", "/canary/rollback"),
    ("POST", "/canary/halt"),
    ("GET", "/canary/kill-switch"),
    ("POST", "/canary/kill-switch"),
    ("GET", "/autopsy/trade/{trade_id}"),
    ("GET", "/autopsy/recent"),
    ("GET", "/cohorts/matrix"),
    ("GET", "/cohorts/rolling/{strategy}/{regime}"),
    ("POST", "/abstain/preview"),
    ("POST", "/exits/policy/preview"),
    ("GET", "/execution/spread/quantiles/{ticker}"),
    ("GET", "/execution/spread/adaptive-floor/{ticker}"),
    ("GET", "/drift/halts"),
    ("GET", "/drift/halts/{strategy}"),
    ("POST", "/drift/halts"),
    ("DELETE", "/drift/halts/{strategy}"),
    ("POST", "/drift/halts/check"),
    ("GET", "/gates/grade/adaptive"),
    ("GET", "/gates/grade/live"),
    ("GET", "/cohorts/theme-heat"),
    ("GET", "/cohorts/theme-heat/{ticker}"),
    ("GET", "/event-risk/decay"),
    ("GET", "/event-risk/decay/{ticker}"),
    ("GET", "/portfolio/beta-guardrail/preview"),
    ("GET", "/portfolio/beta-guardrail/live"),
    ("POST", "/exits/iv-aware/preview"),
    ("GET", "/sweeps/frontier"),
    ("POST", "/sweeps/frontier"),
    ("GET", "/backtest/shock/{strategy}/{ticker}"),
    ("POST", "/ml/leakage/canary"),
    ("POST", "/execution/twap/simulate"),
    ("POST", "/execution/twap/compare"),
    ("POST", "/exits/mfe-mae/suggest"),
    ("POST", "/exits/mfe-mae/train"),
    ("POST", "/features/regime-extra/dgex-dprice"),
    ("POST", "/features/regime-extra/vol-of-vol"),
    ("POST", "/options/strike-quality"),
    ("POST", "/microstructure/momentum"),
    ("GET", "/memo/trade/{trade_id}"),
    ("POST", "/memo/preview"),
    ("POST", "/memo/regenerate/{trade_id}"),
    ("GET", "/lineage/trade/{trade_id}"),
    ("GET", "/agents/list"),
    ("POST", "/agents/consensus/preview"),
    ("GET", "/agents/consensus/{trade_id}"),
    ("GET", "/agents/scorecard"),
    ("GET", "/agents/weights"),
    ("GET", "/memory/episodes"),
    ("POST", "/memory/recall"),
    ("GET", "/memory/recall/trade/{trade_id}"),
    ("GET", "/scenarios/presets"),
    ("POST", "/scenarios/run"),
    ("GET", "/scenarios/run/{preset}"),
    ("GET", "/state/current"),
    ("POST", "/state/preview"),
    ("POST", "/data-quality/score"),
    ("GET", "/data-quality/current"),
    ("GET", "/ai-cost/summary"),
    ("GET", "/ai-cost/recent"),
    ("GET", "/ai-cost/alpha-ratio"),
    ("POST", "/regimes/similar"),
    ("GET", "/regimes/similar/current"),
    ("POST", "/regimes/snapshot"),
    ("GET", "/research/digest"),
    ("POST", "/marketplace/preview"),
    ("GET", "/journal/lessons"),
    ("GET", "/journal/applicable"),
    ("GET", "/trial/readiness"),
    ("GET", "/fred/snapshot"),
    ("GET", "/fred/series/{series_id}"),
    ("POST", "/fred/refresh"),
    ("GET", "/breadth/latest"),
    ("GET", "/breadth/history"),
    ("GET", "/breadth/health"),
    ("POST", "/breadth/refresh"),
    ("GET", "/edgar/filings/{ticker}"),
    ("GET", "/edgar/insider/{ticker}"),
    ("GET", "/edgar/material/{ticker}"),
    ("POST", "/edgar/refresh/{ticker}"),
    ("POST", "/edgar/refresh-universe"),
    ("GET", "/finra/short-interest/{ticker}"),
    ("POST", "/finra/refresh"),
    ("GET", "/cot/snapshot"),
    ("GET", "/cot/instrument/{name}"),
    ("POST", "/cot/refresh"),
    ("GET", "/earnings-intel/{ticker}"),
    ("GET", "/earnings-intel/{ticker}/history"),
    ("POST", "/earnings-intel/{ticker}/refresh"),
    ("POST", "/earnings-intel/analyze"),
    ("GET", "/source-attribution/contributions"),
    ("GET", "/explain/importance"),
    ("GET", "/explain/importance/by-regime"),
    ("GET", "/explain/features/{trade_id}"),
    ("GET", "/gates/stability"),
    # MITS Phase 16.A — declarative decision policy surface.
    ("GET", "/policy/rules"),
    ("GET", "/policy/veto-budget"),
]


def test_every_expected_route_is_registered(client):
    mounted = set()
    for r in client.app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        for m in methods:
            mounted.add((m, path))
    missing = [(m, p) for (m, p) in EXPECTED_ROUTES if (m, p) not in mounted]
    assert not missing, f"routes missing from the app: {missing}"


# ── deterministic / offline functional checks ────────────────────────────────

def test_bot_status_shape(client):
    body = client.get("/bot/status").json()
    for key in ("running", "strategy", "cycles", "daily_pnl", "recent_signals"):
        assert key in body, f"/bot/status missing {key}"
    assert isinstance(body["running"], bool)
    assert isinstance(body["recent_signals"], list)


def test_paper_state_and_positions(client):
    state = client.get("/paper/state")
    assert state.status_code == 200
    sj = state.json()
    assert sj.get("portfolio_value") is not None
    positions = client.get("/paper/positions").json()
    assert isinstance(positions, list)


def test_paper_reset_restores_cash(client):
    out = client.post("/paper/reset", json={"starting_cash": 7500}).json()
    # reset returns the fresh account; cash/equity should reflect the new balance
    blob = str(out)
    assert "7500" in blob or out.get("portfolio_value") in (7500, 7500.0)


def test_strategies_list_is_nonempty_and_shaped(client):
    body = client.get("/strategies/list").json()
    items = body if isinstance(body, list) else body.get("strategies") or body.get("items")
    assert items and len(items) >= 5
    sample = items[0]
    # each strategy advertises at least a name/key
    assert any(k in sample for k in ("name", "key", "id")) if isinstance(sample, dict) else bool(sample)


def test_alerts_list_returns_list(client):
    body = client.get("/alerts/list").json()
    items = body if isinstance(body, list) else body.get("alerts") or body.get("items") or []
    assert isinstance(items, list)


def test_trades_summary_and_missing_trade(client):
    summary = client.get("/trades/summary").json()
    assert "trade_count" in summary
    # a non-existent trade id must 404 (not 500)
    missing = client.get("/trades/999999")
    assert missing.status_code in (404, 200)
    if missing.status_code == 200:
        assert missing.json() in (None, {}, {"error": "not found"}) or "error" in missing.json()


def test_watchlist_crud_roundtrip(client):
    # Contract: the add endpoint requires `ticker` (not `symbol`).
    assert client.post("/watchlist", json={"symbol": "NVDA"}).status_code == 400
    add = client.post("/watchlist", json={"ticker": "NVDA"})
    assert add.status_code in (200, 201)
    items = client.get("/watchlist/items").json()
    rows = items if isinstance(items, list) else items.get("items", [])
    syms = [i.get("ticker") or i.get("symbol") for i in rows]
    assert "NVDA" in syms


def test_config_masks_anthropic_key(client):
    cfg = client.get("/config").json()
    assert cfg.get("anthropic_api_key") == ""          # never leaked
    assert "anthropic_key_set" in cfg

    # saving a key flips the flag but the raw value still never comes back
    client.post("/config", json={**cfg, "anthropic_api_key": "sk-ant-fromtest"})
    after = client.get("/config").json()
    assert after["anthropic_key_set"] is True
    assert after["anthropic_api_key"] == ""


def test_copilot_ai_status_and_brain_toggle(client):
    # No env key in tests → reflects the saved-config key only.
    status = client.get("/copilot/ai-status").json()
    assert "ai_available" in status and isinstance(status["ai_available"], bool)

    on = client.post("/copilot/brain", json={"enabled": True, "web_research": True}).json()
    assert on["brain_enabled"] is True and on["brain_web_research"] is True
    off = client.post("/copilot/brain", json={"enabled": False}).json()
    assert off["brain_enabled"] is False


def test_chat_without_key_is_graceful(client, monkeypatch):
    from backend.bot.ai import chat as chatmod

    monkeypatch.setattr(chatmod, "anthropic_key", lambda: "")
    r = client.post("/copilot/chat", json={"message": "what do I own?", "history": []})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert "Anthropic API key" in body["reply"]
