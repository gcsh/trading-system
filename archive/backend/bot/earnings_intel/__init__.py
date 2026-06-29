"""Stage-19 — Earnings Call Intelligence.

Most retail bots read headlines. Institutions read management language.

This module extracts structured intelligence from earnings releases:

  • ``guidance_change``: improved | maintained | reduced | first_time | withdrawn | none
  • ``margin_trajectory``: expanding | stable | contracting | n/a
  • ``management_tone``: confident | cautious | mixed | neutral
  • ``key_quotes``: 3-5 verbatim lines that capture the story
  • ``forward_looking``: forward-looking statements management volunteered

Two extractors:

  • ``heuristic_extract(text)`` — keyword-banded, deterministic, no API.
    Picks up the headline guidance/margin/tone signals with reasonable
    precision when phrasing is conventional. Always available.

  • ``ClaudeExtractor.extract(text)`` — single batched Claude call that
    returns the full structured shape with quoted lines. Falls through
    silently to heuristic when no API key configured.

Press-release text comes from SEC EDGAR (8-K item 2.02 exhibit 99.1).
Fetcher in ``backend/bot/earnings_intel/fetcher.py`` resolves the
exhibit URL and downloads the text content.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import desc, select

from backend.config import TUNABLES, anthropic_key
from backend.db import session_scope
from backend.models.earnings_intel import EarningsCallIntel

logger = logging.getLogger(__name__)


# ── data types ──────────────────────────────────────────────────────────


@dataclass
class CallIntel:
    guidance_change: str = "none"
    margin_trajectory: str = "n/a"
    management_tone: str = "neutral"
    key_quotes: List[str] = field(default_factory=list)
    forward_looking: List[str] = field(default_factory=list)
    summary: str = ""
    source: str = "heuristic"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── heuristic extractor (always available) ──────────────────────────────


# Confident-language markers — phrases / words companies use when things are good.
_CONFIDENT_MARKERS = [
    "strong demand", "record revenue", "record quarter", "exceeded",
    "outperformed", "raised guidance", "raising guidance",
    "increase guidance", "raising our outlook", "accelerating",
    "robust", "exceptional", "above expectations", "beat expectations",
    "beat consensus", "ahead of expectations", "tailwind",
]
# Cautious-language markers — phrases that telegraph trouble.
_CAUTIOUS_MARKERS = [
    "headwind", "soft demand", "weak demand", "challenging",
    "challenging environment", "lower guidance", "reduced guidance",
    "cutting guidance", "withdrew guidance", "withdrawing guidance",
    "uncertainty", "cautious", "slowdown", "deceleration",
    "below expectations", "missed expectations", "missed consensus",
    "pressure on margins", "margin compression", "softening",
    "weakness", "below consensus",
]
# Guidance phrasing
_GUIDANCE_RAISE = [
    "raising guidance", "raised guidance", "raising our outlook",
    "increase guidance", "increasing guidance", "raising full-year",
    "raise our full year", "above the prior range",
]
_GUIDANCE_REDUCE = [
    "lowering guidance", "lowered guidance", "reducing guidance",
    "cutting guidance", "reduce our outlook", "below the prior range",
    "below previous guidance", "narrowing to the low end",
]
_GUIDANCE_MAINTAIN = [
    "reaffirm guidance", "reaffirming guidance", "maintain our outlook",
    "maintaining guidance", "in line with prior guidance",
]
_GUIDANCE_WITHDRAW = [
    "withdrew guidance", "withdrawing guidance", "suspended guidance",
    "no longer providing guidance",
]
_GUIDANCE_FIRST = [
    "first time providing guidance", "initiating guidance",
    "providing guidance for", "introducing guidance",
]
# Margin direction
_MARGIN_EXPAND = [
    "margin expansion", "expanding margins", "gross margin expanded",
    "operating margin expanded", "margin improvement", "improved margins",
]
_MARGIN_CONTRACT = [
    "margin compression", "margin pressure", "gross margin contracted",
    "operating margin contracted", "margins declined", "lower margins",
]
_MARGIN_STABLE = [
    "stable margins", "margins held", "flat margins",
    "margins were unchanged",
]


def _count_markers(text_lower: str, markers: List[str]) -> int:
    return sum(text_lower.count(m) for m in markers)


def _band_picker(text_lower: str, bands: List[tuple]) -> str:
    """Pick the band with the highest marker count. ``bands`` is a list of
    ``(label, markers)`` tuples. First match wins on ties."""
    best_label = bands[0][0]
    best_count = 0
    for label, markers in bands:
        n = _count_markers(text_lower, markers)
        if n > best_count:
            best_count = n
            best_label = label
    if best_count == 0:
        return bands[-1][0]      # default to last (usually "none" / "neutral")
    return best_label


def _extract_quotes(text: str, *, k: int = 5) -> List[str]:
    """Pull representative sentences mentioning the bands. Heuristic but
    surprisingly readable — we look for sentences that contain the
    matched markers."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    scored: List[tuple] = []
    all_markers = (_CONFIDENT_MARKERS + _CAUTIOUS_MARKERS
                     + _GUIDANCE_RAISE + _GUIDANCE_REDUCE + _GUIDANCE_MAINTAIN
                     + _GUIDANCE_WITHDRAW + _GUIDANCE_FIRST
                     + _MARGIN_EXPAND + _MARGIN_CONTRACT + _MARGIN_STABLE)
    for s in sentences:
        if not 20 <= len(s) <= 280:
            continue
        sl = s.lower()
        score = sum(sl.count(m) for m in all_markers)
        if score > 0:
            scored.append((score, s.strip()))
    # newest-first: assume the document is in reading order, prefer
    # earlier mentions but cap at k.
    seen = set()
    out: List[str] = []
    for _, s in sorted(scored, key=lambda x: -x[0]):
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= k:
            break
    return out


