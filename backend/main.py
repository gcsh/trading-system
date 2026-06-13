"""FastAPI entrypoint: wires routes, WebSocket log stream, scheduler, and engine."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Set

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes import agents as agents_routes
from backend.api.routes import ai_cost as ai_cost_routes
from backend.api.routes import authority as authority_routes
from backend.api.routes import alerts as alerts_routes
from backend.api.routes import analytics as analytics_routes
from backend.api.routes import audit as audit_routes
from backend.api.routes import backtest as backtest_routes
from backend.api.routes import bot as bot_routes
from backend.api.routes import breadth as breadth_routes
from backend.api.routes import cot as cot_routes
from backend.api.routes import earnings_intel as earnings_intel_routes
from backend.api.routes import edgar as edgar_routes
from backend.api.routes import finra as finra_routes
from backend.api.routes import fred as fred_routes
from backend.api.routes import config as config_routes
from backend.api.routes import copilot as copilot_routes
from backend.api.routes import data_quality as data_quality_routes
from backend.api.routes import decision as decision_routes
from backend.api.routes import diagnostics as diagnostics_routes
from backend.api.routes import execution as execution_routes
from backend.api.routes import execution_costs as execution_costs_routes
from backend.api.routes import experiments as experiments_routes
from backend.api.routes import explain as explain_routes
from backend.api.routes import flowintel as flowintel_routes
from backend.api.routes import gates as gates_routes
from backend.api.routes import journal as journal_routes
from backend.api.routes import knowledge as knowledge_routes
from backend.api.routes import lake_status as lake_status_routes
from backend.api.routes import theories as theories_routes
from backend.api.routes import thesis as thesis_routes
from backend.api.routes import detectors as detectors_routes
from backend.api.routes import detector_scorecard as detector_scorecard_routes
from backend.api.routes import brain_scorecard as brain_scorecard_routes
from backend.api.routes import analysis as analysis_routes
from backend.api.routes import strategy_matrix as strategy_matrix_routes
from backend.api.routes import tomorrow as tomorrow_routes
from backend.api.routes import prediction_outcomes as prediction_outcomes_routes
from backend.api.routes import retrospective as retrospective_routes
from backend.api.routes import trial as trial_routes
from backend.api.routes import trial_scorecard as trial_scorecard_routes
from backend.api.routes import flowseeker as flowseeker_routes
from backend.api.routes import heatseeker as heatseeker_routes
from backend.api.routes import iv_regime as iv_regime_routes
from backend.api.routes import gate_diagnostics as gate_diagnostics_routes
from backend.api.routes import pricing_telemetry as pricing_telemetry_routes
from backend.api.routes import divergence as divergence_routes
from backend.api.routes import learning as learning_routes
# MITS Phase 18-FU Gap 4 — flag-gated historical-replay backfill route.
# Lives in a separate module from learning_routes so Stream A + Stream B
# can edit independently. Mounted under the same /learning prefix.
from backend.api.routes import learning_backfill as learning_backfill_routes
# MITS Phase 18-FU Stream D — observability endpoints (per-cycle weight
# log + impact reports + subsystem health). Separate module so Stream D
# can ship without touching Stream A's learning.py.
from backend.api.routes import (
    learning_observability as learning_observability_routes,
)
from backend.api.routes import lineage as lineage_routes
from backend.api.routes import market as market_routes
from backend.api.routes import marketplace as marketplace_routes
from backend.api.routes import memo as memo_routes
from backend.api.routes import memory as memory_routes
from backend.api.routes import scenarios as scenarios_routes
from backend.api.routes import source_attribution as source_attribution_routes
from backend.api.routes import state as state_routes
from backend.api.routes import metrics as metrics_routes
from backend.api.routes import ml as ml_routes
from backend.api.routes import narrative as narrative_routes
from backend.api.routes import notifications as notifications_routes
from backend.api.routes import telegram_webhook as telegram_webhook_routes
from backend.api.routes import options as options_routes
from backend.api.routes import policy as policy_routes
from backend.api.routes import exit_policy as exit_policy_routes
from backend.api.routes import stage4 as stage4_routes
from backend.api.routes import stage7 as stage7_routes
from backend.api.routes import stage8 as stage8_routes
from backend.api.routes import stage9 as stage9_routes
from backend.api.routes import stage10 as stage10_routes
from backend.api.routes import stage10_extra as stage10_extra_routes
from backend.api.routes import stage10c as stage10c_routes
from backend.api.routes import stage10d as stage10d_routes
from backend.api.routes import stage10e as stage10e_routes
from backend.api.routes import paper as paper_routes
from backend.api.routes import portfolio as portfolio_routes
from backend.api.routes import quote as quote_routes
from backend.api.routes import portfolio_optimizer as portfolio_optimizer_routes
from backend.api.routes import regime_similarity as regime_similarity_routes
from backend.api.routes import regime as regime_routes
from backend.api.routes import research as research_routes
from backend.api.routes import predictive as predictive_routes
from backend.api.routes import strategies as strategies_routes
from backend.api.routes import today as today_routes
from backend.api.routes import trades as trades_routes
from backend.api.routes import watchlist as watchlist_routes
from backend.bot.alerts import ALERT_CENTER
from backend.bot.alpaca_executor import AlpacaExecutor
from backend.bot.engine import BotEngine
from backend.bot.executor import Executor
from backend.bot.notifications.telegram import TelegramNotifier
from backend.bot.paper_executor import PaperExecutor
from backend.bot.scheduler import BotScheduler
from backend.bot.warnings_log import install as install_warnings_log
from backend.config import SETTINGS, TUNABLES
from backend.db import init_db

# Log level is env-configurable so the operator can dial verbosity
# without code changes. INFO by default exposes engine cycle activity;
# DEBUG opens the firehose. WARNING quiets normal flow.
_log_level = os.getenv("TB_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Capture every WARNING+ into an in-memory ring buffer so the UI can
# surface them without tailing logs. Must install BEFORE any router
# import that might log on import.
install_warnings_log()


class WebSocketLogHub:
    """Broadcast structured events to every connected WebSocket client."""

    def __init__(self) -> None:
        self.clients: Set[WebSocket] = set()
        self.history: list[dict] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        for event in self.history[-50:]:
            await ws.send_json(event)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, event: dict) -> None:
        self.history.append(event)
        self.history = self.history[-500:]
        dead: list[WebSocket] = []
        for ws in self.clients:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


def _build_executor():
    """Pick the executor based on the bot config / env.

    Looks at the persisted config first (so the UI dropdown can change broker
    without restarting), falling back to ``BROKER`` env var, finally to
    ``local_paper`` (safe).

    - ``local_paper`` (default): in-DB simulator, real prices via yfinance
    - ``alpaca_paper``: Alpaca paper-trading endpoint (needs API keys)
    - ``alpaca_live``:  Alpaca live trading
    - ``robinhood``:    unofficial Robinhood API
    """
    from backend.db import session_scope
    from backend.models.config import load_config

    try:
        with session_scope() as session:
            cfg = load_config(session)
        broker = cfg.get("broker") or SETTINGS.broker or "local_paper"
        starting_cash = float(cfg.get("paper_cash_override", 1000.0) or 1000.0)
    except Exception:
        broker = SETTINGS.broker or "local_paper"
        starting_cash = 1000.0

    if broker == "local_paper":
        return PaperExecutor(starting_cash=starting_cash)
    if broker.startswith("alpaca"):
        paper = broker == "alpaca_paper" or SETTINGS.paper_mode
        return AlpacaExecutor(paper=paper)
    return Executor()


#: SPA routes whose path collides with a backend API router of the same
#: prefix (e.g. ``/tomorrow`` is both a React page and a JSON endpoint).
#: When a browser deep-links / refreshes one of these paths, the API
#: responds first because routers are registered before the SPA
#: catch-all. The ``spa_fallback_for_browsers`` middleware intercepts
#: ``Accept: text/html`` GETs for these prefixes and serves the SPA
#: shell so React Router can take over. API clients (Accept:
#: application/json) fall through to the original endpoint unchanged.
#: Add a new conflict prefix here in one place — no other code change
#: needed.
SPA_CONFLICT_PREFIXES = (
    "/tomorrow",
    "/analysis",
    "/detectors",
    "/trial-scorecard",
    "/retrospective",
)


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Bot", version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Resolve the built-frontend directory once so the conflict-fallback
    # middleware can short-circuit browser GETs without re-stat'ing on
    # every request.
    dist_dir = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    _spa_index = dist_dir / "index.html"

    @app.middleware("http")
    async def spa_fallback_for_browsers(request: Request, call_next):
        """Serve the SPA shell for browser GETs that collide with an API.

        A browser refresh / direct visit to ``/tomorrow`` (and 4 sibling
        pages) used to hit the JSON API because FastAPI matches routers
        in registration order and the SPA catch-all is mounted last.
        This middleware checks the ``Accept`` header: text/html (any
        browser) gets ``index.html`` so React Router renders the page;
        every other content negotiation (the React app itself uses
        ``fetch`` which sends ``Accept: */*`` or ``application/json``)
        falls through to the existing router unchanged.

        Only applied when the built frontend is present so test envs
        without a ``frontend/dist`` directory keep their pre-existing
        behavior.
        """
        if request.method == "GET" and _spa_index.exists():
            accept = request.headers.get("accept", "")
            if "text/html" in accept:
                path = request.url.path
                for prefix in SPA_CONFLICT_PREFIXES:
                    if path == prefix or path.startswith(prefix + "/"):
                        return FileResponse(str(_spa_index))
        return await call_next(request)

    init_db()

    # MITS Phase 15.C — eager-load + validate strategy templates so a
    # malformed YAML aborts boot instead of being discovered at the
    # first /strategy/matrix call.
    from backend.bot.analysis.strategy_templates import load_strategy_templates
    _templates = load_strategy_templates()
    logging.getLogger(__name__).info(
        "strategy_matrix: %d templates loaded", len(_templates),
    )

    # MITS Phase 11.A — seed the default watchlist with the universe
    # if the operator hasn't opted out. Idempotent: existing entries
    # are left alone so an operator-curated list survives boot.
    try:
        if getattr(TUNABLES, "universe_seed_watchlist_on_boot", True):
            from backend.bot.data.universe import seed_watchlist
            stats = seed_watchlist("default")
            logging.getLogger(__name__).info(
                "universe seed: added=%d already=%d (universe_size=%d)",
                stats.get("added", 0), stats.get("already_present", 0),
                stats.get("universe_size", 0),
            )
    except Exception:
        logging.getLogger(__name__).exception(
            "universe seed on boot failed; continuing"
        )

    hub = WebSocketLogHub()

    def sink(event: dict):
        return hub.broadcast(event)

    engine = BotEngine(executor=_build_executor(), log_sink=sink)
    ALERT_CENTER.attach(sink)

    # Telegram notifier — subscribe to ALERT_CENTER so every alert
    # fans out to operator phone (subject to filters). Graceful no-op
    # when credentials are missing; this keeps tests + un-configured
    # boxes booting cleanly.
    telegram = TelegramNotifier()
    if telegram.enabled:
        ALERT_CENTER.subscribe(telegram.on_alert)
        logging.getLogger(__name__).info("telegram notifier enabled")
    else:
        logging.getLogger(__name__).info(
            "telegram notifier disabled (no creds)"
        )

    app.state.engine = engine
    app.state.hub = hub
    app.state.scheduler = BotScheduler(engine, notifier=telegram)
    app.state.alert_center = ALERT_CENTER
    app.state.telegram_notifier = telegram

    app.include_router(bot_routes.router)
    app.include_router(config_routes.router)
    app.include_router(trades_routes.router)
    app.include_router(today_routes.router)
    app.include_router(paper_routes.router)
    app.include_router(portfolio_routes.router)
    app.include_router(watchlist_routes.router)
    app.include_router(alerts_routes.router)
    app.include_router(market_routes.router)
    app.include_router(diagnostics_routes.router)
    app.include_router(backtest_routes.router)
    app.include_router(strategies_routes.router)
    app.include_router(copilot_routes.router)
    app.include_router(heatseeker_routes.router)
    app.include_router(iv_regime_routes.router)
    app.include_router(gate_diagnostics_routes.router)
    app.include_router(pricing_telemetry_routes.router)
    app.include_router(divergence_routes.router)
    app.include_router(flowseeker_routes.router)
    app.include_router(analytics_routes.router)
    app.include_router(learning_routes.router)
    # MITS Phase 18-FU Gap 4 — backfill route under the same /learning prefix.
    app.include_router(learning_backfill_routes.router)
    # MITS Phase 18-FU Stream D — /learning/observability/* endpoints.
    app.include_router(learning_observability_routes.router)
    app.include_router(flowintel_routes.router)
    app.include_router(execution_routes.router)
    app.include_router(execution_costs_routes.router)
    app.include_router(options_routes.router)
    app.include_router(policy_routes.router)
    app.include_router(exit_policy_routes.router)
    app.include_router(stage4_routes.micro_router)
    app.include_router(stage4_routes.cross_router)
    app.include_router(stage4_routes.event_router)
    app.include_router(ml_routes.router)
    app.include_router(portfolio_optimizer_routes.router)
    app.include_router(stage7_routes.drift_router)
    app.include_router(stage7_routes.monitor_router)
    app.include_router(stage7_routes.attr_router)
    app.include_router(stage7_routes.explain_router)
    app.include_router(stage8_routes.stress_router)
    app.include_router(stage8_routes.replay_router)
    app.include_router(stage8_routes.canary_router)
    app.include_router(stage9_routes.autopsy_router)
    app.include_router(stage9_routes.cohort_router)
    app.include_router(stage9_routes.abstain_router)
    app.include_router(stage10_routes.exits_router)
    app.include_router(stage10_routes.spread_router)
    app.include_router(stage10_routes.halt_router)
    app.include_router(stage10_routes.grade_router)
    app.include_router(stage10_extra_routes.heat_router)
    app.include_router(stage10_extra_routes.decay_router)
    app.include_router(stage10c_routes.beta_router)
    app.include_router(stage10c_routes.iv_router)
    app.include_router(stage10c_routes.sweep_router)
    app.include_router(stage10d_routes.shock_router)
    app.include_router(stage10d_routes.leakage_router)
    app.include_router(stage10d_routes.twap_router)
    app.include_router(stage10d_routes.exit_models_router)
    app.include_router(stage10e_routes.regime_router)
    app.include_router(stage10e_routes.quality_router)
    app.include_router(stage10e_routes.momo_router)
    app.include_router(memo_routes.router)
    app.include_router(memory_routes.router)
    app.include_router(scenarios_routes.router)
    app.include_router(state_routes.router)
    app.include_router(regime_similarity_routes.router)
    app.include_router(regime_routes.router)
    app.include_router(research_routes.router)
    app.include_router(marketplace_routes.router)
    app.include_router(data_quality_routes.router)
    app.include_router(ai_cost_routes.router)
    app.include_router(lineage_routes.router)
    app.include_router(decision_routes.router)
    app.include_router(agents_routes.router)
    app.include_router(authority_routes.router)
    app.include_router(authority_routes.system_router)
    app.include_router(narrative_routes.router)
    app.include_router(predictive_routes.router)
    app.include_router(audit_routes.router)
    app.include_router(metrics_routes.router)
    app.include_router(experiments_routes.router)
    app.include_router(explain_routes.router)
    app.include_router(gates_routes.router)
    app.include_router(journal_routes.router)
    app.include_router(knowledge_routes.router)
    app.include_router(lake_status_routes.router)
    app.include_router(lake_status_routes.status_router)
    app.include_router(theories_routes.router)
    app.include_router(quote_routes.router)
    app.include_router(thesis_routes.router)
    app.include_router(detectors_routes.router)
    app.include_router(detector_scorecard_routes.router)
    app.include_router(brain_scorecard_routes.router)
    app.include_router(analysis_routes.router)
    app.include_router(strategy_matrix_routes.router)
    app.include_router(tomorrow_routes.router)
    app.include_router(prediction_outcomes_routes.router)
    app.include_router(retrospective_routes.router)
    app.include_router(trial_routes.router)
    app.include_router(trial_scorecard_routes.router)
    app.include_router(fred_routes.router)
    app.include_router(breadth_routes.router)
    app.include_router(edgar_routes.router)
    app.include_router(finra_routes.router)
    app.include_router(cot_routes.router)
    app.include_router(earnings_intel_routes.router)
    app.include_router(source_attribution_routes.router)
    app.include_router(notifications_routes.router)
    app.include_router(telegram_webhook_routes.router)

    @app.websocket("/ws/log")
    async def ws_log(websocket: WebSocket) -> None:
        await hub.connect(websocket)
        try:
            while True:
                # Keep the connection open; we ignore inbound messages.
                await websocket.receive_text()
        except WebSocketDisconnect:
            hub.disconnect(websocket)

    @app.websocket("/ws/flow")
    async def ws_flow(websocket: WebSocket) -> None:
        """Push the most-urgent options flow to the Flowseeker UI."""
        from datetime import datetime, timezone

        from backend.bot.signals.flow import filter_unseen, live_flow
        from backend.models.config import load_config

        await websocket.accept()
        try:
            while True:
                try:
                    from backend.db import session_scope

                    with session_scope() as session:
                        tickers = load_config(session).get("tickers") or ["SPY"]
                    alerts = await asyncio.to_thread(live_flow, tickers, 25)
                    # Dedup against the persisted seen-set so a reconnect or
                    # restart never replays alerts the client already has (#3).
                    fresh = await asyncio.to_thread(filter_unseen, alerts)
                    await websocket.send_json({
                        "type": "flow", "ts": datetime.now(timezone.utc).isoformat(),
                        "alerts": [a.to_dict() for a in fresh],
                    })
                except Exception:
                    logging.getLogger(__name__).debug("ws/flow tick failed", exc_info=True)
                await asyncio.sleep(float(TUNABLES.flow_cache_ttl))
        except WebSocketDisconnect:
            return

    # Serve the built React UI if present. The router is client-side, so any
    # unknown path serves index.html (SPA fallback). ``dist_dir`` was resolved
    # at the top of ``create_app`` so the conflict-fallback middleware can
    # share it.
    if dist_dir.exists():
        app.mount("/assets", StaticFiles(directory=dist_dir / "assets"), name="assets")

        @app.get("/", response_class=HTMLResponse)
        async def index() -> str:
            return (dist_dir / "index.html").read_text()

        @app.get("/{path:path}", response_class=HTMLResponse)
        async def spa_fallback(path: str) -> str:
            # API routers are registered before this catch-all, so anything that
            # reaches here is a client-side route. Serve index.html so React
            # Router can handle it (deep-links / refresh work on every page).
            # Requests that look like a static file (have an extension) but
            # weren't served by /assets are genuine 404s.
            last = path.rsplit("/", 1)[-1]
            if "." in last:
                from fastapi import HTTPException

                raise HTTPException(status_code=404, detail="not found")
            return (dist_dir / "index.html").read_text()
    else:

        @app.get("/")
        async def index() -> dict:
            return {
                "message": "Trading bot API is running. Build the frontend with 'cd frontend && npm run build' to serve the UI here.",
            }

    @app.on_event("startup")
    async def _startup() -> None:
        if os.getenv("DISABLE_SCHEDULER") != "1":
            app.state.scheduler.start()
        _log_feed_audit()
        # MITS Phase 0 — seed academic / TA-Lib priors. Idempotent.
        try:
            from backend.bot.corpus.priors_loader import load_default_priors
            stats = load_default_priors()
            logging.getLogger("backend.startup_audit").info(
                "MITS priors loaded: %s", stats
            )
        except Exception:
            logging.getLogger("backend.startup_audit").exception(
                "MITS priors load failed"
            )
        # Resume the live trading loop automatically so a service restart
        # mid-session doesn't leave the bot dormant until someone POSTs
        # /bot/start. Toggle off via TB_ENGINE_AUTOSTART_ON_BOOT=0.
        try:
            from backend.config import TUNABLES

            if getattr(TUNABLES, "engine_autostart_on_boot", True):
                app.state.engine.start_live_loop()
                logging.getLogger("backend.startup_audit").info(
                    "engine auto-start: live loop scheduled"
                )
        except Exception:
            logging.getLogger("backend.startup_audit").exception(
                "engine auto-start failed"
            )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        app.state.scheduler.shutdown()

    return app


def _log_feed_audit() -> None:
    """Emit a structured snapshot of which data feeds have fresh rows on
    process boot. Lets the operator see at a glance whether anything is
    silently stale after a redeploy or instance restart."""
    import logging
    from datetime import datetime, timezone
    from sqlalchemy import select, func
    from backend.db import session_scope

    _log = logging.getLogger("backend.startup_audit")

    def _staleness(latest_dt):
        if not latest_dt:
            return "no rows"
        if isinstance(latest_dt, str):
            try:
                latest_dt = datetime.fromisoformat(latest_dt)
            except Exception:
                return "unparseable"
        now = datetime.utcnow()
        delta = now - (latest_dt.replace(tzinfo=None) if hasattr(latest_dt, "tzinfo") else latest_dt)
        hours = delta.total_seconds() / 3600
        if hours < 1:   return f"{int(delta.total_seconds() / 60)}m ago"
        if hours < 48:  return f"{hours:.1f}h ago"
        return f"{int(hours / 24)}d ago"

    feeds = []
    try:
        from backend.models.fred_observation import FredObservation
        with session_scope() as s:
            row = s.execute(
                select(func.max(FredObservation.date))
            ).scalar_one_or_none()
            feeds.append(("FRED", _staleness(row)))
    except Exception as e:
        feeds.append(("FRED", f"err: {e}"))
    try:
        from backend.models.iv_history import IVHistory
        with session_scope() as s:
            row = s.execute(select(func.max(IVHistory.date))).scalar_one_or_none()
            feeds.append(("IV history", _staleness(row)))
    except Exception as e:
        feeds.append(("IV history", f"err: {e}"))
    try:
        from backend.models.decision_log import DecisionLog
        with session_scope() as s:
            row = s.execute(select(func.max(DecisionLog.timestamp))).scalar_one_or_none()
            feeds.append(("decision_log", _staleness(row)))
    except Exception as e:
        feeds.append(("decision_log", f"err: {e}"))
    try:
        from backend.models.short_interest import ShortInterest
        with session_scope() as s:
            row = s.execute(select(func.max(ShortInterest.settlement_date))).scalar_one_or_none()
            feeds.append(("FINRA short-int", _staleness(row)))
    except Exception as e:
        feeds.append(("FINRA short-int", f"err: {e}"))
    try:
        from backend.models.cot_report import CotReport
        with session_scope() as s:
            row = s.execute(select(func.max(CotReport.report_date))).scalar_one_or_none()
            feeds.append(("COT", _staleness(row)))
    except Exception as e:
        feeds.append(("COT", f"err: {e}"))
    try:
        from backend.models.breadth_snapshot import BreadthSnapshot
        with session_scope() as s:
            row = s.execute(select(func.max(BreadthSnapshot.date))).scalar_one_or_none()
            feeds.append(("Breadth", _staleness(row)))
    except Exception as e:
        feeds.append(("Breadth", f"err: {e}"))

    summary = " · ".join(f"{name}={age}" for name, age in feeds)
    _log.info("startup feed audit: %s", summary)


app = create_app()
