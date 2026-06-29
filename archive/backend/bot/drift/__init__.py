"""Stage-7 drift detection — features, predictions, regimes.

Three drift surfaces:
  • **Feature drift**    — has the distribution of a feature shifted from
    the training distribution? Population Stability Index (PSI) is the
    institutional standard; a value > 0.25 typically warrants a retrain.
  • **Prediction drift** — has the distribution of predicted probabilities
    moved? An ML model issuing 0.9 for every input is broken even if its
    Brier is fine on the small sample we have.
  • **Regime drift**     — proportion of decisions in each regime cohort
    shifting over time. Surfaces when the bot is over-exposed to one
    regime and the cross-asset state has just flipped.

Outputs are deterministic given the input series — no DB, no network — so
the same baseline + sample always produces the same PSI. Stored baselines
live alongside the registered model in `bot/ml/registry/`.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ── PSI / KL helpers ─────────────────────────────────────────────────────


def _hist(samples: Sequence[float], edges: Sequence[float]) -> List[float]:
    """Density of ``samples`` per bucket defined by ``edges`` (sorted).
    Returns normalized probabilities summing to ≤ 1.0. The last edge is
    treated as +∞ so values above the last edge land in the top bucket."""
    if not samples or len(edges) < 2:
        return []
    counts = [0] * (len(edges) - 1)
    for v in samples:
        if v is None:
            continue
        # locate bucket
        placed = False
        for i in range(len(edges) - 1):
            if edges[i] <= v < edges[i + 1]:
                counts[i] += 1
                placed = True
                break
        if not placed and v >= edges[-1]:
            counts[-1] += 1
    total = sum(counts) or 1
    return [c / total for c in counts]


def _quantile_edges(samples: Sequence[float], n_bins: int = 10) -> List[float]:
    """Equal-frequency bin edges using empirical quantiles; protects PSI
    from degenerate constant-feature blowups by always returning at least
    two distinct edges."""
    clean = sorted(v for v in samples if v is not None)
    if not clean:
        return [0.0, 1.0]
    if len(clean) < n_bins:
        n_bins = max(2, len(clean))
    step = (len(clean) - 1) / n_bins
    edges = [clean[int(round(i * step))] for i in range(n_bins + 1)]
    # ensure strictly increasing
    out = [edges[0]]
    for e in edges[1:]:
        if e <= out[-1]:
            e = out[-1] + 1e-9
        out.append(e)
    return out


def psi(baseline: Sequence[float], current: Sequence[float],
         n_bins: int = 10) -> Optional[float]:
    """Population Stability Index between two samples of the same feature.

    PSI = Σ (p_cur - p_base) × ln(p_cur / p_base)

    Interpretation (institutional rule of thumb):
      • < 0.10  — no significant change
      • 0.10–0.25 — moderate shift, watch
      • > 0.25  — significant; retrain or investigate
    """
    if not baseline or not current:
        return None
    edges = _quantile_edges(baseline, n_bins=n_bins)
    p_b = _hist(baseline, edges)
    p_c = _hist(current, edges)
    if not p_b or not p_c:
        return None
    eps = 1e-9
    score = 0.0
    for pb, pc in zip(p_b, p_c):
        pb_s = max(pb, eps)
        pc_s = max(pc, eps)
        score += (pc_s - pb_s) * math.log(pc_s / pb_s)
    return round(score, 4)


def _categorical_psi(baseline: Sequence[str],
                       current: Sequence[str]) -> Optional[float]:
    """PSI for a categorical feature — bucket = unique value."""
    if not baseline or not current:
        return None
    cats = sorted(set(baseline) | set(current))
    def _shares(seq):
        n = len(seq) or 1
        return [seq.count(c) / n for c in cats]
    p_b, p_c = _shares(baseline), _shares(current)
    eps = 1e-9
    return round(
        sum((max(pc, eps) - max(pb, eps)) * math.log(max(pc, eps) / max(pb, eps))
             for pb, pc in zip(p_b, p_c)),
        4,
    )


# ── drift report ─────────────────────────────────────────────────────────


@dataclass
class DriftSignal:
    name: str
    psi: Optional[float] = None
    severity: str = "ok"        # ok | watch | critical
    sample_size: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DriftReport:
    signals: List[DriftSignal] = field(default_factory=list)
    overall: str = "ok"
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"signals": [s.to_dict() for s in self.signals],
                 "overall": self.overall, "notes": self.notes}


def severity_for(score: Optional[float]) -> str:
    if score is None:
        return "ok"
    if score >= 0.25:
        return "critical"
    if score >= 0.10:
        return "watch"
    return "ok"


def assess_feature_drift(
    *,
    baseline_numeric: Dict[str, Sequence[float]],
    current_numeric: Dict[str, Sequence[float]],
    baseline_categorical: Optional[Dict[str, Sequence[str]]] = None,
    current_categorical: Optional[Dict[str, Sequence[str]]] = None,
) -> DriftReport:
    """Compare per-feature PSI between baseline + current samples."""
    signals: List[DriftSignal] = []
    for name, baseline in baseline_numeric.items():
        score = psi(baseline, current_numeric.get(name, []))
        signals.append(DriftSignal(name=name, psi=score,
                                       severity=severity_for(score),
                                       sample_size=len(current_numeric.get(name, []))))
    for name, baseline in (baseline_categorical or {}).items():
        score = _categorical_psi(baseline, (current_categorical or {}).get(name, []))
        signals.append(DriftSignal(name=name, psi=score,
                                       severity=severity_for(score),
                                       sample_size=len((current_categorical or {}).get(name, []))))

    if any(s.severity == "critical" for s in signals):
        overall = "critical"
    elif any(s.severity == "watch" for s in signals):
        overall = "watch"
    else:
        overall = "ok"
    return DriftReport(signals=signals, overall=overall)


def assess_prediction_drift(*, baseline_preds: Sequence[float],
                              current_preds: Sequence[float]) -> DriftSignal:
    """Single-axis PSI on predicted probabilities."""
    score = psi(baseline_preds, current_preds)
    return DriftSignal(name="predicted_probability",
                          psi=score, severity=severity_for(score),
                          sample_size=len(current_preds))