def _extract_forward_looking(text: str, *, k: int = 5) -> List[str]:
    """Forward-looking statements companies make — sentences containing
    future-tense markers."""
    markers = [
        "expect", "anticipate", "forecast", "project", "guide",
        "outlook", "for the year", "for the quarter", "going forward",
        "in the coming", "next quarter", "next year", "fiscal 2026",
        "fiscal 2027", "fy26", "fy27",
    ]
    sentences = re.split(r'(?<=[.!?])\s+', text)
    out: List[str] = []
    seen = set()
    for s in sentences:
        if not 30 <= len(s) <= 320:
            continue
        sl = s.lower()
        if any(m in sl for m in markers) and s not in seen:
            out.append(s.strip())
            seen.add(s)
            if len(out) >= k:
                break
    return out


def heuristic_extract(text: str) -> CallIntel:
    """Pure / deterministic extractor. Returns a CallIntel even on very
    short inputs (empty → all defaults)."""
    if not text:
        return CallIntel()
    tl = text.lower()
    guidance = _band_picker(tl, [
        ("improved", _GUIDANCE_RAISE),
        ("reduced", _GUIDANCE_REDUCE),
        ("maintained", _GUIDANCE_MAINTAIN),
        ("withdrawn", _GUIDANCE_WITHDRAW),
        ("first_time", _GUIDANCE_FIRST),
        ("none", []),
    ])
    margin = _band_picker(tl, [
        ("expanding", _MARGIN_EXPAND),
        ("contracting", _MARGIN_CONTRACT),
        ("stable", _MARGIN_STABLE),
        ("n/a", []),
    ])
    confident = _count_markers(tl, _CONFIDENT_MARKERS)
    cautious = _count_markers(tl, _CAUTIOUS_MARKERS)
    if confident >= 3 and confident > cautious * 2:
        tone = "confident"
    elif cautious >= 3 and cautious > confident * 2:
        tone = "cautious"
    elif confident > 0 and cautious > 0 and abs(confident - cautious) <= 2:
        tone = "mixed"
    else:
        tone = "neutral"

    # Build a one-line summary the UI can render without parsing.
    summary_bits = []
    if guidance != "none":
        summary_bits.append(f"guidance {guidance}")
    if margin != "n/a":
        summary_bits.append(f"margins {margin}")
    summary_bits.append(f"tone {tone}")
    summary = " · ".join(summary_bits)

    return CallIntel(
        guidance_change=guidance, margin_trajectory=margin,
        management_tone=tone,
        key_quotes=_extract_quotes(text),
        forward_looking=_extract_forward_looking(text),
        summary=summary, source="heuristic",
    )


