"""Predictive ML probability model — sklearn-backed, optional, A/B-able.

The heuristic ``probability.score_signal`` already gives every signal a
calibrated win-probability. This module trains a lightweight classifier on the
DecisionLog feedback rows (decision context → realized P&L sign) and exposes a
clean ``predict(features)`` so the scorer can blend its number with the model's
when enough data + a saved artifact exist.

Design notes:
  • sklearn-only — lightgbm/xgboost/catboost aren't in this venv. Logistic
    regression is the right starting point: small data, interpretable, calibrated.
  • Pure A/B seam — ``MLProbabilityModel.predict`` returns ``None`` whenever the
    model can't speak (no artifact, missing features, dep missing), so callers
    can always fall back to the heuristic without try/except.
  • Trained artifact lives at a configurable path; the engine never blocks if
    the file is absent. A trainer script (``predictive.train``) refreshes it
    from the live DecisionLog table.

The feature vector is intentionally small and stable — adding a column means a
retrain, not a runtime crash, because ``predict`` only reads the columns the
trained model was fit on (persisted alongside the estimator).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Feature columns extracted from a DecisionLog row + its features_json blob.
# Kept short on purpose: more columns + tiny outcome counts = overfitting.
FEATURE_COLUMNS: List[str] = [
    "confidence",
    "win_probability",
    "atr",
    "rsi_14",
    "iv_rank",
    "composite_bias",
    "darkpool_bias",
    "pinning_probability",
    "gex_total",
]

CATEGORICAL_COLUMNS: List[str] = [
    "regime_trend",        # bullish | bearish | choppy | unknown
    "regime_volatility",   # high | normal | low
    "regime_gamma",        # long_gamma | short_gamma | unknown
    "grade",               # A+ | A | B | C | D
    "hedging_pressure",    # low | normal | high
    "dominant_wall",       # call | put | neutral
]

DEFAULT_MODEL_PATH = os.getenv("TB_ML_PROB_MODEL", "./ml/predictive_model.pkl")
MIN_TRAINING_ROWS = 30   # below this we don't bother — heuristic is the baseline


@dataclass
class TrainingResult:
    rows: int
    accuracy: float
    base_rate: float       # share of wins in the training set
    model_path: str

    def to_dict(self) -> dict:
        return {
            "rows": self.rows,
            "accuracy": round(self.accuracy, 4),
            "base_rate": round(self.base_rate, 4),
            "model_path": self.model_path,
        }


# ── feature extraction ──────────────────────────────────────────────────────


def _features_from_row(row: dict) -> Dict[str, Any]:
    """Flatten a DecisionLog dict into the model's feature dict (numeric +
    categorical). Missing fields become NaN/empty so the pipeline can impute."""
    import json

    feats = {}
    try:
        feats = json.loads(row.get("features_json") or "{}") or {}
    except Exception:
        feats = {}

    out: Dict[str, Any] = {}
    for col in FEATURE_COLUMNS:
        val = row.get(col, feats.get(col))
        if val is None:
            out[col] = None
        else:
            try:
                out[col] = float(val)
            except (TypeError, ValueError):
                out[col] = None      # non-numeric leak in numeric column → skip
    for col in CATEGORICAL_COLUMNS:
        out[col] = str(row.get(col) or feats.get(col) or "unknown")
    return out


def build_dataset(rows: List[dict]) -> Tuple[List[Dict[str, Any]], List[int]]:
    """Convert DecisionLog dicts (must have outcome_pnl) into (X, y) ready for
    sklearn. y=1 if the trade made money, 0 otherwise."""
    X: List[Dict[str, Any]] = []
    y: List[int] = []
    for r in rows:
        pnl = r.get("outcome_pnl")
        if pnl is None:
            continue
        X.append(_features_from_row(r))
        y.append(1 if float(pnl) > 0 else 0)
    return X, y


# ── trainer / predictor ─────────────────────────────────────────────────────


class MLProbabilityModel:
    """Sklearn LogisticRegression with a one-hot + imputer preprocessing front.

    Always degrades gracefully — ``predict`` returns ``None`` when the model
    can't speak, so the scorer can fall back to the heuristic without branching.
    """

    def __init__(self, model_path: Optional[str] = None) -> None:
        self.model_path = model_path or DEFAULT_MODEL_PATH
        self._pipeline = None     # sklearn Pipeline
        self._meta: Dict[str, Any] = {}
        self._tried_load = False

    # ---- availability ------------------------------------------------------

    @property
    def available(self) -> bool:
        """True when a fitted artifact has been loaded into this instance."""
        if self._pipeline is None and not self._tried_load:
            self._maybe_load()
        return self._pipeline is not None

    def _maybe_load(self) -> None:
        self._tried_load = True
        if not os.path.exists(self.model_path):
            return
        try:
            import joblib

            payload = joblib.load(self.model_path)
            self._pipeline = payload["pipeline"]
            self._meta = payload.get("meta") or {}
        except Exception:
            logger.debug("failed to load predictive model at %s", self.model_path, exc_info=True)
            self._pipeline = None
            self._meta = {}

    def metadata(self) -> Dict[str, Any]:
        if self._pipeline is None and not self._tried_load:
            self._maybe_load()
        return dict(self._meta)

    # ---- training ----------------------------------------------------------

    def train(self, rows: List[dict]) -> Optional[TrainingResult]:
        """Fit the pipeline on DecisionLog rows. Returns None if there are too
        few labeled rows or sklearn is missing."""
        try:
            from sklearn.compose import ColumnTransformer
            from sklearn.impute import SimpleImputer
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import Pipeline
            from sklearn.preprocessing import OneHotEncoder, StandardScaler
        except Exception:
            logger.info("sklearn unavailable — skipping ML training")
            return None

        X, y = build_dataset(rows)
        if len(X) < MIN_TRAINING_ROWS:
            logger.info("only %d labeled rows — need %d to train",
                         len(X), MIN_TRAINING_ROWS)
            return None
        if len(set(y)) < 2:
            logger.info("all outcomes are the same class — cannot fit")
            return None

        import numpy as np
        import pandas as pd

        df = pd.DataFrame(X)
        # OneHotEncoder API changed across sklearn versions — handle both.
        try:
            ohe = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
        except TypeError:
            ohe = OneHotEncoder(handle_unknown="ignore", sparse=False)

        pre = ColumnTransformer([
            ("num", Pipeline([
                ("impute", SimpleImputer(strategy="median")),
                ("scale", StandardScaler()),
            ]), FEATURE_COLUMNS),
            ("cat", Pipeline([
                ("impute", SimpleImputer(strategy="most_frequent")),
                ("ohe", ohe),
            ]), CATEGORICAL_COLUMNS),
        ])
        pipeline = Pipeline([
            ("pre", pre),
            ("clf", LogisticRegression(max_iter=400, class_weight="balanced")),
        ])
        pipeline.fit(df, y)
        acc = float(pipeline.score(df, y))
        base_rate = float(sum(y)) / float(len(y))

        self._pipeline = pipeline
        self._meta = {
            "rows": len(X),
            "accuracy": acc,
            "base_rate": base_rate,
            "feature_columns": FEATURE_COLUMNS,
            "categorical_columns": CATEGORICAL_COLUMNS,
        }

        os.makedirs(os.path.dirname(self.model_path) or ".", exist_ok=True)
        import joblib

        joblib.dump({"pipeline": pipeline, "meta": self._meta}, self.model_path)
        _ = np   # keep import alive — used implicitly via pandas dtypes
        return TrainingResult(rows=len(X), accuracy=acc,
                               base_rate=base_rate, model_path=self.model_path)

    # ---- prediction --------------------------------------------------------

    def predict(self, features: Dict[str, Any]) -> Optional[float]:
        """Calibrated win-probability for one signal. ``None`` when the model
        can't speak (no artifact, prediction failure)."""
        if not self.available:
            return None
        try:
            import pandas as pd

            row: Dict[str, Any] = {}
            for col in FEATURE_COLUMNS:
                val = features.get(col)
                if val is None:
                    row[col] = None
                else:
                    try:
                        row[col] = float(val)
                    except (TypeError, ValueError):
                        row[col] = None
            for col in CATEGORICAL_COLUMNS:
                row[col] = str(features.get(col) or "unknown")
            proba = self._pipeline.predict_proba(pd.DataFrame([row]))
            # class 1 = win. Pipeline classes_ can be a numpy array — avoid
            # the truthiness gymnastics and pull from the classifier directly.
            try:
                classes = list(self._pipeline.named_steps["clf"].classes_)
            except Exception:
                classes = [0, 1]
            idx = classes.index(1) if 1 in classes else len(classes) - 1
            return float(proba[0][idx])
        except Exception:
            logger.debug("predict failed", exc_info=True)
            return None


# Module-level singleton — the engine + scorer share it so we only load the
# artifact off disk once per process.
_MODEL: Optional[MLProbabilityModel] = None


def get_model(model_path: Optional[str] = None) -> MLProbabilityModel:
    global _MODEL
    if _MODEL is None or (model_path and _MODEL.model_path != model_path):
        _MODEL = MLProbabilityModel(model_path=model_path)
    return _MODEL


def reset_model() -> None:
    """Test helper — forget the singleton so a freshly written artifact reloads."""
    global _MODEL
    _MODEL = None
