"""Bot engine: build market data → adaptive plan → per-ticker strategy → risk → execute."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from backend.bot.ai import SignalBlender
from backend.bot.ai.brain import AutonomousBrain
from backend.bot.ai.opportunity_brain import OpportunityBrain
from backend.bot.alerts import ALERT_CENTER
from backend.bot.analytics import AnalyticsEngine, gate_by_grade
from backend.bot.decision.policy import DecisionPolicy, PolicyContext
from backend.bot.decision.rules import _register_all as _register_policy_rules
from backend.bot.meta_ai import MetaReasoner
from backend.bot.executor import Executor
from backend.bot.market_data import MarketDataAdapter
from backend.bot.risk import AccountState, RiskManager
from backend.bot.strategies.adaptive import AdaptiveStrategy, DayPlan
from backend.bot.strategies.all_strategies import STRATEGY_REGISTRY, get_strategy
from backend.bot.strategies.base import Action, Signal, Strategy
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.config import load_config
from backend.models.policy_rule_evaluation import PolicyRuleEvaluation
from backend.models.snapshot import PortfolioSnapshot
from backend.models.trade import Trade

logger = logging.getLogger(__name__)


def select_strategy(name: str) -> Strategy:
    """Pick a strategy by name; ``adaptive`` returns the meta-selector."""
    if not name or name == "adaptive":
        return AdaptiveStrategy()
    try:
        return get_strategy(name)
    except ValueError:
        logger.warning("unknown strategy %s, defaulting to adaptive", name)
        return AdaptiveStrategy()


STOCK_ACTIONS = {Action.BUY_STOCK, Action.SELL_STOCK}
SINGLE_LEG_OPTIONS = {Action.BUY_CALL, Action.BUY_PUT}
# Short single-leg options — CSP and Covered Call are NOT spreads; they're one
# leg of a sold contract. Persisting them with instrument="spread" was wrong.
SINGLE_LEG_SHORT_OPTIONS = {Action.SELL_CSP, Action.SELL_COVERED_CALL}
# Multi-leg / true spreads.
SPREAD_OPTIONS = {
    Action.BULL_CALL_SPREAD, Action.BUY_STRADDLE, Action.IRON_CONDOR,
    Action.RATIO_SPREAD, Action.COLLAR,
}
COMPLEX_OPTIONS = SINGLE_LEG_SHORT_OPTIONS | SPREAD_OPTIONS


def _stage19_source_scores(signal: Signal, event: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot each Wave-1/2 data source's verdict at decision time.

    Joined with realized P&L by ``bot/source_attribution.compute_contributions``
    after ≥ 30 closed trades to answer "which sources actually matter?".
    """
    try:
        from backend.bot.source_attribution import snapshot_sources
        # Compose the same context the agents saw + a few extras.
        from backend.bot.breadth import regime_health as _bh
        from backend.bot.data.cot import positioning_snapshot as _cs
        from backend.bot.data.edgar import insider_activity_summary as _ia
        from backend.bot.data.finra import short_pressure as _sp
        from backend.bot.data.fred import macro_snapshot as _ms
        from backend.bot.earnings_intel import latest_for as _ei
        ctx = {
            "action": signal.action.value,
            "breadth": _bh() or {},
            "macro": _ms() or {},
            "earnings_intel": _ei(signal.ticker) or {},
            "short_pressure": _sp(signal.ticker) or {},
            "cot_snapshot": _cs() or {},
            "insider_activity": _ia(signal.ticker) or {},
        }
        return snapshot_sources(ctx)
    except Exception:
        logger.debug("source-scores snapshot failed for %s",
                       signal.ticker, exc_info=True)
        return {}


@dataclass
class EngineStatus:
    running: bool = False
    last_cycle_at: Optional[str] = None
    active_strategy: str = "adaptive"
    market_regime: str = "unknown"
    daily_pnl: float = 0.0
    cycles: int = 0
    recent_signals: List[dict] = field(default_factory=list)
    day_plan: Optional[Dict[str, Any]] = None
    # MITS Phase 7 — discretionary opportunism layer state surface.
    intraday_regime: str = "normal"


@dataclass
class _PseudoAnalog:
    """Phase 19 — minimal AnalogHit duck-type for cohort-fallback scenario
    decomposition on HOLD events. ``decompose_scenarios`` only reads
    ``realized_return_pct`` off each hit, so a one-field dataclass keeps
    the call path lossless when pgvector is cold but
    ``knowledge_evidence`` cohort cells carry per-cell return averages.
    """

    realized_return_pct: float