# ── Claude extractor (richer, opt-in) ───────────────────────────────────


SYSTEM_PROMPT = """You extract structured signals from a single earnings press release or call transcript for a trading bot.

Return ONE JSON object with these exact keys:

  guidance_change: "improved" | "maintained" | "reduced" | "first_time" | "withdrawn" | "none"
  margin_trajectory: "expanding" | "stable" | "contracting" | "n/a"
  management_tone: "confident" | "cautious" | "mixed" | "neutral"
  key_quotes: array of 3-5 short verbatim sentences (≤ 200 chars each) capturing the story
  forward_looking: array of 2-5 forward-looking statements management volunteered
  summary: one sentence (≤ 30 words) describing the read-through

Rules:
- "guidance_change" reflects the explicit verbal change (raise / reaffirm / lower / withdraw / first-time / no mention).
- "management_tone" reflects HOW they sound, not just numbers — adjectives like "robust", "challenging", "uncertainty" matter.
- Quote verbatim — do not paraphrase. If a sentence is too long, take the most material clause.
- Return ONLY the JSON object — no preamble, no markdown fences."""


class ClaudeExtractor:
    """Stateful Claude-backed extractor. Cost-tracked via bot/ai_cost."""

    def __init__(self, *, api_key: Optional[str] = None,
                    client: Any = None) -> None:
        self._api_key = api_key
        self._client = client

    def _key(self) -> str:
        if self._api_key is not None:
            return self._api_key
        return anthropic_key() or ""

    @property
    def available(self) -> bool:
        return self._client is not None or bool(self._key())

    def _anthropic(self):
        if self._client is None:
            from anthropic import Anthropic
            self._client = Anthropic(api_key=self._key(), timeout=30.0)
        return self._client

    def extract(self, text: str, *, ticker: Optional[str] = None) -> CallIntel:
        """Return a Claude-extracted CallIntel. Falls through to heuristic
        on any error so callers always get a result."""
        heuristic = heuristic_extract(text)
        if not self.available or not text:
            return heuristic
        try:
            client = self._anthropic()
            model = getattr(TUNABLES, "earnings_intel_model",
                              getattr(TUNABLES, "chat_model",
                                       "claude-sonnet-4-6"))
            # Keep payload bounded — earnings releases can be 50KB+ but
            # the most signal lives in the first 8-10K chars (headline
            # + management quote + guidance section).
            trimmed = text[:12000]
            response = client.messages.create(
                model=model,
                max_tokens=getattr(TUNABLES, "earnings_intel_max_tokens", 1200),
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                          "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user",
                           "content": (f"Ticker: {ticker or '?'}\n\n"
                                          f"Press release / transcript:\n{trimmed}")}],
            )
            try:
                from backend.bot.ai_cost import record_from_response
                record_from_response(surface="earnings_intel", model=model,
                                        response=response,
                                        extra={"ticker": ticker})
            except Exception:
                pass
            raw = "".join(b.text for b in response.content
                           if getattr(b, "type", None) == "text")
            parsed = _parse_json(raw)
            if not isinstance(parsed, dict):
                return heuristic
        except Exception as exc:
            logger.warning("earnings_intel claude failed: %s", exc)
            return heuristic

        def _str(v, default):
            return str(v) if isinstance(v, str) and v else default

        def _list(v):
            return [str(x)[:240] for x in v
                       if isinstance(x, (str, int, float))][:6] \
                       if isinstance(v, list) else []

        return CallIntel(
            guidance_change=_str(parsed.get("guidance_change"), "none"),
            margin_trajectory=_str(parsed.get("margin_trajectory"), "n/a"),
            management_tone=_str(parsed.get("management_tone"), "neutral"),
            key_quotes=_list(parsed.get("key_quotes")),
            forward_looking=_list(parsed.get("forward_looking")),
            summary=_str(parsed.get("summary"), heuristic.summary)[:280],
            source="claude",
        )


