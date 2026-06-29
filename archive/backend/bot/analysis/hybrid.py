"""Hybrid composer — runs the deterministic fast path always and
optionally promotes the top-N patterns through the deep Claude path.

When both paths emit a thesis, the deep one wins. The ensemble also
carries an `uncertainty_signal` per pattern so the UI can show
"fast/deep disagree" or "wide CI" without re-deriving.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from backend.config import TUNABLES

from backend.bot.analysis import deep_composer as _deep_mod
from backend.bot.analysis.deep_composer import (
    DeepComposerOutput,
    deep_compose_to_legacy_dict,
)
from backend.bot.analysis.fast_composer import (
    FastComposerResult,
    fast_compose_all,
)

if TYPE_CHECKING:
    from backend.bot.analysis.strategy_matrix import StrategyMatrix


DISAGREEMENT_RANK_DELTA = 0.25


@dataclass
class EnsembleResult:
    fast: Dict[str, FastComposerResult]
    deep: Optional[DeepComposerOutput]
    chosen: Dict[str, Dict[str, Any]]
    summary: str
    uncertainty_signal: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fast": {k: v.to_dict() for k, v in self.fast.items()},
            "deep": (self.deep.to_dict() if self.deep is not None else None),
            "chosen": self.chosen,
            "summary": self.summary,
            "uncertainty_signal": self.uncertainty_signal,
        }


def _action_from_legacy(suggested_action: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(suggested_action, dict):
        return None
    raw = suggested_action.get("action")
    if raw is None:
        return None
    return str(raw).upper()


def _disagreement_for(
    pattern: str,
    fast: FastComposerResult,
    deep_thesis: Optional[Dict[str, Any]],
    deep_self_conf: Optional[float],
) -> Dict[str, Any]:
    """Compute the per-pattern fast/deep disagreement signal.

    direction_match = fast.action == deep.suggested_action.action.
    rank_delta = abs(fast.rank - deep.confidence_self_assessment).
    flagged when not direction_match OR rank_delta > DISAGREEMENT_RANK_DELTA.
    """
    if deep_thesis is None:
        return {
            "direction_match": None,
            "rank_delta": None,
            "flagged": False,
            "fast_action": fast.action,
            "deep_action": None,
            "fast_uncertainty": round(fast.uncertainty, 4),
            "deep_self_confidence": None,
        }
    deep_action = _action_from_legacy(deep_thesis.get("suggested_action"))
    if deep_action is None and fast.action == "SKIP":
        direction_match = True
    else:
        direction_match = (deep_action == fast.action)
    rank_delta = None
    if deep_self_conf is not None:
        try:
            rank_delta = abs(float(fast.rank) - float(deep_self_conf))
        except Exception:
            rank_delta = None
    flagged = (not direction_match) or (
        rank_delta is not None and rank_delta > DISAGREEMENT_RANK_DELTA
    )
    return {
        "direction_match": bool(direction_match),
        "rank_delta": (round(rank_delta, 4)
                          if rank_delta is not None else None),
        "flagged": bool(flagged),
        "fast_action": fast.action,
        "deep_action": deep_action,
        "fast_uncertainty": round(fast.uncertainty, 4),
        "deep_self_confidence": deep_self_conf,
    }


def _fast_summary(
    ticker: str, fast: Dict[str, FastComposerResult],
) -> str:
    if not fast:
        return f"{ticker} has no detector hits in the window."
    top = sorted(fast.values(), key=lambda r: r.rank, reverse=True)[:2]
    parts = []
    for r in top:
        parts.append(
            f"{r.pattern} rank {r.rank:.2f} ({r.action})"
        )
    body = "; ".join(parts)
    return f"{ticker} fired {len(fast)} pattern(s). Top: {body}."


def compose_hybrid(
    *,
    ticker: str,
    window: str,
    knowledge: Dict[str, Dict[str, Any]],
    observations: List[Dict[str, Any]],
    bars: List[Dict[str, Any]],
    features: Optional[Dict[str, Any]] = None,
    deep_top_n: int = 3,
    force_deep: bool = False,
    regime_vector_summary: Optional[str] = None,
    strategy_matrix: Optional["StrategyMatrix"] = None,
) -> EnsembleResult:
    """Run the fast composer always, then optionally the deep composer
    on the top-N patterns by fast.rank.

    When ``force_deep`` is True we always attempt the deep path (even
    when fast-only would be cheaper) — used by the EOD pass that
    only carries the single primary pattern.
    """
    spot: Optional[float] = None
    if bars:
        try:
            spot = float(bars[-1].get("close"))
        except Exception:
            spot = None
    fast_results = fast_compose_all(
        ticker=ticker, knowledge=knowledge, spot=spot, features=features,
    )

    if not knowledge:
        return EnsembleResult(
            fast=fast_results,
            deep=None,
            chosen={},
            summary=_fast_summary(ticker, fast_results),
            uncertainty_signal={},
        )

    sorted_patterns = sorted(
        fast_results.items(), key=lambda kv: kv[1].rank, reverse=True,
    )
    n = int(deep_top_n if deep_top_n is not None
              else TUNABLES.deep_composer_top_n)
    selected = [pat for pat, _ in sorted_patterns[:max(1, n)]]
    deep_knowledge = {p: knowledge[p] for p in selected if p in knowledge}

    strategy_matrix_summary: Optional[str] = None
    if strategy_matrix is not None and strategy_matrix.candidates:
        lines = []
        for c in strategy_matrix.candidates[:3]:
            cwr = (f"{c.cohort_win_rate:.2f}"
                      if c.cohort_win_rate is not None else "n/a")
            awr = (f"{c.analog_win_rate:.2f}"
                      if c.analog_win_rate is not None else "n/a")
            lines.append(
                f"{c.strategy_name}: fit={c.fit_score:.2f} "
                f"cohort_wr={cwr} analog_wr={awr}"
            )
        strategy_matrix_summary = "\n".join(lines)

    deep_output: Optional[DeepComposerOutput] = None
    if force_deep or deep_knowledge:
        deep_output = _deep_mod.deep_compose(
            ticker=ticker, window=window, knowledge=deep_knowledge,
            observations=observations, bars=bars,
            top_n=None,
            self_critique=True,
            regime_vector_summary=regime_vector_summary,
            strategy_matrix_summary=strategy_matrix_summary,
        )

    chosen: Dict[str, Dict[str, Any]] = {}
    deep_legacy: Optional[Dict[str, Any]] = None
    if deep_output is not None:
        deep_legacy = deep_compose_to_legacy_dict(
            output=deep_output,
            knowledge=deep_knowledge,
            ticker=ticker,
            spot=spot,
        )
    uncertainty_signal: Dict[str, Any] = {}
    for pat, fast_res in fast_results.items():
        deep_pat_thesis: Optional[Dict[str, Any]] = None
        deep_pat_self_conf: Optional[float] = None
        if deep_legacy is not None:
            deep_pat_thesis = (deep_legacy.get("theses") or {}).get(pat)
            if deep_pat_thesis is not None:
                deep_pat_self_conf = deep_pat_thesis.get(
                    "confidence_self_assessment"
                )
        if deep_pat_thesis is not None:
            chosen[pat] = {
                "source": "deep",
                **deep_pat_thesis,
                "rank": fast_res.rank,
                "uncertainty": fast_res.uncertainty,
            }
        else:
            chosen[pat] = {
                "source": "fast",
                **fast_res.to_dict(),
            }
        if (
            strategy_matrix is not None
            and strategy_matrix.top_strategy is not None
            and pat in (strategy_matrix.top_strategy.supporting_patterns or [])
        ):
            chosen[pat]["top_strategy"] = strategy_matrix.top_strategy.to_dict()
        uncertainty_signal[pat] = _disagreement_for(
            pat, fast_res, deep_pat_thesis, deep_pat_self_conf,
        )

    summary = (
        deep_legacy.get("summary")
        if (deep_legacy is not None and deep_legacy.get("summary"))
        else _fast_summary(ticker, fast_results)
    )
    return EnsembleResult(
        fast=fast_results,
        deep=deep_output,
        chosen=chosen,
        summary=summary,
        uncertainty_signal=uncertainty_signal,
    )


__all__ = [
    "EnsembleResult",
    "compose_hybrid",
    "DISAGREEMENT_RANK_DELTA",
]