class BotEngine:
    # Minimum dollar size for a new stock position — avoids dust trades when
    # buying power is nearly exhausted.
    MIN_NOTIONAL = 25.0

    def __init__(
        self,
        executor: Optional[Executor] = None,
        log_sink: Optional[Callable[[dict], Any]] = None,
        market_data: Optional[MarketDataAdapter] = None,
        blender: Optional[SignalBlender] = None,
        brain: Optional[AutonomousBrain] = None,
    ) -> None:
        self.executor = executor or Executor()
        self.status = EngineStatus()
        self._log_sink = log_sink
        self.market_data = market_data or MarketDataAdapter()
        self.adaptive = AdaptiveStrategy()
        self.blender = blender or SignalBlender()
        self.brain = brain or AutonomousBrain()
        self.analytics = AnalyticsEngine()
        self.meta = MetaReasoner()
        # MITS Phase 7 — discretionary opportunism layer. Classifier
        # tags the tape every cycle; on non-normal regimes the
        # Opportunity Brain reasons in parallel with (and OVERRIDES)
        # the standard consensus.
        from backend.bot.regime.intraday_regime import IntradayRegimeClassifier
        self._intraday_classifier = IntradayRegimeClassifier(
            market_data=self.market_data,
        )
        self._opportunity_brain = OpportunityBrain()
        self._current_regime: str = "normal"
        self._last_opportunity_hypothesis: Optional[Any] = None
        # Daily opportunistic-trade tallies (zeroed at post-market).
        self._opportunistic_daily_notional: float = 0.0
        self._opportunistic_concurrent_open: int = 0
        self._live_task: Optional[asyncio.Task] = None
        # AI Brain per-ticker rejection cooldown. The Brain proposed AMD
        # 8 times in 12 minutes on 2026-06-01 before one variation slipped
        # through the meta gate — by switching BUY_PUT → BUY_CALL with
        # identical features. Cooldown stops that spam-until-it-passes
        # pattern: once a ticker is rejected by any safety gate this run,
        # the engine ignores fresh ai_brain proposals on it for N seconds.
        # Cleared on engine restart.
        self._brain_cooldown: Dict[str, float] = {}
        self._brain_cooldown_seconds: float = 600.0  # 10 min
        # MITS Phase 5 (P5.3) — running per-day tallies for EOD-bias
        # sizing caps. Zeroed by the post-market scheduler (_post_market)
        # so a fresh trading day starts with empty budgets.
        self._eod_high_conviction_open_today: int = 0
        self._eod_daily_notional_today: float = 0.0
        # MITS Phase 16.A — declarative decision policy. The procedural
        # gate stack at run_cycle:L2120-2900 used to live inline; it is
        # now a registered list of PolicyRule evaluators.
        self._decision_policy = DecisionPolicy()
        _register_policy_rules(self._decision_policy)

    # -- control ------------------------------------------------------------
    def start(self) -> None:
        self.status.running = True
        self.executor.login()

    def stop(self) -> None:
        self.status.running = False
        if self._live_task and not self._live_task.done():
            self._live_task.cancel()
        self._live_task = None

    def start_live_loop(self, interval_sec: float = 30.0) -> None:
        """Spin up a background asyncio task that runs cycles continuously.

        Safe to call repeatedly — if a loop is already running, this is a
        no-op. The loop respects ``self.status.running`` so :meth:`stop`
        cleanly halts it.
        """
        if self._live_task is not None and not self._live_task.done():
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if not loop.is_running():
            return
        self.status.running = True
        self.executor.login()
        self._live_task = loop.create_task(self._live_loop(interval_sec))

    async def _live_loop(self, interval_sec: float) -> None:
        """Run cycles until stopped. Sleeps ``interval_sec`` between iterations.

        P1.10 — per-cycle wall-clock budget. A stuck cycle (e.g. Claude
        call hanging) used to back up the queue indefinitely. Each cycle
        now has a hard timeout (default 240s) that cancels the worker
        thread and emits a SystemWarning, so subsequent cycles can run.
        """
        from backend.config import TUNABLES
        cycle_timeout = float(
            getattr(TUNABLES, "engine_cycle_timeout_sec", 240.0)
        )
        logger.info(
            "live loop started, interval=%.1fs cycle_timeout=%.1fs",
            interval_sec, cycle_timeout,
        )
        while self.status.running:
            try:
                # run_cycle is sync, off-load to a thread so we don't
                # block the loop. The timeout protects against stuck
                # cycles (e.g. Claude/HTTPS hangs).
                await asyncio.wait_for(
                    asyncio.to_thread(self.run_cycle),
                    timeout=cycle_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "engine cycle exceeded %.1fs budget — aborting + "
                    "freeing the loop",
                    cycle_timeout,
                )
                # Surface in the system-warnings ring buffer so the
                # operator sees it. Cycle remains running in the worker
                # thread but the loop has moved on.
                try:
                    from backend.bot.alerts import ALERT_CENTER, Alert
                    ALERT_CENTER.add(Alert(
                        severity="warning",
                        title="engine cycle timeout",
                        body=(f"Cycle exceeded {cycle_timeout:.0f}s; "
                                f"loop continuing. Check Anthropic / "
                                f"ThetaData responsiveness."),
                    ))
                except Exception:
                    pass
            except Exception:
                logger.exception("live cycle failed")
            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                break
        logger.info("live loop stopped")

    # -- helpers ------------------------------------------------------------
    def _emit(self, event: dict) -> None:
        logger.info("event %s", event)
        self.status.recent_signals.append(event)
        self.status.recent_signals = self.status.recent_signals[-50:]
        try:
            ALERT_CENTER.fire_from_event(event)
        except Exception:
            logger.exception("alert dispatch failed")
        # Persist every non-HOLD decision to the learning log (best-effort).
        if event.get("action") and event["action"] != "HOLD":
            try:
                from backend.bot.learning import log_decision

                log_decision(event)
            except Exception:
                logger.debug("learning log failed", exc_info=True)
        if self._log_sink is None:
            return
        try:
            result = self._log_sink(event)
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    asyncio.create_task(result)
                else:
                    asyncio.run(result)
        except Exception:
            logger.exception("log sink failed")

    def _populate_strategy_matrix(
        self,
        *,
        event: Dict[str, Any],
        ticker: str,
        data: Dict[str, Any],
        signal: Signal,
    ) -> None:
        """MITS Phase 18-FU Gap R3 — pre-policy StrategyMatrix lift.

        Builds the per-ticker ``RegimeVector`` (the matrix input), then
        calls the TTL-cached matrix builder. Writes both
        ``event["regime_vector"]`` and ``event["strategy_matrix"]`` /
        ``event["top_strategy"]``, so downstream consumers — the policy
        chain, ``_persist_trade`` (engine.py:830, 858) and
        ``_sweep_block_brain_predictions`` (rejected-post-consensus path)
        — all find the same shape regardless of which gate later blocks.

        Fail-open at every step: a regime-vector build failure quietly
        skips the matrix (no upstream effect); a matrix build failure
        is swallowed at the cache layer. The consensus rule still has
        its own fall-through path that re-attempts via the same cache
        (the second call hits — cache key matches).
        """
        from backend.bot.analysis.strategy_matrix_cache import (
            get_or_build as _sm_get_or_build,
        )
        from backend.bot.regime.vector import build_regime_vector

        # Cheap when ``intraday_classifier`` cache is warm; full SPY +
        # sector pull when it's cold (~50ms once per cycle).
        try:
            rv = build_regime_vector(
                ticker=ticker, snapshot=data,
                intraday_classifier=self._intraday_classifier,
            )
        except Exception:
            logger.debug(
                "pre-policy regime_vector build failed for %s",
                ticker, exc_info=True,
            )
            return

        if rv is None:
            return

        # Persist the regime vector dict on the event so the consensus
        # rule sees it already present and SKIPS its own duplicate
        # build (rule reads ctx.event["regime_vector"] today via
        # ctx.scratch["regime_vector_obj"]; we keep both routes intact
        # by stashing only the dict — the consensus rule rebuilds the
        # object once if needed). Cheaper to let the rule rebuild rv
        # once than to leak a regime-vector object through ``event``
        # (event is serialized to JSON downstream).
        try:
            event["regime_vector"] = rv.to_dict()
        except Exception:
            pass

        sm_dict, top_strategy_dict = _sm_get_or_build(
            ticker=ticker, regime_vector=rv,
            signal=signal, analytics=event.get("analytics"),
        )
        if sm_dict is not None:
            event["strategy_matrix"] = sm_dict
        if top_strategy_dict is not None:
            event["top_strategy"] = top_strategy_dict

    def _revalidate_decision_pre_fill(
        self,
        *,
        signal: Signal,
        event: Dict[str, Any],
        ticker: str,
        data: Dict[str, Any],
    ) -> Optional[str]:
        """MITS Phase 16.E — pre-fill drift check.

        Returns ``None`` when safe to proceed, or ``"decision_stale"``
        when the cycle should abort because the world moved meaningfully
        between consensus and execution. Trigger conditions (any one):

          1. ``regime_vector.trend.value`` flipped (bullish ↔ bearish)
          2. ``iv_rank.value`` jumped by > 30 percentage points
          3. ``correlation_cap.worst_rho`` jumped by > 0.20 absolute

        Only fires when ``TUNABLES.decision_rollback_enabled``. The
        operator flips this on after telemetry confirms the abort rate
        is sane. When it does fire, ``event["rollback_reason"]`` carries
        the human-readable explanation so the engine can lift it into
        ``event["reason"]``.

        The RegimeVector + correlation rebuilds are best-effort: a
        rebuild failure leaves that dim's check disabled (returns None
        for that branch) rather than blocking the trade on rebuild
        flake.
        """
        if not bool(TUNABLES.decision_rollback_enabled):
            return None

        original_rv = event.get("regime_vector") or {}
        if not original_rv:
            return None

        from backend.bot.regime.vector import build_regime_vector
        try:
            current_rv = build_regime_vector(
                ticker=ticker,
                snapshot=data or {},
                intraday_classifier=self._intraday_classifier,
            ).to_dict()
        except Exception:
            logger.debug(
                "rollback rv build failed for %s", ticker, exc_info=True,
            )
            return None

        orig_trend = (
            (original_rv.get("trend") or {}).get("value") or ""
        ).lower()
        curr_trend = (
            (current_rv.get("trend") or {}).get("value") or ""
        ).lower()
        if (orig_trend and curr_trend and orig_trend != curr_trend
                and {orig_trend, curr_trend} <= {"bullish", "bearish"}):
            event["rollback_reason"] = (
                f"regime trend flipped from {orig_trend} to "
                f"{curr_trend} between consensus and execution"
            )
            return "decision_stale"

        orig_iv = (original_rv.get("iv_rank") or {}).get("value")
        curr_iv = (current_rv.get("iv_rank") or {}).get("value")
        if orig_iv is not None and curr_iv is not None:
            try:
                iv_jump = abs(float(curr_iv) - float(orig_iv))
            except (TypeError, ValueError):
                iv_jump = 0.0
            if iv_jump > 30.0:
                event["rollback_reason"] = (
                    f"IV rank jumped from {orig_iv} to {curr_iv} "
                    f"(>30pp) between consensus and execution"
                )
                return "decision_stale"

        orig_corr_blob = event.get("correlation_cap") or {}
        orig_corr = orig_corr_blob.get("worst_rho")
        if orig_corr is not None:
            try:
                from backend.bot.gates.correlation_cap_gate import (
                    check_correlation_cap,
                )
                from backend.bot.portfolio_intel.portfolio_context import (
                    build_portfolio_context,
                )
                positions = (
                    self.executor.positions() or []
                    if hasattr(self.executor, "positions") else []
                )
                cand_dir = orig_corr_blob.get("candidate_direction") or "LONG"
                equity = 0.0
                try:
                    for p in positions:
                        equity += float(p.get("market_value") or 0.0)
                except (TypeError, ValueError):
                    equity = 0.0
                pctx_now = build_portfolio_context(
                    positions=positions, equity=equity,
                    candidate_ticker=signal.ticker,
                    candidate_direction=cand_dir,
                )
                curr_result = check_correlation_cap(
                    candidate_ticker=signal.ticker,
                    candidate_direction=cand_dir,
                    portfolio_context=pctx_now,
                    positions=positions,
                )
                curr_corr = curr_result.worst_rho
                if curr_corr is not None:
                    corr_jump = abs(float(curr_corr) - float(orig_corr))
                    if corr_jump > 0.20:
                        event["rollback_reason"] = (
                            f"max correlation jumped from {orig_corr} "
                            f"to {curr_corr} (>0.20 absolute) between "
                            f"consensus and execution"
                        )
                        return "decision_stale"
            except Exception:
                logger.debug(
                    "rollback correlation rebuild failed for %s",
                    ticker, exc_info=True,
                )

        return None

    def _persist_decision_stale_evaluation(
        self,
        ticker: str,
        reason: str,
        cycle_id: Optional[str],
    ) -> None:
        """MITS Phase 16.E — write one policy_rule_evaluations row for
        the rollback abort so /policy/veto-budget aggregates the
        decision_stale rate alongside the rest of the policy library.
        Mirrors the dust-order single-row pattern."""
        import json as _json
        from datetime import datetime as _dt
        try:
            with session_scope() as s:
                s.add(PolicyRuleEvaluation(
                    rule_name="decision_stale",
                    category="data_quality",
                    severity="hard",
                    ticker=ticker,
                    evaluated_at=_dt.utcnow(),
                    blocked=True,
                    reason=reason,
                    sizing_penalty_pct=0.0,
                    evidence_json=_json.dumps({"rollback_reason": reason}),
                    cycle_id=cycle_id,
                ))
        except Exception:
            logger.debug(
                "decision_stale evaluation persist failed for %s",
                ticker, exc_info=True,
            )

    def _persist_dust_evaluation(
        self,
        ticker: str,
        bf,
        cycle_id: Optional[str],
    ) -> None:
        """Single-row policy_rule_evaluations write for the post-sizing
        dust check. The main policy.evaluate() pass deliberately
        short-circuits dust_order until the engine sets
        scratch['post_sizing']; this helper persists the one verdict
        that pass yields."""
        import json as _json
        from datetime import datetime as _dt
        try:
            with session_scope() as s:
                if bf is None:
                    s.add(PolicyRuleEvaluation(
                        rule_name="dust_order", category="execution",
                        severity="hard", ticker=ticker,
                        evaluated_at=_dt.utcnow(), blocked=False,
                        reason="", sizing_penalty_pct=0.0,
                        evidence_json=None, cycle_id=cycle_id,
                    ))
                else:
                    try:
                        evidence_json = _json.dumps(bf.evidence)
                    except (TypeError, ValueError):
                        evidence_json = "{}"
                    s.add(PolicyRuleEvaluation(
                        rule_name=bf.rule, category=bf.category,
                        severity=bf.severity, ticker=ticker,
                        evaluated_at=_dt.utcnow(), blocked=True,
                        reason=bf.reason,
                        sizing_penalty_pct=float(bf.sizing_penalty_pct or 0.0),
                        evidence_json=evidence_json, cycle_id=cycle_id,
                    ))
        except Exception:
            logger.debug(
                "dust evaluation persist failed for %s", ticker, exc_info=True,
            )

    def _persist_policy_evaluations(
        self,
        result,
        ticker: str,
        cycle_id: Optional[str],
    ) -> None:
        """Append one ``policy_rule_evaluations`` row per evaluator
        recorded in ``result.rule_evaluations``. Best-effort: a DB hiccup
        never aborts the cycle (audit invariants still cover the trade
        row), but a healthy DB MUST capture every evaluation so the
        ``/policy/veto-budget`` panel reflects reality."""
        import json as _json

        evidence_index: Dict[str, str] = {}
        for bf in result.blocking_factors:
            try:
                evidence_index[bf.rule] = _json.dumps(bf.evidence)
            except (TypeError, ValueError):
                evidence_index[bf.rule] = "{}"
        sizing_index = {
            bf.rule: float(bf.sizing_penalty_pct or 0.0)
            for bf in result.blocking_factors
        }
        evaluated_at = result.evaluated_at
        try:
            with session_scope() as s:
                for ev in result.rule_evaluations:
                    name = ev["rule"]
                    s.add(PolicyRuleEvaluation(
                        rule_name=name,
                        category=ev["category"],
                        severity=ev["severity"],
                        ticker=ticker,
                        evaluated_at=evaluated_at,
                        blocked=bool(ev["blocked"]),
                        reason=ev.get("reason") or "",
                        sizing_penalty_pct=sizing_index.get(name, 0.0),
                        evidence_json=evidence_index.get(name),
                        cycle_id=cycle_id,
                    ))
        except Exception:
            logger.debug(
                "policy evaluation persist failed for %s", ticker,
                exc_info=True,
            )

    def _persist_trade(
        self,
        signal: Signal,
        quantity: float,
        price: float,
        paper: bool,
        pnl: Optional[float] = None,
        status: str = "open",
        plan: Optional[Dict[str, Any]] = None,
        snapshot: Optional[Dict[str, Any]] = None,
        event: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        import json as _json

        plan = plan or {}
        event = event or {}
        # Keep only the indicator fields useful for explaining the trade.
        snap_keys = (
            "price", "rsi", "macd", "macd_signal", "macd_hist", "ma50", "ma200",
            "adx", "vix", "iv_rank", "news_score", "volume", "avg_volume",
            "spy_trend", "market_trend", "vwap", "gap_pct",
        )
        snap_subset = {k: snapshot.get(k) for k in snap_keys if snapshot and k in snapshot}

        # Stage-19 — Snapshot each data source's numeric verdict at decision
        # time so contribution analysis (compute_contributions) can join
        # source scores ↔ realized outcomes after ≥ 30 closed trades.
        # Helper is module-level _stage19_source_scores referenced below.

        # Stage-12.A3 Unified MarketState — compose one snapshot from the
        # existing regime / cross_asset / features signals and stash it on the
        # event + the module-level cache for /state/current to read.
        market_state_dict: Optional[Dict[str, Any]] = None
        try:
            from backend.bot.state import build_market_state, set_latest
            ms = build_market_state(
                snapshot=snap_subset,
                regime=(event.get("analytics") or {}).get("regime"),
                cross_asset=event.get("cross_asset"),
                features=(event.get("analytics") or {}).get("features"),
                event_risk=event.get("event_risk"),
            )
            set_latest(ms)
            market_state_dict = ms.to_dict()
        except Exception:
            # Bumped from debug to warning — market state is feeding agents;
            # silent failure here means agents may operate on stale view.
            logger.warning("market_state build failed for %s — agents may use partial state",
                              signal.ticker, exc_info=True)

        # Stage-11.5 Memory recall — find the top-3 most-similar past closed
        # trades. Lightweight (≤ 2000-row scan, all in-process) and persisted
        # under detail_json so lineage + Mission Control surface it.
        memory_dict: Optional[Dict[str, Any]] = None
        try:
            from backend.bot.memory import recall_similar, recall_summary
            mem_ctx = {
                "ticker": signal.ticker,
                "action": signal.action.value,
                "analytics": event.get("analytics"),
                "features": (event.get("analytics") or {}).get("features"),
            }
            matches = recall_similar(mem_ctx, k=3)
            memory_dict = {
                "matches": [m.to_dict() for m in matches],
                "summary": recall_summary(matches),
            }
        except Exception:
            logger.debug("memory recall failed for %s", signal.ticker, exc_info=True)

        # Stage-15 — consensus is computed in run_cycle's gate block when
        # called from the live trading path, so we reuse it. Falls through
        # to a fresh compute when callers (tests / exit-manager) don't pre-
        # populate it on the event dict.
        consensus_dict: Optional[Dict[str, Any]] = event.get("consensus")
        if consensus_dict is None:
            try:
                from backend.bot.agents import run_consensus
                # Item #1 — pack memory (lessons + similar trades + per-
                # agent calibration) into the council context so the
                # agents vote with prior outcomes in hand.
                from backend.bot.agent_context import build_agent_context
                agents_ctx = build_agent_context(
                    ticker=signal.ticker,
                    action=signal.action.value,
                    strategy=signal.strategy,
                    analytics=event.get("analytics"),
                    snapshot=snap_subset,
                    portfolio_risk=event.get("portfolio_risk"),
                    optimizer=event.get("optimizer"),
                    cross_asset=event.get("cross_asset"),
                    config=config,
                )
                consensus_dict = run_consensus(
                    agents_ctx, use_dynamic_weights=True,
                ).to_dict()
            except Exception:
                logger.debug("agent consensus failed for %s",
                              signal.ticker, exc_info=True)

        # Stage-11 Trade Memo — synthesize the hedge-fund-style memo before
        # we persist, so the operator can review the rationale per trade.
        # Wrapped in try/except so memo failures never block the order.
        memo_dict: Optional[Dict[str, Any]] = None
        try:
            from backend.bot.memo import get_generator
            context = {
                "ticker": signal.ticker,
                "action": signal.action.value,
                "strategy": signal.strategy,
                "signal_reason": signal.reason,
                "confidence_num": signal.confidence,
                "regime": event.get("analytics", {}).get("regime"),
                "analytics": event.get("analytics"),
                "features": (event.get("analytics") or {}).get("features"),
                "optimizer": event.get("optimizer"),
                "abstain": event.get("abstain"),
                "cross_asset": event.get("cross_asset"),
                "stop_pct": (signal.stop_loss / 100.0) if signal.stop_loss else None,
                "take_profit_pct": (signal.take_profit / 100.0) if signal.take_profit else None,
            }
            memo_dict = get_generator().generate(context=context).to_dict()
        except Exception:
            logger.debug("memo generation failed for %s", signal.ticker, exc_info=True)

        detail = {
            "signal_reason": signal.reason,
            "confidence": signal.confidence,
            "stop_loss_pct": signal.stop_loss,
            "take_profit_pct": signal.take_profit,
            "dte": signal.dte,
            "snapshot": snap_subset,
            "metadata": {k: v for k, v in (signal.metadata or {}).items() if k != "ai_components"},
            "legs": plan.get("legs"),
            "memo": memo_dict,
            # Stage-11.2 — persist the gate/agent decisions so /lineage can
            # reconstruct the full decision chain without re-running compute.
            "analytics": event.get("analytics"),
            "abstain": event.get("abstain"),
            "meta": event.get("meta"),
            "portfolio_risk": event.get("portfolio_risk"),
            "min_grade_tightened": event.get("min_grade_tightened"),
            "risk_decision": event.get("risk"),
            "audit_violations": event.get("audit_violations"),
            "ai_components": (signal.metadata or {}).get("ai_components"),
            "consensus": consensus_dict,
            "memory": memory_dict,
            "market_state": market_state_dict,
            # Stage-19 — per-source score snapshot. After ≥30 closed
            # trades we can compute which sources actually contribute.
            "source_scores": _stage19_source_scores(signal, event),
        }
        # MITS Phase 7 finishing pass — when the opportunistic path is
        # firing this trade, the event already carries the discretionary
        # context (hypothesis + regime snapshot + gate result + sizing).
        # Lift those into ``detail`` so the autopsy/lineage UIs surface
        # the full reasoning chain.
        if event.get("opportunity_hypothesis") is not None:
            detail["opportunity_hypothesis"] = event.get("opportunity_hypothesis")
        if event.get("regime_at_entry") is not None:
            detail["regime_at_entry"] = event.get("regime_at_entry")
        if event.get("opportunistic_gate") is not None:
            detail["opportunistic_gate"] = event.get("opportunistic_gate")
        if event.get("opportunistic_sizing") is not None:
            detail["opportunistic_sizing"] = event.get("opportunistic_sizing")
        if event.get("opportunity_committee") is not None:
            detail["opportunity_committee"] = event.get("opportunity_committee")
        # MITS Phase 17.A item #14 — surface stale-IV warnings on the
        # Trade row's detail_json. The MTM cycle (close path / exit
        # manager) stashes ``mtm_warnings`` onto the event when the
        # stored IV is more than an hour stale; lift them into detail
        # so the post-mortem UI can flag positions priced off old IV.
        mtm_warnings = event.get("mtm_warnings") or []
        if mtm_warnings:
            detail["mtm_warnings"] = list(mtm_warnings)
        with session_scope() as session:
            trade = Trade(
                ticker=signal.ticker,
                action=signal.action.value,
                quantity=quantity,
                price=price,
                strategy=signal.strategy,
                signal_source=signal.metadata.get("source", "engine"),
                confidence=signal.confidence,
                reason=signal.reason,
                paper=1 if paper else 0,
                pnl=pnl,
                status=status,
                instrument=plan.get("instrument", "stock"),
                option_type=plan.get("option_type"),
                strike=plan.get("strike"),
                expiration=plan.get("expiration"),
                contracts=plan.get("contracts"),
                stop_loss_price=plan.get("stop_loss_price"),
                take_profit_price=plan.get("take_profit_price"),
                detail_json=_json.dumps(detail),
                # P1.5 — which data source priced this fill. Phase 2
                # will switch this to the live chain source on real-quote
                # fills. Today's stubbed math gets "paper_stub".
                pricing_source=plan.get("pricing_source", "paper_stub"),
                # P1.7 — accounting model version. v1 today; v2 after
                # Phase 2 cutover replaces premium + MTM math.
                accounting_version=int(plan.get("accounting_version", 1)),
                # MITS Phase 7 finishing pass — opportunistic column for
                # trial-scorecard layer separation, plus must_exit_by_eod
                # marker for the 15:55 ET daily-close sweep.
                opportunistic=1 if bool(event.get("opportunistic")) else 0,
                must_exit_by_eod=(
                    1 if bool(event.get("must_exit_by_eod")) else 0
                ),
                # MITS Phase 17.A — execution-telemetry columns. Each
                # populates from a different source:
                #   • slippage_bps / total_commission: lifted from
                #     order.raw by _finalize_execution into plan
                #   • realized_vs_marked_delta: set by close path
                #   • spot_at_emit / spot_at_fill: captured around
                #     _submit_order in _finalize_execution
                slippage_bps=plan.get("slippage_bps"),
                total_commission=plan.get("total_commission"),
                realized_vs_marked_delta=plan.get("realized_vs_marked_delta"),
                spot_at_emit=plan.get("spot_at_emit"),
                spot_at_fill=plan.get("spot_at_fill"),
                # MITS Phase 17.B — structured fill provenance JSON.
                fill_snapshot_json=plan.get("fill_snapshot_json"),
                # MITS Phase 17.C — sizing provenance chain JSON.
                sizing_chain_json=plan.get("sizing_chain_json"),
                # MITS Phase 17.D — chain selection provenance JSON.
                # NULL on stock paths by design (no chain to select from).
                chain_selection_json=plan.get("chain_selection_json"),
                # MITS Phase 17.E — exit policy provenance JSON. Populated
                # only on CLOSE_OPTION rows where the engine's exit-
                # manager path attached the rich ExitPolicyResult. NULL
                # on entry trades + on closes that didn't go through
                # the declarative policy (manual close, fresh-start
                # sweep, assignment book-entry leg).
                exit_policy_result_json=plan.get("exit_policy_result_json"),
            )
            # Audit invariant — reject sentinel tickers + unknown strategies
            # BEFORE the row hits the DB. AuditViolation propagates so the
            # caller sees exactly why the write was refused.
            try:
                from backend.bot.audit import verify_trade_writable
                verify_trade_writable(trade)
            except Exception:
                logger.error(
                    "verify_trade_writable rejected trade ticker=%s strategy=%s",
                    getattr(trade, "ticker", None),
                    getattr(trade, "strategy", None),
                )
                raise
            session.add(trade)
            session.flush()
            trade_id = int(trade.id)
            self._persist_decision_provenance(
                session, trade_id=trade_id, signal=signal, event=event,
                consensus_dict=consensus_dict, status="submitted",
            )
            return trade_id

    def _persist_decision_provenance(
        self,
        session,
        *,
        trade_id: Optional[int],
        signal: Optional[Signal],
        event: Dict[str, Any],
        consensus_dict: Optional[Dict[str, Any]],
        status: str,
    ) -> None:
        """MITS Phase 16.B — write one decision_provenance row.

        Called from ``_persist_trade`` on the executed path (trade_id set,
        status='submitted') and from ``_sweep_block_brain_predictions``
        on the blocked-post-consensus path (trade_id=None, status from
        the event). Each ``*_json`` column is independently nullable so
        a sparse event still inserts.

        Best-effort: failures here never abort the cycle. The trade row
        is already written by the time we get called on the executed
        path; the provenance row is a parallel audit trail.
        """
        import json as _json
        from backend.models.decision_provenance import DecisionProvenance
        ticker = (
            signal.ticker if signal is not None
            else str(event.get("ticker") or "")
        )

        def _dump(key: str) -> Optional[str]:
            v = event.get(key)
            if not v:
                return None
            try:
                return _json.dumps(v, default=str)
            except (TypeError, ValueError):
                return None

        consensus_json = None
        chairman_memo_json = None
        if consensus_dict:
            try:
                consensus_json = _json.dumps(consensus_dict, default=str)
            except (TypeError, ValueError):
                consensus_json = None
            chairman = consensus_dict.get("chairman_report") or {}
            if chairman:
                try:
                    chairman_memo_json = _json.dumps(chairman, default=str)
                except (TypeError, ValueError):
                    chairman_memo_json = None

        # MITS Phase 16.C — Decision Quality Scorecard. Compute the
        # 4-axis + composite score off the same bag-of-fields the row
        # is about to persist, so callers can read it without rebuilding
        # the JSON envelope.
        dqs_json: Optional[str] = None
        dqs_dict: Optional[Dict[str, Any]] = None
        try:
            from backend.bot.decision.scorecard import score_decision
            prov_bag = {
                "regime_vector": event.get("regime_vector"),
                "strategy_matrix": event.get("strategy_matrix"),
                "consensus": consensus_dict,
                "chairman_memo": (
                    (consensus_dict or {}).get("chairman_report")
                ),
                "policy_result": event.get("policy_result"),
                "simulator_verdict": event.get("simulator_verdict"),
                "correlation_cap": event.get("correlation_cap"),
                "portfolio_context": event.get("portfolio_context"),
                "agent_outputs": event.get("agent_outputs"),
            }
            dqs = score_decision(prov_bag)
            dqs_dict = dqs.to_dict()
            dqs_json = _json.dumps(dqs_dict)
        except Exception:
            logger.debug(
                "decision_quality_score computation failed for %s",
                ticker, exc_info=True,
            )

        # MITS Phase 19 — would_have_been execution panel for non-
        # submitted rows. Computed off the live event so the cockpit
        # shows meaningful content (fill/sizing/chain/exit projections)
        # instead of EmptyState on HOLDs and post-consensus blocks.
        # Skip on executed trades — Trade.fill_snapshot_json etc. carry
        # the real provenance there. ``would_have_been`` is OBSERVATIONAL
        # only — never writes a trade row, never changes policy.
        would_have_been_json: Optional[str] = None
        if status != "submitted":
            try:
                wb = self._compute_would_have_been(event, signal)
                if wb:
                    would_have_been_json = _json.dumps(wb)
            except Exception:
                logger.debug(
                    "would_have_been compute failed for %s",
                    ticker, exc_info=True,
                )

        # Issue 11b — provenance is the audit trail for EVERY decision,
        # not just option spreads. There is no instrument-type gate here
        # by design: stock entries, option singles, spreads, EOD sweeps,
        # thesis_health closes — ALL pass through and write a row, even
        # when several JSON columns are empty. Without this the linkage
        # ratio (Trade.id → DecisionProvenance.trade_id) drops below the
        # 80% audit target.
        try:
            prov = DecisionProvenance(
                trade_id=trade_id,
                event_status=status,
                ticker=ticker,
                decision_timestamp=datetime.utcnow(),
                cycle_id=str(event.get("timestamp") or "") or None,
                regime_vector_json=_dump("regime_vector"),
                strategy_matrix_json=_dump("strategy_matrix"),
                agent_inputs_json=_dump("agent_input"),
                agent_outputs_json=_dump("agent_outputs"),
                consensus_json=consensus_json,
                chairman_memo_json=chairman_memo_json,
                policy_result_json=_dump("policy_result"),
                simulator_verdict_json=_dump("simulator_verdict"),
                correlation_cap_json=_dump("correlation_cap"),
                portfolio_context_json=_dump("portfolio_context"),
                decision_quality_score_json=dqs_json,
                would_have_been_json=would_have_been_json,
            )
            session.add(prov)
            session.flush()
        except Exception:
            # Bumped from debug → warning so silent provenance failures
            # surface in the log — they cause the 11b linkage gap.
            logger.warning(
                "decision_provenance persist failed for %s (trade_id=%s)",
                ticker, trade_id, exc_info=True,
            )

        # Lift onto the executed Trade.detail_json so the autopsy /
        # lineage UI surfaces the score next to the trade.
        if trade_id is not None and dqs_dict is not None:
            try:
                from backend.models.trade import Trade
                trade_obj = session.query(Trade).filter(
                    Trade.id == trade_id
                ).first()
                if trade_obj is not None and trade_obj.detail_json:
                    detail_blob = _json.loads(trade_obj.detail_json)
                    detail_blob["decision_quality_score"] = dqs_dict
                    trade_obj.detail_json = _json.dumps(detail_blob)
                    session.flush()
            except Exception:
                logger.debug(
                    "decision_quality_score lift onto Trade failed for "
                    "trade_id=%s", trade_id, exc_info=True,
                )

    def _writeback_paper_account_realized(
        self,
        close_pnl: Optional[float],
        *,
        source_kind: str = "live",
    ) -> None:
        """Add a closed position's realized P&L onto ``paper_account.realized_pnl``.

        Called from each engine-level exit close path IMMEDIATELY after
        ``close_pnl`` is computed. Local ``PaperExecutor`` already updates
        ``paper_account.realized_pnl`` inside its close paths, so we skip the
        writeback when the executor is a ``PaperExecutor`` to avoid
        double-counting; for non-paper executors (AlpacaExecutor + live
        brokers) this is the ONLY hand on the lever.

        Synthetic-backfill closes must NEVER touch live realized_pnl — they
        are routed through ``backend.bot.learning.backfill`` which doesn't
        call here. We still guard explicitly so a future caller that passes
        ``source_kind='synthetic_backfill'`` is safe.
        """
        if close_pnl is None:
            return
        if source_kind != "live":
            return
        try:
            from backend.bot.paper_executor import PaperExecutor
            if isinstance(self.executor, PaperExecutor):
                # PaperExecutor.close_option / place_stock_order already
                # incremented account.realized_pnl in the same transaction
                # that updated PaperPosition. Don't double-count.
                return
        except Exception:
            # If we can't import PaperExecutor, fall through and do the
            # writeback — the safety-net path is correct for non-paper.
            pass
        try:
            from backend.models.paper import PaperAccount
            with session_scope() as session:
                acct = session.query(PaperAccount).first()
                if acct is not None:
                    acct.realized_pnl = (
                        float(acct.realized_pnl or 0.0) + float(close_pnl)
                    )
                    acct.updated_at = datetime.utcnow()
        except Exception:
            logger.warning(
                "paper_account realized_pnl writeback failed for "
                "close_pnl=%s", close_pnl, exc_info=True,
            )

    def _persist_brain_prediction_engine(
        self,
        *,
        ticker: str,
        suggested_action: Optional[str],
        suggested_direction: Optional[str],
        posterior_at_decision: Optional[float] = None,
        sample_size_at_decision: Optional[int] = None,
        confidence_self_assessment: Optional[float] = None,
        invalidation: Optional[List[str]] = None,
        thesis_paragraph: Optional[str] = None,
        regime_vector: Optional[Dict[str, Any]] = None,
        confidence_breakdown: Optional[Dict[str, Any]] = None,
        top_strategy: Optional[Dict[str, Any]] = None,
        linked_trade_id: Optional[int] = None,
        outcome: str = "pending",
    ) -> Optional[int]:
        """MITS Phase 15 follow-up Item 2 — log a live engine-cycle
        decision into the BrainPrediction ledger.

        Writes one row with ``surface='engine'``. JSON-encodes the three
        decision-time snapshots (regime vector, council confidence
        breakdown, top StrategyMatrix candidate) so the nightly linker
        can attribute realized outcomes back to each component. Best
        effort: failures never abort the cycle.
        """
        import json as _json
        from backend.models.brain_prediction import BrainPrediction
        try:
            with session_scope() as s:
                row = BrainPrediction(
                    surface="engine",
                    ticker=ticker,
                    window=None,
                    pattern=None,
                    suggested_action=suggested_action,
                    suggested_direction=suggested_direction,
                    posterior_at_decision=posterior_at_decision,
                    sample_size_at_decision=sample_size_at_decision,
                    confidence_self_assessment=confidence_self_assessment,
                    invalidation_json=(
                        _json.dumps(list(invalidation))
                        if invalidation else None
                    ),
                    thesis_paragraph=thesis_paragraph,
                    regime_at_decision=(
                        _json.dumps(regime_vector) if regime_vector else None
                    ),
                    confidence_breakdown_at_decision=(
                        _json.dumps(confidence_breakdown)
                        if confidence_breakdown else None
                    ),
                    top_strategy_at_decision=(
                        _json.dumps(top_strategy) if top_strategy else None
                    ),
                    linked_trade_id=linked_trade_id,
                    outcome=outcome,
                )
                s.add(row)
                s.flush()
                return int(row.id)
        except Exception:
            logger.debug(
                "engine brain prediction persist failed for %s", ticker,
                exc_info=True,
            )
            return None

    @staticmethod
    def _compute_would_have_been(
        event: Dict[str, Any], signal: Optional[Signal] = None,
    ) -> Optional[Dict[str, str]]:
        """MITS Phase 19 — synthesize a "would-have-been" execution panel
        for a non-submitted decision.

        Returns plain-English summaries for the same four execution
        surfaces a real trade carries on the linked Trade row
        (``fill_snapshot``, ``sizing_chain``, ``chain_selection``,
        ``exit_policy_result``) so the Decision Cockpit's execution
        panel has content even when no order shipped. Pure-read /
        observational — never invokes the executor, never writes a
        trade row, never modifies the event.

        Returns ``None`` only on hard import failures so the persist
        path can carry a real NULL through to the column instead of
        an empty dict the UI would misread as "computed but empty".
        """
        try:
            ticker = (event.get("ticker") or "").upper()
            if not ticker or ticker == "—":
                return None
            snapshot = event.get("snapshot") or {}
            spot = float(
                snapshot.get("price") or snapshot.get("close") or 0.0
            )

            # Fill snapshot — bid/ask/mid/spread. Cheap; the quote source
            # has its own TTL cache so a second call inside the cycle
            # hits the cache.
            fill_str = "quote unavailable — fill provenance can't be projected"
            try:
                from backend.bot.data.quote_source import get_quote
                q = get_quote(ticker)
                if q is not None and q.price > 0:
                    if spot <= 0:
                        spot = float(q.price)
                    # quote_source emits a mid-price; approximate the
                    # spread as TUNABLES.equity_default_spread_bps if
                    # we have no live book.
                    from backend.config import TUNABLES as _T
                    spread_bps = float(
                        getattr(_T, "equity_default_spread_bps", 2.0)
                    )
                    half = q.price * (spread_bps / 10_000.0) / 2.0
                    bid = round(q.price - half, 4)
                    ask = round(q.price + half, 4)
                    fill_str = (
                        f"If executed at current quote: bid=${bid}, "
                        f"ask=${ask}, mid=${round(q.price, 4)}, "
                        f"spread≈{round(spread_bps, 2)} bps "
                        f"(source={q.source})"
                    )
            except Exception:
                logger.debug(
                    "would_have_been fill_snapshot failed for %s",
                    ticker, exc_info=True,
                )

            # Sizing chain — risk-baseline qty. Reads ``risk`` config
            # off the event (set by the engine cycle in the rejected
            # path) or falls back to TUNABLES defaults.
            sizing_str = (
                "Risk-baseline sizing unavailable — config not threaded "
                "through to the rejected event"
            )
            try:
                config = event.get("_config") or event.get("config") or {}
                risk_cfg = config.get("risk") or {}
                max_pos_usd = float(
                    risk_cfg.get("max_position_size_usd") or 1000.0
                )
                max_cash_pct = float(
                    risk_cfg.get("max_cash_usage_pct") or 50.0
                )
                if spot > 0:
                    baseline_qty = max(0.0, round(max_pos_usd / spot, 4))
                    sizing_str = (
                        f"Risk-baseline qty={baseline_qty:.4f} "
                        f"@ ${round(spot, 2)} "
                        f"(max_position_size_usd=${int(max_pos_usd)}, "
                        f"max_cash_usage_pct={max_cash_pct:.0f}%)"
                    )
                else:
                    sizing_str = (
                        f"Risk-baseline cap: max_position_size_usd="
                        f"${int(max_pos_usd)} (spot unavailable)"
                    )
            except Exception:
                logger.debug(
                    "would_have_been sizing_chain failed for %s",
                    ticker, exc_info=True,
                )

            # Chain selection — only meaningful when the proposed action
            # is an option attempt. For stock-direction HOLDs we surface
            # "would not have selected a chain" rather than fabricate.
            chain_str = "Stock-direction decision — no option chain to select"
            try:
                action = (event.get("action") or "").upper()
                if "CALL" in action or "PUT" in action:
                    # Strike defaults to nearest ATM; delta target reads
                    # off TUNABLES.
                    from backend.config import TUNABLES as _T
                    delta_target = float(
                        getattr(_T, "default_delta_target", 0.30)
                    )
                    if spot > 0:
                        chain_str = (
                            f"Would have targeted strike ~${round(spot, 2)} "
                            f"(delta target {delta_target:.2f}, "
                            f"DTE bucket TBD by signal metadata)"
                        )
                    else:
                        chain_str = (
                            "Option action proposed but spot unavailable — "
                            "chain selection cannot be projected"
                        )
            except Exception:
                logger.debug(
                    "would_have_been chain_selection failed for %s",
                    ticker, exc_info=True,
                )

            # Exit policy — default TP/SL from signal metadata or risk
            # config. Reads ``signal.take_profit`` / ``signal.stop_loss``
            # when present (they're in PERCENT units), else risk config.
            exit_str = "Default exit policy would apply"
            try:
                tp_pct = None
                sl_pct = None
                if signal is not None:
                    tp_pct = signal.take_profit
                    sl_pct = signal.stop_loss
                if tp_pct is None or sl_pct is None:
                    config = event.get("_config") or event.get("config") or {}
                    risk_cfg = config.get("risk") or {}
                    if tp_pct is None:
                        tp_pct = float(
                            risk_cfg.get("take_profit_pct") or 10.0
                        )
                    if sl_pct is None:
                        sl_pct = float(
                            risk_cfg.get("stop_loss_pct") or 5.0
                        )
                exit_str = (
                    f"Would have armed: take_profit at +{tp_pct:.1f}%, "
                    f"stop_loss at -{sl_pct:.1f}%"
                )
            except Exception:
                logger.debug(
                    "would_have_been exit_policy_result failed for %s",
                    ticker, exc_info=True,
                )

            return {
                "fill_snapshot": fill_str,
                "sizing_chain": sizing_str,
                "chain_selection": chain_str,
                "exit_policy_result": exit_str,
            }
        except Exception:
            logger.debug(
                "would_have_been compute failed for %s",
                event.get("ticker"), exc_info=True,
            )
            return None

    def _ensure_simulator_scenarios_on_hold(
        self, event: Dict[str, Any],
    ) -> None:
        """MITS Phase 19 — populate ``event['simulator_verdict']`` with
        scenarios on HOLD / blocked decisions so the Decision Cockpit
        shows analog clusters instead of an EmptyState.

        The council's ``agent_simulator`` (registered in
        ``rule_consensus_exception``) does run on HOLD events because
        all hard policy rules run, but its analog branch can return
        zero hits when pgvector is cold / unreachable / no matching
        regime, which leaves ``simulator_verdict.scenarios == []``. If
        that happens AND ``TUNABLES.scenario_decomposition_on_hold``
        is True, this helper re-invokes the simulator with the council
        context and lifts any non-empty scenario list back onto the
        event.

        Observational only — never mutates non-scenario verdict fields
        (so cached numeric outputs stay bit-identical for the same
        cache key, preserving the 14.C back-compat guarantee), never
        writes a trade row, never changes the policy outcome. Safe to
        call repeatedly; the second invocation is a cache hit.
        """
        from backend.config import TUNABLES as _T
        if not bool(getattr(_T, "scenario_decomposition_on_hold", True)):
            return
        sv = event.get("simulator_verdict") or {}
        existing = list(sv.get("scenarios") or [])
        if existing:
            return  # Already populated — nothing to do.
        # Best-effort. Failure must NEVER abort the cycle / sweep.
        try:
            from backend.bot.analysis.simulator import (
                SimulatorAgent, decompose_scenarios,
            )

            analytics = event.get("analytics") or {}
            regime_block = analytics.get("regime") or {}
            ticker = (event.get("ticker") or "").upper()
            if not ticker:
                return
            snapshot = event.get("snapshot") or {}
            spot = float(
                snapshot.get("price") or snapshot.get("close") or 0.0
            )
            if spot <= 0:
                # Fall back to the quote source — cheap when warm.
                try:
                    from backend.bot.data.quote_source import get_quote
                    q = get_quote(ticker)
                    if q is not None and q.price > 0:
                        spot = float(q.price)
                except Exception:
                    pass
            if spot <= 0:
                return

            regime = str(regime_block.get("trend") or "unknown").lower()
            vol_state = str(
                regime_block.get("volatility") or "normal"
            ).lower()
            # Cohort + pattern mirror the agent_simulator contract.
            cohort_cells = (
                (event.get("knowledge_evidence") or {}).get("cells") or []
            )
            # When the event doesn't carry knowledge_evidence (the policy
            # path stashes it on agents_ctx, not on event), pull cohort
            # cells directly from the knowledge_graph for the (ticker,
            # regime, vol_state) tuple. Fail-open — no cells means the
            # cohort fallback later skips and we just persist the empty
            # simulator_verdict, same as before this helper existed.
            if not cohort_cells:
                try:
                    from backend.bot.agent_context import (
                        load_knowledge_evidence,
                    )
                    ke = load_knowledge_evidence(
                        ticker=ticker, regime=regime, vol_state=vol_state,
                        snapshot=snapshot,
                    )
                    cohort_cells = (ke or {}).get("cells") or []
                except Exception:
                    pass
            pattern = ""
            if cohort_cells:
                pattern = str(cohort_cells[0].get("pattern") or "")

            sim = SimulatorAgent()
            verdict = sim.simulate(
                ticker=ticker, pattern=pattern, regime=regime,
                vol_state=vol_state, direction="long_stock", spot=spot,
                strike=None, dte=None, cohort_cells=cohort_cells,
            )
            verdict_dict = verdict.to_dict()
            scenarios = list(verdict_dict.get("scenarios") or [])

            # If pgvector still came back empty, synthesize a scenario
            # cluster off the knowledge_evidence cohort cells. The user
            # spec is explicit: HOLDs should show analog clusters even
            # when the pgvector store is cold. Cohort cells carry
            # avg_return_pct + sample_size, which is enough to bucket
            # into the same continuation/fake_breakout/stop_out/macro
            # taxonomy.
            if not scenarios and cohort_cells:
                pseudo = []
                for cell in cohort_cells:
                    n = int(cell.get("sample_size") or 0)
                    r = cell.get("avg_return_pct")
                    if r is None or n <= 0:
                        continue
                    # cells store decimals (0.012 = +1.2%) → percent.
                    pct = float(r) * 100.0
                    pseudo.extend([_PseudoAnalog(pct)] * min(n, 50))
                if pseudo:
                    clusters = decompose_scenarios(
                        pseudo, direction="long_stock", spot=spot,
                    )
                    scenarios = [sc.to_dict() for sc in clusters]

            # Lift the scenarios back onto the existing verdict dict
            # (preserves cache-hit semantics for the numeric fields).
            if scenarios:
                if sv:
                    sv = dict(sv)
                else:
                    sv = dict(verdict_dict)
                sv["scenarios"] = scenarios
                event["simulator_verdict"] = sv
        except Exception:
            logger.debug(
                "scenario decomposition on hold failed for %s",
                event.get("ticker"), exc_info=True,
            )

    def _sweep_block_brain_predictions(self, events: List[dict]) -> None:
        """MITS Phase 15 follow-up Item 2 — at end of cycle, persist a
        BrainPrediction row for every blocked-post-consensus event so
        the linker can score the council's reasoning on rejections.

        Executed events are skipped because ``_finalize_execution``
        already wrote them with ``linked_trade_id`` populated. Pre-
        consensus blocks (market_closed, calendar_gate, options_disabled,
        catalyst_gate, low_confidence, low_grade, drift_halt, abstain,
        event_hold, iv_too_rich, meta_rejected, kill_switch) carry no
        ``consensus`` key and are skipped — there is no council vote to
        capture. The ``_signal_for_brain`` scratch key is consumed and
        removed so it never leaks into the UI / lineage payload.
        """
        for event in events:
            sig_blob = event.pop("_signal_for_brain", None)
            consensus_dict = event.get("consensus") or {}
            status = event.get("status")
            if not consensus_dict or sig_blob is None:
                continue
            if status == "submitted":
                continue
            # MITS Phase 19 — ensure simulator scenarios are populated on
            # HOLD-class events so the Decision Cockpit's scenario panel
            # doesn't render an EmptyState. Observational only.
            self._ensure_simulator_scenarios_on_hold(event)
            action_str = sig_blob.get("action") or event.get("action")
            direction = None
            if action_str:
                au = action_str.upper()
                if "PUT" in au or au.startswith("SELL"):
                    direction = "short"
                elif au.startswith("BUY"):
                    direction = "long"
                else:
                    direction = "neutral"
            self._persist_brain_prediction_engine(
                ticker=event.get("ticker") or "",
                suggested_action=action_str,
                suggested_direction=direction,
                confidence_self_assessment=(
                    float(consensus_dict.get("confidence"))
                    if consensus_dict.get("confidence") is not None else None
                ),
                invalidation=sig_blob.get("invalidation"),
                thesis_paragraph=(sig_blob.get("reason") or "")[:1000] or None,
                regime_vector=event.get("regime_vector"),
                confidence_breakdown=consensus_dict.get("confidence_breakdown"),
                top_strategy=event.get("top_strategy"),
                linked_trade_id=None,
                outcome="not_traded",
            )
            # MITS Phase 16.B — also persist a decision_provenance row
            # for the blocked-post-consensus path. trade_id stays None;
            # event_status captures the legacy_status set by the
            # rejecting policy rule (simulator_veto, correlation_cap_block,
            # consensus_abstain, etc.) so /decision/provenance can list
            # both executed AND rejected decisions with the same shape.
            try:
                with session_scope() as s:
                    self._persist_decision_provenance(
                        s, trade_id=None, signal=None, event=event,
                        consensus_dict=consensus_dict,
                        status=str(status or "blocked"),
                    )
            except Exception:
                logger.debug(
                    "decision_provenance sweep persist failed for %s",
                    event.get("ticker"), exc_info=True,
                )

    @staticmethod
    def _engine_brain_prediction_args(
        *, signal: Signal, event: Dict[str, Any], consensus_dict: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Pack the recurring arg-bundle for ``_persist_brain_prediction_engine``
        from a (signal, event, consensus) tuple. Centralizes the
        action→direction mapping and the consensus-confidence proxy used
        as the council's self-assessment (the engine cycle doesn't run
        the deep composer, so the council's aggregate confidence is the
        closest available stand-in)."""
        action_str = (signal.action.value if signal else None) or event.get("action")
        direction = None
        if action_str:
            au = action_str.upper()
            if "PUT" in au or au.startswith("SELL"):
                direction = "short"
            elif au.startswith("BUY"):
                direction = "long"
            else:
                direction = "neutral"
        return {
            "ticker": event.get("ticker") or (signal.ticker if signal else ""),
            "suggested_action": action_str,
            "suggested_direction": direction,
            "confidence_self_assessment": (
                float(consensus_dict.get("confidence"))
                if consensus_dict.get("confidence") is not None else None
            ),
            "invalidation": (signal.metadata or {}).get("invalidation") if signal else None,
            "thesis_paragraph": (signal.reason or "")[:1000] if signal else None,
            "regime_vector": event.get("regime_vector"),
            "confidence_breakdown": consensus_dict.get("confidence_breakdown"),
            "top_strategy": event.get("top_strategy"),
        }

    def _held_tickers(self) -> set:
        """Set of tickers we currently hold a stock position in. Stock-only
        because options dedup is keyed on (ticker, strike, expiry) — see
        :meth:`_held_option_keys`."""
        try:
            positions = self.executor.positions() if hasattr(self.executor, "positions") else []
        except Exception:
            return set()
        return {
            (p.get("ticker") or "").upper()
            for p in positions
            if p.get("kind", "stock") == "stock" and float(p.get("quantity", 0)) > 0
        }

    def _held_option_keys(self) -> set:
        """Set of (ticker, kind, strike, expiry) tuples we already hold an
        OPTION position in. Used to prevent option pyramiding — without
        this an AI Brain proposing the same BUY_CALL twice in one cycle,
        or across consecutive cycles, would open two identical contracts
        and double position risk on the same trade thesis."""
        try:
            positions = self.executor.positions() if hasattr(self.executor, "positions") else []
        except Exception:
            return set()
        out: set = set()
        for p in positions:
            kind = (p.get("kind") or "stock").lower()
            if kind == "stock":
                continue
            if float(p.get("quantity", 0)) <= 0:
                continue
            ticker = (p.get("ticker") or "").upper()
            strike = p.get("strike")
            expiry = p.get("expiry") or p.get("expiration")
            try:
                strike_f = round(float(strike), 2) if strike is not None else None
            except (TypeError, ValueError):
                strike_f = None
            out.add((ticker, kind, strike_f, str(expiry) if expiry else None))
        return out

    def _close_eod_positions(self) -> List[dict]:
        """MITS Phase 7 finishing pass — the 15:55 ET EOD sweep.

        Walks every position whose corresponding open Trade row has
        ``must_exit_by_eod=1`` and closes it once the trading day is
        within ``TUNABLES.eod_close_minutes_before_close`` minutes of
        16:00 ET. Idempotent: closed positions don't reappear, and
        running multiple cycles inside the close window safely no-ops.

        Returns the close events (one per position closed).
        """
        from backend.bot.calendar import minutes_until_close

        events: List[dict] = []
        threshold = float(getattr(
            TUNABLES, "eod_close_minutes_before_close", 5))
        try:
            remaining = minutes_until_close()
        except Exception:
            remaining = None
        if remaining is None or remaining > threshold:
            return events
        if not hasattr(self.executor, "positions"):
            return events

        # Index of open Trade rows tagged must_exit_by_eod, keyed by
        # (ticker, instrument, strike, expiration). For stocks the
        # strike/expiration are None, so the key still matches.
        try:
            from backend.models.trade import Trade
            eod_keys: set = set()
            with session_scope() as session:
                rows = (session.query(Trade)
                        .filter(Trade.status == "open",
                                  Trade.must_exit_by_eod == 1)
                        .all())
                for r in rows:
                    eod_keys.add((
                        (r.ticker or "").upper(),
                        (r.instrument or "stock").lower(),
                        float(r.strike) if r.strike is not None else None,
                        str(r.expiration) if r.expiration else None,
                    ))
        except Exception:
            logger.debug("EOD sweep: trade lookup failed", exc_info=True)
            return events
        if not eod_keys:
            return events

        try:
            positions = self.executor.positions()
        except Exception:
            logger.debug("EOD sweep: positions() failed", exc_info=True)
            return events

        for pos in positions:
            kind = (pos.get("kind") or "stock").lower()
            ticker = (pos.get("ticker") or "").upper()
            strike = pos.get("strike")
            try:
                strike_f = (float(strike)
                              if strike is not None else None)
            except (TypeError, ValueError):
                strike_f = None
            expiration = (str(pos.get("expiration"))
                              if pos.get("expiration") else None)
            instrument = "option" if kind in ("option", "spread") else "stock"
            key = (ticker, instrument, strike_f, expiration)
            if key not in eod_keys:
                continue
            qty = float(pos.get("quantity") or 0)
            if abs(qty) < 1e-9:
                continue
            reason = (
                f"must_exit_by_eod sweep: {threshold:.0f} min before "
                f"16:00 ET close"
            )
            if instrument == "option" and hasattr(self.executor, "close_option"):
                try:
                    order = self.executor.close_option(
                        ticker, strike_f or 0.0, expiration or "",
                        reason=reason,
                    )
                except Exception:
                    logger.warning(
                        "EOD sweep close_option failed for %s",
                        ticker, exc_info=True,
                    )
                    continue
                realized = ((order.raw or {}).get("pnl")
                              if getattr(order, "success", False) else None)
                evt = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "ticker": ticker,
                    "action": "CLOSE_OPTION",
                    "confidence": 1.0,
                    "reason": reason,
                    "strategy": "eod_sweep",
                    "status": "closed" if order.success else "failed",
                    "instrument": "option",
                    "option_type": pos.get("option_type"),
                    "strike": strike_f,
                    "expiration": expiration,
                    "quantity": abs(qty),
                    "price": (order.raw or {}).get("price", 0.0),
                    "pnl": realized,
                    "order_id": getattr(order, "order_id", None),
                    "paper": getattr(order, "paper", True),
                    "must_exit_by_eod_closed": True,
                }
                if order.success:
                    exit_signal = Signal(
                        ticker=ticker, action=Action.CLOSE_OPTION,
                        confidence=1.0, reason=reason,
                        strategy="eod_sweep",
                        metadata={"source": "eod_sweep"},
                    )
                    try:
                        self._persist_trade(
                            exit_signal, abs(qty),
                            float((order.raw or {}).get("price") or 0),
                            getattr(order, "paper", True),
                            pnl=realized, status="closed",
                            plan={"instrument": "option", "side": "CLOSE",
                                    "option_type": pos.get("option_type"),
                                    "strike": strike_f,
                                    "expiration": expiration,
                                    "contracts": int(abs(qty))},
                            snapshot={"price": (order.raw or {}).get(
                                "price", 0.0)},
                        )
                    except Exception:
                        logger.debug("EOD sweep persist failed",
                                          exc_info=True)
                    if realized is not None:
                        self.status.daily_pnl += float(realized)
                        # Issue 11a — keep paper_account.realized_pnl in
                        # sync with closed-trade pnl when the executor
                        # isn't a local PaperExecutor (which already
                        # bumps it inside close_option).
                        self._writeback_paper_account_realized(realized)
                events.append(evt)
            elif instrument == "stock":
                try:
                    order = self.executor.place_stock_order(
                        ticker, "SELL", abs(qty),
                    )
                except Exception:
                    logger.warning(
                        "EOD sweep stock close failed for %s",
                        ticker, exc_info=True,
                    )
                    continue
                price = float(pos.get("current_price")
                                  or pos.get("avg_cost") or 0.0)
                avg_cost = float(pos.get("avg_cost") or 0.0)
                realized = round((price - avg_cost) * abs(qty), 2)
                evt = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "ticker": ticker,
                    "action": "SELL_STOCK",
                    "confidence": 1.0,
                    "reason": reason,
                    "strategy": "eod_sweep",
                    "status": "submitted" if order.success else "failed",
                    "instrument": "stock",
                    "quantity": abs(qty),
                    "price": price,
                    "pnl": realized,
                    "order_id": getattr(order, "order_id", None),
                    "paper": getattr(order, "paper", True),
                    "must_exit_by_eod_closed": True,
                }
                if order.success:
                    exit_signal = Signal(
                        ticker=ticker, action=Action.SELL_STOCK,
                        confidence=1.0, reason=reason,
                        strategy="eod_sweep",
                        metadata={"source": "eod_sweep"},
                    )
                    try:
                        self._persist_trade(
                            exit_signal, abs(qty), price,
                            getattr(order, "paper", True),
                            pnl=realized, status="closed",
                            plan={"instrument": "stock", "side": "SELL"},
                            snapshot={"price": price,
                                          "entry_price": avg_cost},
                        )
                    except Exception:
                        logger.debug("EOD sweep stock persist failed",
                                          exc_info=True)
                    if realized is not None:
                        self.status.daily_pnl += float(realized)
                        # Issue 11a — paper_account realized_pnl writeback.
                        self._writeback_paper_account_realized(realized)
                events.append(evt)
        return events

    def _manage_exits(self, config: dict) -> List[dict]:
        """Close open stock positions that hit stop-loss or take-profit.

        This is what turns the bot from a buy-only machine into one that
        realizes P&L. Runs before new entries each cycle.

        MITS Phase 7 finishing pass — the must_exit_by_eod sweep runs
        FIRST so opportunistic positions that hit the closing window
        get flat before the standard stop/target logic even looks at
        them. Operator rule: crisis-day discretionary trades are not
        swing positions — they MUST be flat overnight.
        """
        events: List[dict] = []
        # EOD sweep BEFORE stop/target logic. Honors must_exit_by_eod.
        try:
            eod_events = self._close_eod_positions()
            for e in eod_events:
                events.append(e)
                self._emit(e)
        except Exception:
            logger.debug("EOD sweep raised", exc_info=True)
        if not hasattr(self.executor, "positions"):
            return events
        risk_cfg = config.get("risk", {}) or {}
        stop_pct = float(risk_cfg.get("stop_loss_pct", 5)) / 100.0
        take_pct = float(risk_cfg.get("take_profit_pct", 10)) / 100.0
        try:
            positions = self.executor.positions()
        except Exception:
            logger.exception("could not load positions for exit management")
            return events

        for pos in positions:
            kind = pos.get("kind", "stock")
            if kind == "option":
                opt_event = self._maybe_close_option(pos)
                if opt_event is not None:
                    events.append(opt_event)
                    self._emit(opt_event)
                continue
            if kind != "stock":
                continue
            ticker = (pos.get("ticker") or "").upper()
            qty = float(pos.get("quantity", 0) or 0)
            avg_cost = float(pos.get("avg_cost", 0) or 0)
            if qty <= 0 or avg_cost <= 0:
                continue
            # Use the marked price the executor returned, else fetch fresh.
            price = float(pos.get("current_price") or 0)
            if price <= 0:
                try:
                    snap = self.market_data.snapshot(ticker)
                    price = float(snap.data.get("price") or 0)
                except Exception:
                    price = 0.0
            if price <= 0:
                continue

            change = (price - avg_cost) / avg_cost
            reason = None
            if take_pct > 0 and change >= take_pct:
                reason = f"take-profit hit: +{change * 100:.1f}% ≥ {take_pct * 100:.0f}%"
            elif stop_pct > 0 and change <= -stop_pct:
                reason = f"stop-loss hit: {change * 100:.1f}% ≤ -{stop_pct * 100:.0f}%"
            if reason is None:
                continue

            order = self.executor.place_stock_order(ticker, "SELL", qty)
            realized = None
            if getattr(order, "raw", None):
                realized = order.raw.get("pnl")
            # Fall back to a deterministic realized P&L from the position itself
            # (paper exec doesn't surface it on the order result).
            if realized is None:
                realized = round((price - avg_cost) * qty, 2)
            exit_signal = Signal(
                ticker=ticker,
                action=Action.SELL_STOCK,
                confidence=1.0,
                reason=reason,
                strategy="exit_manager",
                metadata={"source": "exit_manager"},
            )
            event = {
                "timestamp": datetime.utcnow().isoformat(),
                "ticker": ticker,
                "action": "SELL_STOCK",
                "confidence": 1.0,
                "reason": reason,
                "strategy": "exit_manager",
                "status": "submitted" if order.success else "failed",
                "order_id": getattr(order, "order_id", None),
                "paper": getattr(order, "paper", True),
                "quantity": round(qty, 4),
                "price": round(price, 2),
                "pnl": realized,
            }
            if order.success:
                self._persist_trade(
                    exit_signal, qty, price, getattr(order, "paper", True),
                    pnl=realized, status="closed",
                    plan={"instrument": "stock", "side": "SELL"},
                    snapshot={"price": price, "entry_price": avg_cost},
                )
                if realized is not None:
                    self.status.daily_pnl += float(realized)
                    # Issue 11a — paper_account realized_pnl writeback.
                    self._writeback_paper_account_realized(realized)
                # Tag the original entry decision with its realized outcome so the
                # learning loop can compute per-strategy / per-regime win rates.
                try:
                    from backend.bot.learning import record_outcome

                    record_outcome(ticker, float(realized or 0.0))
                except Exception:
                    logger.debug("record_outcome failed for %s", ticker, exc_info=True)
            events.append(event)
            self._emit(event)
        return events

    def _maybe_close_via_thesis_health(
        self, *, ticker: str, pos: dict,
        entry_per_share: float, current_per_share: float,
        peak_per_share: Optional[float], entry_iv: Optional[float],
        current_iv: Optional[float], dte: int, strike: float,
        expiration: Any,
    ) -> Optional[dict]:
        """MITS-5 — consult the thesis_health agent on whether to exit.

        Builds a council context with the open-position dict + the
        appropriate WinnerProfile (looked up by the position's stored
        strategy/pattern + current regime). Calls ``run_consensus`` with
        ``only=["thesis_health"]`` so we get a fast single-agent vote
        without re-running the whole 7-agent panel. Returns a close-event
        dict when the agent votes EXIT with confidence; returns None to
        let EXIT.1's mechanical logic run as the safety net.

        The check is gated by ``TUNABLES.thesis_health_check_interval_cycles``
        — set >1 to run every Nth cycle (cuts cost on long swing holds).
        """
        try:
            from backend.bot.agents import run_consensus, STANCE_BUY, STANCE_SELL
            from backend.bot.thesis import build_winner_profile
        except Exception:
            return None

        # Interval gate. The cycle counter ticks per run_cycle iteration;
        # we modulo against the configured interval. Default 1 → every
        # cycle.
        interval = max(1, int(getattr(TUNABLES,
                                                "thesis_health_check_interval_cycles", 1)))
        try:
            if interval > 1 and (self.status.cycles % interval) != 0:
                return None
        except Exception:
            pass

        meta = pos.get("meta") or {}
        # Detector pattern that triggered the entry — falls back to the
        # strategy slug when an explicit pattern isn't recorded.
        pattern = (meta.get("pattern")
                       or meta.get("detector_pattern")
                       or pos.get("strategy")
                       or "")
        regime = (meta.get("regime")
                       or pos.get("regime")
                       or "")
        # Build (or pull cached) winner profile.
        try:
            profile = build_winner_profile(
                pattern=str(pattern), regime=str(regime),
                horizon="1d", ticker=ticker,
            )
        except Exception:
            logger.debug("winner-profile build failed for %s/%s",
                              pattern, regime, exc_info=True)
            return None

        # Hydrate the position dict with everything calculate_health needs.
        position_ctx = {
            "ticker": ticker,
            "option_type": pos.get("option_type"),
            "strike": strike,
            "expiration": str(expiration),
            "entry_price": entry_per_share,
            "current_price": current_per_share,
            "mark": current_per_share,
            "peak_premium": peak_per_share,
            "entry_iv": entry_iv,
            "current_iv": current_iv,
            "dte": dte,
            "meta": meta,
        }
        # Surface VWAP / flag_low / bos_pivot from market_data when
        # available so the trait checks have something to evaluate.
        try:
            snap = self.market_data.snapshot(ticker)
            snap_data = snap.data if snap else {}
            if snap_data:
                position_ctx.setdefault("vwap", snap_data.get("vwap"))
                position_ctx.setdefault("current_vwap", snap_data.get("vwap"))
        except Exception:
            pass

        # Hold-minutes since open.
        try:
            opened_at = pos.get("opened_at") or meta.get("opened_at")
            if opened_at:
                if isinstance(opened_at, str):
                    opened_dt = datetime.fromisoformat(
                        opened_at.replace("Z", "+00:00"))
                else:
                    opened_dt = opened_at
                position_ctx["hold_minutes"] = max(
                    0.0,
                    (datetime.utcnow() - opened_dt.replace(tzinfo=None)
                        if hasattr(opened_dt, "tzinfo") and opened_dt.tzinfo
                        else datetime.utcnow() - opened_dt
                     ).total_seconds() / 60.0,
                )
        except Exception:
            pass

        # Run JUST the thesis_health agent.
        ctx = {
            "ticker": ticker,
            "action": "CLOSE_OPTION",
            "open_position": position_ctx,
            "winner_profile": profile.to_dict(),
        }
        try:
            consensus = run_consensus(ctx, only=["thesis_health"])
        except Exception:
            logger.debug("thesis_health council run failed", exc_info=True)
            return None

        # The agent votes SELL on long-call exits and BUY on long-put
        # exits; we treat either stance as "close this position".
        votes = consensus.votes or []
        if not votes:
            return None
        v = votes[0]
        stance = v.get("stance") if isinstance(v, dict) else getattr(v, "stance", None)
        conf = float(v.get("confidence") if isinstance(v, dict)
                          else getattr(v, "confidence", 0.0))
        reasoning = (v.get("reasoning") if isinstance(v, dict)
                          else getattr(v, "reasoning", "")) or ""
        # Exit requires (a) a SELL or BUY stance (not HOLD, not ABSTAIN)
        # and (b) confidence above 0.55 (the agent's exit-vote floor).
        if stance not in (STANCE_SELL, STANCE_BUY):
            return None
        if conf < 0.55:
            return None

        # Close the position with the agent's reasoning string.
        reason = reasoning[:240] if reasoning else "thesis_health_exit"
        order = self.executor.close_option(
            ticker, strike, expiration, reason=reason)
        realized = (order.raw or {}).get("pnl") if order.success else None
        event = {
            "timestamp": datetime.utcnow().isoformat(),
            "ticker": ticker,
            "action": "CLOSE_OPTION",
            "confidence": round(conf, 3),
            "reason": reason,
            "strategy": "thesis_health",
            "status": "closed" if order.success else "failed",
            "instrument": "option",
            "option_type": pos.get("option_type"),
            "strike": strike,
            "expiration": expiration,
            "quantity": float(pos.get("quantity") or 0),
            "price": (order.raw or {}).get("price", 0.0),
            "pnl": realized,
            "order_id": getattr(order, "order_id", None),
            "paper": getattr(order, "paper", True),
        }
        if order.success:
            exit_signal = Signal(
                ticker=ticker, action=Action.CLOSE_OPTION,
                confidence=conf, reason=reason, strategy="thesis_health",
                metadata={"source": "thesis_health",
                              "winner_profile_sample_size": profile.sample_size,
                              "winner_profile_confidence": profile.confidence},
            )
            self._persist_trade(
                exit_signal,
                float(pos.get("quantity") or 0),
                float((order.raw or {}).get("price") or 0),
                getattr(order, "paper", True),
                pnl=realized, status="closed",
                plan={"instrument": "option", "side": "CLOSE",
                       "option_type": pos.get("option_type"),
                       "strike": strike, "expiration": expiration,
                       "contracts": int(abs(pos.get("quantity") or 0))},
                snapshot={"price": (order.raw or {}).get("price", 0.0)},
            )
            if realized is not None:
                self.status.daily_pnl += float(realized)
                # Issue 11a — paper_account realized_pnl writeback.
                self._writeback_paper_account_realized(realized)
            try:
                from backend.bot.learning import record_outcome
                record_outcome(ticker, float(realized or 0.0))
            except Exception:
                logger.debug("record_outcome failed for %s", ticker, exc_info=True)
        return event

    def _maybe_close_option(self, pos: dict) -> Optional[dict]:
        """Close an open option position based on adaptive exit logic.

        MITS-5 — PRIMARY exit is the council `thesis_health` agent
        ("does this trade still match historical winners?").
        EXIT.1 — SECONDARY/safety-net exit: peak-tracking trailing exit
        from ``OptionExitManager``. See backend/bot/options/exit_manager.py
        for the full mechanical decision tree.

        Side effect: updates the persisted peak_premium high-water mark
        and last-seen IV on each cycle, so the trailing logic survives
        engine restarts. AI Brain / council exits still override at any
        time via the standard run_cycle path."""
        from backend.bot.options.exit_manager import (
            compute_dte,
            decide_exit_with_policy,
            persist_exit_evaluations,
        )
        if not hasattr(self.executor, "close_option"):
            return None
        ticker = (pos.get("ticker") or "").upper()
        strike = float(pos.get("strike") or 0.0)
        expiration = pos.get("expiration")
        if not (ticker and strike and expiration):
            return None
        dte = compute_dte(str(expiration))

        # Per-share premium values. current_price/mark on the position
        # dict is per-share (set by paper_executor.positions() — see
        # P2.3); convert to a uniform per-share number for the manager.
        # SHORTs carry NEGATIVE avg_cost (premium received) so abs() to
        # get magnitude — the manager + 17.A assignment-row write both
        # need this path to run for SHORT positions at expiry.
        entry_per_share = abs(float(pos.get("avg_cost") or 0.0)) / 100.0
        current_per_share = float(
            pos.get("mark") or pos.get("current_price") or 0.0
        )
        if entry_per_share <= 0 or current_per_share <= 0:
            return None

        # Pull persisted peak + entry IV from the DB row so the trailing
        # math sees the true high-water mark, not just this cycle's mark.
        peak_per_share: Optional[float] = None
        entry_iv: Optional[float] = None
        current_iv: Optional[float] = None
        stored_iv_age: Optional[float] = None
        try:
            from backend.db import session_scope
            from backend.models.paper import PaperPosition
            with session_scope() as session:
                row = session.query(PaperPosition).filter(
                    PaperPosition.ticker == ticker,
                    PaperPosition.strike == strike,
                    PaperPosition.expiration == str(expiration),
                    PaperPosition.kind == "option",
                ).first()
                if row is not None:
                    peak_per_share = row.peak_premium_per_share
                    entry_iv = row.entry_iv
                    current_iv = row.stored_iv or row.last_iv_seen
                    now_utc = datetime.utcnow()
                    # MITS Phase 17.A item #14 — measure how stale the
                    # stored IV is. Reported on the event so the
                    # close-path persistence layer can warn the operator
                    # when an MTM cycle priced this position from an
                    # IV more than an hour old.
                    if row.stored_iv_at is not None:
                        stored_iv_age = (
                            now_utc - row.stored_iv_at
                        ).total_seconds()
                    # MITS Phase 17.A item #6 — re-anchor peak when
                    # the chain resumes after a BS-fallback stretch.
                    # When chain freshness comes back AND the prior
                    # peak was last touched BEFORE the latest IV
                    # refresh, the recorded peak is from a stale source.
                    # Re-anchor to the current fresh mid so the
                    # trailing-stop math doesn't trip from a stale high.
                    pos_pricing_source = pos.get("pricing_source")
                    if (pos_pricing_source == "thetadata"
                            and row.peak_premium_at is not None
                            and row.stored_iv_at is not None
                            and row.peak_premium_at < row.stored_iv_at):
                        new_peak = max(
                            float(current_per_share),
                            float(peak_per_share or 0.0),
                        )
                        if new_peak != peak_per_share:
                            logger.info(
                                "peak re-anchored on chain resume "
                                "ticker=%s strike=%.2f exp=%s "
                                "old_peak=%s new_peak=%.4f",
                                ticker, strike, expiration,
                                peak_per_share, new_peak,
                            )
                            row.peak_premium_per_share = new_peak
                            row.peak_premium_at = now_utc
                            peak_per_share = new_peak
                    # Persist this cycle's mark as the new peak if it
                    # exceeds the prior high-water mark.
                    if (peak_per_share is None
                            or current_per_share > peak_per_share):
                        row.peak_premium_per_share = current_per_share
                        row.peak_premium_at = now_utc
                        peak_per_share = current_per_share
                    # Update last-seen IV if available on the mark.
                    mark_iv = pos.get("iv") or pos.get("stored_iv")
                    if mark_iv:
                        try:
                            row.last_iv_seen = float(mark_iv)
                            row.last_iv_seen_at = now_utc
                            current_iv = float(mark_iv)
                        except Exception:
                            pass
                    # MITS Phase 17.A item #8 — IV-crush stamp.
                    # When the current IV has collapsed below the
                    # operator-configured ratio of entry IV, latch the
                    # detection time. Idempotent (only first detection
                    # is recorded).
                    try:
                        from backend.config import TUNABLES as _TUNABLES
                        crush_ratio = float(getattr(
                            _TUNABLES, "opt_exit_iv_crush_ratio", 0.6,
                        ))
                    except Exception:
                        crush_ratio = 0.6
                    if (entry_iv and entry_iv > 0
                            and current_iv
                            and float(current_iv) / float(entry_iv) < crush_ratio
                            and row.iv_crush_first_detected_at is None):
                        row.iv_crush_first_detected_at = now_utc
        except Exception:
            logger.debug("peak update failed for %s", ticker, exc_info=True)

        # MITS Phase 17.A item #14 — surface the stored-IV age + warn
        # downstream persistence (_persist_trade) when it's older than
        # the 1-hour threshold. The Trade row's detail_json["mtm_warnings"]
        # is the operator-visible breadcrumb.
        mtm_event_extras: Dict[str, Any] = {"stored_iv_age_sec": stored_iv_age}
        if stored_iv_age is not None and stored_iv_age > 3600:
            mtm_event_extras["mtm_warnings"] = ["stored_iv_stale"]

        # MITS-5 — PRIMARY exit: consult the council's thesis_health
        # agent BEFORE the mechanical EXIT.1 trailing-stop logic runs.
        # The agent computes "does this trade still match historical
        # winners?" and votes EXIT when the score falls below threshold.
        # When the agent votes EXIT with conviction, we close immediately
        # with the agent's reasoning string. Otherwise, EXIT.1's
        # mechanical logic runs as the safety net.
        thesis_exit = self._maybe_close_via_thesis_health(
            ticker=ticker, pos=pos,
            entry_per_share=entry_per_share,
            current_per_share=current_per_share,
            peak_per_share=peak_per_share,
            entry_iv=entry_iv,
            current_iv=current_iv,
            dte=dte,
            strike=strike,
            expiration=expiration,
        )
        if thesis_exit is not None:
            return thesis_exit

        # MITS Phase 17.E — drive the close path through the declarative
        # ExitPolicy. ``decision`` retains its legacy contract (engine +
        # downstream code consume ExitDecision.should_exit/.reason etc.);
        # ``exit_policy_result`` is the rich per-rule ledger that
        # populates Trade.exit_policy_result_json + the cockpit's "Why
        # this exact exit?" panel.
        position_id_for_eval: Optional[int] = None
        try:
            position_id_for_eval = (
                int(pos.get("id")) if pos.get("id") is not None else None
            )
        except Exception:
            position_id_for_eval = None
        decision, exit_policy_result = decide_exit_with_policy(
            entry_premium_per_share=entry_per_share,
            current_premium_per_share=current_per_share,
            peak_premium_per_share=peak_per_share,
            dte=dte,
            entry_iv=entry_iv,
            current_iv=current_iv,
            position_id=position_id_for_eval,
            ticker=ticker,
        )
        # Persist EVERY rule evaluation (fired or not). Best-effort —
        # telemetry must never block the close path.
        try:
            persist_exit_evaluations(
                result=exit_policy_result,
                position_id=position_id_for_eval,
                ticker=ticker,
            )
        except Exception:
            logger.debug(
                "exit_rule_evaluations persist raised for %s",
                ticker, exc_info=True,
            )
        if not decision.should_exit:
            return None
        reason = decision.reason
        # Serialize once so both the event payload + the Trade row see
        # the same dict, and the json.dumps cost happens out of the
        # _finalize_execution path.
        try:
            import json as _json
            exit_policy_result_dict = exit_policy_result.to_dict()
            exit_policy_result_json = _json.dumps(exit_policy_result_dict)
        except Exception:
            exit_policy_result_dict = None
            exit_policy_result_json = None

        order = self.executor.close_option(ticker, strike, expiration, reason=reason)
        realized = (order.raw or {}).get("pnl") if order.success else None
        event = {
            "timestamp": datetime.utcnow().isoformat(),
            "ticker": ticker,
            "action": "CLOSE_OPTION",
            "confidence": 1.0,
            "reason": reason,
            "strategy": "exit_manager",
            "status": "closed" if order.success else "failed",
            "instrument": "option",
            "option_type": pos.get("option_type"),
            "strike": strike,
            "expiration": expiration,
            "quantity": float(pos.get("quantity") or 0),
            "price": (order.raw or {}).get("price", 0.0),
            "pnl": realized,
            "order_id": getattr(order, "order_id", None),
            "paper": getattr(order, "paper", True),
        }
        # MITS Phase 17.A item #14 — propagate the mtm staleness warning
        # captured during the peak-update block above so the persisted
        # Trade row carries the breadcrumb.
        if mtm_event_extras.get("mtm_warnings"):
            event["mtm_warnings"] = mtm_event_extras["mtm_warnings"]
        if order.success:
            exit_signal = Signal(
                ticker=ticker, action=Action.CLOSE_OPTION,
                confidence=1.0, reason=reason, strategy="exit_manager",
                metadata={"source": "exit_manager"},
            )
            # MITS Phase 17.A item #12 — plumb realized_vs_marked_delta
            # from the executor's close path. paper_executor stamps
            # this when chain/BS marking succeeds; live executors will
            # populate it once they switch to a real-mark close path.
            close_plan = {
                "instrument": "option", "side": "CLOSE",
                "option_type": pos.get("option_type"),
                "strike": strike, "expiration": expiration,
                "contracts": int(abs(pos.get("quantity") or 0)),
                "realized_vs_marked_delta": (
                    (order.raw or {}).get("realized_vs_marked_delta")
                ),
                "pricing_source": (order.raw or {}).get("pricing_source"),
                # MITS Phase 17.E — attach the rich exit policy result
                # so _persist_trade can stamp Trade.exit_policy_result_json
                # and the cockpit's execution panel can render "Why this
                # exact exit?" with every concurrent trigger surfaced.
                "exit_policy_result_json": exit_policy_result_json,
            }
            self._persist_trade(
                exit_signal,
                float(pos.get("quantity") or 0),
                float((order.raw or {}).get("price") or 0),
                getattr(order, "paper", True),
                pnl=realized, status="closed",
                plan=close_plan,
                snapshot={"price": (order.raw or {}).get("price", 0.0)},
                event=event,
            )
            if realized is not None:
                self.status.daily_pnl += float(realized)
                # Issue 11a — paper_account realized_pnl writeback.
                self._writeback_paper_account_realized(realized)
            try:
                from backend.bot.learning import record_outcome
                record_outcome(ticker, float(realized or 0.0))
            except Exception:
                logger.debug("record_outcome failed for %s", ticker, exc_info=True)

            # MITS Phase 17.A item #9 — when an assignment was settled
            # inside close_option, also write a SECOND Trade row for the
            # stock leg. Without this row the books "lose" the
            # cash-debit-for-shares (CSP) or cash-credit-from-shares
            # (CC) event entirely; analytics + audit would see only
            # the option close.
            assignment = (order.raw or {}).get("assignment")
            if assignment:
                assignment_kind = assignment.get("kind") or ""
                if "put" in assignment_kind:
                    stock_action = Action.BUY_STOCK
                    shares = float(assignment.get("shares_received") or 0)
                else:
                    stock_action = Action.SELL_STOCK
                    shares = float(assignment.get("shares_removed") or 0)
                stock_signal = Signal(
                    ticker=ticker, action=stock_action,
                    confidence=1.0,
                    reason=(
                        f"assignment_from_{ticker}_{strike}_{expiration}"
                    ),
                    strategy="exit_manager",
                    metadata={"source": "assignment"},
                )
                self._persist_trade(
                    stock_signal,
                    shares,
                    float(assignment.get("strike") or strike),
                    getattr(order, "paper", True),
                    pnl=None, status="open",
                    plan={
                        "instrument": "stock",
                        "side": ("BUY" if stock_action == Action.BUY_STOCK
                                 else "SELL"),
                        "pricing_source": "paper_stub",
                    },
                    snapshot={
                        "price": float(assignment.get("strike") or strike),
                    },
                    event={
                        "assignment": assignment,
                        "reason": stock_signal.reason,
                    },
                )
        return event

    def plan_for_session(self, tickers: List[str]) -> DayPlan:
        """Build the rich per-ticker dict and pick today's primary strategy."""
        per_ticker: Dict[str, Dict[str, Any]] = {}
        for ticker in tickers:
            snap = self.market_data.snapshot(ticker)
            per_ticker[ticker] = snap.data
        market_dict: Dict[str, Any] = {"tickers": per_ticker}
        # Lift market-wide context from the first ticker (SPY/QQQ if present).
        seed = per_ticker.get("SPY") or per_ticker.get("QQQ") or next(iter(per_ticker.values()), {})
        for key in ("vix", "spy_trend", "spy_adx", "market_trend"):
            if key in seed:
                market_dict[key] = seed[key]
        plan = self.adaptive.plan_day(tickers, market_dict)
        self.status.day_plan = {
            "primary_strategy": plan.primary_strategy,
            "market_regime": plan.market_regime,
            "recommended_tickers": plan.recommended_tickers,
            "top_scores": dict(sorted(plan.confidence_scores.items(), key=lambda kv: kv[1], reverse=True)[:5]),
            "reason": plan.reason,
        }
        self.status.market_regime = plan.market_regime
        self.status.active_strategy = plan.primary_strategy
        return plan

    # -- per-trade submission helper (used by both legacy + marketplace paths)
    def _finalize_execution(self, *, event: dict, signal: Signal,
                                decision: Any, price: float, data: Dict[str, Any],
                                ticker: str, held: set) -> dict:
        """Build the order plan, run audit, submit, persist on success.

        This is the chunk of ``run_cycle`` that used to be inlined per
        iteration. Extracted so Stage-14.D10 can call it for the survivors
        of the marketplace selection pass without duplicating logic.

        Mutates ``event`` in place + returns it for convenience.
        """
        from backend.bot.audit import audit_order_plan

        is_paper = bool(event.get("paper", True))

        plan = self.build_order_plan(signal, decision.quantity, price)
        plan["stop_loss_price"] = plan.get("stop_loss_price") or decision.stop_loss_price
        plan["take_profit_price"] = plan.get("take_profit_price") or decision.take_profit_price

        audit = audit_order_plan(signal.action.value, plan)
        if not audit.ok:
            event["audit_violations"] = audit.violations
            if is_paper:
                event["status"] = "audit_blocked"
                event["reason"] = "; ".join(
                    v["message"] for v in audit.violations
                )[:240]
                return event
            else:
                logger.warning(
                    "[audit] live order has violations but proceeding: %s",
                    audit.violations,
                )

        # MITS Phase 16.E — pre-fill rollback hook. Default OFF; flag
        # gated via TUNABLES.decision_rollback_enabled. When ON, compare
        # the live regime + IV + correlation to the snapshot persisted
        # on the event during the council pass and abort the trade if
        # any drift threshold trips.
        stale = self._revalidate_decision_pre_fill(
            signal=signal, event=event, ticker=ticker, data=data,
        )
        if stale == "decision_stale":
            event["status"] = stale
            event["reason"] = event.get("rollback_reason") or (
                "decision stale; aborting pre-fill"
            )
            self._persist_decision_stale_evaluation(
                ticker, event["reason"], cycle_id=event.get("timestamp"),
            )
            return event

        # MITS Phase 17.A item #3 — spot_at_emit: the underlying price
        # observed RIGHT BEFORE the order is submitted. Compared with
        # spot_at_fill (post-fill) the operator can see how much the
        # underlying moved while the order was in flight, attributing
        # slippage to "the market moved" vs "we paid the spread".
        spot_at_emit: Optional[float] = None
        try:
            from backend.bot.data.quote_source import get_quote as _get_quote
            q_emit = _get_quote(ticker)
            if q_emit and q_emit.price > 0:
                spot_at_emit = float(q_emit.price)
        except Exception:
            spot_at_emit = None

        # MITS Phase 17.C — stamp the rounded final quantity onto the
        # sizing chain BEFORE submit. The executor's int conversion is
        # captured here so the chain reflects the order the broker
        # actually saw.
        from backend.bot.execution.sizing_chain import finalize_sizing_chain
        finalize_sizing_chain(event, decision.quantity)

        order = self._submit_order(signal, decision.quantity, price, plan=plan)
        filled_qty = plan.get("quantity", decision.quantity)
        event["status"] = "submitted" if order.success else "failed"
        event["order_id"] = order.order_id
        event["paper"] = order.paper
        event["quantity"] = round(float(filled_qty), 4)
        event["price"] = round(price, 2)
        event["instrument"] = plan.get("instrument")
        event["option_type"] = plan.get("option_type")
        event["strike"] = plan.get("strike")
        event["expiration"] = plan.get("expiration")
        if order.success:
            # MITS Phase 17.A item #11 — pricing_source is now a
            # single source of truth. The executor stamps the real
            # source onto order.raw; lift it into plan BEFORE
            # _persist_trade reads plan["pricing_source"]. Without this
            # lift, every Trade row got "paper_stub" regardless of
            # whether the chain or BS fallback actually priced the fill.
            raw_pricing_source = (order.raw or {}).get("pricing_source")
            if raw_pricing_source:
                plan["pricing_source"] = raw_pricing_source

            # MITS Phase 17.A item #4 — bubble slippage + #5 commission
            # + #3 spot fields into plan so _persist_trade writes the
            # new Trade columns from a single dict.
            plan["slippage_bps"] = (order.raw or {}).get("slippage_bps")
            plan["total_commission"] = (order.raw or {}).get("total_commission")
            plan["spot_at_emit"] = spot_at_emit
            # MITS Phase 17.B — lift the structured FillSnapshot JSON
            # (or {"legs": [...]} envelope on multi-leg structures) so
            # _persist_trade writes Trade.fill_snapshot_json from the
            # same single plan dict.
            plan["fill_snapshot_json"] = (order.raw or {}).get(
                "fill_snapshot_json"
            )
            # MITS Phase 17.C — serialize the sizing provenance chain
            # (base_qty, ordered multiplier steps, final_qty, rounded
            # final) so _persist_trade writes Trade.sizing_chain_json
            # from the same single plan dict.
            import json as _json_sc
            plan["sizing_chain_json"] = (
                _json_sc.dumps(event["sizing_chain"])
                if event.get("sizing_chain") else None
            )
            # MITS Phase 17.D — lift the chain-selection provenance off
            # ``plan['chain_selection']`` (set inside build_order_plan
            # by ``_chain_strike``) into the event dict for cockpit/UI
            # readers, then serialize it for _persist_trade so the
            # Trade.chain_selection_json column is populated. Stock
            # trades have no chain_selection — the lift safely no-ops
            # and the column stays NULL.
            cs_obj = plan.get("chain_selection")
            cs_dict: Optional[Dict[str, Any]] = None
            if cs_obj is not None:
                try:
                    cs_dict = cs_obj.to_dict()
                except AttributeError:
                    # Already a dict (e.g. round-tripped through json).
                    cs_dict = cs_obj if isinstance(cs_obj, dict) else None
            if cs_dict is not None:
                event["chain_selection"] = cs_dict
            plan["chain_selection_json"] = (
                _json_sc.dumps(cs_dict) if cs_dict else None
            )
            spot_at_fill: Optional[float] = None
            raw_underlying = (order.raw or {}).get("underlying")
            if raw_underlying:
                try:
                    spot_at_fill = float(raw_underlying)
                except (TypeError, ValueError):
                    spot_at_fill = None
            if spot_at_fill is None:
                try:
                    from backend.bot.data.quote_source import (
                        get_quote as _get_quote2,
                    )
                    q_fill = _get_quote2(ticker)
                    if q_fill and q_fill.price > 0:
                        spot_at_fill = float(q_fill.price)
                except Exception:
                    spot_at_fill = None
            plan["spot_at_fill"] = spot_at_fill

            trade_id = self._persist_trade(
                signal, filled_qty, price, order.paper,
                status="open", plan=plan, snapshot=data, event=event,
            )
            event["trade_id"] = trade_id
            held.add(ticker.upper())
            # MITS Phase 15 follow-up Item 2 — stamp the council's
            # decision-time snapshots into brain_predictions so the
            # nightly linker can score each component against the trade.
            consensus_dict = event.get("consensus") or {}
            if consensus_dict:
                args = self._engine_brain_prediction_args(
                    signal=signal, event=event,
                    consensus_dict=consensus_dict,
                )
                self._persist_brain_prediction_engine(
                    **args, linked_trade_id=trade_id, outcome="pending",
                )
            try:
                from backend.bot.execution_intel import log_execution

                fill_price = float(getattr(order, "raw", {}).get("price") or price)
                log_execution(
                    ticker=ticker,
                    side="BUY" if signal.action.value.startswith("BUY") else "SELL",
                    quantity=float(filled_qty), expected_price=float(price),
                    fill_price=fill_price, trade_id=trade_id,
                )
            except Exception:
                # Bumped from debug to warning — silent execution-telemetry
                # failures were what caused the EXECUTION pillar to read
                # "Unknown" for hours on 2026-05-31.
                logger.warning("execution telemetry failed for %s — fill not logged",
                                  ticker, exc_info=True)
        else:
            event["reason"] = order.error or signal.reason
        return event

    # -- Stage-14.D10 marketplace selection pass -----------------------------
    def _marketplace_finalize(self, *, pending: List[Dict[str, Any]],
                                  events: List[dict], held: set,
                                  capital_available: float,
                                  config_ai: Dict[str, Any]) -> None:
        """Run ``marketplace.select()`` across all deferred candidates, then
        finalize submission for the chosen subset + emit ``marketplace_skipped``
        for the rest.

        ``config_ai`` knobs:
          • ``marketplace_max_positions`` (default 10)
          • ``marketplace_capital_pct`` (default 0.5) — fraction of buying
            power exposed in a single cycle
          • ``marketplace_min_expected_value`` (default 0.0)
        """
        try:
            from backend.bot.marketplace import candidate_from, select
        except Exception:
            logger.warning(
                "marketplace import failed; falling back to direct execution"
            )
            for p in pending:
                self._finalize_execution(
                    event=p["event"], signal=p["signal"],
                    decision=p["decision"], price=p["price"],
                    data=p["data"], ticker=p["ticker"], held=held,
                )
                events.append(p["event"])
                self._emit(p["event"])
            return

        # Synthesize a Candidate per pending execution.
        candidates = []
        idx_by_id: Dict[int, Dict[str, Any]] = {}
        for p in pending:
            sig = p["signal"]
            dec = p["decision"]
            cap_req = max(1.0, float(dec.quantity or 0) * float(p["price"] or 0))
            analytics = (p["event"].get("analytics") or {})
            prob = (analytics.get("probability") or {}).get("probability")
            consensus = (p["event"].get("consensus") or {})
            conf = float(consensus.get("confidence") or sig.confidence or 0.55)
            features = analytics.get("features") or {}
            vol_ratio = float(features.get("volume_ratio") or 1.0)
            liq = max(0.2, min(1.0, vol_ratio / 1.5))
            c = candidate_from(
                ticker=p["ticker"], action=sig.action.value,
                strategy=sig.strategy or "",
                stop_pct=sig.stop_loss, take_profit_pct=sig.take_profit,
                probability=prob, capital_required=cap_req,
                liquidity_score=liq, confidence=conf,
                metadata={"pending_idx": id(p)},
            )
            candidates.append(c)
            idx_by_id[id(p)] = p

        cap_pct = float(config_ai.get("marketplace_capital_pct", 0.5))
        max_pos = int(config_ai.get("marketplace_max_positions", 10))
        min_ev = float(config_ai.get("marketplace_min_expected_value", 0.0))
        budget = max(0.0, capital_available * max(0.0, min(1.0, cap_pct)))

        result = select(
            candidates, capital_available=budget,
            max_positions=max_pos, min_expected_value=min_ev,
        )

        selected_ids = {c.metadata.get("pending_idx") for c in result.selected}
        for c in result.selected:
            p = idx_by_id.get(c.metadata.get("pending_idx"))
            if p is None:
                continue
            evt = p["event"]
            evt["marketplace"] = {"selected": True, "score": c.score,
                                       "expected_value": c.expected_value,
                                       "score_per_dollar": c.score_per_dollar}
            self._finalize_execution(
                event=evt, signal=p["signal"], decision=p["decision"],
                price=p["price"], data=p["data"], ticker=p["ticker"],
                held=held,
            )
            events.append(evt)
            self._emit(evt)

        for c in result.rejected:
            p = idx_by_id.get(c.metadata.get("pending_idx"))
            if p is None:
                continue
            evt = p["event"]
            evt["status"] = "marketplace_skipped"
            evt["reason"] = c.rejection_reason or "not selected by marketplace"
            evt["marketplace"] = {"selected": False,
                                       "rejection_reason": c.rejection_reason,
                                       "score": c.score,
                                       "expected_value": c.expected_value}
            events.append(evt)
            self._emit(evt)

    # -- MITS Phase 7 — discretionary opportunism layer --------------------
    @staticmethod
    def _opportunistic_action_for(side: str) -> Optional[Action]:
        """Map an OpportunisticGateResult.side onto an :class:`Action`.

        ``side`` is one of ``long_put`` | ``long_call`` |
        ``iron_condor`` | ``long_straddle``. Returns ``None`` when the
        side has no representable engine action so the caller can
        gracefully bail.
        """
        s = (side or "").lower()
        if s == "long_put":
            return Action.BUY_PUT
        if s == "long_call":
            return Action.BUY_CALL
        if s == "long_straddle":
            return Action.BUY_STRADDLE
        if s == "iron_condor":
            return Action.IRON_CONDOR
        return None

    def _run_opportunity_pass(self, *, config: dict,
                                 account: Any,
                                 held: Optional[set] = None) -> List[dict]:
        """When the intraday regime is non-normal, ask the Opportunity
        Brain for the single asymmetric trade RIGHT NOW, route it
        through the opportunistic gate + sizing path, and FIRE A REAL
        TRADE via ``_finalize_execution`` so a Trade row is created
        with ``signal_source='intraday_opportunistic'``,
        ``opportunistic=1``, ``must_exit_by_eod=1``, and the full
        hypothesis + regime snapshot in ``detail_json``.

        Returns the produced events. Returns ``[]`` on normal regime,
        or when the Brain abstains, or when conviction falls below
        ``opportunity_brain_min_conviction``.
        """
        events: List[dict] = []
        held = held if held is not None else self._held_tickers()
        regime = (self._current_regime or "normal").lower()
        if regime == "normal":
            self._last_opportunity_hypothesis = None
            return events
        if not self._opportunity_brain.available:
            return events

        # Assemble the live tape blob.
        try:
            from backend.bot.ai.live_tape import assemble_live_context
            live_context = assemble_live_context(
                regime, market_data=self.market_data,
            )
        except Exception:
            logger.debug("live tape assembly failed", exc_info=True)
            return events

        # Ask the Brain.
        try:
            hypothesis = self._opportunity_brain.analyze(
                regime, live_context,
            )
        except Exception:
            logger.warning("opportunity brain analyze raised", exc_info=True)
            return events
        if hypothesis is None:
            return events
        self._last_opportunity_hypothesis = hypothesis

        # Frozen snapshot of the classifier inputs at the moment of trade
        # entry — what the autopsy reads to reconstruct why this fired.
        regime_at_entry: Dict[str, Any] = {"state": regime}
        try:
            cached = getattr(self._intraday_classifier, "_cache", None)
            if cached is not None and hasattr(cached, "to_dict"):
                regime_at_entry = cached.to_dict()
        except Exception:
            pass

        min_conv = float(TUNABLES.opportunity_brain_min_conviction)
        evt: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(),
            "ticker": hypothesis.ticker,
            "action": "OPPORTUNITY_BRAIN",
            "confidence": round(float(hypothesis.conviction), 3),
            "reason": hypothesis.thesis[:240],
            "strategy": "opportunity_brain",
            "intraday_regime": regime,
            "opportunity_hypothesis": hypothesis.to_dict(),
            "regime_at_entry": regime_at_entry,
            "opportunistic": True,
            "paper": True,
        }
        if hypothesis.conviction < min_conv:
            evt["status"] = "below_opportunity_floor"
            evt["reason"] = (
                f"opportunity brain conviction "
                f"{hypothesis.conviction:.2f} < floor {min_conv:.2f}"
            )
            events.append(evt)
            self._emit(evt)
            return events

        # Run through the opportunistic gate.
        try:
            from backend.bot.gates.opportunistic_gate import vet as opp_vet
            snap = self.market_data.snapshot(hypothesis.ticker).data
        except Exception:
            snap = {}
        ctx = {
            "regime_state": regime,
            "atr_30m": snap.get("atr_30m") or snap.get("atr"),
            "price": snap.get("price"),
            "snapshot": snap,
        }
        gate_result = opp_vet(hypothesis, ctx, regime_state=regime)
        evt["opportunistic_gate"] = gate_result.to_dict()
        evt["must_exit_by_eod"] = bool(gate_result.must_exit_by_eod)
        if not gate_result.passes:
            evt["status"] = "opportunistic_blocked"
            evt["reason"] = gate_result.reason or "opportunistic gate blocked"
            events.append(evt)
            self._emit(evt)
            return events

        # MITS Phase 16.D — Opportunity Committee Lite. A 3-agent mini-
        # council (risk / analog / devils-advocate) reviews EVERY brain
        # hypothesis the gate passes. Hard reject from the committee
        # short-circuits the cycle BEFORE sizing; SIZE_DOWN lowers the
        # base multiplier that feeds into ``opportunistic_sizing``.
        try:
            from backend.bot.decision.opportunity_committee import (
                review_opportunity,
            )
            committee_ctx = {
                "snapshot": snap,
                "regime_state": regime,
                "account": account,
                "opportunistic_concurrent_open": (
                    self._opportunistic_concurrent_open
                ),
                "live_context": live_context,
                "simulator_verdict": evt.get("simulator_verdict") or {},
            }
            committee = review_opportunity(hypothesis, committee_ctx)
            evt["opportunity_committee"] = committee.to_dict()
        except Exception:
            logger.warning(
                "opportunity committee review raised", exc_info=True,
            )
            committee = None

        if committee is not None and committee.recommendation == "REJECT":
            evt["status"] = "opportunity_committee_reject"
            evt["reason"] = (
                committee.rec_reason or "opportunity committee rejected"
            )
            events.append(evt)
            self._emit(evt)
            return events

        committee_size_mult = 1.0
        if committee is not None and committee.recommendation == "SIZE_DOWN":
            committee_size_mult = max(
                0.30, float(committee.composite_score),
            )
            evt["opportunity_committee_size_mult"] = committee_size_mult

        # MITS Phase 7 finishing pass — catalyst-gate short-circuit.
        # The standard catalyst gate halves position size on FOMC /
        # earnings days. For OPPORTUNISTIC trades, the regime IS the
        # opportunity — shrinking would defeat the point. We honor only
        # the hard ABSTAIN path (short-DTE option INTO earnings),
        # everything else passes through with multiplier 1.0.
        catalyst_mult = 1.0
        bypass_threshold = float(getattr(
            TUNABLES, "opportunistic_catalyst_bypass_conviction", 0.70))
        skip_catalyst_shrink = (
            regime != "normal"
            and float(hypothesis.conviction) >= bypass_threshold
        )
        try:
            from backend.bot.gates import catalyst_gate
            instrument_for_gate = (
                "spread" if gate_result.instrument == "spread" else "option"
            )
            cgate = catalyst_gate.check(
                hypothesis.ticker,
                instrument=instrument_for_gate,
                dte=int(gate_result.dte),
            )
            evt["catalyst_gate"] = cgate.to_dict()
            evt["catalyst_shrink_skipped"] = bool(skip_catalyst_shrink)
            if not cgate.passes:
                # Short-DTE-into-earnings ABSTAIN ALWAYS wins, even on
                # high-conviction crisis regimes — operator's rule.
                evt["status"] = "catalyst_gate_abstain"
                evt["reason"] = (
                    cgate.reason
                    or "catalyst_gate: abstain on short-DTE earnings"
                )
                events.append(evt)
                self._emit(evt)
                return events
            if not skip_catalyst_shrink:
                catalyst_mult = float(cgate.conviction_multiplier or 1.0)
        except Exception:
            logger.debug("opportunistic catalyst gate failed", exc_info=True)

        # Sizing pass.
        try:
            from backend.bot.eod_sizing import opportunistic_sizing
        except Exception:
            logger.debug("opportunistic_sizing import failed", exc_info=True)
            return events

        price = float(snap.get("price") or 0.0)
        # Minimum 1 contract / 1 share notional placeholder. Sizing
        # multiplier dictates whether we scale up; build_order_plan
        # below converts notional into actual contracts.
        proposed_notional = max(1.0, price * 1.0)
        try:
            # MITS Phase 16.D — committee SIZE_DOWN multiplier chains
            # into the existing ``catalyst_multiplier`` knob so the
            # sizing path stays single-input.
            sizing = opportunistic_sizing(
                conviction=hypothesis.conviction,
                regime=regime,
                equity=float(getattr(account, "portfolio_value", 0.0) or 0.0),
                proposed_notional=proposed_notional,
                daily_notional_used=self._opportunistic_daily_notional,
                concurrent_open=self._opportunistic_concurrent_open,
                catalyst_multiplier=catalyst_mult * committee_size_mult,
            )
        except Exception:
            logger.debug("opportunistic sizing failed", exc_info=True)
            return events

        evt["opportunistic_sizing"] = sizing.to_dict()
        if sizing.multiplier <= 0:
            evt["status"] = "opportunistic_size_zero"
            evt["reason"] = (
                f"opportunistic sizing collapsed: "
                f"{sizing.cap_reason or 'multiplier=0'}"
            )
            events.append(evt)
            self._emit(evt)
            return events

        # Map gate side → Action.
        action = self._opportunistic_action_for(gate_result.side)
        if action is None:
            evt["status"] = "opportunistic_unsupported_side"
            evt["reason"] = (
                f"opportunistic gate side '{gate_result.side}' "
                f"not yet wired to an Action"
            )
            events.append(evt)
            self._emit(evt)
            return events

        # Don't pyramid an already-held ticker.
        if hypothesis.ticker.upper() in held:
            evt["status"] = "opportunistic_already_held"
            evt["reason"] = (
                f"{hypothesis.ticker} already in book; opportunistic pass "
                f"will not pyramid"
            )
            events.append(evt)
            self._emit(evt)
            return events

        # ----- Build a real Signal + finalize execution -----------------
        # signal_source="intraday_opportunistic" → trial scorecard splits
        # the layer cleanly. opportunity_hypothesis + regime_at_entry
        # are carried via metadata + the event so _persist_trade lifts
        # them onto detail_json.
        take_profit_pct = float(getattr(
            TUNABLES, "opportunistic_take_profit_pct", 50.0))
        signal = Signal(
            action=action,
            ticker=hypothesis.ticker,
            confidence=float(hypothesis.conviction),
            reason=(hypothesis.thesis or "opportunistic discretionary")[:240],
            strategy="opportunity_brain",
            stop_loss=gate_result.stop_loss_pct,
            take_profit=take_profit_pct,
            dte=int(gate_result.dte),
            metadata={
                "source": "intraday_opportunistic",
                "opportunity_hypothesis": hypothesis.to_dict(),
                "regime_at_entry": regime_at_entry,
                "opportunistic_gate": gate_result.to_dict(),
                "opportunistic_sizing": sizing.to_dict(),
                "dte": int(gate_result.dte),
            },
        )

        # MITS Phase 17.C — sizing provenance chain for the opportunistic
        # path. Base quantity is 1 contract (the discretionary unit); the
        # committee + catalyst + opportunistic sizer each chain a step.
        # Initialized HERE — the statistical path's init in run_cycle does
        # not run on the opportunistic branch. ``_finalize_execution`` will
        # stamp rounded_final from decision.quantity.
        from backend.bot.execution.sizing_chain import (
            init_sizing_chain as _opp_init_chain,
            record_sizing_step as _opp_record_step,
        )
        _opp_init_chain(evt, 1.0)
        running_qty = 1.0
        if committee is not None and committee.recommendation == "SIZE_DOWN":
            running_qty = _opp_record_step(
                evt,
                name="opportunity_committee.size_mult",
                input_qty=running_qty,
                factor=committee_size_mult,
                evidence={
                    "recommendation": committee.recommendation,
                    "composite_score": float(committee.composite_score),
                },
            )
        if catalyst_mult != 1.0:
            running_qty = _opp_record_step(
                evt,
                name="opportunistic.catalyst_multiplier",
                input_qty=running_qty,
                factor=catalyst_mult,
            )
        running_qty = _opp_record_step(
            evt,
            name="opportunistic.sizing.multiplier",
            input_qty=running_qty,
            factor=float(sizing.multiplier),
            evidence={
                "regime": regime,
                "conviction": float(hypothesis.conviction),
                "cap_reason": sizing.cap_reason,
            },
        )

        # Quantity = number of contracts after sizing multiplier. Floor
        # at 1 so a multiplier of 0.x still trades (the sizing caps
        # already enforce daily/concurrency budgets).
        contracts = max(1, int(round(float(sizing.multiplier))))

        # Track usage BEFORE the execution call so a downstream partial
        # failure doesn't open the door to a duplicate fill on retry.
        self._opportunistic_daily_notional += (
            proposed_notional * sizing.multiplier
        )
        self._opportunistic_concurrent_open += 1

        # Synthesize a Decision-shaped object that _finalize_execution
        # accepts (.quantity, .stop_loss_price, .take_profit_price).
        @dataclass
        class _OppDecision:
            quantity: float = 1.0
            stop_loss_price: Optional[float] = None
            take_profit_price: Optional[float] = None

        decision = _OppDecision(quantity=float(contracts))

        # Run the same path the statistical layer uses — audit
        # invariants apply, _persist_trade picks up opportunity_hypothesis
        # + opportunistic + must_exit_by_eod off the event dict.
        try:
            self._finalize_execution(
                event=evt, signal=signal, decision=decision,
                price=price or 1.0,
                data=snap if isinstance(snap, dict) else {},
                ticker=hypothesis.ticker, held=held,
            )
            if evt.get("status") == "submitted":
                evt["signal_source"] = "intraday_opportunistic"
        except Exception:
            logger.warning(
                "opportunistic execution finalize raised", exc_info=True,
            )
            evt["status"] = "opportunistic_execution_failed"

        events.append(evt)
        self._emit(evt)
        return events

    # -- main loop ----------------------------------------------------------
    def _apply_auto_market_mode(self, session, config: Dict[str, Any]) -> Dict[str, Any]:
        """When ``auto_market_mode`` is enabled (default), keep AI Brain
        and Meta-AI in sync with NYSE hours so the operator never wakes
        up to find the Brain ran overnight burning tokens.

        Fires on TRANSITIONS only (closed→open at 9:30 ET, open→closed
        at 16:00 ET). Between transitions the operator's manual toggles
        in System Controls are respected — flip brain off at 11 AM and
        it stays off until 4 PM (next transition) auto-applies the
        closed-market state. Operators who want full manual control
        can set ``auto_market_mode = false`` in config.

        The first cycle after engine restart always syncs to the current
        state (treated as transition from "unknown"), so starting the bot
        at 11 PM gets brain+meta turned OFF immediately, not waiting for
        the next 16:00 cron.
        """
        if not config.get("auto_market_mode", True):
            return config
        try:
            from backend.bot.calendar import is_us_market_open
            want_on = is_us_market_open()
        except Exception:
            return config
        last = getattr(self, "_last_market_open_state", None)
        if last is not None and last == want_on:
            return config  # same state as last cycle — respect manual toggles
        # Transition (or first sync after restart) — apply the auto state.
        self._last_market_open_state = want_on
        ai = dict(config.get("ai") or {})
        cur_brain = bool(ai.get("brain_enabled"))
        cur_meta = bool(ai.get("meta_enabled"))
        if cur_brain == want_on and cur_meta == want_on:
            return config  # already aligned; no write needed
        ai["brain_enabled"] = want_on
        ai["meta_enabled"] = want_on
        new_cfg = dict(config)
        new_cfg["ai"] = ai
        try:
            from backend.models.config import save_config
            save_config(session, new_cfg)
            logger.info(
                "auto_market_mode: market %s transition — brain/meta -> %s",
                "open" if want_on else "closed",
                "ON" if want_on else "OFF",
            )
        except Exception:
            logger.debug("auto_market_mode persist failed", exc_info=True)
        return new_cfg

    def run_cycle(self) -> List[dict]:
        # MITS Phase 7 — classify the intraday regime FIRST so every
        # downstream gate + the Opportunity Brain see the same view.
        # The classifier caches internally so repeated calls within a
        # 30s window don't re-fetch SPY / sector quotes.
        try:
            regime_state = self._intraday_classifier.classify()
            self._current_regime = regime_state.state
            self.status.intraday_regime = regime_state.state
        except Exception:
            logger.debug("intraday regime classify failed", exc_info=True)
            self._current_regime = "normal"
            self.status.intraday_regime = "normal"

        # MITS Phase 15.A — emit one consolidated regime view per cycle,
        # anchored on SPY (the canonical macro carrier). The vector is
        # logged for observability; downstream consumers (analysis route,
        # agent context, EOD composer) build their own per-ticker vectors.
        try:
            from backend.bot.regime.vector import build_regime_vector
            spy_snap = self.market_data.snapshot("SPY").data
            rv = build_regime_vector(
                ticker="SPY",
                snapshot=spy_snap,
                intraday_classifier=self._intraday_classifier,
            )
            logger.info(
                "regime_vector_built ticker=%s trend=%s iv_rank=%s intraday=%s health=%s",
                rv.ticker, rv.trend.value, rv.iv_rank.value,
                rv.intraday_regime.value, rv.health,
            )
        except Exception:
            logger.debug("regime_vector build failed", exc_info=True)
        with session_scope() as session:
            config = load_config(session)
            # Auto-market-mode: keep brain + meta in sync with NYSE
            # hours. Default ON; disable via auto_market_mode=false.
            config = self._apply_auto_market_mode(session, config)
            # Union config.tickers with the user's watchlist so the
            # bot scans everything the operator added to their watchlist
            # by default. Config.tickers stays as the explicit list;
            # watchlist additions augment it. Deduped + uppercased.
            from backend.models.watchlist import WatchlistItem
            watchlist_tickers = [
                w.ticker.upper().strip()
                for w in session.query(WatchlistItem).all()
                if w.ticker and w.ticker.strip()
            ]
        cfg_tickers = [t.upper().strip() for t in (config.get("tickers") or []) if t]
        seen = set()
        tickers: List[str] = []
        for t in cfg_tickers + watchlist_tickers:
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)

        # MITS Phase 5 (P5.1) — promote high-conviction EOD-bias tickers
        # into the scan universe even when they aren't in the operator's
        # watchlist. The bias map is also passed downstream so the
        # per-ticker loop can tag the resulting trade with
        # signal_source='eod_bias' + apply conviction sizing.
        eod_bias_map: Dict[str, Any] = {}
        try:
            from backend.bot.eod_bias import (
                load_eod_bias, priority_tickers_from_bias,
            )
            eod_bias_map = load_eod_bias()
            for promo in priority_tickers_from_bias(eod_bias_map):
                if promo not in seen:
                    seen.add(promo)
                    tickers.append(promo)
        except Exception:
            logger.debug("eod_bias load failed", exc_info=True)
            eod_bias_map = {}

        if not tickers:
            return []

        # Calendar gate — when NYSE is closed, skip the cycle entirely.
        # Saves ~70% of daily Claude / data costs (market closed
        # overnight + weekends + holidays = 67%+ of clock time). When
        # closed: prices are stale, no trades can execute anyway, and
        # AI Brain reasoning is wasted tokens. Operator override via
        # `force_run_when_closed` in config for backtest / debug.
        try:
            from backend.bot.calendar import is_us_market_open, market_status
            if not config.get("force_run_when_closed") and not is_us_market_open():
                ms = market_status()
                self.status.cycles += 1
                self.status.last_cycle_at = datetime.utcnow()
                ev = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "ticker": "—",
                    "action": "HOLD",
                    "status": "market_closed",
                    "reason": f"NYSE closed ({ms.get('reason')}) — cycle skipped to save tokens",
                    "strategy": "calendar_gate",
                }
                self._emit(ev)
                return [ev]
        except Exception:
            logger.debug("calendar gate check failed; running cycle anyway", exc_info=True)

        strategy_name = config.get("strategy", "adaptive")
        ai_config = config.get("ai") or {}
        # Fully-autonomous "AI Brain": Claude reasons over the whole snapshot and
        # decides directly, unbound by the fixed strategy list. Falls back to the
        # rule strategies whenever no API key is configured.
        # The AI Brain only reasons while the bot is ON (running). When stopped /
        # watch-only it stays "armed" — this matches the UI and avoids surprise
        # paid API calls on a stray cycle while the bot is off.
        use_brain = (
            (strategy_name == "ai_brain" or bool(ai_config.get("brain_enabled")))
            and self.brain.available
            and self.status.running
        )
        strategy: Optional[Strategy] = None
        if use_brain:
            self.status.active_strategy = "ai_brain"
        elif strategy_name == "adaptive":
            plan = self.plan_for_session(tickers)
            strategy = get_strategy(plan.primary_strategy)
            self.status.active_strategy = plan.primary_strategy
        else:
            strategy = select_strategy(strategy_name)
            self.status.active_strategy = strategy.name

        risk = RiskManager(config)
        account_dict = self.executor.get_account_state()
        account = AccountState(
            buying_power=account_dict.get("buying_power", 0.0),
            portfolio_value=account_dict.get("portfolio_value", 0.0),
            open_positions=account_dict.get("open_positions", 0),
            daily_pnl=self.status.daily_pnl,
        )

        auto_execute = bool(config.get("auto_execute", False))
        # Stage-14.D10 — opt-in Decision Marketplace mode. When enabled,
        # the per-ticker loop defers submission and a cross-ticker pass
        # selects the best subset. Default off so legacy behavior is preserved.
        marketplace_enabled = bool((config.get("ai") or {}).get("marketplace_enabled"))

        events: List[dict] = []
        pending_executions: List[Dict[str, Any]] = []

        # ---- 1. Manage existing positions: exit on stop-loss / take-profit ----
        if auto_execute:
            events.extend(self._manage_exits(config))
            # Refresh account after any exits freed up cash.
            account_dict = self.executor.get_account_state()
            account.buying_power = account_dict.get("buying_power", 0.0)
            account.portfolio_value = account_dict.get("portfolio_value", 0.0)
            account.open_positions = account_dict.get("open_positions", 0)

        held = self._held_tickers()
        held_option_keys = self._held_option_keys()

        # Portfolio-wide risk snapshot (sector/theme concentration, net beta /
        # delta) — cheap and attached to every actionable event so the UI and
        # downstream meta-AI / ranker can reason about portfolio exposure.
        portfolio_risk_dict = None
        try:
            from backend.bot.portfolio_intel import assess_portfolio

            if hasattr(self.executor, "positions"):
                portfolio_risk_dict = assess_portfolio(self.executor.positions() or []).to_dict()
        except Exception:
            portfolio_risk_dict = None

        # ---- AI Brain: one reasoning pass over the whole watchlist -----------
        brain_signals: Dict[str, Signal] = {}
        brain_snaps: Dict[str, Dict[str, Any]] = {}
        if use_brain:
            # Skip tickers we already hold — the engine blocks pyramiding
            # via the `already_held` gate downstream, so asking the Brain
            # to reason about NVDA when we already own NVDA is wasted
            # tokens. The held tickers still flow through the per-ticker
            # loop below and get an `already_held` event; we just don't
            # spend Claude on them.
            brain_snaps = {
                tk: self.market_data.snapshot(tk).data
                for tk in tickers if tk.upper() not in held
            }
            # MITS Phase 1 — inject the knowledge-graph evidence block into
            # each snapshot the Brain sees so its prompt can reason OVER
            # historical analogs. Fail-open: if the corpus is cold the
            # block stays empty and the Brain's prompt just doesn't show
            # a "Memory says" line for that ticker.
            try:
                from backend.bot.agent_context import load_knowledge_evidence
                for tk, snap in brain_snaps.items():
                    try:
                        ke = load_knowledge_evidence(
                            ticker=tk,
                            regime=(snap.get("regime") or "unknown"),
                            vol_state=(snap.get("vol_state") or "normal"),
                            snapshot=snap,
                        )
                        if ke and ke.get("cells"):
                            snap["knowledge_evidence"] = ke
                    except Exception:
                        continue
            except Exception:
                logger.debug("brain knowledge evidence injection failed",
                                    exc_info=True)

            # MITS Phase 11.I — also weave Form 4 insider + 13F top-funds
            # + similar-regime analogs into the per-ticker brain snapshot.
            # Cheap query per ticker (already-indexed tables) and the
            # _fmt_snapshot helper in brain.py renders them as
            # "Recent insider activity / Smart money / Today most
            # resembles" lines. Fail-open per ticker.
            try:
                from backend.bot.agent_context import build_agent_context
                for tk, snap in brain_snaps.items():
                    try:
                        enr = build_agent_context(
                            ticker=tk,
                            action="evaluate",
                            strategy=str(snap.get("strategy") or "unknown"),
                            snapshot=snap,
                            analytics={"regime": {
                                "trend": snap.get("regime"),
                                "volatility": snap.get("vol_state"),
                                "gamma": snap.get("gamma_regime"),
                            }},
                        )
                        if enr.get("insider_recent"):
                            snap["insider_recent"] = enr["insider_recent"]
                            snap["insider_cluster_buy_30d"] = (
                                enr.get("insider_cluster_buy_30d"))
                            snap["insider_cluster_distinct_buyers_30d"] = (
                                enr.get(
                                    "insider_cluster_distinct_buyers_30d"))
                        if enr.get("smart_money", {}).get("top_funds"):
                            snap["smart_money"] = enr["smart_money"]
                        if enr.get("similar_regime_days"):
                            snap["similar_regime_days"] = (
                                enr["similar_regime_days"])
                    except Exception:
                        continue
            except Exception:
                logger.debug("brain Phase 11.I enrichment failed",
                                    exc_info=True)
            portfolio_ctx = {
                "cash": account.buying_power,
                "portfolio_value": account.portfolio_value,
                "open_positions": account.open_positions,
                "daily_pnl": self.status.daily_pnl,
                "held": sorted(held),
                "min_confidence": config.get("min_confidence", 0.4),
                "max_position_usd": (config.get("risk") or {}).get("max_position_size_usd"),
                "web_research": bool(ai_config.get("brain_web_research")),
            }
            brain_signals = self.brain.decide_portfolio(brain_snaps, portfolio_ctx)

        # ---- 2. Look for new entries -----------------------------------------
        # Stage-8 global kill-switch — single source of truth, beats every
        # other config. If active, no new entries fire at all.
        try:
            from backend.bot.canary import kill_switch_active
            kill_active = kill_switch_active()
        except Exception:
            kill_active = False
        if kill_active:
            logger.warning("[engine] kill-switch ACTIVE — skipping all new entries")

        # Stage-4 event-risk gate. Compute event auto-hold once per cycle and
        # consult per-ticker. Default ON in paper to model real-world caution;
        # opt out via config.event_risk.enabled=false.
        event_cfg = (config.get("event_risk") or {})
        event_risk_enabled = event_cfg.get("enabled", True)

        import time as _time
        for ticker in tickers:
            if use_brain:
                # Brain cooldown: if this ticker was rejected by any safety
                # gate within the cooldown window, treat the proposal as
                # HOLD. Stops the spam-retry-until-it-passes pattern that
                # produced the AMD BUY_CALL on 2026-06-01.
                tk_upper = ticker.upper()
                cooldown_until = self._brain_cooldown.get(tk_upper, 0.0)
                if cooldown_until > _time.time():
                    remaining = int(cooldown_until - _time.time())
                    signal = Signal.hold(
                        ticker, "ai_brain",
                        f"cooldown: ticker rejected recently, retry in {remaining}s",
                    )
                    data = brain_snaps.get(ticker) or self.market_data.snapshot(ticker).data
                else:
                    data = brain_snaps.get(ticker) or self.market_data.snapshot(ticker).data
                    signal = brain_signals.get(tk_upper) or Signal.hold(ticker, "ai_brain", "no actionable setup")
            else:
                data = self.market_data.snapshot(ticker).data
                rule_signal = strategy.analyze(ticker, data)
                # AI blend (no-op when neither claude nor ml is enabled).
                signal = self.blender.blend(ticker, data, rule_signal, ai_config=ai_config)
            event: Dict[str, Any] = {
                "timestamp": datetime.utcnow().isoformat(),
                "ticker": ticker,
                "action": signal.action.value,
                "confidence": round(signal.confidence, 3),
                "reason": signal.reason,
                "approach": signal.metadata.get("approach"),
                "strategy": signal.strategy or (strategy.name if strategy else "ai_brain"),
                "ai_components": signal.metadata.get("ai_components"),
            }

            # MITS Phase 18-FU Gap R3 — pre-policy StrategyMatrix lift.
            # The legacy build site lives inside ``rule_consensus_exception``
            # and only fires when the policy chain reaches that rule. That
            # left ~46% of evaluations (kill_switch, options_disabled,
            # abstain, low_confidence, already_held, ...) without matrix
            # coverage on their persisted ``decision_provenance`` row.
            # Lifting the build HERE, before the policy chain, plus the
            # 5-minute TTL cache in ``strategy_matrix_cache`` (zero cost on
            # subsequent calls within a bucket), drives matrix coverage to
            # 100% of evaluations. The consensus rule still calls the cache
            # — same key → hit → free — so the existing wire-up survives.
            # Fail-open in every branch: builder exceptions never block.
            if bool(getattr(
                TUNABLES, "engine_strategy_matrix_enabled", True,
            )):
                try:
                    self._populate_strategy_matrix(event=event, ticker=ticker, data=data, signal=signal)
                except Exception:
                    logger.debug(
                        "pre-policy strategy_matrix lift failed for %s",
                        ticker, exc_info=True,
                    )

            # MITS Phase 5 (P5.1) — annotate event with the EOD bias for
            # this ticker (if any). High-conviction setups are tagged
            # with signal_source='eod_bias' so the trade row + decision
            # log persist the corpus→trade attribution. Lower-conviction
            # rows surface as "info_only" → operators see the corpus
            # weighed in but the bot didn't auto-promote it.
            bias_row = eod_bias_map.get(ticker.upper()) if eod_bias_map else None
            if bias_row is not None:
                event["eod_bias"] = bias_row.to_dict()
                if bias_row.is_high_conviction():
                    signal.metadata = dict(signal.metadata or {})
                    signal.metadata["source"] = "eod_bias"
                    signal.metadata["eod_bias_rank"] = bias_row.rank
                    signal.metadata["eod_bias_posterior"] = bias_row.posterior

            # MITS Phase 16.A — declarative decision policy. Every gate
            # the legacy procedural block used to enforce (kill switch,
            # options pause, abstain, event-risk, catalyst, analytics,
            # hold, low_confidence, drift, low_grade, IV richness, meta,
            # consensus + simulator + correlation cap, already-held,
            # risk-manager, dust order) now lives as a registered
            # ``PolicyRule``. The policy emits a structured result;
            # ``event["status"]`` is set to the headline blocker's
            # ``legacy_status`` so every downstream consumer (UI,
            # decision_log analytics, gate_diagnostics) sees the same
            # strings as before.
            policy_ctx = PolicyContext(
                ticker=ticker,
                signal=signal,
                event=event,
                data=data,
                analytics_cfg=(config.get("analytics") or {}),
                ai_config=ai_config,
                config=config,
                kill_active=kill_active,
                portfolio_risk_dict=portfolio_risk_dict,
                eod_bias_map=eod_bias_map,
                brain_cooldown=self._brain_cooldown,
                use_brain=use_brain,
                cycle_id=event.get("timestamp"),
                held_tickers=set(held),
                held_option_keys=set(held_option_keys),
                risk_manager=risk,
                account=account,
                analytics_engine=self.analytics,
                meta_engine=self.meta,
                intraday_classifier=self._intraday_classifier,
                executor=self.executor,
            )
            policy_ctx.scratch["min_notional"] = self.MIN_NOTIONAL
            policy_ctx.scratch["brain_cooldown_seconds"] = (
                self._brain_cooldown_seconds
            )
            policy_ctx.scratch["active_strategy"] = (
                self.status.active_strategy
            )
            _t_policy = _time.monotonic()
            policy_result = self._decision_policy.evaluate(policy_ctx)
            event["policy_eval_ms"] = round(
                (_time.monotonic() - _t_policy) * 1000.0, 3,
            )
            event["policy_result"] = policy_result.to_dict()
            self._persist_policy_evaluations(
                policy_result, ticker, cycle_id=event.get("timestamp"),
            )

            if not policy_result.eligible:
                headline = policy_result.headline_blocker()
                if headline is None:
                    raise RuntimeError(
                        "policy ineligible without a hard blocker — "
                        "registration order invariant violated"
                    )
                event["status"] = headline.legacy_status
                if headline.override_event_reason:
                    event["reason"] = headline.reason
                event["blocking_factors"] = [
                    b.to_dict() for b in policy_result.blocking_factors
                ]
                events.append(event)
                self._emit(event)
                continue

            event["policy_soft_penalty_pct"] = (
                policy_result.soft_penalties_total_pct
            )
            if policy_result.blocking_factors:
                event["blocking_factors"] = [
                    b.to_dict() for b in policy_result.blocking_factors
                ]
            # Pull artifacts the policy populated for the sizing layer
            # below: meta_dict drives the meta-AI sizing multiplier;
            # decision + price feed eod sizing + finalize.
            meta_dict = policy_ctx.scratch.get("meta_dict")
            decision = policy_ctx.scratch["risk_decision"]
            price = float(policy_ctx.scratch["price"])

            # MITS Phase 17.C — seed the sizing provenance chain with the
            # risk-manager's baseline quantity. Every subsequent multiplier
            # in this block appends one step; ``_finalize_execution``
            # finalizes with the rounded quantity the executor receives.
            from backend.bot.execution.sizing_chain import (
                finalize_sizing_chain, init_sizing_chain, record_sizing_step,
            )
            init_sizing_chain(event, decision.quantity)

            # Council sizing inputs come from the consensus + chairman
            # blobs the policy populated. The chairman comment in
            # ``backend/bot/agents/chairman.py`` (position_size_modifier)
            # documents them as multipliers on the consensus
            # size_multiplier — chain both onto the base quantity here so
            # the recorded provenance matches the documented design.
            consensus_blob = event.get("consensus") or {}
            cons_size_mult = consensus_blob.get("size_multiplier")
            if cons_size_mult is not None and decision.quantity > 0:
                cons_size_mult = float(cons_size_mult)
                if cons_size_mult != 1.0:
                    decision.quantity = round(record_sizing_step(
                        event,
                        name="consensus.size_multiplier",
                        input_qty=decision.quantity,
                        factor=cons_size_mult,
                        evidence={
                            "recommendation": consensus_blob.get("recommendation"),
                            "confidence": consensus_blob.get("confidence"),
                        },
                    ), 4)
                    event["consensus_size_applied"] = cons_size_mult

            chairman_report = consensus_blob.get("chairman_report") or {}
            chairman_psm = chairman_report.get("position_size_modifier")
            if chairman_psm is not None and decision.quantity > 0:
                chairman_psm = float(chairman_psm)
                if chairman_psm != 1.0:
                    decision.quantity = round(record_sizing_step(
                        event,
                        name="chairman.position_size_modifier",
                        input_qty=decision.quantity,
                        factor=chairman_psm,
                        evidence={
                            "decision": chairman_report.get("decision"),
                            "overlap_coefficient": chairman_report.get(
                                "overlap_coefficient"),
                        },
                    ), 4)
                    event["chairman_size_applied"] = chairman_psm

            # Apply the meta-AI's position-sizing modifier (e.g. 0.7 → take 70%).
            if meta_dict and decision.quantity > 0:
                rm = float(meta_dict.get("risk_modifier") or 1.0)
                if rm != 1.0:
                    decision.quantity = round(record_sizing_step(
                        event,
                        name="meta_ai.risk_modifier",
                        input_qty=decision.quantity,
                        factor=rm,
                    ), 4)
                    event["meta_size_applied"] = rm

            # MITS Phase 5 (P5.3) — conviction-weighted sizing for
            # eod_bias-sourced trades + catalyst-multiplier rollup. Trades
            # NOT sourced from eod_bias still receive the catalyst
            # multiplier (it's a portfolio-wide gate) but skip the rank
            # multiplier. Cap by daily EOD-bias notional is enforced
            # via apply_conviction_sizing's truncate path.
            try:
                from backend.bot.eod_sizing import apply_conviction_sizing
                is_eod_bias_trade = (
                    (signal.metadata or {}).get("source") == "eod_bias"
                )
                catalyst_mult = float(event.get("catalyst_multiplier") or 1.0)
                if is_eod_bias_trade and decision.quantity > 0:
                    rank = int((signal.metadata or {}).get(
                        "eod_bias_rank") or 99)
                    proposed_notional = float(decision.quantity) * float(price)
                    high_conv_open = getattr(
                        self, "_eod_high_conviction_open_today", 0)
                    daily_notional = getattr(
                        self, "_eod_daily_notional_today", 0.0)
                    sizing = apply_conviction_sizing(
                        rank=rank,
                        high_conviction_open=high_conv_open,
                        daily_notional_used=daily_notional,
                        equity=float(account.portfolio_value),
                        proposed_notional=proposed_notional,
                        catalyst_multiplier=catalyst_mult,
                    )
                    event["eod_sizing"] = sizing.to_dict()
                    if sizing.multiplier <= 0:
                        event["status"] = "eod_size_zero"
                        event["reason"] = (
                            f"eod_bias sizing collapsed: "
                            f"{sizing.cap_reason or 'multiplier=0'}"
                        )
                        events.append(event)
                        self._emit(event)
                        continue
                    decision.quantity = round(record_sizing_step(
                        event,
                        name="eod.conviction_sizing",
                        input_qty=decision.quantity,
                        factor=sizing.multiplier,
                        evidence={
                            "rank_tier": sizing.rank_tier,
                            "cap_reason": sizing.cap_reason,
                            "catalyst_multiplier": catalyst_mult,
                        },
                    ), 4)
                    # Track the bias trade against the daily caps so the
                    # NEXT eod_bias trade in this cycle sees the updated
                    # usage. Increment only when we know the trade will
                    # try to execute. Tracker resets at post-market.
                    self._eod_daily_notional_today = float(
                        daily_notional) + (
                        float(decision.quantity) * float(price)
                    )
                    if sizing.rank_tier in ("rank_1", "rank_2_3"):
                        self._eod_high_conviction_open_today = (
                            high_conv_open + 1
                        )
                elif catalyst_mult != 1.0 and decision.quantity > 0:
                    decision.quantity = round(record_sizing_step(
                        event,
                        name="catalyst.multiplier",
                        input_qty=decision.quantity,
                        factor=catalyst_mult,
                    ), 4)
                    event["catalyst_size_applied"] = catalyst_mult
            except Exception:
                logger.debug("conviction sizing failed for %s",
                                  ticker, exc_info=True)

            # MITS Phase 16.C — chain the correlation soft-cap sizing
            # multiplier into the sizing pipeline. The cap result is
            # written into event["correlation_cap"] by
            # rule_correlation_cap_block. When |rho| sits in the soft
            # zone (0.5..rho_thr) the multiplier is < 1.0 and we shrink
            # the proposed quantity proportionally. Hard blocks already
            # short-circuited upstream via the policy gate.
            try:
                corr_blob = event.get("correlation_cap") or {}
                corr_mult = float(corr_blob.get("sizing_multiplier", 1.0))
            except (TypeError, ValueError):
                corr_mult = 1.0
            if corr_mult != 1.0 and decision.quantity > 0:
                corr_blob_evidence = event.get("correlation_cap") or {}
                decision.quantity = round(record_sizing_step(
                    event,
                    name="correlation_cap.sizing_multiplier",
                    input_qty=decision.quantity,
                    factor=corr_mult,
                    evidence={
                        "worst_rho": corr_blob_evidence.get("worst_rho"),
                        "worst_peer": corr_blob_evidence.get("worst_peer"),
                    },
                ), 4)
                event["correlation_size_applied"] = corr_mult

            # Post-sizing dust check. The policy registers
            # ``dust_order`` as deferred so the main evaluate() pass
            # skips it — only the engine here, AFTER eod sizing has
            # applied its multiplier, knows the final quantity.
            # Persist one ``policy_rule_evaluations`` row by hand so
            # /policy/veto-budget sees the verdict.
            from backend.bot.decision.rules import rule_dust_order
            policy_ctx.scratch["risk_decision"] = decision
            policy_ctx.scratch["price"] = price
            dust_bf = rule_dust_order(policy_ctx)
            self._persist_dust_evaluation(
                ticker, dust_bf, cycle_id=event.get("timestamp"),
            )
            if dust_bf is not None:
                event["status"] = dust_bf.legacy_status
                event["reason"] = dust_bf.reason
                bfs = event.get("blocking_factors") or []
                bfs.append(dust_bf.to_dict())
                event["blocking_factors"] = bfs
                events.append(event)
                self._emit(event)
                continue

            # Auto-execute gate: when OFF, emit a "signal_only" event so the UI
            # alerts the user but no order goes out.
            if not auto_execute:
                event["status"] = "signal_only"
                event["paper"] = True
                events.append(event)
                self._emit(event)
                continue

            # Stage-14.D10 — marketplace mode: defer submission so a
            # cross-ticker selection pass can pick the best subset after
            # all per-ticker gates have evaluated. Default OFF; only
            # active when ai.marketplace_enabled is true. Legacy behavior
            # is preserved bit-for-bit when the flag is off.
            if marketplace_enabled:
                pending_executions.append({
                    "event": event, "signal": signal, "decision": decision,
                    "price": price, "data": data, "ticker": ticker,
                })
                continue

            self._finalize_execution(
                event=event, signal=signal, decision=decision,
                price=price, data=data, ticker=ticker, held=held,
            )
            events.append(event)
            self._emit(event)

        # Stage-14.D10 — run the marketplace selection pass on pending events,
        # then finalize the chosen subset and emit skip-reasons for the rest.
        if marketplace_enabled and pending_executions:
            self._marketplace_finalize(
                pending=pending_executions, events=events,
                held=held, capital_available=float(account.buying_power),
                config_ai=ai_config,
            )

        # MITS Phase 7 — on non-normal intraday regimes, run the
        # discretionary Opportunity Brain pass AFTER the statistical
        # layer so operators can see both paths in the activity feed.
        # The opportunity events carry signal_source=intraday_opportunistic
        # so the trial scorecard can attribute P&L cleanly. The held
        # set is threaded through so the opportunistic path doesn't
        # pyramid onto a ticker the statistical layer just opened.
        try:
            opp_events = self._run_opportunity_pass(
                config=config, account=account, held=held,
            )
            if opp_events:
                events.extend(opp_events)
        except Exception:
            logger.debug("opportunity pass failed", exc_info=True)

        # MITS Phase 15 follow-up Item 2 — capture the council's
        # decision-time snapshots on every blocked-post-consensus event
        # so the nightly linker can grade the council on the gates that
        # rejected too, not just the trades that fired. Executed events
        # were already written from _finalize_execution.
        self._sweep_block_brain_predictions(events)

        self.status.cycles += 1
        self.status.last_cycle_at = datetime.utcnow().isoformat()
        # P3.2 — every Nth cycle, reconcile DB-side PaperPosition rows
        # against executor.positions(). Drift means a write bypassed the
        # audit invariant. Auto-heal isn't safe; emit a warning instead.
        if self.status.cycles % 10 == 0:
            self._reconcile_positions()
        # Snapshot the *post-trade* account so the equity curve reflects fills.
        # DRIFT.FIX — fetch positions ONCE and share between get_account_state
        # and _record_equity_snapshot. Two independent positions() calls used
        # to cause cents-level drift from fresh-price ticks between them; one
        # shared call is drift-free by construction.
        marked_positions: list = []
        try:
            if hasattr(self.executor, "positions"):
                marked_positions = self.executor.positions()
        except Exception:
            marked_positions = []
        try:
            final_state = self.executor.get_account_state(
                positions=marked_positions
            ) if hasattr(self.executor, "positions") else (
                self.executor.get_account_state()
            )
        except TypeError:
            # Brokers that haven't been updated to accept ``positions``
            # (e.g. AlpacaExecutor) — fall back to the no-arg signature.
            final_state = self.executor.get_account_state()
        except Exception:
            final_state = account_dict
        self._record_equity_snapshot(final_state, positions=marked_positions)
        return events

    def _reconcile_positions(self) -> None:
        """P3.2 — verify PaperPosition table matches executor.positions().
        Drift = a write bypassed the audit invariant. Don't auto-heal —
        just surface a SystemWarning so the operator investigates."""
        try:
            from backend.models.paper import PaperPosition
            from backend.db import session_scope
            exec_positions = (self.executor.positions()
                                  if hasattr(self.executor, "positions") else [])
            exec_ids = {(p.get("ticker") or "").upper()
                            for p in exec_positions
                            if float(p.get("quantity") or 0) != 0}
            with session_scope() as session:
                db_ids = {(p.ticker or "").upper()
                              for p in session.query(PaperPosition).all()
                              if abs(p.quantity or 0) > 1e-9}
            only_db = db_ids - exec_ids
            only_exec = exec_ids - db_ids
            if only_db or only_exec:
                logger.warning(
                    "position reconciliation drift: only_in_db=%s "
                    "only_in_executor=%s",
                    sorted(only_db), sorted(only_exec),
                )
                try:
                    from backend.bot.alerts import ALERT_CENTER, Alert
                    ALERT_CENTER.add(Alert(
                        severity="warning",
                        title="position reconciliation drift",
                        body=(f"DB-only: {sorted(only_db)}; "
                                  f"executor-only: {sorted(only_exec)}. "
                                  f"A write bypassed the audit invariant."),
                    ))
                except Exception:
                    pass
        except Exception:
            logger.debug("position reconciliation check failed",
                              exc_info=True)

    def _record_equity_snapshot(self, account_dict: Dict[str, Any],
                                positions: Optional[list] = None) -> None:
        """Persist a portfolio-value snapshot so the equity curve has data.

        P1.3 — account reconciliation invariant at cycle close. Asserts
        ``cash + sum(position_value) ≈ portfolio_value`` within $0.01.
        Drift surfaces as a SystemWarning and the snapshot's data_quality
        is downgraded to ``degraded``. Catches silent money leaks.

        P1.4 — snapshot integrity contract. We re-read positions here so
        the snapshot value matches account state at this exact moment,
        not a stale read from earlier in the cycle (fixes the #142 class
        of MTM-discontinuity bug).

        P1.6 — quality fields populated on every write.
        """
        import json as _json
        try:
            portfolio_value = float(account_dict.get("portfolio_value", 0.0))
            cash = float(account_dict.get("cash",
                                              account_dict.get("buying_power", 0.0)))
            # Account reconciliation — sum position values via executor.
            data_quality = "good"
            pricing_mix: Dict[str, int] = {}
            try:
                # DRIFT.FIX — prefer the positions list passed from the
                # caller (same MTM pass that produced account_dict). Only
                # fetch fresh as a fallback for callers that didn't pass.
                if positions is None:
                    positions = (self.executor.positions()
                                 if hasattr(self.executor, "positions")
                                 else [])
                position_value = 0.0
                for p in positions:
                    qty = float(p.get("quantity") or 0)
                    if p.get("kind", "stock") == "stock":
                        px = float(p.get("current_price")
                                       or p.get("avg_cost") or 0)
                        position_value += qty * px
                    else:
                        # Options: market_value is the per-contract MTM total
                        # (mark_per_share × 100 × contracts). Use it directly
                        # so the contract multiplier is preserved. avg_cost is
                        # the per-contract premium, so the fallback multiplies
                        # by |qty| (contracts), NOT current_price (per-share —
                        # would silently drop the 100× and was the source of
                        # the $565 phantom drift on 2026-06-04).
                        mv = p.get("market_value")
                        if mv:
                            position_value += float(mv)
                        else:
                            position_value += abs(qty) * float(
                                p.get("avg_cost") or 0
                            )
                    src = p.get("pricing_source") or "paper_stub"
                    pricing_mix[src] = pricing_mix.get(src, 0) + 1
                drift = portfolio_value - (cash + position_value)
                # DRIFT.FIX (2026-06-05) — with the shared positions list,
                # portfolio_value and the position_value sum here are
                # mathematically the same numbers, so drift can only come
                # from round(.., 2) accumulation. Tolerate $1 (5¢ × 20
                # positions is the realistic worst case). Real bugs
                # (multiplier slips, sign flips) move the needle by
                # hundreds, well above this floor.
                drift_tolerance = max(1.00, 0.05 * len(positions))
                if abs(drift) > drift_tolerance:
                    data_quality = "degraded"
                    logger.warning(
                        "snapshot drift: portfolio_value=%.2f cash=%.2f "
                        "positions=%.2f drift=%.2f — accounting may be leaking",
                        portfolio_value, cash, position_value, drift,
                    )
            except Exception:
                data_quality = "partial"
                logger.debug("snapshot reconciliation probe failed",
                                  exc_info=True)

            with session_scope() as session:
                session.add(
                    PortfolioSnapshot(
                        portfolio_value=portfolio_value,
                        cash=cash,
                        realized_pnl=float(account_dict.get("realized_pnl", 0.0)),
                        open_positions=int(account_dict.get("open_positions", 0)),
                        broker=self.executor.__class__.__name__,
                        # P1.6 quality fields.
                        data_quality=data_quality,
                        accounting_version=1,   # bumped to 2 after Phase 2.
                        pricing_source_mix=(_json.dumps(pricing_mix)
                                                if pricing_mix else None),
                        excludes_synthetic=1,
                    )
                )
        except Exception:
            logger.exception("failed to persist equity snapshot")

    @staticmethod
    def _default_expiration(dte: Optional[int]) -> str:
        """Pick an expiration date ~dte days out (default 30), as YYYY-MM-DD."""
        from datetime import timedelta

        days = int(dte) if dte else 30
        target = datetime.utcnow().date() + timedelta(days=max(1, days))
        return target.isoformat()

    def build_order_plan(self, signal: Signal, quantity: float, price: float) -> Dict[str, Any]:
        """Describe the concrete instrument an action maps to.

        Returns a dict the executor + persistence layer both understand:
        instrument, side/option_type, strike, expiration, contracts, qty, and
        stop/target prices.
        """
        action = signal.action
        stop_pct = (signal.stop_loss or 0) / 100.0
        take_pct = (signal.take_profit or 0) / 100.0
        plan: Dict[str, Any] = {"instrument": "stock"}

        if action in STOCK_ACTIONS:
            side = "BUY" if action == Action.BUY_STOCK else "SELL"
            plan.update(
                instrument="stock",
                side=side,
                quantity=round(quantity, 4),
                stop_loss_price=round(price * (1 - stop_pct), 2) if stop_pct and side == "BUY" else None,
                take_profit_price=round(price * (1 + take_pct), 2) if take_pct and side == "BUY" else None,
            )
            return plan

        from backend.bot.data.options import chain_strike as _ds_chain_strike
        from backend.bot.data.chain_selection import (
            _paper_stub_provenance,
            chain_strike_with_provenance as _chain_strike_with_prov,
        )
        from backend.bot.options_chain import nearest_available_strike

        # MITS Phase 17.D — every option strike picked through ``_chain_strike``
        # also stamps a ChainSelectionProvenance row into ``plan['chain_selection']``.
        # Multi-leg spreads call ``_chain_strike`` once per side; the LAST call
        # wins (the dominant leg) because that's the strike the executor + UI
        # treat as the canonical contract. Per-leg provenance is captured in
        # ``signal.metadata['legs']`` for multi-leg structures.
        chain_selection_holder: Dict[str, Any] = {"prov": None}

        def _chain_strike(target_price: float, option_type: str,
                            moneyness: float = 0.0,
                            side: str = "BUY") -> float:
            """Pick a real listed, liquid strike. Three-tier fallback:

                1. ``chain_strike_with_provenance`` (ThetaData, sanity-gated,
                   liquidity-filtered, AND records the considered candidate
                   set) — Phase 17.D upgrade over the bare ``chain_strike``.
                2. ``nearest_available_strike`` (legacy yfinance/cboe path,
                   kept as a fallback so engine never breaks if ThetaData
                   is down and the legacy code happens to have a stale
                   cache)
                3. The arithmetic ``snap_strike`` is baked into
                   ``_ds_chain_strike`` as its own last resort.
            """
            # 17.D — primary path. Carries the provenance side-channel.
            try:
                _exp, strike, _opt, prov = _chain_strike_with_prov(
                    signal.ticker, target_price, option_type,
                    side=side, moneyness=moneyness,
                    target_dte=int(signal.dte or 30),
                )
                if strike > 0:
                    chain_selection_holder["prov"] = prov
                    return strike
            except Exception:
                pass
            # Tier 2 — legacy ``chain_strike`` (no provenance side-channel).
            try:
                strike = _ds_chain_strike(
                    signal.ticker, target_price, option_type,
                    moneyness=moneyness,
                )
                if strike > 0:
                    # Record a paper_stub provenance so the persisted Trade
                    # row still answers "why this contract?" — degraded
                    # source, but the chosen contract is recorded.
                    chain_selection_holder["prov"] = _paper_stub_provenance(
                        ticker=signal.ticker,
                        direction=(
                            ("long_" if side.upper() == "BUY" else "short_")
                            + ("call" if option_type.lower().startswith("c")
                                else "put")
                        ),
                        requested_dte=int(signal.dte or 30),
                        requested_delta_band=(0.30, 0.45),
                        underlying_spot=target_price,
                        chosen_expiry="",
                        chosen_strike=float(strike),
                        chosen_option_type=(
                            "C" if option_type.lower().startswith("c") else "P"
                        ),
                    )
                    return strike
            except Exception:
                pass
            target = target_price * (1 + moneyness)
            try:
                strike, _src = nearest_available_strike(
                    signal.ticker, target=target, kind=option_type,
                    spot_hint=target_price,
                )
                if strike > 0:
                    chain_selection_holder["prov"] = _paper_stub_provenance(
                        ticker=signal.ticker,
                        direction=(
                            ("long_" if side.upper() == "BUY" else "short_")
                            + ("call" if option_type.lower().startswith("c")
                                else "put")
                        ),
                        requested_dte=int(signal.dte or 30),
                        requested_delta_band=(0.30, 0.45),
                        underlying_spot=target_price,
                        chosen_expiry="",
                        chosen_strike=float(strike),
                        chosen_option_type=(
                            "C" if option_type.lower().startswith("c") else "P"
                        ),
                    )
                    return strike
            except Exception:
                pass
            # _ds_chain_strike's own snap_strike fallback already covered
            # the success case; this just guarantees a non-zero return
            # when both chain paths above blew up entirely.
            from backend.bot.data.options import snap_strike
            arithmetic_strike = snap_strike(
                target_price, option_type, moneyness=moneyness,
            )
            chain_selection_holder["prov"] = _paper_stub_provenance(
                ticker=signal.ticker,
                direction=(
                    ("long_" if side.upper() == "BUY" else "short_")
                    + ("call" if option_type.lower().startswith("c") else "put")
                ),
                requested_dte=int(signal.dte or 30),
                requested_delta_band=(0.30, 0.45),
                underlying_spot=target_price,
                chosen_expiry="",
                chosen_strike=float(arithmetic_strike),
                chosen_option_type=(
                    "C" if option_type.lower().startswith("c") else "P"
                ),
            )
            return arithmetic_strike

        if action in SINGLE_LEG_OPTIONS:
            contracts = max(1, int(quantity * price / 10_000)) if price else 1
            option_type = "call" if action == Action.BUY_CALL else "put"
            if signal.strike:
                strike = float(signal.strike)
                # Strategy supplied the strike — record a paper_stub
                # provenance so the audit trail still answers "why this
                # contract" with at least the chosen contract on file.
                # Strategy-provided strikes lose the candidate set; that's
                # a known tradeoff vs. the AI Brain path which goes
                # through ``_chain_strike`` and gets full provenance.
                chain_selection_holder["prov"] = _paper_stub_provenance(
                    ticker=signal.ticker,
                    direction="long_" + option_type,
                    requested_dte=int(signal.dte or 30),
                    requested_delta_band=(0.30, 0.45),
                    underlying_spot=price,
                    chosen_expiry=(
                        signal.metadata.get("expiration")
                        or self._default_expiration(signal.dte)
                    ),
                    chosen_strike=strike,
                    chosen_option_type="C" if option_type == "call" else "P",
                )
            else:
                strike = _chain_strike(price, option_type, side="BUY")
            expiration = signal.metadata.get("expiration") or self._default_expiration(signal.dte)
            plan.update(
                instrument="option",
                option_type=option_type,
                side="BUY",
                strike=strike,
                expiration=expiration,
                contracts=contracts,
                quantity=contracts,
                chain_selection=chain_selection_holder["prov"],
            )
            return plan

        if action in SINGLE_LEG_SHORT_OPTIONS:
            # Cash-Secured Put / Covered Call — ONE short option leg. Not a spread.
            contracts = max(1, int(quantity * price / 10_000)) if price else 1
            option_type = "put" if action == Action.SELL_CSP else "call"
            expiration = signal.metadata.get("expiration") or self._default_expiration(signal.dte)
            # Sensible default if the strategy didn't supply one (CSP = 5% OTM put,
            # CC = 3% OTM call). Always snap to a real strike interval.
            default_money = -0.05 if action == Action.SELL_CSP else 0.03
            if signal.strike:
                strike = float(signal.strike)
                chain_selection_holder["prov"] = _paper_stub_provenance(
                    ticker=signal.ticker,
                    direction="short_" + option_type,
                    requested_dte=int(signal.dte or 30),
                    requested_delta_band=(0.30, 0.45),
                    underlying_spot=price,
                    chosen_expiry=expiration,
                    chosen_strike=strike,
                    chosen_option_type="C" if option_type == "call" else "P",
                )
            else:
                strike = _chain_strike(price, option_type,
                                          moneyness=default_money, side="SELL")
            plan.update(
                instrument="option",
                option_type=option_type,
                side="SELL",
                strike=strike,
                expiration=expiration,
                contracts=contracts,
                quantity=contracts,
                chain_selection=chain_selection_holder["prov"],
            )
            return plan

        # Multi-leg / true spreads.
        contracts = max(1, int(quantity * price / 10_000)) if price else 1
        expiration = signal.metadata.get("expiration") or self._default_expiration(signal.dte)
        opt = "call" if action in (Action.BULL_CALL_SPREAD,) else (
            "put" if action in () else "mixed"
        )
        if signal.strike:
            spread_strike = float(signal.strike)
            chain_selection_holder["prov"] = _paper_stub_provenance(
                ticker=signal.ticker,
                direction="long_" + (opt if opt in ("call", "put") else "call"),
                requested_dte=int(signal.dte or 30),
                requested_delta_band=(0.30, 0.45),
                underlying_spot=price,
                chosen_expiry=expiration,
                chosen_strike=spread_strike,
                chosen_option_type="C" if opt == "call" else "P",
            )
        else:
            spread_strike = _chain_strike(price, "call", side="BUY")
        plan.update(
            instrument="spread",
            option_type=opt,
            side="SPREAD",
            strike=spread_strike,
            expiration=expiration,
            contracts=contracts,
            quantity=contracts,
            legs=signal.metadata.get("legs") or signal.metadata.get("members"),
            chain_selection=chain_selection_holder["prov"],
        )
        return plan

    def _submit_order(self, signal: Signal, quantity: float, price: float, plan: Optional[Dict[str, Any]] = None):
        plan = plan or self.build_order_plan(signal, quantity, price)
        action = signal.action
        if action in STOCK_ACTIONS:
            return self.executor.place_stock_order(signal.ticker, plan["side"], plan["quantity"])

        if action in SINGLE_LEG_OPTIONS or action in SINGLE_LEG_SHORT_OPTIONS:
            return self.executor.place_options_order(
                signal.ticker,
                action.value,
                quantity=plan["contracts"],
                strike=plan["strike"],
                expiration=plan["expiration"],
            )

        logger.info(
            "complex options action %s for %s — booking via place_complex_order",
            action.value, signal.ticker,
        )
        return self.executor.place_complex_order(signal)
