"""MITS Phase 14.A — hybrid fast/deep composer package.

Public API:
  - compose_hybrid: ensemble entry point used by the analysis route +
    EOD analysis composer.
  - FastComposerResult / EnsembleResult / DeepComposerOutput:
    consumer-facing dataclasses + pydantic schema.
"""
from __future__ import annotations

from backend.bot.analysis.deep_composer import DeepComposerOutput
from backend.bot.analysis.fast_composer import FastComposerResult
from backend.bot.analysis.hybrid import EnsembleResult, compose_hybrid


__all__ = [
    "compose_hybrid",
    "FastComposerResult",
    "EnsembleResult",
    "DeepComposerOutput",
]
