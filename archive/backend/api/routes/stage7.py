"""Stage-7 endpoints — drift, monitoring, attribution, explainability."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.bot.attribution import (
    attribution_by_grade,
    attribution_by_regime,
    attribution_by_strategy,
    explain_trade,
)
from backend.bot.drift import (
    DriftReport,
    DriftSignal,
    assess_feature_drift,
    assess_prediction_drift,
    psi,
    severity_for,
)
from backend.bot.monitoring import (
    feed_health,
    feed_summary,
    record_failure,
    record_success,
)


# ── drift ─────────────────────────────────────────────────────────────────


drift_router = APIRouter(prefix="/drift", tags=["drift"])


class FeatureDriftBody(BaseModel):
    baseline_numeric: Dict[str, List[float]]
    current_numeric: Dict[str, List[float]]
    baseline_categorical: Optional[Dict[str, List[str]]] = None
    current_categorical: Optional[Dict[str, List[str]]] = None


@drift_router.post("/feature")
async def feature_drift_post(body: FeatureDriftBody) -> dict:
    """Compute PSI per feature given a baseline + current sample. The
    caller supplies the data — making the endpoint stateless and the unit
    tested independently of training-time artifacts."""
    report = assess_feature_drift(
        baseline_numeric=body.baseline_numeric,
        current_numeric=body.current_numeric,
        baseline_categorical=body.baseline_categorical,
        current_categorical=body.current_categorical,
    )
    return report.to_dict()


class PredictionDriftBody(BaseModel):
    baseline_preds: List[float]
    current_preds: List[float]


@drift_router.post("/prediction")
async def prediction_drift_post(body: PredictionDriftBody) -> dict:
    signal = assess_prediction_drift(
        baseline_preds=body.baseline_preds, current_preds=body.current_preds,
    )
    return signal.to_dict()


@drift_router.get("/psi")
async def psi_inline(
    baseline: str = Query(..., description="comma-separated floats"),
    current: str = Query(..., description="comma-separated floats"),
    n_bins: int = Query(10, ge=2, le=50),
) -> dict:
    """Quick PSI between two inline series — debug helper."""
    def _parse(s: str) -> List[float]:
        out: List[float] = []
        for chunk in s.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                out.append(float(chunk))
            except ValueError:
                continue
        return out
    b = _parse(baseline)
    c = _parse(current)
    score = psi(b, c, n_bins=n_bins)
    return {"psi": score, "severity": severity_for(score),
             "n_baseline": len(b), "n_current": len(c)}


# ── monitoring ──────────────────────────────────────────────────────────


monitor_router = APIRouter(prefix="/monitoring", tags=["monitoring"])


@monitor_router.get("/health")
async def monitor_health() -> dict:
    return feed_summary()


@monitor_router.get("/feed/{name}")
async def feed_detail(name: str) -> dict:
    return feed_health(name).to_dict()


class RecordBody(BaseModel):
    feed: str
    latency_ms: Optional[float] = None
    success: bool = True
    error: Optional[str] = None


@monitor_router.post("/record")
async def record(body: RecordBody) -> dict:
    """Manual recorder — used by tests + when a caller wants to ping the
    monitoring layer without using the timing context manager."""
    if body.success:
        record_success(body.feed, body.latency_ms or 0.0)
    else:
        record_failure(body.feed, error=body.error)
    return {"recorded": True, "feed": body.feed, "success": body.success}


# ── attribution ─────────────────────────────────────────────────────────


attr_router = APIRouter(prefix="/attribution", tags=["attribution"])


@attr_router.get("/by-strategy")
async def attr_by_strategy(limit: int = Query(5000, ge=10, le=20000)) -> dict:
    return {"buckets": attribution_by_strategy(limit=limit)}


@attr_router.get("/by-regime")
async def attr_by_regime(limit: int = Query(5000, ge=10, le=20000)) -> dict:
    return {"buckets": attribution_by_regime(limit=limit)}


@attr_router.get("/by-grade")
async def attr_by_grade(limit: int = Query(5000, ge=10, le=20000)) -> dict:
    return {"buckets": attribution_by_grade(limit=limit)}


# ── explainability ─────────────────────────────────────────────────────


explain_router = APIRouter(prefix="/explain", tags=["explain"])


@explain_router.get("/trade/{trade_id}")
async def explain(trade_id: int) -> dict:
    result = explain_trade(trade_id)
    if result is None:
        raise HTTPException(status_code=404, detail="trade not found")
    return result.to_dict()
