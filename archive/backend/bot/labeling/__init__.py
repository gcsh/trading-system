"""Label contract — the bridge between trade outcomes and ML / evaluation.

A LABEL is everything we need to:
  • Train and evaluate models honestly (binary win / continuous return)
  • Score a strategy or regime cohort against ground truth
  • Detect feature/label drift over time

The contract is intentionally narrow and stable; new fields require a
schema-version bump (``LABEL_SCHEMA``) so older artifacts can be detected.

Built from the join of ``Trade`` (the order + realized P&L) and
``DecisionLog`` (the analytical context at signal time). Never reaches across
to features stored later than the decision — no lookahead leakage.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

LABEL_SCHEMA = 1


@dataclass
class TradeLabel:
    """Canonical label row. ``win`` is the binary classification target;
    ``pnl_pct`` is the continuous regression target."""
    trade_id: int
    timestamp: str                           # ISO — decision time, NOT close time
    ticker: str
    strategy: str
    action: str
    instrument: str

    # Decision context (what the system "knew" when it acted)
    regime_trend: str = "unknown"
    regime_volatility: str = "normal"
    regime_gamma: str = "unknown"
    grade: str = ""
    confidence: float = 0.0
    win_probability: Optional[float] = None  # the predicted prob at decision time

    # Realized outcome (filled in only when closed)
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None          # pnl / entry_notional
    win: Optional[int] = None                # 1 if pnl > 0, else 0; None if open
    exit_reason: Optional[str] = None        # take_profit | stop_loss | expiry | manual
    holding_minutes: Optional[int] = None
    closed_at: Optional[str] = None

    # Provenance — lets us detect bad labels later
    schema: int = LABEL_SCHEMA

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _entry_notional(rec: Dict[str, Any]) -> float:
    """Best-effort estimate of the capital deployed on this trade. Used to
    convert raw $ P&L into a percent — keeps return distributions comparable
    across stock vs option positions."""
    qty = float(rec.get("quantity") or 0.0)
    price = float(rec.get("price") or 0.0)
    if rec.get("instrument") in ("option", "spread"):
        contracts = float(rec.get("contracts") or rec.get("quantity") or 0.0)
        # Premium per SHARE ≈ 3% of strike, floored at $0.05/share — same
        # floor the paper executor uses. The per-CONTRACT notional is then
        # ``per_share × 100 × contracts``.
        strike = float(rec.get("strike") or 0.0)
        if strike:
            per_share = max(0.05, 0.03 * strike)
            return round(per_share * 100 * abs(contracts), 2)
    return round(qty * price, 2)


def _exit_reason(rec: Dict[str, Any]) -> Optional[str]:
    """Infer the exit reason from the trade row + reason text."""
    if rec.get("status") != "closed":
        return None
    reason = (rec.get("reason") or "").lower()
    if "take-profit" in reason or "take_profit" in reason:
        return "take_profit"
    if "stop-loss" in reason or "stop_loss" in reason:
        return "stop_loss"
    if "expiry" in reason or rec.get("action") == "CLOSE_OPTION":
        return "expiry"
    return "manual"


def build_labels(trade_rows: Sequence[Dict[str, Any]],
                  decision_rows: Sequence[Dict[str, Any]] = ()) -> List[TradeLabel]:
    """Materialize labels from trade rows + matching decision-log rows.

    Both inputs are plain dicts (extract them inside ``session_scope`` before
    calling). Decision rows are looked up by ``trade_id`` so the analytical
    context that drove the decision is preserved as features for ML.
    """
    by_trade: Dict[int, Dict[str, Any]] = {
        int(d["trade_id"]): d for d in decision_rows if d.get("trade_id") is not None
    }
    out: List[TradeLabel] = []
    for r in trade_rows:
        if r.get("id") is None:
            continue
        notional = _entry_notional(r)
        pnl = r.get("pnl")
        decision = by_trade.get(int(r["id"])) or {}
        out.append(TradeLabel(
            trade_id=int(r["id"]),
            timestamp=str(r.get("timestamp") or ""),
            ticker=str(r.get("ticker") or ""),
            strategy=str(r.get("strategy") or ""),
            action=str(r.get("action") or ""),
            instrument=str(r.get("instrument") or "stock"),
            regime_trend=str(decision.get("regime_trend") or "unknown"),
            regime_volatility=str(decision.get("regime_volatility") or "normal"),
            regime_gamma=str(decision.get("regime_gamma") or "unknown"),
            grade=str(decision.get("grade") or ""),
            confidence=float(r.get("confidence") or 0.0),
            win_probability=(float(decision["win_probability"])
                              if decision.get("win_probability") is not None else None),
            pnl=float(pnl) if pnl is not None else None,
            pnl_pct=(round(float(pnl) / notional, 4)
                       if pnl is not None and notional > 0 else None),
            win=(1 if (pnl is not None and float(pnl) > 0)
                  else 0 if pnl is not None else None),
            exit_reason=_exit_reason(r),
        ))
    return out


# ── label-quality audit ─────────────────────────────────────────────────────


def label_quality(labels: Sequence[TradeLabel]) -> Dict[str, Any]:
    """Diagnose dataset health before anyone tries to train on it.

    Catches the silent failure modes: empty class (all wins / all losses),
    missing predictions (no win_probability ever populated), and small-sample
    bias (need ≥ N closed before any per-cohort metric is trustworthy).
    """
    closed = [l for l in labels if l.win is not None]
    n_closed = len(closed)
    n_wins = sum(1 for l in closed if l.win == 1)
    n_losses = n_closed - n_wins
    n_pred = sum(1 for l in closed if l.win_probability is not None)
    warnings: List[str] = []
    if n_closed == 0:
        warnings.append("no closed trades — every metric will be n/a")
    elif n_closed < 30:
        warnings.append(f"only {n_closed} closed trades — need ≥30 for stable metrics")
    if n_closed and n_wins == 0:
        warnings.append("all closed trades lost — Brier/calibration are degenerate")
    if n_closed and n_losses == 0:
        warnings.append("all closed trades won — likely overfitting or sampling bias")
    if n_closed and n_pred == 0:
        warnings.append("no predicted probabilities — calibration error unavailable")
    return {
        "labels_total": len(labels),
        "closed": n_closed,
        "open": len(labels) - n_closed,
        "wins": n_wins,
        "losses": n_losses,
        "with_prediction": n_pred,
        "schema": LABEL_SCHEMA,
        "warnings": warnings,
        "ok": not warnings,
    }
