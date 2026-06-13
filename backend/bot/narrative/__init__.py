"""Narrative + Macro Intelligence Layer.

Looks at the day's headlines (across the configured universe) and tells the bot:
*what story is the market trading right now, who benefits, and how risky is the
macro backdrop?* Two paths, same interface:

  • **Claude** when an Anthropic key is configured — best quality, picks themes
    and beneficiaries that a human analyst would call out.
  • **Heuristic** keyword/sector match otherwise — never blocks; always returns
    a NarrativeState the dashboards can render.

Pure given the headlines + universe — the route layer is responsible for
gathering inputs and calling ``NarrativeAnalyzer.analyze``.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from backend.bot.portfolio_intel import _SECTOR, _THEMES, themes_for
from backend.config import TUNABLES, anthropic_key

logger = logging.getLogger(__name__)


# Keyword vocabulary that maps a headline to a candidate macro theme. Kept
# intentionally small + readable — easy to grow as new market regimes appear.
_THEME_KEYWORDS: Dict[str, List[str]] = {
    "AI infrastructure": ["AI", "artificial intelligence", "GPU", "datacenter", "data center",
                           "Nvidia", "OpenAI", "semiconductor", "chip", "Anthropic"],
    "Fed / rates":       ["Fed", "Federal Reserve", "rate hike", "rate cut", "FOMC",
                           "inflation", "CPI", "interest rate", "Powell"],
    "Earnings season":   ["earnings", "quarterly", "guidance", "beat", "miss", "results"],
    "Geopolitics":       ["war", "Russia", "Ukraine", "China", "Taiwan", "Israel",
                           "Middle East", "tariff", "sanction"],
    "EV / Auto":         ["EV", "electric vehicle", "Tesla", "battery", "autonomous"],
    "Crypto":            ["bitcoin", "crypto", "Ethereum", "ETF", "stablecoin"],
    "Energy":            ["oil", "OPEC", "gas prices", "drilling", "refinery"],
    "Cloud / software":  ["cloud", "SaaS", "subscription", "enterprise software", "AWS",
                           "Azure", "Google Cloud"],
    "Healthcare":        ["FDA", "drug", "clinical", "trial", "approval", "vaccine"],
}

# Bearish keywords drive macro_risk; bullish ones soften it.
_BEARISH = ["recession", "layoff", "layoffs", "bankrupt", "bankruptcy", "default",
             "crash", "selloff", "plunge", "collapse", "downgrade", "lawsuit",
             "investigation", "fraud", "sanctions", "tariff"]
_BULLISH = ["rally", "soar", "surge", "record high", "all-time high", "breakthrough",
             "approves", "approval", "upgrades", "beat estimates", "blowout"]


@dataclass
class NarrativeState:
    dominant_theme: str = "—"
    beneficiaries: List[str] = field(default_factory=list)
    macro_risk: str = "LOW"                       # HIGH | MODERATE | LOW
    summary: str = ""
    themes: List[Dict[str, Any]] = field(default_factory=list)    # [{name, tickers, confidence}]
    source: str = "heuristic"                     # heuristic | claude

    def to_dict(self) -> dict:
        return asdict(self)


# ── heuristic path ──────────────────────────────────────────────────────────

def heuristic_narrative(headlines: List[str], universe: Optional[List[str]] = None) -> NarrativeState:
    """Keyword + sector heuristic. Always returns a usable NarrativeState even
    when the input is sparse — no exceptions to the caller."""
    if not headlines:
        return NarrativeState(summary="No recent headlines available.", source="heuristic")

    text = " ".join(headlines).lower()
    theme_hits: Counter = Counter()
    for theme, kws in _THEME_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in text:
                theme_hits[theme] += 1
    # Also lift implicit sectors mentioned by ticker name (NVDA → Semis).
    universe_upper = [t.upper() for t in (universe or [])]
    ticker_themes: Counter = Counter()
    for tk in set(_SECTOR) | set(universe_upper):
        if tk in text.upper():
            for t in themes_for(tk):
                ticker_themes[t] += 1
    for k, v in ticker_themes.items():
        theme_hits[k] += v

    if not theme_hits:
        return NarrativeState(summary="Headlines didn't match any tracked theme.",
                               source="heuristic")

    dominant_theme, top_hits = theme_hits.most_common(1)[0]
    # Beneficiaries: tickers in that theme that we actually have in the universe
    # (or anywhere in the static theme map if no universe is given).
    bucket = _THEMES.get(dominant_theme, set())
    beneficiaries = sorted(t for t in bucket if (not universe_upper or t in universe_upper))[:5]
    if not beneficiaries:
        beneficiaries = sorted(bucket)[:5]

    bearish_hits = sum(1 for kw in _BEARISH if kw in text)
    bullish_hits = sum(1 for kw in _BULLISH if kw in text)
    n = max(1, len(headlines))
    bearish_ratio = bearish_hits / n
    if bearish_ratio > 0.30 or (bearish_hits >= 5 and bearish_hits > bullish_hits):
        macro_risk = "HIGH"
    elif bearish_ratio > 0.10:
        macro_risk = "MODERATE"
    else:
        macro_risk = "LOW"

    themes = [
        {"name": name, "tickers": sorted(_THEMES.get(name, set()))[:6], "hits": hits}
        for name, hits in theme_hits.most_common(4)
    ]
    return NarrativeState(
        dominant_theme=dominant_theme, beneficiaries=beneficiaries, macro_risk=macro_risk,
        themes=themes, source="heuristic",
        summary=(f"Headlines lean toward '{dominant_theme}' ({top_hits} mentions). "
                  f"{bearish_hits} bearish vs {bullish_hits} bullish keyword(s)."),
    )


# ── Claude-backed analyzer ──────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a sell-side macro strategist. From the day's headlines, extract:

1. dominant_theme — ONE short label (e.g. "AI infrastructure", "Fed easing cycle", "China tariff escalation")
2. beneficiaries — up to 5 tickers (uppercase, US-listed only) that most directly benefit IF this theme keeps playing out
3. macro_risk — HIGH | MODERATE | LOW based on whether the headlines describe risk-off (war / recession / defaults / aggressive Fed) vs steady-state
4. summary — 1-2 plain-English sentences naming the story

Use the supplied UNIVERSE of tickers to pick beneficiaries where possible; if a relevant beneficiary outside the universe is obvious (e.g. NVDA for an AI story even when missing) you may add it.

Return ONLY a JSON object — no prose:
{"dominant_theme": "...", "beneficiaries": ["..."], "macro_risk": "HIGH|MODERATE|LOW",
 "summary": "..."}"""


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON in narrative response")
    return json.loads(text[start: end + 1])


