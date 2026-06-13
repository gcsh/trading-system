"""MITS Phase 3 — operator-facing detector control plane.

Endpoints:
  GET  /detectors                 — list every registered detector +
                                    its config row (enabled, params, source).
  PATCH /detectors/{name}         — toggle / update params for one detector.
  POST /detectors/import-pine     — register a custom detector from Pine.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from backend.bot.detectors import (
    DETECTOR_REGISTRY, all_detectors, clear_detector_config_cache,
    rebuild_registry,
)
from backend.bot.detectors.pine_custom import can_evaluate_translation
from backend.bot.pine_import import translate_pine
from backend.db import session_scope
from backend.models.detector_config import DetectorConfig

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/detectors", tags=["detectors"])


def _ensure_row(session, name: str) -> DetectorConfig:
    """Get-or-create the DetectorConfig row for a detector name."""
    row = session.execute(
        select(DetectorConfig).where(DetectorConfig.name == name)
    ).scalar_one_or_none()
    if row is None:
        row = DetectorConfig(name=name, enabled=True, params_json="{}",
                                  source="builtin")
        session.add(row)
        session.flush()
    return row


@router.get("")
async def list_detectors() -> List[Dict[str, Any]]:
    """Return every registered detector + its persisted config (or the
    defaults when no row exists yet). Sorted by family then name so the
    UI grouping is stable.
    """
    # Pull persisted config rows in one query so the response is O(1)
    # session work even for the 30+ detectors.
    cfg_by_name: Dict[str, DetectorConfig] = {}
    try:
        with session_scope() as s:
            for row in s.execute(select(DetectorConfig)).scalars().all():
                cfg_by_name[row.name] = row.to_dict()
    except Exception:
        logger.debug("detector_config load failed", exc_info=True)

    out: List[Dict[str, Any]] = []
    for det in all_detectors():
        defaults = det.default_params() or {}
        cfg = cfg_by_name.get(det.pattern)
        if cfg is None:
            out.append({
                "name": det.pattern,
                "family": getattr(det, "family", "uncategorized"),
                "description": getattr(det, "description", "") or "",
                "enabled": True,
                "params": {},
                "default_params": defaults,
                "source": "builtin",
                "pine_source": None,
                "last_updated_at": None,
            })
        else:
            out.append({
                "name": det.pattern,
                "family": getattr(det, "family", "uncategorized"),
                "description": getattr(det, "description", "") or "",
                "enabled": bool(cfg["enabled"]),
                "params": cfg["params"] or {},
                "default_params": defaults,
                "source": cfg["source"] or "builtin",
                "pine_source": cfg["pine_source"],
                "last_updated_at": cfg["last_updated_at"],
            })
    # Stable sort: family then name.
    out.sort(key=lambda d: (d["family"], d["name"]))
    return out


class PatchBody(BaseModel):
    enabled: Optional[bool] = None
    params: Optional[Dict[str, Any]] = None


@router.patch("/{name}")
async def patch_detector(name: str, body: PatchBody) -> Dict[str, Any]:
    """Patch a detector's config row. Body fields are all optional; the
    server only updates fields you send.

    400 when the detector name isn't in the registry (and no existing
    row carries that name — guards against typos persisting forever).
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="detector name required")
    if name not in DETECTOR_REGISTRY:
        # Allow PATCH on a custom Pine-import detector that's registered
        # in the DB but not in the in-process registry yet.
        with session_scope() as s:
            existing = s.execute(
                select(DetectorConfig).where(DetectorConfig.name == name)
            ).scalar_one_or_none()
            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"detector '{name}' not registered",
                )

    with session_scope() as s:
        row = _ensure_row(s, name)
        if body.enabled is not None:
            row.enabled = bool(body.enabled)
        if body.params is not None:
            try:
                row.params_json = json.dumps(body.params)
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"params not JSON-serializable: {exc}",
                )
        row.last_updated_at = datetime.utcnow()
        result = row.to_dict()
    clear_detector_config_cache()
    return result


class PineImportBody(BaseModel):
    name: str
    source: str


@router.post("/import-pine")
async def import_pine_detector(body: PineImportBody) -> Dict[str, Any]:
    """Translate a Pine Script source into a registered custom detector.

    NOTE (Phase 3 limitation): the existing `backend.bot.pine_import`
    translator targets the legacy custom-rule strategy DSL, not a true
    detector. We use it here to validate the script + extract recognized
    rules, then PERSIST a `DetectorConfig` row with `source='pine_import'`
    and the original Pine in `pine_source`. The row is a placeholder —
    the runtime registry only fires it when a future detector-flavoured
    Pine translator is wired in. For now this gives operators a stable
    place to stash their scripts + see them in the UI, without breaking
    the live detection pipeline.

    Returns the persisted row + the translator's report (recognized /
    skipped rules) so the operator sees what the parser caught.
    """
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if any(ch in name for ch in (" ", "\t", "\n")):
        raise HTTPException(status_code=400,
                             detail="name must not contain whitespace")
    source = body.source or ""
    if not source.strip():
        raise HTTPException(status_code=400, detail="source required")

    # Translate to extract recognized rules (best-effort).
    try:
        result = translate_pine(source)
    except Exception as exc:
        raise HTTPException(status_code=400,
                             detail=f"pine translate failed: {exc}")

    with session_scope() as s:
        existing = s.execute(
            select(DetectorConfig).where(DetectorConfig.name == name)
        ).scalar_one_or_none()
        if existing is None:
            row = DetectorConfig(
                name=name,
                enabled=True,
                params_json="{}",
                source="pine_import",
                pine_source=source,
            )
            s.add(row)
            s.flush()
        else:
            row = existing
            row.source = "pine_import"
            row.pine_source = source
            row.last_updated_at = datetime.utcnow()
        out = row.to_dict()
    clear_detector_config_cache()
    # MITS Phase 4 (P4.2) — the registry is built once at import time;
    # rebuild it so the newly-persisted Pine row starts firing on the
    # very next detection cycle instead of waiting for an app restart.
    try:
        rebuild_registry()
    except Exception:
        logger.debug("rebuild_registry after pine import failed",
                            exc_info=True)
    will_fire = can_evaluate_translation(result)
    response: Dict[str, Any] = {
        "row": out,
        "recognized": list(result.recognized),
        "rules": list(result.rules),
        "skipped": list(result.skipped),
        "will_fire_next_cycle": will_fire,
    }
    if not will_fire:
        response["limitations"] = (
            "Translator did not recognise a rule the runtime can "
            "evaluate. Supported: MACD signal/zero-line cross, RSI "
            "threshold cross, SMA/EMA cross, price-vs-MA cross. The "
            "script is persisted for audit but won't fire."
        )
    return response
