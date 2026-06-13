"""Predictive ML status endpoint — exposes whether a trained probability
model is loaded and what it was fit on."""
from __future__ import annotations

import os

from fastapi import APIRouter

from backend.bot.predictive import (
    DEFAULT_MODEL_PATH,
    MIN_TRAINING_ROWS,
    get_model,
)

router = APIRouter(prefix="/predictive", tags=["predictive"])


@router.get("/status")
async def predictive_status() -> dict:
    """Whether the ML probability model is loaded + its training metadata."""
    model = get_model()
    path = model.model_path
    meta = model.metadata() if model.available else {}
    return {
        "available": model.available,
        "model_path": path,
        "artifact_exists": os.path.exists(path),
        "min_training_rows": MIN_TRAINING_ROWS,
        "default_path": DEFAULT_MODEL_PATH,
        **({"meta": meta} if meta else {}),
    }
