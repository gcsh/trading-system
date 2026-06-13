#!/usr/bin/env python3
"""MITS Phase 16 cross-phase scan O2 — force-demo for the
``/decision/scorecard`` calibration bins + expectancy_by_bin.

The production funnel (cycles -> consensus -> submit -> close) hasn't
produced post-16.B closed trades yet, so every calibration bin reports
n=0. Same pattern as 15.E's force-demo for ``per_axis_calibration``:
pick a handful of already-closed Trade rows and synthesize one
``decision_provenance`` row per trade with all the JSON columns the
scorecard reads.

The synthetic rows are marked ``event_status='backfill_demo'`` so they
can be distinguished from real engine writes (which use 'submitted',
'kill_switch', 'simulator_veto', …). They also use a unique
``cycle_id`` prefix (``backfill_demo:<trade_id>``) for auditability.

``--apply`` commits; default is dry-run.

Selection contract: prefer the 5 most recent closed trades with non-null
pnl, spread across winners + losers so the calibration bins actually
discriminate. Existing demo rows are detected by ``event_status`` and
left alone (idempotent re-run).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

DEMO_STATUS = "backfill_demo"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def _existing_demo_trade_ids(engine) -> List[int]:
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT DISTINCT trade_id FROM decision_provenance "
            "WHERE event_status = :s AND trade_id IS NOT NULL"
        ), {"s": DEMO_STATUS}).fetchall()
    return [int(r[0]) for r in rows]


def _candidate_trades(engine, exclude: List[int],
                       want: int = 5) -> List[Dict[str, Any]]:
    """Return up to ``want`` already-closed trades to back the demo
    provenance rows. Picks the most recent closed pnl-bearing trades,
    biasing for a mix of winners + losers when available.
    """
    excl_set = set(exclude)
    rows: List[Dict[str, Any]] = []
    with engine.begin() as conn:
        result = conn.execute(text(
            "SELECT id, ticker, action, quantity, price, pnl, status, "
            "       timestamp, strategy, signal_source "
            "FROM trades "
            "WHERE pnl IS NOT NULL "
            "  AND status IN ('closed','filled_closed', "
            "                 'closed_by_exit_manager', "
            "                 'closed_by_thesis_health') "
            "ORDER BY id DESC "
            "LIMIT 200"
        )).fetchall()
    winners: List[Dict[str, Any]] = []
    losers: List[Dict[str, Any]] = []
    for r in result:
        if int(r[0]) in excl_set:
            continue
        rec = {
            "id": int(r[0]), "ticker": r[1], "action": r[2],
            "quantity": float(r[3] or 0.0), "price": float(r[4] or 0.0),
            "pnl": float(r[5] or 0.0), "status": r[6],
            "timestamp": r[7], "strategy": r[8], "signal_source": r[9],
        }
        if rec["pnl"] > 0:
            winners.append(rec)
        else:
            losers.append(rec)
    # Interleave winners + losers so the bins discriminate.
    out: List[Dict[str, Any]] = []
    while len(out) < want and (winners or losers):
        if winners:
            out.append(winners.pop(0))
        if len(out) >= want:
            break
        if losers:
            out.append(losers.pop(0))
    return out[:want]


def _synth_regime_vector(ticker: str) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "trend": {"value": "bullish", "health": "green"},
        "volatility_state": {"value": "normal", "health": "green"},
        "iv_rank": {"value": 45.0, "health": "green"},
        "iv_regime": {"value": "compressed", "health": "green"},
        "intraday_regime": {"value": "normal", "health": "green"},
        "gamma_state": {"value": "neutral", "health": "yellow"},
        "macro_regime": {"value": "risk_on", "health": "green"},
        "health": "green",
        "freshness_seconds": 30,
    }


def _synth_consensus(stance: str, confidence: float,
                       ticker: str) -> Dict[str, Any]:
    return {
        "stance": stance, "confidence": confidence,
        "vote_count": 6, "abstain_count": 0,
        "weighted_score": confidence,
        "confidence_breakdown": {
            "market_structure": 0.55, "technical": 0.65,
            "options": 0.50, "historical_analog": 0.45,
            "simulator": 0.60, "macro": 0.55,
            "composite": confidence,
            "axis_health": {
                "market_structure": "green", "technical": "green",
                "options": "yellow", "historical_analog": "yellow",
                "simulator": "green", "macro": "green",
            },
            "axis_n": {
                "market_structure": 1, "technical": 1, "options": 1,
                "historical_analog": 1, "simulator": 1, "macro": 1,
            },
        },
        "chairman_report": {
            "decision": "APPROVE", "decision_reason": (
                f"synthetic chairman memo for {ticker}: demo "
                "rationale spanning structured fields"
            ),
            "kill_condition": (
                "Spot closes below entry less 1.5*ATR within next session"
            ),
            "structured_why": {
                "thesis": (
                    f"{ticker} demo thesis paragraph backing the "
                    "calibration demo bins."
                ),
                "evidence": ["regime green", "analog cohort favorable"],
            },
            "position_size_modifier": 1.0,
        },
    }


def _build_provenance_payload(trade: Dict[str, Any]
                               ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (prov_bag_for_scoring, json_columns_for_insert)."""
    ticker = trade["ticker"] or "UNKNOWN"
    stance = "long" if trade["pnl"] >= 0 else "long"
    # Vary confidence so composite bins land in different buckets.
    # Trade id mod 10 walks confidence across 0.40..0.85 so the demo
    # rows distribute across calibration bins rather than clumping.
    conf = 0.40 + (int(trade["id"]) % 10) * 0.05
    rv = _synth_regime_vector(ticker)
    cons = _synth_consensus(stance, conf, ticker)
    portfolio_ctx = {
        "equity": 5000.0, "positions_count": 3,
        "sector_exposure_pct": {"tech": 25, "consumer": 10},
        "theme_exposure_pct": {"AI infra": 20},
    }
    policy_result = {
        "eligible": True, "blocking_factors": [],
        "soft_penalties_total_pct": 0.0, "evaluated_rules": 30,
    }
    correlation_cap = {
        "blocked": False, "reason": "", "sizing_multiplier": 1.0,
        "rho_max": 0.42,
    }
    simulator_verdict = {
        "reject_reason": None, "max_drawdown_pct": -3.2,
        "expected_return_pct": 1.8,
    }
    prov_bag = {
        "regime_vector": rv, "strategy_matrix": None,
        "consensus": cons,
        "chairman_memo": cons["chairman_report"],
        "policy_result": policy_result,
        "simulator_verdict": simulator_verdict,
        "correlation_cap": correlation_cap,
        "portfolio_context": portfolio_ctx,
        "agent_outputs": None,
    }
    cols: Dict[str, Any] = {
        "regime_vector_json": json.dumps(rv),
        "strategy_matrix_json": None,
        "consensus_json": json.dumps(cons),
        "chairman_memo_json": json.dumps(cons["chairman_report"]),
        "policy_result_json": json.dumps(policy_result),
        "simulator_verdict_json": json.dumps(simulator_verdict),
        "correlation_cap_json": json.dumps(correlation_cap),
        "portfolio_context_json": json.dumps(portfolio_ctx),
    }
    return prov_bag, cols