def _parse_json(text: str) -> Any:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start: end + 1])
    except Exception:
        return None


# ── module singletons + cache ───────────────────────────────────────────


_EXTRACTOR: Optional[ClaudeExtractor] = None


def get_extractor() -> ClaudeExtractor:
    global _EXTRACTOR
    if _EXTRACTOR is None:
        _EXTRACTOR = ClaudeExtractor()
    return _EXTRACTOR


def reset_extractor() -> None:
    """Test helper — drop the cached Claude extractor."""
    global _EXTRACTOR
    _EXTRACTOR = None


def analyze(*, ticker: str, accession_number: str, filed_at: datetime,
              text: str, prefer_claude: bool = True) -> Dict[str, Any]:
    """Analyze a release + persist the result. Returns the stored dict."""
    extractor = get_extractor()
    intel = (extractor.extract(text, ticker=ticker)
                if prefer_claude and extractor.available
                else heuristic_extract(text))

    try:
        with session_scope() as session:
            existing = session.execute(
                select(EarningsCallIntel)
                .where(EarningsCallIntel.ticker == ticker.upper())
                .where(EarningsCallIntel.accession_number == accession_number)
            ).scalar_one_or_none()
            if existing is not None:
                # Update in place — operator may re-run with Claude after
                # the heuristic version landed first.
                existing.guidance_change = intel.guidance_change
                existing.margin_trajectory = intel.margin_trajectory
                existing.management_tone = intel.management_tone
                existing.key_quotes_json = json.dumps(intel.key_quotes)
                existing.forward_looking_json = json.dumps(intel.forward_looking)
                existing.summary = intel.summary
                existing.source = intel.source
                return existing.to_dict()
            row = EarningsCallIntel(
                ticker=ticker.upper(), accession_number=accession_number,
                filed_at=filed_at,
                guidance_change=intel.guidance_change,
                margin_trajectory=intel.margin_trajectory,
                management_tone=intel.management_tone,
                key_quotes_json=json.dumps(intel.key_quotes),
                forward_looking_json=json.dumps(intel.forward_looking),
                summary=intel.summary, source=intel.source,
            )
            session.add(row); session.flush()
            return row.to_dict()
    except Exception:
        logger.exception("earnings_intel persist failed for %s", ticker)
        return {**intel.to_dict(), "ticker": ticker.upper(),
                  "accession_number": accession_number,
                  "filed_at": filed_at.isoformat() if filed_at else None}


def latest_for(ticker: str) -> Optional[Dict[str, Any]]:
    try:
        with session_scope() as session:
            row = session.execute(
                select(EarningsCallIntel)
                .where(EarningsCallIntel.ticker == ticker.upper())
                .order_by(desc(EarningsCallIntel.filed_at))
                .limit(1)
            ).scalar_one_or_none()
            return row.to_dict() if row else None
    except Exception:
        return None


def history_for(ticker: str, *, limit: int = 8) -> List[Dict[str, Any]]:
    try:
        with session_scope() as session:
            rows = list(session.execute(
                select(EarningsCallIntel)
                .where(EarningsCallIntel.ticker == ticker.upper())
                .order_by(desc(EarningsCallIntel.filed_at))
                .limit(limit)
            ).scalars().all())
            return [r.to_dict() for r in rows]
    except Exception:
        return []
