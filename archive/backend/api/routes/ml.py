"""Stage-5 ML endpoints — feature store, model train/list/active, A/B splits."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.bot.ml import (
    active_model,
    calibrate_model,
    create_model,
    decide_arm,
    feature_store_stats,
    list_models,
    register_model,
    register_split,
    set_active,
    supported_models,
)
from backend.bot.ml.ab import get_split, list_splits
from backend.bot.ml.feature_store import build_dataset
from backend.bot.metrics import brier_score, calibration_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ml", tags=["ml"])


# ── feature store ──────────────────────────────────────────────────────────


@router.get("/feature-store/stats")
async def fs_stats() -> dict:
    return feature_store_stats()


# ── model lifecycle ────────────────────────────────────────────────────────


@router.get("/models")
async def models_index() -> dict:
    return {"models": list_models(), "supported_types": supported_models()}


@router.get("/active")
async def active_index() -> dict:
    active = active_model()
    if active is None:
        return {"active": None, "reason": "no model registered yet"}
    return {"active": {"version": active["version"], "meta": active["meta"]}}


class TrainBody(BaseModel):
    model_type: str = "hist_gb"
    calibration: Optional[str] = "isotonic"     # "sigmoid" | "isotonic" | None
    min_closed: int = 30
    cv: int = 3
    set_active: bool = False
    notes: str = ""


@router.post("/train")
async def train(body: TrainBody) -> dict:
    """Train a model from the live DecisionLog feature store and persist it.

    Honest failure mode: when the labelled set is too thin we return 422 with
    the warnings from the feature store so the user knows exactly why.
    """
    X, y, meta = build_dataset(min_closed=body.min_closed)
    if X is None or y is None:
        raise HTTPException(status_code=422,
                              detail={"reason": "insufficient data",
                                       "feature_store": meta})
    try:
        if body.model_type not in supported_models():
            raise HTTPException(status_code=422,
                                  detail=f"unknown model_type '{body.model_type}'")
        pipeline = create_model(body.model_type)
        if body.calibration in ("sigmoid", "isotonic"):
            fitted = calibrate_model(pipeline, X, y,
                                       method=body.calibration, cv=body.cv)
        else:
            pipeline.fit(X, y)
            fitted = pipeline

        # Quick on-data Brier + ECE for the metadata snapshot.
        try:
            probs = fitted.predict_proba(X)[:, 1].tolist()
        except Exception:
            probs = None
        brier = brier_score(probs, y) if probs else None
        ece = calibration_error(probs, y) if probs else None

        registered = register_model(
            model=fitted,
            model_type=body.model_type,
            calibration=body.calibration,
            rows_trained=len(y),
            cv_brier=brier,
            cv_calibration_error=ece,
            notes=body.notes,
        )
        if body.set_active:
            set_active(registered.version)
        return {"model": registered.to_dict(),
                 "set_active": body.set_active,
                 "feature_store": meta}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("ml/train failed")
        raise HTTPException(status_code=500, detail=f"train failed: {exc}")


class SetActiveBody(BaseModel):
    version: str


@router.post("/set-active")
async def post_set_active(body: SetActiveBody) -> dict:
    try:
        meta = set_active(body.version)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"active": meta}


# ── A/B splits ─────────────────────────────────────────────────────────────


class CreateSplitBody(BaseModel):
    name: str
    control_version: str
    candidate_version: str
    candidate_share: float = 0.10
    notes: str = ""


@router.get("/ab")
async def ab_index() -> dict:
    return {"splits": list_splits()}


@router.post("/ab")
async def ab_create(body: CreateSplitBody) -> dict:
    rec = register_split(name=body.name,
                            control_version=body.control_version,
                            candidate_version=body.candidate_version,
                            candidate_share=body.candidate_share,
                            notes=body.notes)
    return rec.to_dict()


@router.get("/ab/{name}/route/{ticker}")
async def ab_route(name: str, ticker: str) -> dict:
    rec = get_split(name)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"unknown split '{name}'")
    arm = decide_arm(rec, ticker)
    return {"split": name, "ticker": ticker.upper(), "arm": arm,
             "version": rec.candidate_version if arm == "candidate"
                          else rec.control_version}
