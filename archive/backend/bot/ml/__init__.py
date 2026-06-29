"""Stage-5 ML upgrade — gradient boosting + calibration + versioning + A/B.

This package wraps the per-piece submodules with a clean public surface so
external callers (engine, API, tests) don't reach into internals:

  • ``feature_store``  — DecisionLog → feature matrix + label vector
  • ``models``         — sklearn model factory (logistic / hist_gb)
  • ``calibration``    — Platt + isotonic wrappers around any classifier
  • ``registry``       — versioned artifact + active-model pointer
  • ``ab``             — deterministic ticker-bucket A/B routing

The legacy ``backend/bot/predictive/`` keeps its public predict() signature
so existing call sites (engine, /analytics) keep working. When the registry
has an "active" model that's newer than the legacy artifact, predict() can
delegate to it through ``ml.score()``.
"""
from __future__ import annotations

from backend.bot.ml.ab import bucket_for, decide_arm, register_split
from backend.bot.ml.calibration import calibrate_model
from backend.bot.ml.feature_store import (
    FeatureRow,
    build_dataset,
    feature_store_stats,
)
from backend.bot.ml.models import (
    MODEL_FACTORY,
    create_model,
    supported_models,
)
from backend.bot.ml.registry import (
    active_model,
    list_models,
    register_model,
    set_active,
)

__all__ = [
    "bucket_for", "decide_arm", "register_split",
    "calibrate_model",
    "FeatureRow", "build_dataset", "feature_store_stats",
    "MODEL_FACTORY", "create_model", "supported_models",
    "active_model", "list_models", "register_model", "set_active",
]
