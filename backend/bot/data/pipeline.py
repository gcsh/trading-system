"""Standard data pipeline: Raw → Clean → Normalize → Validate → Enrich.

Every external feed must flow through these stages before any signal or
strategy consumes it — the bot never eats raw, unvalidated data. Each data
module supplies the stage functions; this runner sequences them and stops
cleanly at the first failure, recording where and why.

A ``validate`` issue prefixed with ``!`` is fatal (pipeline stops); other
issues are kept as non-fatal warnings on the result.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    ok: bool
    source: str
    stage: str                       # last stage reached (or where it failed)
    data: Any = None                 # the enriched result when ok
    issues: List[str] = field(default_factory=list)


def run_pipeline(
    *,
    source: str,
    fetch: Callable[[], Any],
    clean: Callable[[Any], Any],
    normalize: Callable[[Any], Any],
    validate: Callable[[Any], List[str]],
    enrich: Callable[[Any], Any],
) -> PipelineResult:
    issues: List[str] = []

    # --- Raw ---
    try:
        raw = fetch()
    except Exception as exc:
        logger.warning("[pipeline:%s] raw fetch failed: %s", source, exc)
        return PipelineResult(False, source, "raw", None, [f"fetch failed: {exc}"])
    if raw is None or (hasattr(raw, "__len__") and len(raw) == 0):
        return PipelineResult(False, source, "raw", None, ["no raw data"])

    # --- Clean ---
    try:
        cleaned = clean(raw)
    except Exception as exc:
        return PipelineResult(False, source, "clean", None, [f"clean failed: {exc}"])

    # --- Normalize ---
    try:
        norm = normalize(cleaned)
    except Exception as exc:
        return PipelineResult(False, source, "normalize", None, [f"normalize failed: {exc}"])

    # --- Validate ---
    try:
        issues = list(validate(norm) or [])
    except Exception as exc:
        return PipelineResult(False, source, "validate", None, [f"validate failed: {exc}"])
    if any(i.startswith("!") for i in issues):
        return PipelineResult(False, source, "validate", norm, issues)

    # --- Enrich ---
    try:
        enriched = enrich(norm)
    except Exception as exc:
        return PipelineResult(False, source, "enrich", norm, issues + [f"enrich failed: {exc}"])

    return PipelineResult(True, source, "enrich", enriched, issues)
