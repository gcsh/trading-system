"""Experiment tracking — Stage-1.5 reproducibility layer.

Every meaningful evaluation run (walk-forward, calibration sweep, model train)
gets logged here with enough provenance that an outside reviewer can reproduce
the number: which trades were in scope (dataset_hash), which random seed,
which model version, which git commit the code was at, and the resulting
metric snapshot.

Without this layer, "Sharpe went from 0.6 → 0.9" is unverifiable; with it,
the bump is auditable and rollback-able.

Stored in a dedicated SQLite table so artifacts can be queried independently
of trades — surfacing via `/experiments`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import (
    Column, DateTime, Float, Integer, String, desc, select,
)

from backend.db import Base, session_scope

logger = logging.getLogger(__name__)


# ── ORM model ───────────────────────────────────────────────────────────────


class ExperimentRecord(Base):
    """One row per evaluation run. Frozen at write time; never updated."""
    __tablename__ = "experiment_record"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    name = Column(String, index=True)              # e.g. "walkforward_v1"
    kind = Column(String, default="evaluation")    # evaluation | training | calibration
    dataset_hash = Column(String, index=True)
    seed = Column(Integer, default=0)
    model_version = Column(String, default="")
    code_sha = Column(String, default="")
    params_json = Column(String, default="{}")
    metrics_json = Column(String, default="{}")
    label_quality_json = Column(String, default="{}")
    notes = Column(String, default="")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "name": self.name,
            "kind": self.kind,
            "dataset_hash": self.dataset_hash,
            "seed": self.seed,
            "model_version": self.model_version,
            "code_sha": self.code_sha,
            "params": _safe_load(self.params_json),
            "metrics": _safe_load(self.metrics_json),
            "label_quality": _safe_load(self.label_quality_json),
            "notes": self.notes,
        }


def _safe_load(blob: Optional[str]) -> Any:
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except Exception:
        return {}


# ── provenance helpers ──────────────────────────────────────────────────────


def dataset_hash(records: Sequence[Dict[str, Any]] | Sequence[Any]) -> str:
    """SHA-256 over a canonical serialization of the records. Used as the
    dataset identity for an experiment so two runs over "the same data" can
    be proven identical (or different) by hash."""
    h = hashlib.sha256()
    for r in records:
        if hasattr(r, "to_dict"):
            r = r.to_dict()
        # Sort keys so dict insertion order can't change the hash.
        h.update(json.dumps(r, sort_keys=True, default=str).encode())
    return h.hexdigest()[:16]


def code_sha() -> str:
    """Best-effort git SHA of the current checkout. Empty string when not in
    a git working copy (the project may not be a repo locally)."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return ""


# ── recorder ────────────────────────────────────────────────────────────────


def record_experiment(
    name: str,
    *,
    kind: str = "evaluation",
    dataset_hash: str = "",
    seed: int = 0,
    model_version: str = "",
    params: Optional[Dict[str, Any]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    label_quality: Optional[Dict[str, Any]] = None,
    notes: str = "",
) -> int:
    """Persist one frozen experiment record. Returns the row id."""
    code = code_sha()
    with session_scope() as session:
        row = ExperimentRecord(
            name=str(name),
            kind=str(kind),
            dataset_hash=dataset_hash or "",
            seed=int(seed),
            model_version=str(model_version),
            code_sha=code,
            params_json=json.dumps(params or {}, sort_keys=True, default=str),
            metrics_json=json.dumps(metrics or {}, sort_keys=True, default=str),
            label_quality_json=json.dumps(label_quality or {}, sort_keys=True, default=str),
            notes=str(notes)[:500],
        )
        session.add(row)
        session.flush()
        return int(row.id)


def list_experiments(limit: int = 100, name: Optional[str] = None) -> List[Dict[str, Any]]:
    with session_scope() as session:
        q = select(ExperimentRecord).order_by(desc(ExperimentRecord.timestamp))
        if name:
            q = q.where(ExperimentRecord.name == name)
        rows = session.execute(q.limit(limit)).scalars().all()
        return [r.to_dict() for r in rows]


def get_experiment(experiment_id: int) -> Optional[Dict[str, Any]]:
    with session_scope() as session:
        row = session.get(ExperimentRecord, experiment_id)
        return row.to_dict() if row else None


def compare_experiments(a_id: int, b_id: int) -> Dict[str, Any]:
    """Diff two experiments. Useful for "did the new model actually improve
    things vs the old one over the same data?". Returns matched metric deltas."""
    a = get_experiment(a_id)
    b = get_experiment(b_id)
    if not a or not b:
        return {"error": "one or both experiments missing", "a": a, "b": b}
    a_metrics = a.get("metrics") or {}
    b_metrics = b.get("metrics") or {}
    keys = sorted(set(a_metrics) | set(b_metrics))
    diffs: List[Dict[str, Any]] = []
    for k in keys:
        av, bv = a_metrics.get(k), b_metrics.get(k)
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            diffs.append({"metric": k, "a": av, "b": bv,
                            "delta": round(bv - av, 4)})
        else:
            diffs.append({"metric": k, "a": av, "b": bv, "delta": None})
    return {"a": a, "b": b, "diffs": diffs,
             "same_dataset": a["dataset_hash"] == b["dataset_hash"],
             "same_code": a["code_sha"] == b["code_sha"] and a["code_sha"] != ""}