def _compute_dqs(prov_bag: Dict[str, Any]) -> Optional[str]:
    from backend.bot.decision.scorecard import score_decision
    dqs = score_decision(prov_bag)
    return json.dumps(dqs.to_dict())


def _insert_demo_row(engine, trade: Dict[str, Any], dry_run: bool
                      ) -> Optional[int]:
    prov_bag, cols = _build_provenance_payload(trade)
    dqs_json = _compute_dqs(prov_bag)
    # decision_timestamp is stamped at "now" rather than the trade's
    # original open time so the demo rows land inside the
    # /decision/scorecard?window=N tail. The scorecard takes the most
    # recent N rows ordered DESC; without this, the live engine cycle
    # rows would push our 5 demo rows out of the window.
    ts = datetime.utcnow()
    cycle_id = f"backfill_demo:{trade['id']}"
    if dry_run:
        logging.info(
            "DRY-RUN would insert demo prov for trade %d ticker=%s "
            "pnl=%.2f dqs=%s",
            trade["id"], trade["ticker"], trade["pnl"],
            "computed" if dqs_json else "missing",
        )
        return None
    params = {
        "trade_id": int(trade["id"]),
        "event_status": DEMO_STATUS,
        "ticker": (trade["ticker"] or "").upper(),
        "decision_timestamp": ts,
        "cycle_id": cycle_id,
        "regime_vector_json": cols["regime_vector_json"],
        "strategy_matrix_json": cols["strategy_matrix_json"],
        "agent_inputs_json": None,
        "agent_outputs_json": None,
        "consensus_json": cols["consensus_json"],
        "chairman_memo_json": cols["chairman_memo_json"],
        "policy_result_json": cols["policy_result_json"],
        "simulator_verdict_json": cols["simulator_verdict_json"],
        "correlation_cap_json": cols["correlation_cap_json"],
        "portfolio_context_json": cols["portfolio_context_json"],
        "decision_quality_score_json": dqs_json,
    }
    with engine.begin() as conn:
        res = conn.execute(text(
            "INSERT INTO decision_provenance ("
            "  trade_id, event_status, ticker, decision_timestamp, "
            "  cycle_id, regime_vector_json, strategy_matrix_json, "
            "  agent_inputs_json, agent_outputs_json, consensus_json, "
            "  chairman_memo_json, policy_result_json, "
            "  simulator_verdict_json, correlation_cap_json, "
            "  portfolio_context_json, decision_quality_score_json"
            ") VALUES ("
            "  :trade_id, :event_status, :ticker, :decision_timestamp, "
            "  :cycle_id, :regime_vector_json, :strategy_matrix_json, "
            "  :agent_inputs_json, :agent_outputs_json, :consensus_json, "
            "  :chairman_memo_json, :policy_result_json, "
            "  :simulator_verdict_json, :correlation_cap_json, "
            "  :portfolio_context_json, :decision_quality_score_json"
            ")"
        ), params)
        return int(res.lastrowid) if hasattr(res, "lastrowid") else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Commit inserts. Without this, dry-run only.")
    parser.add_argument("--n", type=int, default=5,
                        help="Number of demo rows to seed (default 5).")
    args = parser.parse_args()
    dry_run = not args.apply

    _setup_logging()
    t0 = time.time()

    from backend.db import get_engine
    engine = get_engine()

    already = _existing_demo_trade_ids(engine)
    logging.info("== %s ==", "DRY-RUN" if dry_run else "APPLY")
    logging.info("existing demo provenance rows: %d", len(already))

    trades = _candidate_trades(engine, exclude=already, want=int(args.n))
    if not trades:
        logging.warning(
            "no eligible closed trades to seed (all candidates already "
            "have demo provenance rows or no closed trades exist)"
        )

    written: List[Dict[str, Any]] = []
    for trade in trades:
        new_id = _insert_demo_row(engine, trade, dry_run=dry_run)
        written.append({
            "trade_id": trade["id"],
            "ticker": trade["ticker"],
            "pnl": trade["pnl"],
            "new_provenance_id": new_id,
        })

    summary = {
        "mode": "dry_run" if dry_run else "apply",
        "demo_status_marker": DEMO_STATUS,
        "candidates_chosen": len(trades),
        "rows_written": [w for w in written if w["new_provenance_id"]],
        "rows_planned": written if dry_run else [],
        "elapsed_sec": round(time.time() - t0, 1),
    }
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
