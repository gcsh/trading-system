"""Experiment tracking endpoints — Stage-1.5 reproducibility surface."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.bot.experiments import (
    compare_experiments,
    get_experiment,
    list_experiments,
    record_experiment,
)
from backend.bot.evaluation import walk_forward_evaluate
from backend.bot.experiments import code_sha
from backend.bot.experiments import dataset_hash as _dataset_hash
from backend.bot.labeling import label_quality
from backend.bot.metrics import summarize

router = APIRouter(prefix="/experiments", tags=["experiments"])


@router.get("")
async def experiments_index(limit: int = Query(100, ge=1, le=2000),
                              name: Optional[str] = None) -> dict:
    return {"experiments": list_experiments(limit=limit, name=name),
             "code_sha": code_sha()}


@router.get("/{experiment_id}")
async def experiment_detail(experiment_id: int) -> dict:
    exp = get_experiment(experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return exp


@router.get("/compare/{a_id}/{b_id}")
async def experiment_compare(a_id: int, b_id: int) -> dict:
    return compare_experiments(a_id, b_id)


@router.post("/run/walkforward")
async def run_walkforward_experiment(
    train_size: int = Query(100, ge=10, le=2000),
    test_size: int = Query(30, ge=5, le=1000),
    seed: int = Query(0, ge=0),
    notes: str = Query(""),
) -> dict:
    """Run a walk-forward evaluation against the live labels and persist the
    full provenance (dataset hash, params, metrics, code SHA) as an experiment
    row. Returns the experiment id so it can be referenced + compared later."""
    # Inline import to avoid the metrics route importing this module.
    from backend.api.routes.metrics import _load_labels

    labels = _load_labels(limit=20000)
    quality = label_quality(labels)
    ds_hash = _dataset_hash([l.to_dict() for l in labels])
    result = walk_forward_evaluate(
        labels, train_size=train_size, test_size=test_size,
    )
    exp_id = record_experiment(
        name="walkforward",
        kind="evaluation",
        dataset_hash=ds_hash,
        seed=seed,
        params={"train_size": train_size, "test_size": test_size},
        metrics=result.get("summary") or {},
        label_quality=quality,
        notes=notes,
    )
    return {
        "experiment_id": exp_id,
        "dataset_hash": ds_hash,
        "summary": result.get("summary") or {},
        "n_windows": len(result.get("windows") or []),
        "label_quality": quality,
    }
