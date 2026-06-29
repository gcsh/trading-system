"""Claude-backed deep composer with strict pydantic validation.

Replaces the hand-parsed `text.find('{')` flow in analysis.py and
eod_analysis.py. Returns ``None`` on any structural validation
failure so the caller can transparently fall back to the
fast-composer output.

Self-critique pass fires when the model's first
``confidence_self_assessment`` clears ``deep_composer_self_critique_threshold``.
The second call grills the suggested action against the CI width and
regime and can revise (or kill) it.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import pydantic
from pydantic import BaseModel, Field, ValidationError

from backend.config import TUNABLES

from backend.bot.analysis._actions import (
    SUGGESTED_ACTION_MIN_POSTERIOR,
    SUGGESTED_ACTION_MIN_SAMPLES,
    resolve_suggested_strike,
)


logger = logging.getLogger(__name__)


class SuggestedActionSchema(BaseModel):
    action: str
    expiry: Optional[str] = None
    strike: Optional[float] = None
    rationale: Optional[str] = None

    model_config = pydantic.ConfigDict(extra="allow")


class DeepThesisSchema(BaseModel):
    headline: str = Field(min_length=10, max_length=240)
    thesis_paragraph: str = Field(min_length=20, max_length=1600)
    suggested_action: Optional[SuggestedActionSchema] = None
    invalidation: List[str] = Field(min_length=1, max_length=6)
    confidence_self_assessment: float = Field(ge=0.0, le=1.0)


class DeepComposerOutput(BaseModel):
    summary: str = Field(max_length=1200)
    theses: Dict[str, DeepThesisSchema]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "theses": {
                k: v.model_dump() for k, v in self.theses.items()
            },
        }


def _format_pattern_block(pattern: str, k: Dict[str, Any]) -> str:
    n = int(k.get("sample_size") or 0)
    post = k.get("posterior_win_rate")
    wr = k.get("win_rate")
    avg_ret = k.get("avg_return_pct")
    avg_hold = k.get("avg_hold_minutes")
    lo = k.get("confidence_lower")
    hi = k.get("confidence_upper")
    ci_width = k.get("ci_width")
    if ci_width is None and lo is not None and hi is not None:
        try:
            ci_width = float(hi) - float(lo)
        except Exception:
            ci_width = None
    parts = [f"pattern={pattern}", f"N={n}"]
    if post is not None:
        parts.append(f"posterior={post*100:.0f}%")
    if wr is not None:
        parts.append(f"frequentist_wr={wr*100:.0f}%")
    if avg_ret is not None:
        parts.append(f"avg_move={avg_ret*100:+.1f}%")
    if avg_hold is not None:
        try:
            parts.append(f"avg_hold_min={float(avg_hold):.0f}")
        except Exception:
            pass
    if lo is not None and hi is not None:
        try:
            parts.append(f"CI=[{float(lo)*100:.0f}%, {float(hi)*100:.0f}%]")
        except Exception:
            pass
    if ci_width is not None:
        parts.append(f"ci_width={float(ci_width):.3f}")
    return ", ".join(parts)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first balanced JSON object from a Claude response.

    The model is instructed to return ONLY JSON, but defence in depth:
    we look for the outermost `{...}` block and json-loads it. None on
    failure so the caller falls back cleanly.
    """
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def _claude_client():
    from backend.config import anthropic_key
    if not anthropic_key():
        return None
    try:
        from anthropic import Anthropic
    except Exception:
        return None
    return Anthropic(api_key=anthropic_key(), timeout=30.0)


def _record_cost(response) -> None:
    try:
        from backend.bot.ai_cost import record_from_response
        record_from_response(
            surface="analysis", model=TUNABLES.ai_brain_model,
            response=response,
        )
    except Exception:
        pass


