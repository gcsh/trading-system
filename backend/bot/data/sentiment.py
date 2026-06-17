"""MITS Phase 11.C — finance-specialized sentiment scoring.

Two-tier scorer:

  1. FinBERT (``ProsusAI/finbert`` from HuggingFace) — primary. Trained
     on finance news. ~440MB, CPU-friendly, lazy-loaded on first call so
     the bot doesn't pay the load cost unless news is actually fetched.
  2. VADER (``nltk.sentiment.vader``) — fallback. Lexicon-based, pure
     Python, no model download. Used when ``transformers`` is missing
     or model load fails (e.g. EC2 boot before HuggingFace cache is
     populated).

Both produce the same shape:

    {"label": "positive" | "neutral" | "negative",
     "score": float in [0, 1],
     "model": "finbert" | "vader"}

The orchestrator-friendly API is :func:`score_text`. It's safe to call
under load — the model is cached as a module-level singleton with a
lock so we never load it twice.

Truncation rule: FinBERT's BERT-base backbone has a 512-token context.
We hard-cap input at 512 characters (chars, not tokens — slightly
conservative) so a giant earnings-call summary gets the headline-style
treatment FinBERT was actually trained on.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


# ── public shape ──────────────────────────────────────────────────────


@dataclass
class SentimentResult:
    label: str             # "positive" | "neutral" | "negative"
    score: float           # confidence of ``label`` in [0, 1]
    model: str             # "finbert" | "vader" | "empty"

    def to_dict(self) -> dict:
        return {"label": self.label, "score": self.score, "model": self.model}


_FINBERT_LABELS = ("positive", "neutral", "negative")


# ── lazy model cache ──────────────────────────────────────────────────


_MODEL_LOCK = threading.Lock()
_FINBERT_PIPE = None
_FINBERT_LOAD_FAILED = False
_VADER = None


def _try_load_finbert():
    """Returns a transformers ``pipeline('sentiment-analysis', model=...)``
    object or ``None`` if loading fails. Lazy + singleton + lock-guarded
    so repeated calls don't reload the model and concurrent callers
    don't race on the load.
    """
    global _FINBERT_PIPE, _FINBERT_LOAD_FAILED
    if _FINBERT_LOAD_FAILED:
        return None
    if _FINBERT_PIPE is not None:
        return _FINBERT_PIPE
    with _MODEL_LOCK:
        if _FINBERT_PIPE is not None:
            return _FINBERT_PIPE
        if _FINBERT_LOAD_FAILED:
            return None
        try:
            from transformers import (  # type: ignore
                AutoModelForSequenceClassification, AutoTokenizer, pipeline,
            )
            model_name = getattr(
                TUNABLES, "sentiment_finbert_model", "ProsusAI/finbert")
            # Force CPU — EC2 t4g.small has no GPU, and the dual-load
            # path on a CPU box waste 2x memory.
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            model = AutoModelForSequenceClassification.from_pretrained(
                model_name)
            _FINBERT_PIPE = pipeline(
                "sentiment-analysis",
                model=model, tokenizer=tokenizer,
                device=-1,  # CPU
                truncation=True,
                max_length=512,
            )
            logger.info("sentiment: FinBERT loaded model=%s", model_name)
            return _FINBERT_PIPE
        except Exception as exc:
            _FINBERT_LOAD_FAILED = True
            logger.warning(
                "sentiment: FinBERT load failed (%s) — falling back to VADER",
                exc,
            )
            return None


def _try_load_vader():
    """Returns an nltk VADER ``SentimentIntensityAnalyzer`` or ``None``."""
    global _VADER
    if _VADER is not None:
        return _VADER
    with _MODEL_LOCK:
        if _VADER is not None:
            return _VADER
        try:
            from nltk.sentiment.vader import (  # type: ignore
                SentimentIntensityAnalyzer,
            )
            try:
                _VADER = SentimentIntensityAnalyzer()
            except LookupError:
                # NLTK requires the ``vader_lexicon`` data file. Try a
                # silent download (idempotent — already-present packages
                # are no-ops). If it fails (offline EC2), we surface the
                # exception so callers fall through to "empty" sentiment.
                import nltk  # type: ignore
                nltk.download("vader_lexicon", quiet=True)
                _VADER = SentimentIntensityAnalyzer()
            logger.info("sentiment: VADER lexicon loaded")
            return _VADER
        except Exception as exc:
            logger.warning("sentiment: VADER load failed (%s)", exc)
            return None


# ── scoring ───────────────────────────────────────────────────────────


def _vader_to_finbert_label(compound: float) -> str:
    """Map VADER's compound score in [-1, +1] to a FinBERT-style label.
    Thresholds are configurable so the operator can dial sensitivity.
    """
    pos_thr = float(getattr(TUNABLES, "sentiment_vader_positive_threshold", 0.05))
    neg_thr = float(getattr(TUNABLES, "sentiment_vader_negative_threshold", -0.05))
    if compound >= pos_thr:
        return "positive"
    if compound <= neg_thr:
        return "negative"
    return "neutral"


def score_text(text: str) -> SentimentResult:
    """Classify ``text`` into positive / neutral / negative with a
    confidence score. Empty / whitespace-only input returns a NEUTRAL
    result with ``model="empty"`` so callers can distinguish "we tried
    but had nothing" from a real model output.

    The function NEVER raises — sentiment errors degrade to neutral so
    the news-ingest pipeline still lands the row.
    """
    if not text or not text.strip():
        return SentimentResult(label="neutral", score=0.0, model="empty")
    clipped = text.strip()[:512]

    # Path 1 — FinBERT.
    pipe = _try_load_finbert()
    if pipe is not None:
        try:
            out = pipe(clipped)
            if isinstance(out, list) and out:
                first = out[0]
                raw_label = str(first.get("label") or "neutral").lower()
                if raw_label not in _FINBERT_LABELS:
                    # Some FinBERT checkpoints return capitalized labels
                    # or "LABEL_0"/"LABEL_1"/"LABEL_2". Normalize via a
                    # small map.
                    raw_label = {
                        "label_0": "positive", "label_1": "negative",
                        "label_2": "neutral",
                    }.get(raw_label, "neutral")
                score = float(first.get("score") or 0.0)
                return SentimentResult(
                    label=raw_label, score=round(score, 4),
                    model="finbert",
                )
        except Exception as exc:
            logger.warning("sentiment: FinBERT inference failed (%s); "
                              "falling back to VADER", exc)

    # Path 2 — VADER.
    vader = _try_load_vader()
    if vader is not None:
        try:
            scores = vader.polarity_scores(clipped)
            compound = float(scores.get("compound") or 0.0)
            label = _vader_to_finbert_label(compound)
            # VADER compound is signed; convert to a [0, 1] confidence
            # by taking abs() (or 1 - magnitude when neutral).
            if label == "neutral":
                confidence = 1.0 - min(1.0, abs(compound) * 2.0)
            else:
                confidence = min(1.0, abs(compound))
            return SentimentResult(
                label=label, score=round(confidence, 4),
                model="vader",
            )
        except Exception as exc:
            logger.warning("sentiment: VADER inference failed (%s)", exc)

    # Path 3 — both backends unavailable.
    return SentimentResult(label="neutral", score=0.0, model="empty")


def score_headline_summary(headline: str,
                              summary: Optional[str]) -> SentimentResult:
    """Convenience wrapper: concatenate headline + summary (when both
    are present) before scoring. Empty summary degrades to headline-only
    scoring."""
    parts = [(headline or "").strip()]
    if summary and summary.strip() and summary.strip() != (headline or "").strip():
        parts.append(summary.strip())
    return score_text(". ".join(parts))


__all__ = [
    "SentimentResult",
    "score_text",
    "score_headline_summary",
]
