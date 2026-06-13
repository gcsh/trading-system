"""Numeric promotion gates endpoint — Stage-1.5 contract surface."""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.bot.gates import CATALOG, evaluate_gates
from backend.bot.gates.calibration_stability import compute_stability

router = APIRouter(prefix="/gates", tags=["gates"])


@router.get("/catalog")
async def gates_catalog() -> dict:
    """Static list of every gate + threshold the system is held to."""
    return {
        "gates": [
            {
                "name": g.name,
                "threshold": g.threshold,
                "direction": g.direction,
                "metric_path": g.metric_path,
                "minimum_sample": g.minimum_sample,
                "description": g.description,
            }
            for g in CATALOG
        ],
    }


@router.get("/status")
async def gates_status() -> dict:
    """Run the catalog against the live metrics summary and return the verdict."""
    from backend.api.routes.metrics import build_summary

    return evaluate_gates(build_summary())


@router.get("/stability")
async def gates_stability(window_size: int = Query(30, ge=10, le=200),
                            limit: int = Query(5000, ge=100, le=20000)) -> dict:
    """Full per-window Brier + ECE breakdown for the calibration-stability
    gates — feeds a UI sparkline so the operator can see *where* drift
    happened, not just whether the gate passed/failed."""
    from backend.api.routes.metrics import _load_labels

    labels = _load_labels(limit=limit)
    return compute_stability(labels, window_size=window_size).to_dict()
