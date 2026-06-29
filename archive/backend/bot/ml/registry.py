"""Model registry — versioned artifacts + an active pointer.

Each registered model is written to disk under ``./ml/registry/`` as a
``.pkl`` plus a JSON metadata sidecar. The active version is recorded in
``./ml/registry/active.json`` so the engine can be told to switch versions
without a restart.

Designed so a future canary path (Stage 8) can promote / demote by editing
``active.json`` and the rest of the system picks it up on the next poll.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REGISTRY_DIR = os.getenv("TB_ML_REGISTRY_DIR", "./ml/registry")
ACTIVE_POINTER = "active.json"


@dataclass
class ModelMetadata:
    version: str                      # e.g. "v3-hist_gb-isotonic"
    model_type: str                   # "logistic" | "hist_gb"
    calibration: Optional[str] = None # "sigmoid" | "isotonic" | None
    trained_at: str = ""              # ISO timestamp
    rows_trained: int = 0
    cv_brier: Optional[float] = None
    cv_calibration_error: Optional[float] = None
    notes: str = ""
    artifact_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _ensure_dir() -> None:
    os.makedirs(REGISTRY_DIR, exist_ok=True)


def _meta_path(version: str) -> str:
    return os.path.join(REGISTRY_DIR, f"{version}.json")


def _artifact_path(version: str) -> str:
    return os.path.join(REGISTRY_DIR, f"{version}.pkl")


def _active_path() -> str:
    return os.path.join(REGISTRY_DIR, ACTIVE_POINTER)


# ── persistence ────────────────────────────────────────────────────────────


def register_model(*, model: Any, model_type: str,
                    calibration: Optional[str] = None,
                    rows_trained: int = 0,
                    cv_brier: Optional[float] = None,
                    cv_calibration_error: Optional[float] = None,
                    notes: str = "") -> ModelMetadata:
    """Save a trained model + metadata; returns the version metadata."""
    import joblib

    _ensure_dir()
    ts = time.strftime("%Y%m%d-%H%M%S")
    version = f"{ts}-{model_type}" + (f"-{calibration}" if calibration else "")
    artifact = _artifact_path(version)
    joblib.dump({"model": model}, artifact)
    meta = ModelMetadata(
        version=version, model_type=model_type, calibration=calibration,
        trained_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        rows_trained=rows_trained, cv_brier=cv_brier,
        cv_calibration_error=cv_calibration_error, notes=notes,
        artifact_path=artifact,
    )
    with open(_meta_path(version), "w") as f:
        json.dump(meta.to_dict(), f, indent=2)
    return meta


def list_models() -> List[Dict[str, Any]]:
    """Return every registered model sorted newest-first."""
    if not os.path.isdir(REGISTRY_DIR):
        return []
    out: List[Dict[str, Any]] = []
    for name in os.listdir(REGISTRY_DIR):
        if not name.endswith(".json") or name == ACTIVE_POINTER:
            continue
        try:
            with open(os.path.join(REGISTRY_DIR, name)) as f:
                out.append(json.load(f))
        except Exception:
            continue
    out.sort(key=lambda m: m.get("trained_at", ""), reverse=True)
    return out


def set_active(version: str) -> Dict[str, Any]:
    """Point ``active.json`` at the given version. Returns the new active blob."""
    meta_path = _meta_path(version)
    if not os.path.exists(meta_path):
        raise ValueError(f"unknown model version '{version}'")
    with open(meta_path) as f:
        meta = json.load(f)
    _ensure_dir()
    with open(_active_path(), "w") as f:
        json.dump({"version": version, "set_at": time.strftime(
            "%Y-%m-%dT%H:%M:%S")}, f, indent=2)
    return meta


def active_model() -> Optional[Dict[str, Any]]:
    """Load + return the active model + metadata, or ``None`` when no active
    pointer exists (Stage-5 cold-start)."""
    import joblib

    ap = _active_path()
    if not os.path.exists(ap):
        return None
    try:
        with open(ap) as f:
            pointer = json.load(f)
        version = pointer["version"]
        with open(_meta_path(version)) as f:
            meta = json.load(f)
        payload = joblib.load(meta["artifact_path"])
        return {"version": version, "meta": meta, "model": payload["model"]}
    except Exception:
        logger.debug("active_model load failed", exc_info=True)
        return None
