"""Probability calibration — Platt (sigmoid) + isotonic wrappers.

A model can be discriminating (high AUC) AND yet uncalibrated (predicted
0.80 actually wins 0.60). The MetricsCard's Brier + ECE will surface that;
this module fixes it.

Two flavours, both via sklearn's CalibratedClassifierCV with prefit=False
(it cross-validates the base model + fits the calibrator). The same
interface works for any model the factory produces.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

CalibrationKind = Literal["sigmoid", "isotonic"]


def calibrate_model(
    pipeline: Any,
    X: Any, y: Any,
    *,
    method: CalibrationKind = "sigmoid",
    cv: int = 3,
) -> Any:
    """Return a CalibratedClassifierCV that wraps ``pipeline`` with the
    chosen method.

    Args:
        pipeline: any sklearn-compatible classifier (Pipeline OK)
        method:   "sigmoid" (Platt) or "isotonic"
        cv:       CV folds — use 3 for small samples (Stage-1 dataset is thin)
    """
    from sklearn.calibration import CalibratedClassifierCV

    calibrator = CalibratedClassifierCV(
        estimator=pipeline, method=method, cv=cv,
    )
    calibrator.fit(X, y)
    return calibrator
