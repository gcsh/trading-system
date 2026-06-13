"""ML feature store — DecisionLog rows materialized as a feature matrix.

DecisionLog is the persisted substrate (Stage 1 wired it). This module is a
thin builder on top that gives ML callers:

  • ``build_dataset(min_closed)``   — (X_df, y_series, meta) for training
  • ``FeatureRow``                  — same row shape used by predict()
  • ``feature_store_stats()``       — quick "what's in here" summary

Stays narrow on purpose: we never let ML training see anything that wasn't
known at signal time (no lookahead). The DecisionLog row was written when the
engine fired the signal, so by construction every column is causal.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.decision_log import DecisionLog

logger = logging.getLogger(__name__)


# ── canonical feature schema (frozen) ─────────────────────────────────────


NUMERIC_FEATURES: List[str] = [
    "confidence",
    "win_probability",          # the heuristic at decision time
    "atr",
    "rsi_14",
    "iv_rank",
    "composite_bias",
    "darkpool_bias",
    "pinning_probability",
    "gex_total",
]

CATEGORICAL_FEATURES: List[str] = [
    "regime_trend",
    "regime_volatility",
    "regime_gamma",
    "grade",
    "hedging_pressure",
    "dominant_wall",
]


@dataclass
class FeatureRow:
    """One ML-ready record. y is the binary outcome target."""
    trade_id: Optional[int]
    ticker: str
    strategy: str
    timestamp: str
    numeric: Dict[str, Optional[float]] = field(default_factory=dict)
    categorical: Dict[str, str] = field(default_factory=dict)
    y: Optional[int] = None     # 1 if win, 0 if loss, None if not closed

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── builder ────────────────────────────────────────────────────────────────


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_from_decision(row_dict: Dict[str, Any]) -> FeatureRow:
    """Project a DecisionLog dict into a FeatureRow."""
    try:
        feats = json.loads(row_dict.get("features_json") or "{}") or {}
    except Exception:
        feats = {}
    numeric: Dict[str, Optional[float]] = {}
    for col in NUMERIC_FEATURES:
        if col in row_dict and row_dict[col] is not None:
            numeric[col] = _coerce_float(row_dict[col])
        elif col in feats:
            numeric[col] = _coerce_float(feats[col])
        else:
            numeric[col] = None
    categorical = {col: str(row_dict.get(col) or feats.get(col) or "unknown")
                    for col in CATEGORICAL_FEATURES}
    pnl = row_dict.get("outcome_pnl")
    y = (1 if (pnl is not None and float(pnl) > 0)
          else 0 if pnl is not None else None)
    return FeatureRow(
        trade_id=row_dict.get("trade_id"),
        ticker=str(row_dict.get("ticker") or ""),
        strategy=str(row_dict.get("strategy") or ""),
        timestamp=str(row_dict.get("timestamp") or ""),
        numeric=numeric, categorical=categorical, y=y,
    )


def _load_decision_rows(limit: int = 20_000) -> List[Dict[str, Any]]:
    """Pull DecisionLog rows as plain dicts (avoids DetachedInstanceError)."""
    with session_scope() as session:
        rows = session.execute(
            select(DecisionLog).order_by(desc(DecisionLog.timestamp)).limit(limit)
        ).scalars().all()
        return [r.to_dict() for r in rows]


def build_dataset(
    *,
    min_closed: int = 30,
    limit: int = 20_000,
) -> Tuple[Optional[Any], Optional[List[int]], Dict[str, Any]]:
    """Return ``(X_df, y_list, meta)`` from the labelled portion of DecisionLog.

    ``meta`` always contains the row count + warnings so callers can decide
    whether to train. If fewer than ``min_closed`` outcomes exist, returns
    ``(None, None, meta_with_warnings)`` — never an empty DataFrame that a
    naive trainer could overfit.
    """
    rows = _load_decision_rows(limit=limit)
    feature_rows = [_row_from_decision(r) for r in rows]
    labelled = [r for r in feature_rows if r.y is not None]

    meta: Dict[str, Any] = {
        "total_decisions": len(feature_rows),
        "labelled": len(labelled),
        "wins": sum(1 for r in labelled if r.y == 1),
        "losses": sum(1 for r in labelled if r.y == 0),
        "min_closed_required": min_closed,
        "warnings": [],
    }
    if len(labelled) < min_closed:
        meta["warnings"].append(
            f"only {len(labelled)} labelled rows; need ≥{min_closed} to train"
        )
        return None, None, meta
    if meta["wins"] == 0 or meta["losses"] == 0:
        meta["warnings"].append("class imbalance: one outcome class empty")
        return None, None, meta

    try:
        import pandas as pd
    except Exception:
        meta["warnings"].append("pandas unavailable")
        return None, None, meta

    records: List[Dict[str, Any]] = []
    for fr in labelled:
        rec = {**fr.numeric}
        for col in CATEGORICAL_FEATURES:
            rec[col] = fr.categorical.get(col, "unknown")
        records.append(rec)
    X = pd.DataFrame(records)
    y = [int(fr.y) for fr in labelled]
    return X, y, meta


def feature_store_stats() -> Dict[str, Any]:
    """Quick summary of the labelled portion + recent class balance."""
    rows = _load_decision_rows(limit=20_000)
    labelled = [r for r in rows if r.get("outcome_pnl") is not None]
    wins = sum(1 for r in labelled if (r.get("outcome_pnl") or 0) > 0)
    return {
        "total_decisions": len(rows),
        "labelled": len(labelled),
        "wins": wins, "losses": len(labelled) - wins,
        "numeric_features": NUMERIC_FEATURES,
        "categorical_features": CATEGORICAL_FEATURES,
    }
