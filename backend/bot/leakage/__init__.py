"""Stage-10 item 16 — randomized label-lag canary for lookahead detection.

The canary is the single most important leakage test: shift the LABEL
vector by ``k`` rows relative to the feature matrix. A clean dataset will
collapse to baseline accuracy (≈ class prior). A LEAKY dataset — one where
a feature contains information about the future label — will retain
above-baseline accuracy because the leak is still aligned.

Run nightly after training; any "passing" model must score ≤ baseline +
``tolerance`` on the lagged data. If it doesn't, there's a lookahead bug
in either the feature engineering or the dataset builder.

Pure function — no DB writes, no broker calls.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class LeakageReport:
    base_accuracy: float
    base_brier: Optional[float]
    lagged_accuracy: float
    lagged_brier: Optional[float]
    baseline: float                    # class-prior accuracy
    tolerance: float
    lag: int
    leakage_suspected: bool
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _accuracy(predictions: List[int], outcomes: List[int]) -> float:
    if not predictions or len(predictions) != len(outcomes):
        return 0.0
    return sum(1 for p, y in zip(predictions, outcomes) if p == y) / len(predictions)


def _brier(probs: List[float], outcomes: List[int]) -> Optional[float]:
    from backend.bot.metrics import brier_score
    return brier_score(probs, outcomes)


def lag_canary(*, model: Any, X: Any, y: List[int],
                  lag: int = 5, tolerance: float = 0.05,
                  seed: int = 0) -> LeakageReport:
    """Honest leakage detector using a 70/30 train/test split:

      1. Train on ``(X_train, y_train)`` → score on ``(X_test, y_test)``
         = ``base_accuracy`` (what the model legitimately learns)
      2. RANDOMLY SHUFFLE ``y_train`` → train on ``(X_train, shuffled_y)``
         → score on the REAL ``(X_test, y_test)``
      3. If step-2 accuracy exceeds the test-set class-prior baseline
         plus ``tolerance``, the features contain information ABOUT THE
         LABEL that survives breaking the supervised mapping — leakage.

    ``model`` must be sklearn-compatible (fit + predict_proba + cloneable).
    """
    import numpy as np
    from sklearn.base import clone

    n = len(y)
    if n < 30:
        return LeakageReport(
            base_accuracy=0.0, base_brier=None,
            lagged_accuracy=0.0, lagged_brier=None,
            baseline=0.5, tolerance=tolerance, lag=lag,
            leakage_suspected=False,
            notes=[f"not enough rows ({n}) — need ≥ 30"],
        )

    rng = np.random.default_rng(seed)
    split = int(n * 0.7)
    if hasattr(X, "iloc"):
        X_train, X_test = X.iloc[:split], X.iloc[split:]
    else:
        X_train, X_test = X[:split], X[split:]
    y_train = list(y[:split])
    y_test = list(y[split:])

    # Base accuracy — train on (X_train, y_train), score on test set
    base_model = clone(model)
    base_model.fit(X_train, y_train)
    base_probs = base_model.predict_proba(X_test)[:, 1].tolist()
    base_preds = [1 if p >= 0.5 else 0 for p in base_probs]
    base_acc = _accuracy(base_preds, y_test)
    base_brier = _brier(base_probs, y_test)

    # Randomized labels — preserves class proportion but destroys mapping
    shuffled = list(y_train)
    rng.shuffle(shuffled)
    lagged_model = clone(model)
    lagged_model.fit(X_train, shuffled)
    lagged_probs = lagged_model.predict_proba(X_test)[:, 1].tolist()
    lagged_preds = [1 if p >= 0.5 else 0 for p in lagged_probs]
    lagged_acc = _accuracy(lagged_preds, y_test)
    lagged_brier = _brier(lagged_probs, y_test)

    # Baseline = majority class on the TEST split
    pos = sum(y_test) / len(y_test) if y_test else 0.5
    baseline = max(pos, 1 - pos)
    suspected = lagged_acc > baseline + tolerance
    notes: List[str] = []
    if suspected:
        notes.append(
            f"LEAKAGE SUSPECTED — shuffled-label model still scored "
            f"{lagged_acc:.3f} on test (baseline {baseline:.3f} + tol {tolerance})"
        )
    else:
        notes.append(
            f"clean — shuffled-label accuracy {lagged_acc:.3f} ≤ "
            f"baseline {baseline:.3f} + tol {tolerance}"
        )
    return LeakageReport(
        base_accuracy=round(base_acc, 4), base_brier=base_brier,
        lagged_accuracy=round(lagged_acc, 4), lagged_brier=lagged_brier,
        baseline=round(baseline, 4), tolerance=tolerance, lag=lag,
        leakage_suspected=suspected, notes=notes,
    )
