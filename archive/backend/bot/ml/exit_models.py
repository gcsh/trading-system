"""Stage-10 item 10 — quantile MFE/MAE exit models.

Replaces fixed take-profit and stop-loss bands with model-suggested
percentiles of next-k-bar Maximum Favorable Excursion (MFE) and Maximum
Adverse Excursion (MAE).

Two HGB quantile regressors:
  • ``mfe_model`` — predicts the 75th percentile of next-k-bar MFE %
  • ``mae_model`` — predicts the 75th percentile of next-k-bar MAE %

The engine uses these to set adaptive TP / SL bands per trade instead of
"always 10% TP, 5% SL". When the regressors aren't trained (cold start),
``suggest_tp_sl`` returns the static config defaults so the rest of the
system still works.

sklearn-only — HistGradientBoostingRegressor + quantile loss. Versioned
through the existing `bot/ml/registry.py` with the prefix ``exit-mfe`` /
``exit-mae`` so they don't collide with the classifier registry.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

EXIT_MODEL_DIR = os.getenv("TB_EXIT_MODEL_DIR", "./ml/exit_models")


@dataclass
class MfeMaeSuggestion:
    take_profit_pct: float
    stop_loss_pct: float
    expected_mfe_pct: Optional[float] = None
    expected_mae_pct: Optional[float] = None
    source: str = "static"           # "static" | "model"
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── trainer ──────────────────────────────────────────────────────────────


def _make_quantile_regressor(quantile: float, *,
                              random_state: int = 0) -> Any:
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler
    from backend.bot.ml.feature_store import (
        CATEGORICAL_FEATURES, NUMERIC_FEATURES,
    )

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
    return Pipeline([
        ("pre", pre),
        ("reg", HistGradientBoostingRegressor(
            loss="quantile", quantile=quantile,
            max_depth=4, learning_rate=0.07, max_iter=200,
            random_state=random_state,
        )),
    ])


def train_mfe_mae_models(
    X: Any, mfe_targets: List[float], mae_targets: List[float],
    *,
    quantile: float = 0.75,
) -> Tuple[Any, Any]:
    """Fit + return (mfe_model, mae_model). The caller computes MFE/MAE
    from historical bar data and feeds them in."""
    if len(mfe_targets) != len(mae_targets):
        raise ValueError("MFE and MAE target arrays must have the same length")
    mfe_model = _make_quantile_regressor(quantile)
    mae_model = _make_quantile_regressor(quantile, random_state=7)
    mfe_model.fit(X, mfe_targets)
    mae_model.fit(X, mae_targets)
    return mfe_model, mae_model


def save_exit_models(mfe_model: Any, mae_model: Any,
                       *, version: str = "default") -> Dict[str, str]:
    import joblib
    os.makedirs(EXIT_MODEL_DIR, exist_ok=True)
    mfe_path = os.path.join(EXIT_MODEL_DIR, f"mfe-{version}.pkl")
    mae_path = os.path.join(EXIT_MODEL_DIR, f"mae-{version}.pkl")
    joblib.dump(mfe_model, mfe_path)
    joblib.dump(mae_model, mae_path)
    return {"mfe_path": mfe_path, "mae_path": mae_path,
             "version": version}


def load_exit_models(version: str = "default"
                       ) -> Tuple[Optional[Any], Optional[Any]]:
    import joblib
    mfe_path = os.path.join(EXIT_MODEL_DIR, f"mfe-{version}.pkl")
    mae_path = os.path.join(EXIT_MODEL_DIR, f"mae-{version}.pkl")
    if not (os.path.exists(mfe_path) and os.path.exists(mae_path)):
        return None, None
    try:
        return joblib.load(mfe_path), joblib.load(mae_path)
    except Exception:
        logger.debug("exit model load failed", exc_info=True)
        return None, None


# ── inference ────────────────────────────────────────────────────────────


def suggest_tp_sl(
    *,
    features_row: Dict[str, Any],
    fallback_tp_pct: float = 0.10,
    fallback_sl_pct: float = 0.05,
    version: str = "default",
) -> MfeMaeSuggestion:
    """Return adaptive TP/SL using the quantile regressors when available,
    falling back to the static defaults otherwise."""
    mfe_model, mae_model = load_exit_models(version=version)
    if mfe_model is None or mae_model is None:
        return MfeMaeSuggestion(
            take_profit_pct=fallback_tp_pct, stop_loss_pct=fallback_sl_pct,
            source="static",
            notes=["no exit models trained yet — using static bands"],
        )
    try:
        import pandas as pd
        from backend.bot.ml.feature_store import (
            CATEGORICAL_FEATURES, NUMERIC_FEATURES,
        )
        row: Dict[str, Any] = {}
        for col in NUMERIC_FEATURES:
            val = features_row.get(col)
            try:
                row[col] = float(val) if val is not None else None
            except (TypeError, ValueError):
                row[col] = None
        for col in CATEGORICAL_FEATURES:
            row[col] = str(features_row.get(col) or "unknown")
        df = pd.DataFrame([row])
        expected_mfe = float(mfe_model.predict(df)[0])
        expected_mae = float(mae_model.predict(df)[0])
    except Exception:
        logger.debug("exit model inference failed", exc_info=True)
        return MfeMaeSuggestion(
            take_profit_pct=fallback_tp_pct, stop_loss_pct=fallback_sl_pct,
            source="static", notes=["inference error — fell back to static"],
        )

    # Use quantile predictions as the TP/SL bands directly. Clip to sensible
    # ranges so a degenerate prediction can't blow risk.
    tp = max(0.005, min(0.50, abs(expected_mfe)))
    sl = max(0.005, min(0.20, abs(expected_mae)))
    return MfeMaeSuggestion(
        take_profit_pct=round(tp, 4), stop_loss_pct=round(sl, 4),
        expected_mfe_pct=round(expected_mfe, 4),
        expected_mae_pct=round(expected_mae, 4),
        source="model",
        notes=[f"model-suggested: TP={tp:.3f}, SL={sl:.3f}"],
    )