class NarrativeAnalyzer:
    def __init__(self, api_key: Optional[str] = None, client: Any = None) -> None:
        self._api_key = api_key
        self._client = client

    def _key(self) -> str:
        return self._api_key if self._api_key is not None else anthropic_key()

    @property
    def available(self) -> bool:
        return self._client is not None or bool(self._key())

    def _anthropic(self):
        if self._client is None:
            from anthropic import Anthropic  # type: ignore

            self._client = Anthropic(api_key=self._key(), timeout=30.0)
        return self._client

    def analyze(
        self,
        headlines: List[str],
        universe: Optional[List[str]] = None,
    ) -> NarrativeState:
        """Claude when available, heuristic otherwise. Never raises."""
        if not self.available:
            return heuristic_narrative(headlines, universe)
        if not headlines:
            return NarrativeState(summary="No recent headlines available.", source="claude")
        try:
            client = self._anthropic()
            payload = "Universe: " + ", ".join((universe or [])[:50]) + "\n\nHeadlines:\n"
            payload += "\n".join(f"- {h[:280]}" for h in headlines[:40])
            model = getattr(TUNABLES, "narrative_model", TUNABLES.chat_model)
            response = client.messages.create(
                model=model,
                max_tokens=getattr(TUNABLES, "narrative_max_tokens", 500),
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                          "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": payload}],
            )
            try:
                from backend.bot.ai_cost import record_from_response
                record_from_response(surface="narrative", model=model, response=response)
            except Exception:
                pass
            text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
            parsed = _parse_json(text)
        except Exception as exc:
            logger.warning("narrative analyze failed: %s", exc)
            return heuristic_narrative(headlines, universe)

        macro_risk = str(parsed.get("macro_risk") or "MODERATE").upper()
        if macro_risk not in ("HIGH", "MODERATE", "LOW"):
            macro_risk = "MODERATE"
        beneficiaries = [str(t).upper() for t in (parsed.get("beneficiaries") or [])][:5]
        # Themes: provide the heuristic ones for context too, so the UI always has
        # a list to render even when Claude only names the dominant one.
        heuristic = heuristic_narrative(headlines, universe)
        return NarrativeState(
            dominant_theme=str(parsed.get("dominant_theme") or "—")[:80],
            beneficiaries=beneficiaries,
            macro_risk=macro_risk,
            summary=str(parsed.get("summary") or "")[:400],
            themes=heuristic.themes,
            source="claude",
        )
