"""Model factory — sklearn classifiers wrapped in a uniform preprocessor.

Stage-1.5 Predictive had Logistic Regression only. Stage 5 adds gradient
boosting via sklearn's HistGradientBoostingClassifier (closest substitute
when LightGBM/XGBoost aren't installed — same algorithm, same speed). Both
go through the same pre-processing pipeline so calibration and A/B routing
don't have to special-case model type.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

from backend.bot.ml.feature_store import CATEGORICAL_FEATURES, NUMERIC_FEATURES

logger = logging.getLogger(__name__)


def _build_pipeline(estimator: Any):
    """Compose the same scale + impute + one-hot preprocessor for every model."""
    from sklearn.compose import ColumnTransformer
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
        ]), NUMERIC_FEATURES),
        ("cat", Pipeline([
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("ohe", ohe),
        ]), CATEGORICAL_FEATURES),
    ])
    return Pipeline([("pre", pre), ("clf", estimator)])


# ── factory registry ───────────────────────────────────────────────────────


def _make_logistic() -> Any:
    from sklearn.linear_model import LogisticRegression
    return _build_pipeline(LogisticRegression(max_iter=400, class_weight="balanced"))


def _make_hist_gradient_boost() -> Any:
    from sklearn.ensemble import HistGradientBoostingClassifier
    return _build_pipeline(
        HistGradientBoostingClassifier(
            max_depth=4, learning_rate=0.07, max_iter=200,
            l2_regularization=1.0, random_state=0,
        )
    )


def _make_ensemble() -> Any:
    """Stage-10 two-HGB ensemble + logistic stacker. Honors the same
    predict_proba interface so calibration + registry + A/B routing all
    work unchanged."""
    from backend.bot.ml.ensemble import create_ensemble
    return create_ensemble()


MODEL_FACTORY: Dict[str, Callable[[], Any]] = {
    "logistic": _make_logistic,
    "hist_gb": _make_hist_gradient_boost,
    "ensemble": _make_ensemble,
}


def supported_models() -> List[str]:
    return sorted(MODEL_FACTORY)


def create_model(model_type: str = "hist_gb") -> Any:
    if model_type not in MODEL_FACTORY:
        raise ValueError(
            f"unknown model type '{model_type}' — supported: {supported_models()}"
        )
    return MODEL_FACTORY[model_type]()
