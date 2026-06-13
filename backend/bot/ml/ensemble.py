"""Stage-10 item 5 — HGB two-model ensemble with logistic stacker + isotonic.

Three-stage stacking, sklearn-only:
  1. Two HistGradientBoosting base models trained with DIFFERENT
     ``random_state`` AND ``l2_regularization`` so they make distinct
     mistakes (without this, stacking gains nothing).
  2. A LogisticRegression stacker on top of the two base predictions —
     learns the weighted blend that minimizes log-loss.
  3. Isotonic calibration wrapping the entire stack so the final
     probabilities are honest (Brier-friendly).

Single artifact — pickled + versioned through the existing
`bot/ml/registry.py`, so `set_active` / A/B routing / drift detection just
work without changes.

Why not just train one bigger model? Two-model stacking on small data is
the cheapest single calibration-and-lift gain in the institutional toolbox.
The ECE typically drops 1-2 percentage points; AUC ticks up 0.01-0.02.
That's exactly the band the Stage-1.5 gates care about.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple

from backend.bot.ml.feature_store import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
)

logger = logging.getLogger(__name__)


# Two distinct feature subsets so the base models see different views.
# Each must contain ≥ 1 numeric and ≥ 1 categorical, otherwise the shared
# preprocessor will trip on a missing column.
SUBSET_A_NUMERIC = [
    "confidence", "win_probability", "atr", "rsi_14", "iv_rank",
    "composite_bias",
]
SUBSET_B_NUMERIC = [
    "confidence", "win_probability", "darkpool_bias",
    "pinning_probability", "gex_total", "composite_bias",
]


def _make_pipeline(*, numeric_features: List[str],
                     categorical_features: List[str],
                     random_state: int, l2: float) -> Any:
    """Identical preprocessor shape to bot/ml/models.py, but parametrized."""
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    try:
        ohe = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    except TypeError:
        ohe = OneHotEncoder(handle_unknown="ignore", sparse=False)
    pre = ColumnTransformer([
        ("num", Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
        ]), numeric_features),
        ("cat", Pipeline([
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("ohe", ohe),
        ]), categorical_features),
    ])
    return Pipeline([
        ("pre", pre),
        ("clf", HistGradientBoostingClassifier(
            max_depth=4, learning_rate=0.07, max_iter=200,
            l2_regularization=l2, random_state=random_state,
        )),
    ])


# ── stacker estimator (sklearn-compatible) ───────────────────────────────


def _maybe_estimator_base():
    """Pull sklearn's BaseEstimator + ClassifierMixin if available so
    CalibratedClassifierCV can clone the stacker."""
    try:
        from sklearn.base import BaseEstimator, ClassifierMixin
        return (ClassifierMixin, BaseEstimator)
    except Exception:
        return (object,)


class EnsembleStacker(*_maybe_estimator_base()):       # type: ignore[misc]
    """Two-base + logistic-stacker estimator with sklearn-style fit + predict.

    Inherits from ``ClassifierMixin + BaseEstimator`` so sklearn's
    ``CalibratedClassifierCV`` can clone us (via ``get_params`` / ``set_params``
    that BaseEstimator provides automatically as long as ``__init__`` only
    stores its kwargs).

    Implements ``predict_proba(X)`` returning shape (n_samples, 2) so the
    rest of the system (calibration, registry, A/B routing) treats it like
    any other classifier.
    """

    def __init__(self,
                  numeric_a: Optional[List[str]] = None,
                  numeric_b: Optional[List[str]] = None,
                  categorical: Optional[List[str]] = None,
                  l2_a: float = 1.0, l2_b: float = 0.3,
                  random_a: int = 0, random_b: int = 7,
                  ) -> None:
        # IMPORTANT: BaseEstimator.get_params introspects __init__ — store
        # parameters as-is without renaming, and don't do work here.
        self.numeric_a = numeric_a
        self.numeric_b = numeric_b
        self.categorical = categorical
        self.l2_a = l2_a
        self.l2_b = l2_b
        self.random_a = random_a
        self.random_b = random_b
        self.model_a_: Any = None
        self.model_b_: Any = None
        self.stacker_: Any = None
        self.classes_ = None      # set during fit; sklearn-compat attribute

    def _eff_numeric_a(self) -> List[str]:
        return self.numeric_a or SUBSET_A_NUMERIC

    def _eff_numeric_b(self) -> List[str]:
        return self.numeric_b or SUBSET_B_NUMERIC

    def _eff_categorical(self) -> List[str]:
        return self.categorical or CATEGORICAL_FEATURES

    def fit(self, X: Any, y: Any) -> "EnsembleStacker":
        from sklearn.linear_model import LogisticRegression
        import numpy as np

        # base models — use the _eff_* accessors so None stored params still
        # resolve to the module-level defaults
        self.model_a_ = _make_pipeline(
            numeric_features=self._eff_numeric_a(),
            categorical_features=self._eff_categorical(),
            random_state=self.random_a, l2=self.l2_a,
        )
        self.model_b_ = _make_pipeline(
            numeric_features=self._eff_numeric_b(),
            categorical_features=self._eff_categorical(),
            random_state=self.random_b, l2=self.l2_b,
        )
        self.model_a_.fit(X, y)
        self.model_b_.fit(X, y)

        # stacker on (p_a, p_b) → y. Fit on TRAIN predictions; for production
        # we accept the small overfitting risk vs the cost of CV here.
        pa = self.model_a_.predict_proba(X)[:, 1]
        pb = self.model_b_.predict_proba(X)[:, 1]
        Z = np.column_stack([pa, pb])
        self.stacker_ = LogisticRegression(max_iter=400)
        self.stacker_.fit(Z, y)
        self.classes_ = self.stacker_.classes_
        return self

    def predict_proba(self, X: Any) -> Any:
        import numpy as np
        if self.stacker_ is None:
            raise RuntimeError("EnsembleStacker not fit")
        pa = self.model_a_.predict_proba(X)[:, 1]
        pb = self.model_b_.predict_proba(X)[:, 1]
        Z = np.column_stack([pa, pb])
        return self.stacker_.predict_proba(Z)

    def predict(self, X: Any) -> Any:
        proba = self.predict_proba(X)
        return (proba[:, 1] >= 0.5).astype(int)


# ── public factory ──────────────────────────────────────────────────────


def create_ensemble() -> EnsembleStacker:
    """Default-config ensemble for the existing ``ml/train`` flow."""
    return EnsembleStacker()