def _ground_suggested_action(
    sa: Optional[SuggestedActionSchema], *,
    ticker: str, spot: Optional[float], pattern: str,
    knowledge: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Replace Claude's strike with a chain-listed one and re-apply the
    posterior/N gate. Returns the cleaned action dict or None when the
    cohort fails the gate."""
    if sa is None:
        return None
    post = float(knowledge.get("posterior_win_rate") or 0.0)
    n = int(knowledge.get("sample_size") or 0)
    if post < SUGGESTED_ACTION_MIN_POSTERIOR or n < SUGGESTED_ACTION_MIN_SAMPLES:
        return None
    action = (sa.action or "").upper()
    direction = ("long_call" if action == "BUY_CALL"
                  else "long_put" if action == "BUY_PUT"
                  else None)
    if direction is None:
        return None
    dte_target = 30
    if sa.expiry:
        try:
            dte_target = int(sa.expiry)
        except Exception:
            dte_target = 30
    strike = sa.strike
    strike_source = "claude"
    if spot:
        listed, src = resolve_suggested_strike(
            ticker, float(spot), direction, dte_target,
        )
        if listed is not None:
            strike = listed
        strike_source = src
    return {
        "action": action,
        "direction": direction,
        "strike": strike,
        "strike_source": strike_source,
        "dte": dte_target,
        "dte_target": dte_target,
        "target_premium_pct": 50,
        "stop_premium_pct": 30,
        "rationale": sa.rationale or "",
    }


def _build_prompt(
    *,
    ticker: str, window: str, knowledge: Dict[str, Dict[str, Any]],
    observations: List[Dict[str, Any]], spot: Optional[float],
    regime_vector_summary: Optional[str] = None,
    strategy_matrix_summary: Optional[str] = None,
) -> Dict[str, str]:
    pattern_blocks = [
        f"- {_format_pattern_block(p, k)}" for p, k in knowledge.items()
    ]
    patterns_text = "\n".join(pattern_blocks)

    obs_summary = []
    for o in observations[:20]:
        obs_summary.append(
            f"- {o.get('pattern')} fired at {o.get('timestamp')} "
            f"(regime={o.get('regime')}, vol={o.get('vol_state')})"
        )
    obs_text = "\n".join(obs_summary) if obs_summary else "(none)"

    regime_block = (
        f"\nRegime context:\n{regime_vector_summary}\n"
        if regime_vector_summary else ""
    )

    system_prompt = (
        "You are a markets analyst writing concrete trade theses for a "
        "PAPER trading operator. Reason OVER the historical cohort "
        "statistics provided; do not invent numbers the data doesn't "
        "support. Be specific, name the cohort stats, and write in plain "
        "English. The operator is a beginner — use accessible language. "
        "Return ONLY a JSON object — no prose before or after."
    )

    schema_block = (
        '{"summary": "<2-sentence overall summary of the ticker today>",\n'
        ' "theses": {\n'
        '   "<pattern_name>": {\n'
        '     "headline": "<one-line: ticker + pattern + win rate + N (10+ chars)>",\n'
        '     "thesis_paragraph": "<2-3 sentences explaining the setup '
        "(20+ chars)>\",\n"
        '     "suggested_action": null OR '
        '{"action": "BUY_CALL"|"BUY_PUT", "strike": <float>, '
        '"expiry": "<DTE int as string>", "rationale": "<one line>"},\n'
        '     "invalidation": ["<bullet 1>", "<bullet 2>", ...],\n'
        '     "confidence_self_assessment": <float 0.0-1.0>\n'
        '   }\n'
        ' }\n'
        '}'
    )

    strategy_block = (
        f"Strategy candidates:\n{strategy_matrix_summary}\n\n"
        if strategy_matrix_summary else ""
    )

    user_prompt = (
        f"{strategy_block}"
        f"Ticker: {ticker}\n"
        f"Window: {window}\n"
        f"Current spot: {spot}\n"
        f"{regime_block}\n"
        f"Patterns that fired today (with cohort statistics including "
        f"Wilson confidence interval width):\n{patterns_text}\n\n"
        f"Recent detector hits in the window:\n{obs_text}\n\n"
        f"For EACH pattern, write a thesis. Only include suggested_action "
        f"when posterior > {SUGGESTED_ACTION_MIN_POSTERIOR*100:.0f}% AND "
        f"sample_size >= {SUGGESTED_ACTION_MIN_SAMPLES}; otherwise set it "
        f"to null. confidence_self_assessment should reflect how strong "
        f"this read is given posterior, sample size, AND CI width.\n\n"
        f"Return ONLY this JSON shape:\n{schema_block}"
    )
    return {"system": system_prompt, "user": user_prompt}


def _call_claude(client, *, system: str, user: str) -> Optional[str]:
    try:
        response = client.messages.create(
            model=TUNABLES.ai_brain_model,
            max_tokens=TUNABLES.ai_brain_max_tokens,
            system=[{"type": "text", "text": system,
                       "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
    except Exception:
        logger.debug("deep composer Claude call failed", exc_info=True)
        return None
    _record_cost(response)
    try:
        return "".join(
            b.text for b in response.content
            if getattr(b, "type", None) == "text"
        )
    except Exception:
        return None


def _self_critique(
    *, client, ticker: str, pattern: str, thesis: DeepThesisSchema,
    knowledge: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Second Claude call that grills the thesis against CI width +
    regime. Returns the parsed JSON `{keep, revised_thesis?, revised_action?}`
    or None if the call/parse failed."""
    n = int(knowledge.get("sample_size") or 0)
    post = float(knowledge.get("posterior_win_rate") or 0.0)
    ci_width = knowledge.get("ci_width")
    regime = knowledge.get("regime") or "unknown"
    system = (
        "You are a markets risk reviewer. You are handed ONE thesis "
        "and the cohort it leans on. Your job is to decide whether the "
        "suggested_action is defensible given the CI width and regime. "
        "Return ONLY a JSON object."
    )
    user = (
        f"Ticker: {ticker}\nPattern: {pattern}\n"
        f"Posterior: {post*100:.1f}%, N={n}, CI width: {ci_width}, "
        f"regime: {regime}.\n\n"
        f"Original headline: {thesis.headline}\n"
        f"Original thesis: {thesis.thesis_paragraph}\n"
        f"Original suggested_action: "
        f"{thesis.suggested_action.model_dump() if thesis.suggested_action else None}\n"
        f"Original self-assessed confidence: {thesis.confidence_self_assessment}\n\n"
        f"Is the suggested action defensible given the CI width and "
        f"regime? Return JSON:\n"
        '{"keep": true|false, '
        '"revised_thesis": "<optional rewritten paragraph>", '
        '"revised_action": null OR {"action":"BUY_CALL"|"BUY_PUT", '
        '"strike": <float>, "expiry": "<DTE>", "rationale": "<line>"}}'
    )
    text = _call_claude(client, system=system, user=user)
    if text is None:
        return None
    return _extract_json(text)


def deep_compose(
    *,
    ticker: str,
    window: str,
    knowledge: Dict[str, Dict[str, Any]],
    observations: List[Dict[str, Any]],
    bars: List[Dict[str, Any]],
    top_n: Optional[int] = None,
    self_critique: bool = False,
    regime_vector_summary: Optional[str] = None,
    strategy_matrix_summary: Optional[str] = None,
) -> Optional[DeepComposerOutput]:
    """Make Claude calls to compose theses for the given knowledge slice.

    Returns ``None`` when (a) knowledge is empty, (b) no API key is
    configured, (c) Claude returns malformed JSON, or (d) pydantic
    rejects the structure. Callers fall back to the fast-composer
    output transparently.
    """
    if not knowledge:
        return None
    client = _claude_client()
    if client is None:
        return None

    if top_n is not None:
        knowledge = dict(list(knowledge.items())[: int(top_n)])

    spot: Optional[float] = None
    if bars:
        try:
            spot = float(bars[-1].get("close"))
        except Exception:
            spot = None

    prompts = _build_prompt(
        ticker=ticker, window=window, knowledge=knowledge,
        observations=observations, spot=spot,
        regime_vector_summary=regime_vector_summary,
        strategy_matrix_summary=strategy_matrix_summary,
    )
    text = _call_claude(client, system=prompts["system"], user=prompts["user"])
    if text is None:
        return None
    parsed = _extract_json(text)
    if parsed is None:
        return None
    theses_in = parsed.get("theses") or {}
    cleaned_theses: Dict[str, DeepThesisSchema] = {}
    for pat in knowledge.keys():
        item = theses_in.get(pat)
        if not isinstance(item, dict):
            continue
        try:
            t = DeepThesisSchema.model_validate(item)
        except ValidationError:
            logger.debug("deep composer pydantic reject %s", pat)
            continue
        if self_critique and (
            t.confidence_self_assessment
            >= float(TUNABLES.deep_composer_self_critique_threshold)
        ):
            critique = _self_critique(
                client=client, ticker=ticker, pattern=pat, thesis=t,
                knowledge=knowledge[pat],
            )
            if critique and not critique.get("keep", True):
                revised_para = critique.get("revised_thesis")
                revised_action = critique.get("revised_action")
                update: Dict[str, Any] = {}
                if isinstance(revised_para, str) and len(revised_para) >= 20:
                    update["thesis_paragraph"] = revised_para[:1600]
                if revised_action is None:
                    update["suggested_action"] = None
                elif isinstance(revised_action, dict):
                    try:
                        update["suggested_action"] = (
                            SuggestedActionSchema.model_validate(
                                revised_action,
                            )
                        )
                    except ValidationError:
                        pass
                if update:
                    t = t.model_copy(update=update)
        cleaned_theses[pat] = t

    summary = parsed.get("summary") or ""
    if not isinstance(summary, str):
        summary = str(summary)
    try:
        output = DeepComposerOutput(
            summary=summary[:1200],
            theses=cleaned_theses,
        )
    except ValidationError:
        return None
    return output


def deep_compose_to_legacy_dict(
    *,
    output: DeepComposerOutput,
    knowledge: Dict[str, Dict[str, Any]],
    ticker: str,
    spot: Optional[float],
) -> Dict[str, Any]:
    """Project a DeepComposerOutput onto the legacy dict shape used by
    the analysis route and EOD analysis (so existing UI doesn't break)."""
    fallback_invalidation = [
        "Position closes the day below the breakdown level",
        "Volume dries up below the 20-bar median",
        "Regime flips counter to the cohort regime",
    ]
    out_theses: Dict[str, Any] = {}
    for pat, t in output.theses.items():
        sa = _ground_suggested_action(
            t.suggested_action, ticker=ticker, spot=spot,
            pattern=pat, knowledge=knowledge.get(pat, {}),
        )
        out_theses[pat] = {
            "headline": t.headline,
            "thesis_paragraph": t.thesis_paragraph,
            "suggested_action": sa,
            "invalidation": (list(t.invalidation)
                                if t.invalidation else fallback_invalidation)[:6],
            "confidence_self_assessment": t.confidence_self_assessment,
        }
    return {"theses": out_theses, "summary": output.summary}


__all__ = [
    "DeepComposerOutput",
    "DeepThesisSchema",
    "SuggestedActionSchema",
    "deep_compose",
    "deep_compose_to_legacy_dict",
]
