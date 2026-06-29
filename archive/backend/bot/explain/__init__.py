"""Stage-11.7 Feature Importance + per-trade attribution.

Two surfaces:

  • ``compute_importance()`` — for the active model, builds the labelled
    feature matrix from ``DecisionLog`` and runs sklearn's
    ``permutation_importance`` on a held-out fraction. Caches in-process
    keyed by model version + dataset size (so it doesn't re-run on every
    API call). Returns ``ImportanceReport`` with mean / std per feature,
    sorted descending.

  • ``explain_trade_features(trade_id)`` — for a single trade, joins the
    persisted feature snapshot (Trade.detail_json → snapshot/features) with
    the global importance vector to return the top-K features that mattered
    in the model's prediction *and* this trade's value for each, plus a
    "high / mid / low" qualitative tag vs the corpus median.

Heuristic, transparent, no SHAP / LightGBM-only paths. Falls back to a
degraded "uniform importance" stub when there isn't enough labelled data
yet — so the UI always renders something rather than crashing on Stage-5
cold-start systems.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.bot.ml.feature_store import (
    CATEGORICAL_FEATURES,
    NUMERIC_FEATURES,
    build_dataset,
)

logger = logging.getLogger(__name__)


# ── data types ───────────────────────────────────────────────────────────


@dataclass
class FeatureImportance:
    feature: str
    importance: float
    std: float = 0.0
    kind: str = "numeric"          # numeric | categorical

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ImportanceReport:
    model_version: Optional[str]
    model_type: Optional[str]
    importances: List[FeatureImportance]
    sample_size: int
    method: str                    # permutation | uniform_fallback | not_trained
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_version": self.model_version,
            "model_type": self.model_type,
            "importances": [i.to_dict() for i in self.importances],
            "sample_size": self.sample_size,
            "method": self.method,
            "warnings": self.warnings,
        }


@dataclass
class FeatureAttribution:
    feature: str
    importance: float
    value: Any
    kind: str
    quality: str                   # high | mid | low | n/a

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── importance: cached compute on labelled cohort ────────────────────────


_CACHE: Dict[str, ImportanceReport] = {}


def _uniform_report(reason: str) -> ImportanceReport:
    feats = NUMERIC_FEATURES + CATEGORICAL_FEATURES
    weight = round(1.0 / len(feats), 4)
    return ImportanceReport(
        model_version=None, model_type=None, sample_size=0,
        method="uniform_fallback",
        importances=[FeatureImportance(
            feature=f, importance=weight, std=0.0,
            kind=("categorical" if f in CATEGORICAL_FEATURES else "numeric"),
        ) for f in feats],
        warnings=[reason],
    )


def _cache_key(version: Optional[str], sample: int) -> str:
    return f"{version or 'none'}:{sample}"


def compute_importance(*,
                          n_repeats: int = 5,
                          min_labelled: int = 40,
                          test_size: float = 0.3,
                          random_state: int = 17,
                          force: bool = False) -> ImportanceReport:
    """Run permutation importance on the active model's held-out cohort.

    Cached in-process per (model_version × labelled-row-count) so the
    second request is instant. Pass ``force=True`` to recompute.
    """
    from backend.bot.ml.registry import active_model

    active = active_model()
    if active is None:
        return _uniform_report("no active model — using uniform importances")

    X, y, ds_meta = build_dataset(min_closed=min_labelled)
    if X is None or y is None:
        warn = (ds_meta.get("warnings") or ["dataset too small"])[0]
        rpt = _uniform_report(warn)
        rpt.model_version = active.get("version")
        return rpt

    key = _cache_key(active.get("version"), len(y))
    if not force and key in _CACHE:
        return _CACHE[key]

    try:
        import numpy as np
        from sklearn.inspection import permutation_importance
        from sklearn.model_selection import train_test_split
    except Exception:
        return _uniform_report("sklearn.inspection unavailable")

    try:
        X_tr, X_te, y_tr, y_te = train_test_split(
            X, y, test_size=test_size, random_state=random_state,
            stratify=y if len(set(y)) > 1 else None,
        )
        # Use the existing fitted pipeline as-is (already preprocesses).
        model = active["model"]
        result = permutation_importance(
            model, X_te, y_te, n_repeats=n_repeats,
            random_state=random_state, scoring="roc_auc",
        )
        means = result.importances_mean
        stds = result.importances_std
    except Exception as exc:
        rpt = _uniform_report(f"permutation_importance failed: {exc}")
        rpt.model_version = active.get("version")
        return rpt

    feats = list(X.columns)
    importances: List[FeatureImportance] = []
    for f, m, s in zip(feats, means, stds):
        importances.append(FeatureImportance(
            feature=f, importance=round(float(m), 5),
            std=round(float(s), 5),
            kind=("categorical" if f in CATEGORICAL_FEATURES else "numeric"),
        ))
    importances.sort(key=lambda x: x.importance, reverse=True)
    rpt = ImportanceReport(
        model_version=active.get("version"),
        model_type=(active.get("meta") or {}).get("model_type"),
        importances=importances,
        sample_size=len(y),
        method="permutation",
    )
    _CACHE[key] = rpt
    return rpt


def top_features(k: int = 10) -> List[Dict[str, Any]]:
    rpt = compute_importance()
    return [fi.to_dict() for fi in rpt.importances[:k]]


# ── per-trade attribution ────────────────────────────────────────────────


def _value_for(feature: str, snapshot: Dict[str, Any],
                 features: Dict[str, Any], analytics: Dict[str, Any]) -> Any:
    """Best-effort lookup: per-trade features can land in snapshot,
    analytics.features, or the dedicated features dict."""
    if feature in (features or {}):
        return features[feature]
    if feature in (snapshot or {}):
        return snapshot[feature]
    if feature == "grade":
        return (analytics.get("rank") or {}).get("grade")
    if feature == "win_probability":
        return (analytics.get("probability") or {}).get("probability")
    if feature == "regime_trend":
        return (analytics.get("regime") or {}).get("trend")
    if feature == "regime_volatility":
        return (analytics.get("regime") or {}).get("volatility")
    if feature == "regime_gamma":
        return (analytics.get("regime") or {}).get("gamma")
    if feature in ("hedging_pressure", "dominant_wall"):
        return (analytics.get("features") or {}).get(feature)
    return None


def _quality_tag(feature: str, value: Any,
                   numeric_ranges: Optional[Dict[str, Tuple[float, float, float]]] = None
                   ) -> str:
    """Classify a feature value as high/mid/low. Heuristic per feature."""
    if value is None or value == "unknown":
        return "n/a"
    # Categorical features map directly.
    if feature in CATEGORICAL_FEATURES:
        if feature == "grade":
            return "high" if str(value).startswith("A") else (
                "low" if str(value) in ("D", "F") else "mid")
        if feature == "regime_trend":
            return "high" if str(value) == "bullish" else (
                "low" if str(value) == "bearish" else "mid")
        return "mid"
    # Numeric: tag by simple band.
    try:
        v = float(value)
    except Exception:
        return "n/a"
    if feature == "rsi_14":
        return "high" if v > 70 else "low" if v < 30 else "mid"
    if feature == "iv_rank":
        return "high" if v > 70 else "low" if v < 30 else "mid"
    if feature == "win_probability":
        return "high" if v > 0.65 else "low" if v < 0.45 else "mid"
    if feature == "confidence":
        return "high" if v > 0.7 else "low" if v < 0.5 else "mid"
    if feature in ("composite_bias", "darkpool_bias"):
        return "high" if v > 0.3 else "low" if v < -0.3 else "mid"
    if feature == "pinning_probability":
        return "high" if v > 0.6 else "low" if v < 0.2 else "mid"
    if feature == "gex_total":
        return "high" if abs(v) > 1.0 else "mid"
    if feature == "atr":
        return "high" if v > 5 else "low" if v < 1 else "mid"
    return "mid"


def explain_trade_features(trade_id: int, top_k: int = 5) -> Optional[Dict[str, Any]]:
    """Per-trade feature attribution.

    Joins the trade's persisted feature snapshot with global importance and
    returns the top-K features sorted by importance. Returns ``None`` when
    the trade does not exist.
    """
    import json as _json

    from backend.db import session_scope
    from backend.models.trade import Trade

    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            return None
        try:
            detail = _json.loads(trade.detail_json or "{}") or {}
        except Exception:
            detail = {}
        ticker = trade.ticker
        action = trade.action

    snapshot = detail.get("snapshot") or {}
    analytics = detail.get("analytics") or {}
    features = (analytics.get("features") or {})

    rpt = compute_importance()
    attributions: List[FeatureAttribution] = []
    for fi in rpt.importances[:max(top_k, 5) * 2]:
        v = _value_for(fi.feature, snapshot, features, analytics)
        attributions.append(FeatureAttribution(
            feature=fi.feature, importance=fi.importance, value=v,
            kind=fi.kind, quality=_quality_tag(fi.feature, v),
        ))
        if len(attributions) >= top_k:
            break

    return {
        "trade_id": trade_id, "ticker": ticker, "action": action,
        "model_version": rpt.model_version,
        "model_type": rpt.model_type,
        "method": rpt.method,
        "attributions": [a.to_dict() for a in attributions],
    }


def reset_cache() -> None:
    """Test helper — clear the in-process importance cache."""
    _CACHE.clear()


# ── Stage-15 per-regime feature importance ──────────────────────────────


# Cache of regime-bucketed reports keyed by (model_version, sample_size,
# regime_trend) so each split survives across requests without recomputing.
_REGIME_CACHE: Dict[str, ImportanceReport] = {}


def reset_regime_cache() -> None:
    """Test helper — clear the per-regime importance cache."""
    _REGIME_CACHE.clear()


def compute_importance_by_regime(*,
                                      min_per_regime: int = 30,
                                      n_repeats: int = 5,
                                      test_size: float = 0.3,
                                      random_state: int = 17,
                                      force: bool = False,
                                      ) -> Dict[str, ImportanceReport]:
    """Compute permutation importance **separately per regime trend**.

    Answers "in bull tape, which features matter? in chop, which?". Returns
    a dict ``{regime_trend: ImportanceReport}``. Regimes with fewer than
    ``min_per_regime`` labelled rows get a ``method="uniform_fallback"``
    report so the UI still has something to render.

    Heuristic split — uses ``DecisionLog.regime_trend`` as the bucket
    key. When the active model can't be applied to a bucket (cross-tab too
    sparse), that bucket also falls through to uniform.
    """
    from backend.bot.ml.feature_store import build_dataset
    from backend.bot.ml.registry import active_model

    active = active_model()

    # We need the labelled corpus split by regime. Reuse build_dataset to
    # get the full matrix, then re-split using the DecisionLog rows by
    # regime_trend (carried in the dataframe via the 'regime_trend'
    # categorical column).
    X, y, ds_meta = build_dataset(min_closed=min_per_regime)
    if X is None or y is None or active is None:
        # No model or no data — return uniform per known regime.
        bands = ("bullish", "bearish", "choppy", "unknown")
        return {
            r: _uniform_report(
                "no model or insufficient labelled data per regime"
            )
            for r in bands
        }

    out: Dict[str, ImportanceReport] = {}
    try:
        import numpy as np
        from sklearn.inspection import permutation_importance
        from sklearn.model_selection import train_test_split
    except Exception:
        return {r: _uniform_report("sklearn.inspection unavailable")
                  for r in ("bullish", "bearish", "choppy", "unknown")}

    try:
        regimes = X["regime_trend"].unique().tolist() if "regime_trend" in X.columns \
            else ["unknown"]
    except Exception:
        regimes = ["unknown"]

    model = active["model"]
    model_version = active.get("version")
    model_type = (active.get("meta") or {}).get("model_type")

    for regime in regimes:
        try:
            mask = (X["regime_trend"] == regime).values if "regime_trend" in X.columns \
                else np.ones(len(y), dtype=bool)
            X_r = X[mask]
            y_r = [yy for yy, m in zip(y, mask) if m]
        except Exception:
            X_r, y_r = None, []
        if X_r is None or len(y_r) < min_per_regime:
            rpt = _uniform_report(
                f"only {len(y_r)} labelled rows in '{regime}' regime; "
                f"need ≥{min_per_regime}"
            )
            rpt.model_version = model_version
            out[regime] = rpt
            continue

        cache_key = f"{model_version}:{regime}:{len(y_r)}"
        if not force and cache_key in _REGIME_CACHE:
            out[regime] = _REGIME_CACHE[cache_key]
            continue

        try:
            stratify = y_r if len(set(y_r)) > 1 else None
            X_tr, X_te, y_tr, y_te = train_test_split(
                X_r, y_r, test_size=test_size,
                random_state=random_state, stratify=stratify,
            )
            result = permutation_importance(
                model, X_te, y_te, n_repeats=n_repeats,
                random_state=random_state, scoring="roc_auc",
            )
            means = result.importances_mean
            stds = result.importances_std
        except Exception as exc:
            rpt = _uniform_report(
                f"permutation_importance failed for '{regime}': {exc}"
            )
            rpt.model_version = model_version
            out[regime] = rpt
            continue

        feats = list(X_r.columns)
        importances = [
            FeatureImportance(
                feature=f, importance=round(float(m), 5),
                std=round(float(s), 5),
                kind=("categorical" if f in CATEGORICAL_FEATURES else "numeric"),
            )
            for f, m, s in zip(feats, means, stds)
        ]
        importances.sort(key=lambda x: x.importance, reverse=True)
        rpt = ImportanceReport(
            model_version=model_version, model_type=model_type,
            importances=importances, sample_size=len(y_r),
            method="permutation",
        )
        _REGIME_CACHE[cache_key] = rpt
        out[regime] = rpt

    return out
