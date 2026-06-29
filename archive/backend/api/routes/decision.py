"""MITS Phase 16.B / 16.C / 16.E — Decision Provenance + Quality + Cockpit API.

  • ``GET /decision/provenance/{trade_id}``    — full provenance row for one trade
  • ``GET /decision/provenance``               — list recent rows, filterable
  • ``GET /decision/scorecard``                — rolling DQS distribution + calibration bins
  • ``GET /decision/cockpit/{identifier}``     — unified per-decision bundle for the cockpit UI

All ``*_json`` columns are JSON-decoded into nested dicts on the way
out so the operator UI / replay tooling can read them without a second
parse step.
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance
from backend.models.trade import Trade


router = APIRouter(prefix="/decision", tags=["decision"])


_JSON_FIELDS = (
    "regime_vector",
    "strategy_matrix",
    "agent_inputs",
    "agent_outputs",
    "consensus",
    "chairman_memo",
    "policy_result",
    "simulator_verdict",
    "correlation_cap",
    "portfolio_context",
)


def _decode(blob: Optional[str]) -> Any:
    if blob is None or blob == "":
        return None
    try:
        return json.loads(blob)
    except (TypeError, ValueError):
        return None


def _row_to_response(row: DecisionProvenance) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": row.id,
        "trade_id": row.trade_id,
        "event_status": row.event_status,
        "ticker": row.ticker,
        "decision_timestamp": (
            row.decision_timestamp.isoformat()
            if row.decision_timestamp else None
        ),
        "cycle_id": row.cycle_id,
    }
    for key in _JSON_FIELDS:
        out[key] = _decode(getattr(row, f"{key}_json"))
    return out


@router.get("/provenance/{trade_id}")
async def get_provenance_by_trade(trade_id: int) -> Dict[str, Any]:
    """Return the provenance row for one executed trade.

    Maps to the row written when ``_persist_trade`` succeeded — i.e.
    the trade reached "submitted" status. Pre-execution blocks and
    post-consensus rejections have ``trade_id=NULL`` and are listed
    via the collection endpoint instead.
    """
    with session_scope() as session:
        row = session.query(DecisionProvenance).filter(
            DecisionProvenance.trade_id == trade_id,
        ).order_by(DecisionProvenance.id.desc()).first()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"no decision_provenance for trade_id={trade_id}",
            )
        return _row_to_response(row)


@router.get("/provenance")
async def list_provenance(
    ticker: Optional[str] = Query(None),
    since: Optional[str] = Query(
        None,
        description="ISO timestamp lower bound (inclusive) on decision_timestamp",
    ),
    limit: int = Query(20, ge=1, le=200),
) -> Dict[str, Any]:
    """List recent provenance rows, optionally filtered by ticker / since."""
    with session_scope() as session:
        q = session.query(DecisionProvenance)
        if ticker:
            q = q.filter(DecisionProvenance.ticker == ticker.upper())
        if since:
            try:
                since_ts = datetime.fromisoformat(since)
                q = q.filter(
                    DecisionProvenance.decision_timestamp >= since_ts,
                )
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid since timestamp: {since!r}",
                )
        rows = q.order_by(
            DecisionProvenance.decision_timestamp.desc(),
        ).limit(limit).all()
        items: List[Dict[str, Any]] = [_row_to_response(r) for r in rows]
    return {"count": len(items), "items": items}


@router.get("/replay/latest")
async def replay_latest(n: int = Query(10, ge=1, le=100)) -> Dict[str, Any]:
    """Issue 11e — re-run the last ``n`` provenance rows through the
    canonical replayer and report per-row drift.

    Phase 16.B invariant: drift must be 0.0 (stance match + confidence
    drift < 0.01). When the invariant trips we want a single API surface
    that says exactly which row drifted by how much — the cockpit polls
    this endpoint so a failed replay surfaces in the UI without an
    operator having to ssh in.

    Each item carries:

    * ``provenance_id``    — row primary key
    * ``trade_id``         — linked trade (None for blocked-post-consensus)
    * ``ticker``           — convenience
    * ``decision_timestamp`` — when the decision was first made
    * ``drift``            — confidence_drift (float, 0.0 on a clean replay)
    * ``stance_drift``     — bool, True on stance mismatch
    * ``match``            — bool, True when both drifts are within tolerance
    * ``replay_status``    — "ok" / "error" / "skip:no_consensus"
    * ``error``            — error string on replay_status == "error"
    """
    from backend.bot.decision.replay import replay_consensus_from_provenance

    with session_scope() as session:
        rows = (
            session.query(DecisionProvenance)
            .order_by(DecisionProvenance.decision_timestamp.desc())
            .limit(n)
            .all()
        )
        prov_specs: List[Dict[str, Any]] = [
            {
                "provenance_id": r.id,
                "trade_id": r.trade_id,
                "ticker": r.ticker,
                "decision_timestamp": (
                    r.decision_timestamp.isoformat()
                    if r.decision_timestamp else None
                ),
                "has_consensus": bool(r.consensus_json),
            }
            for r in rows
        ]

    items: List[Dict[str, Any]] = []
    total_drift = 0.0
    drift_count = 0
    for spec in prov_specs:
        item: Dict[str, Any] = {
            "provenance_id": spec["provenance_id"],
            "trade_id": spec["trade_id"],
            "ticker": spec["ticker"],
            "decision_timestamp": spec["decision_timestamp"],
        }
        if not spec["has_consensus"]:
            # Blocked-pre-consensus rows have no votes to replay; surface
            # explicitly rather than scoring them as a drift hit.
            item.update({
                "replay_status": "skip:no_consensus",
                "drift": None,
                "stance_drift": None,
                "match": None,
            })
            items.append(item)
            continue
        try:
            result = replay_consensus_from_provenance(spec["provenance_id"])
            drift_block = result.get("drift") or {}
            cd = float(drift_block.get("confidence_drift") or 0.0)
            item.update({
                "replay_status": "ok",
                "drift": cd,
                "stance_drift": bool(drift_block.get("stance_drift")),
                "match": bool(result.get("match")),
                "persisted": result.get("persisted"),
                "replayed": result.get("replayed"),
            })
            total_drift += cd
            drift_count += 1
        except Exception as exc:
            item.update({
                "replay_status": "error",
                "drift": None,
                "stance_drift": None,
                "match": False,
                "error": str(exc),
            })
        items.append(item)

    mean_drift = (total_drift / drift_count) if drift_count else 0.0
    return {
        "count": len(items),
        "replayed_count": drift_count,
        "mean_confidence_drift": round(mean_drift, 4),
        "items": items,
    }


_SUB_SCORES = (
    "analysis_quality",
    "council_agreement",
    "risk_quality",
    "execution_quality",
)


def _composite_bin_label(score: float) -> str:
    """Map composite score to its 10-wide bin label (0-10, 10-20, …, 90-100)."""
    if score >= 100.0:
        return "90-100"
    lo = int(score // 10) * 10
    return f"{lo}-{lo + 10}"


@router.get("/scorecard")
async def get_decision_scorecard(
    window: int = Query(50, ge=10, le=500),
) -> Dict[str, Any]:
    """MITS Phase 16.C — rolling DQS distribution + calibration bins.

    Queries the last ``window`` provenance rows that have a populated
    ``decision_quality_score_json``. Joins each row to its Trade.pnl
    (when ``trade_id`` is set) so we can compute realized win rate +
    mean pnl per composite-score bin. Calibration uses 10 fixed bins
    (0–10, 10–20, …, 90–100) so the UI always has a stable x-axis.
    """
    bins: List[str] = [
        f"{i}-{i + 10}" for i in range(0, 100, 10)
    ]
    empty_bin = lambda b: {"bin": b, "n": 0, "win_rate": None}
    empty_exp = lambda b: {"bin": b, "n": 0, "mean_pnl_pct": None}

    with session_scope() as session:
        rows = (
            session.query(DecisionProvenance)
            .filter(DecisionProvenance.decision_quality_score_json.isnot(None))
            .order_by(DecisionProvenance.decision_timestamp.desc())
            .limit(window)
            .all()
        )

        composites: List[float] = []
        by_axis: Dict[str, List[float]] = {k: [] for k in _SUB_SCORES}
        # bucket → list of (pnl_pct, win_flag).
        bucket_pnl: Dict[str, List[float]] = {b: [] for b in bins}
        bucket_wins: Dict[str, List[int]] = {b: [] for b in bins}

        for row in rows:
            try:
                dqs = json.loads(row.decision_quality_score_json)
            except (TypeError, ValueError):
                continue
            comp = dqs.get("composite")
            if comp is None:
                continue
            try:
                comp_f = float(comp)
            except (TypeError, ValueError):
                continue
            composites.append(comp_f)
            for axis in _SUB_SCORES:
                v = dqs.get(axis)
                if v is None:
                    continue
                try:
                    by_axis[axis].append(float(v))
                except (TypeError, ValueError):
                    continue
            if row.trade_id is None:
                continue
            trade = session.query(Trade).filter(
                Trade.id == row.trade_id
            ).first()
            if trade is None or trade.pnl is None:
                continue
            # Use pnl_pct when we can derive it (entry price × qty), else
            # fall back to raw pnl normalized by 100. The bin axis is
            # only relative so absolute units don't matter for shape.
            try:
                pnl_pct = float(trade.pnl)
                notional = float(trade.price or 0.0) * float(
                    trade.quantity or 0.0)
                if notional > 0:
                    pnl_pct = (float(trade.pnl) / notional) * 100.0
            except (TypeError, ValueError):
                continue
            label = _composite_bin_label(comp_f)
            if label not in bucket_pnl:
                continue
            bucket_pnl[label].append(pnl_pct)
            bucket_wins[label].append(1 if float(trade.pnl) > 0 else 0)

    composite_distribution: Dict[str, Optional[float]] = {
        "mean": (round(statistics.fmean(composites), 2) if composites else None),
        "stddev": (
            round(statistics.pstdev(composites), 2)
            if len(composites) >= 2 else None
        ),
        "median": (
            round(statistics.median(composites), 2) if composites else None
        ),
    }
    by_sub_score: Dict[str, Dict[str, Optional[float]]] = {}
    for axis, values in by_axis.items():
        by_sub_score[axis] = {
            "mean": (round(statistics.fmean(values), 2) if values else None),
            "median": (
                round(statistics.median(values), 2) if values else None
            ),
        }

    calibration_bins: List[Dict[str, Any]] = []
    expectancy_by_bin: List[Dict[str, Any]] = []
    for b in bins:
        wins = bucket_wins[b]
        pnls = bucket_pnl[b]
        if wins:
            calibration_bins.append({
                "bin": b,
                "n": len(wins),
                "win_rate": round(sum(wins) / len(wins), 4),
            })
        else:
            calibration_bins.append(empty_bin(b))
        if pnls:
            expectancy_by_bin.append({
                "bin": b,
                "n": len(pnls),
                "mean_pnl_pct": round(statistics.fmean(pnls), 4),
            })
        else:
            expectancy_by_bin.append(empty_exp(b))

    return {
        "window": window,
        "n_rows": len(composites),
        "composite_distribution": composite_distribution,
        "by_sub_score": by_sub_score,
        "calibration_bins": calibration_bins,
        "expectancy_by_bin": expectancy_by_bin,
    }


def _resolve_cockpit_row(
    session, identifier: str,
) -> Optional[DecisionProvenance]:
    """MITS Phase 16.E — pick the provenance row for the cockpit URL.

    ``identifier`` is the raw URL segment. Resolution order:
      1. If it parses as an integer:
         a. Look up by ``Trade.id`` — return the most recent
            provenance row pointing at that trade.
         b. Fall back to a direct ``DecisionProvenance.id`` lookup
            (covers blocked-post-consensus rows that carry no
            trade_id).
      2. Otherwise treat it as a ticker symbol and return the most
         recent provenance row for that ticker.
    """
    raw = (identifier or "").strip()
    if not raw:
        return None

    try:
        as_int = int(raw)
    except (TypeError, ValueError):
        as_int = None

    if as_int is not None:
        by_trade = (
            session.query(DecisionProvenance)
            .filter(DecisionProvenance.trade_id == as_int)
            .order_by(DecisionProvenance.id.desc())
            .first()
        )
        if by_trade is not None:
            return by_trade
        by_id = (
            session.query(DecisionProvenance)
            .filter(DecisionProvenance.id == as_int)
            .first()
        )
        if by_id is not None:
            return by_id
        return None

    ticker_u = raw.upper()
    return (
        session.query(DecisionProvenance)
        .filter(DecisionProvenance.ticker == ticker_u)
        .order_by(DecisionProvenance.decision_timestamp.desc())
        .first()
    )


def _cockpit_policy_panel(decoded: Dict[str, Any]) -> Dict[str, Any]:
    policy = decoded.get("policy_result") or {}
    return {
        "eligible": bool(policy.get("eligible", True)),
        "blocking_factors": list(policy.get("blocking_factors") or []),
        "soft_penalties_total_pct": float(
            policy.get("soft_penalties_total_pct") or 0.0
        ),
        "evaluated_at": policy.get("evaluated_at"),
    }


def _cockpit_council_panel(decoded: Dict[str, Any]) -> Dict[str, Any]:
    consensus = decoded.get("consensus") or {}
    chairman_report = consensus.get("chairman_report") or {}
    return {
        "consensus": consensus,
        "agent_outputs": list(decoded.get("agent_outputs") or []),
        "agent_inputs": decoded.get("agent_inputs"),
        "dissent": chairman_report.get("dissent") or {},
    }


def _cockpit_chairman_panel(decoded: Dict[str, Any]) -> Dict[str, Any]:
    consensus = decoded.get("consensus") or {}
    chairman_report = consensus.get("chairman_report") or {}
    return {
        "decision": chairman_report.get("decision"),
        "decision_reason": chairman_report.get("decision_reason"),
        "kill_condition": chairman_report.get("kill_condition"),
        "structured_why": list(chairman_report.get("structured_why") or []),
        "main_risk": chairman_report.get("main_risk"),
        "confidence_pct": chairman_report.get("confidence_pct"),
        "conviction": chairman_report.get("conviction"),
        "position_size_modifier": chairman_report.get("position_size_modifier"),
        "evidence_correlation": chairman_report.get("evidence_correlation"),
        "independent_signal_count": (
            chairman_report.get("independent_signal_count")
        ),
        "bull_case": chairman_report.get("bull_case"),
        "bear_case": chairman_report.get("bear_case"),
    }


def _cockpit_portfolio_panel(decoded: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "portfolio_context": decoded.get("portfolio_context"),
        "correlation_cap": decoded.get("correlation_cap"),
    }


def _cockpit_simulator_panel(
    decoded: Dict[str, Any],
) -> Dict[str, Any]:
    verdict = decoded.get("simulator_verdict") or {}
    scenarios = list(verdict.get("scenarios") or [])
    return {
        "scenarios": scenarios,
        "verdict": verdict,
    }


@router.get("/cockpit/{identifier}")
async def get_decision_cockpit(identifier: str) -> Dict[str, Any]:
    """MITS Phase 16.E — unified per-decision bundle for the Cockpit UI.

    ``identifier`` may be:

      * an integer trade_id (preferred — most recent provenance row
        whose ``trade_id`` matches)
      * a direct DecisionProvenance ``id`` (fallback when the trade_id
        lookup misses, covers blocked-post-consensus rows)
      * a ticker symbol (returns the latest provenance row for that
        ticker, executed or blocked)

    The response composes the 11 JSON columns + the cached decision
    quality score into 6 stacked panels (policy / council / chairman /
    portfolio impact / decision quality / simulator scenarios) plus
    the optional ``opportunity_committee`` blob lifted off the
    chairman_memo when the opportunistic path produced it.

    This route is READ-ONLY — it never recomputes provenance fields.
    """
    with session_scope() as session:
        row = _resolve_cockpit_row(session, identifier)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"no decision_provenance matches identifier={identifier!r}",
            )
        decoded: Dict[str, Any] = {}
        for key in _JSON_FIELDS:
            decoded[key] = _decode(getattr(row, f"{key}_json"))
        dqs = _decode(row.decision_quality_score_json)
        decision_id = int(row.id)
        trade_id = row.trade_id
        ticker = row.ticker
        event_status = row.event_status
        decision_timestamp = (
            row.decision_timestamp.isoformat()
            if row.decision_timestamp else None
        )
        cycle_id = row.cycle_id
        # MITS Phase 19 — surface the would_have_been execution panel for
        # non-submitted rows so the cockpit shows meaningful execution
        # content on HOLDs. Persisted by the engine on every non-
        # submitted decision (see BotEngine._compute_would_have_been);
        # NULL for legacy rows persisted before Phase 19 ships — UI
        # treats absence as "fall back to EmptyState".
        would_have_been = _decode(
            getattr(row, "would_have_been_json", None)
        )
        # MITS Phase 17.B — read Trade.fill_snapshot_json off the linked
        # trade row so the cockpit response carries fill provenance. 17.E
        # will compose the unified execution panel; surfacing the
        # decoded snapshot here keeps the response forward-compatible.
        # MITS Phase 17.C — same trade row also carries the sizing
        # provenance chain.
        # MITS Phase 17.D — same trade row also carries the chain-selection
        # provenance ("why this contract?"). NULL on stock trades.
        # MITS Phase 17.E — same trade row also carries the exit-policy
        # provenance ("why this exact exit?"). NULL on entry trades and
        # on closes that bypass the declarative exit_manager path.
        fill_snapshot: Optional[Any] = None
        sizing_chain: Optional[Any] = None
        chain_selection: Optional[Any] = None
        exit_policy_result: Optional[Any] = None
        if trade_id is not None:
            trade_row = session.query(Trade).filter(
                Trade.id == trade_id,
            ).first()
            if trade_row is not None:
                fill_snapshot = _decode(trade_row.fill_snapshot_json)
                sizing_chain = _decode(trade_row.sizing_chain_json)
                chain_selection = _decode(trade_row.chain_selection_json)
                exit_policy_result = _decode(
                    trade_row.exit_policy_result_json
                )

    chairman_report = (decoded.get("consensus") or {}).get(
        "chairman_report"
    ) or decoded.get("chairman_memo") or {}
    # 16.D — opportunity_committee is lifted off the chairman memo on
    # the opportunistic path; absent on the statistical path.
    opportunity_committee = (
        chairman_report.get("opportunity_committee")
        if isinstance(chairman_report, dict) else None
    )

    return {
        "decision_id": decision_id,
        "trade_id": trade_id,
        "ticker": ticker,
        "event_status": event_status,
        "decision_timestamp": decision_timestamp,
        "cycle_id": cycle_id,
        "policy_result": _cockpit_policy_panel(decoded),
        "council_breakdown": _cockpit_council_panel(decoded),
        "chairman_memo": _cockpit_chairman_panel(decoded),
        "portfolio_impact": _cockpit_portfolio_panel(decoded),
        "decision_quality_score": dqs,
        "simulator_scenarios": (
            _cockpit_simulator_panel(decoded)["scenarios"]
        ),
        "simulator_verdict": decoded.get("simulator_verdict"),
        "regime_vector": decoded.get("regime_vector"),
        "strategy_matrix": decoded.get("strategy_matrix"),
        "opportunity_committee": opportunity_committee,
        # MITS Phase 17.B — execution panel seed. 17.E will extend with
        # the unified execution-quality surface; the cockpit consumer
        # treats this whole block as optional. 17.C adds the sizing
        # provenance chain so the panel answers both "What did we pay?"
        # and "Why this size?" with a single read.
        # 17.D adds the chain-selection provenance so the panel ALSO
        # answers "Why this contract?" — the candidate set considered,
        # per-loser rejection reasons, the requested delta band/DTE,
        # and chain freshness — with the same single read.
        # 17.E adds the exit-policy provenance so the panel ALSO answers
        # "Why this exact exit?" — every concurrent ExitTrigger surfaced
        # with its severity, legacy_action, reason, and evidence dict —
        # populated on CLOSE_OPTION trades; NULL on entries.
        "execution": {
            "fill_snapshot": fill_snapshot,
            "sizing_chain": sizing_chain,
            "chain_selection": chain_selection,
            "exit_policy_result": exit_policy_result,
        },
        # MITS Phase 19 — counterfactual execution panel for non-
        # submitted decisions. Plain-English projections of fill /
        # sizing / chain / exit so the cockpit's execution surfaces
        # render meaningful content on HOLDs. Persisted alongside the
        # provenance row by the engine; ``None`` for legacy rows that
        # predate Phase 19 — the UI falls back to its existing
        # EmptyState in that case.
        "would_have_been": would_have_been,
        # MITS Phase 18.B — Counterfactual Replayer. Best-effort: if the
        # bundle compute fails we surface ``None`` rather than 500'ing the
        # cockpit. The cockpit's What-if panel reads this slot to render
        # the sizing curve + policy CF + consensus CF without a second
        # round-trip. compute_all_counterfactuals never raises by design.
        "counterfactuals": _safe_compute_counterfactuals(decision_id),
        # MITS Phase 18.C — Learning insights summary.
        #   * attribution_summary    — high-level digest from 18.A
        #     (latest computed_at + per-scope row counts + first row
        #     n_closed). Read-only convenience surface so the cockpit
        #     can show "agents calibration is N=N over window=W"
        #     without a second fetch.
        #   * active_policy_recommendations — latest persisted
        #     PolicyTuning rows from 18.C (advisory thresholds). NULL
        #     until the advisory pass runs at least once.
        # Best-effort: any compute failure surfaces ``None`` so the
        # cockpit response stays valid.
        "learning_insights": _safe_compute_learning_insights(),
    }


def _safe_compute_counterfactuals(prov_id: int) -> Optional[Dict[str, Any]]:
    """Wrapper so a counterfactual compute failure can never break the
    cockpit endpoint. compute_all_counterfactuals is pure-read + uses
    its own session, so the only way this raises is a code bug — log
    + return None so the cockpit response stays valid."""
    try:
        from backend.bot.learning.counterfactual import (
            compute_all_counterfactuals,
        )
        return compute_all_counterfactuals(int(prov_id)).to_dict()
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "counterfactual compute failed for prov_id=%s", prov_id,
        )
        return None


def _safe_compute_learning_insights() -> Optional[Dict[str, Any]]:
    """MITS Phase 18.C — best-effort assembly of the learning insights
    panel for the cockpit. Returns ``None`` only on hard import errors;
    on per-section failures we surface ``None`` for that section only
    so the cockpit can render whichever surfaces are live.

    Three sections:
      * ``attribution_summary``       — 18.A latest snapshot meta
      * ``active_policy_recommendations`` — 18.C latest advisory rows
      * ``active_weight_proposals``   — 18.D adaptive weight rows
    """
    import logging
    log = logging.getLogger(__name__)
    try:
        attribution_summary: Optional[Dict[str, Any]] = None
        active_policy_recommendations: Optional[Dict[str, Any]] = None
        try:
            from backend.bot.learning.attribution_writer import (
                latest_attribution_rows,
            )
            attr_rows = latest_attribution_rows(limit=200)
            if attr_rows:
                from collections import Counter
                counts = Counter(
                    str(r.get("scope_kind") or "") for r in attr_rows
                )
                attribution_summary = {
                    "computed_at": attr_rows[0].get("computed_at"),
                    "window_days": attr_rows[0].get("window_days"),
                    "n_rows": len(attr_rows),
                    "by_scope": dict(counts),
                }
            else:
                attribution_summary = {
                    "computed_at": None,
                    "n_rows": 0,
                    "note": "no attribution rows yet — pass not yet run",
                }
        except Exception:
            log.debug("attribution_summary section failed", exc_info=True)
            attribution_summary = None

        try:
            from backend.bot.learning.policy_tuning import (
                latest_policy_tuning_rows,
            )
            from backend.config import TUNABLES as _TUNABLES
            pt_rows = latest_policy_tuning_rows(limit=50)
            active_policy_recommendations = {
                "advisory_enabled": bool(
                    _TUNABLES.policy_tuning_advisory_enabled
                ),
                "auto_apply_enabled": bool(
                    _TUNABLES.policy_tuning_auto_apply_enabled
                ),
                "computed_at": (
                    pt_rows[0].get("computed_at") if pt_rows else None
                ),
                "n_recommendations": len(pt_rows),
                "rows": [
                    {
                        "rule_name": r.get("rule_name"),
                        "current_value": r.get("current_value"),
                        "recommended_value": r.get("recommended_value"),
                        "recommendation_confidence": r.get(
                            "recommendation_confidence",
                        ),
                    }
                    for r in pt_rows
                ],
            }
        except Exception:
            log.debug(
                "active_policy_recommendations section failed",
                exc_info=True,
            )
            active_policy_recommendations = None

        # MITS Phase 18.D — Online Agent Weight Adaptation (Advisory).
        # Surface the latest weight proposals so the cockpit shows
        # "council has 8 proposed weights — n_closed=0 for all (cold
        # start)" without needing a second fetch. NULL until the
        # advisory pass runs at least once.
        active_weight_proposals: Optional[Dict[str, Any]] = None
        try:
            from backend.bot.learning.weight_adaptation import (
                latest_weight_rows,
                AGENT_BASE_WEIGHTS,
            )
            from backend.config import TUNABLES as _TUNABLES2
            aw_rows = latest_weight_rows(limit=64)
            active_weight_proposals = {
                "advisory_enabled": bool(getattr(
                    _TUNABLES2, "adaptive_weights_advisory_enabled", False,
                )),
                "apply_enabled": bool(getattr(
                    _TUNABLES2, "adaptive_weights_apply_enabled", False,
                )),
                "known_agents": list(AGENT_BASE_WEIGHTS.keys()),
                "computed_at": (
                    aw_rows[0].get("computed_at") if aw_rows else None
                ),
                "n_proposals": len(aw_rows),
                "rows": [
                    {
                        "agent": r.get("agent"),
                        "base_weight": r.get("base_weight"),
                        "weight_proposed": r.get("weight_proposed"),
                        "weight_active": r.get("weight_active"),
                        "adaptive_multiplier": r.get("adaptive_multiplier"),
                        "n_closed": r.get("n_closed"),
                        "confidence_level": r.get("confidence_level"),
                    }
                    for r in aw_rows
                ],
            }
        except Exception:
            log.debug(
                "active_weight_proposals section failed", exc_info=True,
            )
            active_weight_proposals = None
        # MITS Phase 18-FU Stream A — Decision Funnel snapshot. Reads
        # the most recent decision_funnel_daily row + flags anomalies
        # when any stage dropped > 50% from the trailing 7d median.
        # Surfaces only the top-level numerics + the surgical change
        # advisory; the full payload lives behind /learning/funnel.
        funnel_snapshot: Optional[Dict[str, Any]] = None
        try:
            from backend.bot.learning.funnel import (
                funnel_history, is_anomalous_drop, latest_funnel_row,
            )
            latest = latest_funnel_row()
            if latest is not None:
                hist = funnel_history(days=8)
                # Exclude the current row from the comparison cohort —
                # the median of 7d excluding today is the right anchor.
                history_rows = [
                    r for r in hist
                    if r.get("date") and r["date"] != latest.get("date")
                ][:7]
                anomaly = is_anomalous_drop(latest, history_rows)
                # Decode payload_json to surface the top surgical
                # advisory + the 10 stage counts without forcing the
                # consumer to make a second GET.
                payload = latest.get("payload_json")
                top_advisory: Optional[Dict[str, Any]] = None
                stages: List[Dict[str, Any]] = []
                if payload:
                    try:
                        decoded = json.loads(payload)
                        top_advisory = decoded.get(
                            "top_surgical_change_candidate"
                        )
                        stages = decoded.get("stages") or []
                    except (TypeError, ValueError):
                        pass
                funnel_snapshot = {
                    "date": latest.get("date"),
                    "computed_at": latest.get("computed_at"),
                    "watchlist_size": latest.get("watchlist_size"),
                    "n_evaluations": latest.get("n_evaluations"),
                    "n_submitted": latest.get("n_submitted"),
                    "n_closed_with_pnl": latest.get("n_closed_with_pnl"),
                    "n_cooldown_hits": latest.get("n_cooldown_hits"),
                    "n_cooldown_lost_opportunities": latest.get(
                        "n_cooldown_lost_opportunities"
                    ),
                    "composite_quality_mean": latest.get(
                        "composite_quality_mean"
                    ),
                    "stages": stages,
                    "anomaly": anomaly,
                    "top_surgical_change_candidate": top_advisory,
                }
            else:
                funnel_snapshot = {
                    "date": None,
                    "note": (
                        "no decision_funnel_daily row yet — nightly "
                        "21:55 ET job has not run since deploy"
                    ),
                }
        except Exception:
            log.debug("funnel_snapshot section failed", exc_info=True)
            funnel_snapshot = None

        return {
            "attribution_summary": attribution_summary,
            "active_policy_recommendations": active_policy_recommendations,
            "active_weight_proposals": active_weight_proposals,
            "funnel_snapshot": funnel_snapshot,
        }
    except Exception:
        log.exception("learning_insights compute failed")
        return None
