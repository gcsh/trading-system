"""Meta-AI Reasoning Layer.

Once the analytical layer has produced a grade + win-probability, the meta-AI
acts as the *portfolio strategist*: it gets the full picture — the candidate
trade, the regime, the multi-timeframe confluence, the portfolio's existing
exposure — and returns a single approve/reject + a risk modifier (0.5–1.0) for
position sizing, with auditable bullet-point reasoning.

It deliberately can NOT bypass the risk manager or place trades — it only votes
on whether to act on the upstream signal and how much size to take. With no key
configured it returns a safe pass-through (approve=True, risk_modifier=1.0) so
the engine behaves exactly as it would without this layer.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.config import TUNABLES, anthropic_key

logger = logging.getLogger(__name__)

# Process-local TTL cache for meta-AI audit verdicts. Keyed by a hash of
# the input context (ticker + signal + analytics + portfolio_risk) so a
# busy engine cycle that asks the same question twice within the TTL
# window collapses to a single Anthropic call. 5-minute TTL matches the
# simulator/_five_min_bucket cadence already used elsewhere in the
# decision layer.
_AUDIT_CACHE_TTL_SEC = 300.0
_AUDIT_CACHE_MAX = 64
_AUDIT_CACHE: Dict[str, Tuple[float, "MetaDecision"]] = {}
_AUDIT_CACHE_LOCK = threading.Lock()


def _audit_cache_key(
    ticker: str,
    signal_summary: Dict[str, Any],
    analytics: Dict[str, Any],
    portfolio_risk: Optional[Dict[str, Any]],
) -> str:
    payload = json.dumps(
        {
            "t": (ticker or "").upper(),
            "s": signal_summary or {},
            "a": analytics or {},
            "p": portfolio_risk or {},
        },
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit_cache_get(key: str) -> Optional["MetaDecision"]:
    now = time.monotonic()
    with _AUDIT_CACHE_LOCK:
        entry = _AUDIT_CACHE.get(key)
        if entry is None:
            return None
        expires_at, verdict = entry
        if expires_at < now:
            _AUDIT_CACHE.pop(key, None)
            return None
        return verdict


def _audit_cache_put(key: str, verdict: "MetaDecision") -> None:
    now = time.monotonic()
    with _AUDIT_CACHE_LOCK:
        if len(_AUDIT_CACHE) >= _AUDIT_CACHE_MAX:
            # Evict the oldest entries (by expires_at) until under cap.
            for ek, _ in sorted(
                _AUDIT_CACHE.items(), key=lambda kv: kv[1][0],
            )[: max(1, len(_AUDIT_CACHE) - _AUDIT_CACHE_MAX + 1)]:
                _AUDIT_CACHE.pop(ek, None)
        _AUDIT_CACHE[key] = (now + _AUDIT_CACHE_TTL_SEC, verdict)

SYSTEM_PROMPT = """You are an institutional portfolio strategist auditing a single candidate paper trade. You will be given:
- the candidate signal (ticker, action, the strategy that fired it, the bot's stated reason)
- the analytical layer's view (regime, multi-timeframe confluence, win probability, grade)
- the live portfolio's concentration / net beta / correlation clusters / concentration flags
- a compact market snapshot

Decide:
1. Should this trade execute? (approve: true/false)
2. If approve=true, what size? Return risk_modifier in [0.5, 1.0] — 1.0 = full size, lower = reduce because of risk/concentration/regime concern.
3. Briefly explain — 3-5 short bullets — naming the SPECIFIC signals you weighed (e.g. "regime aligned: bullish trend + expanding momentum", "portfolio already 70% semis — reduce to 0.6", "fighting daily downtrend — reject").

Veto if any of these hold:
- direction fights the regime AND multi-timeframe confluence
- portfolio is already concentrated (≥60% in the new trade's sector, or the same theme)
- the analytical layer's grade is "Reject"

Return ONLY a JSON object — no prose:
{"approve": true|false, "confidence": 0.0-1.0, "risk_modifier": 0.5-1.0, "reasoning": ["...", "..."]}
"""

_APPROVE_FALLBACK = "approve_pass_through"


@dataclass
class MetaDecision:
    approve: bool = True
    confidence: float = 0.0
    risk_modifier: float = 1.0
    reasoning: List[str] = field(default_factory=list)
    source: str = ""             # "claude" | "pass_through" | "error"

    def to_dict(self) -> dict:
        return asdict(self)


def _user_payload(
    ticker: str,
    signal_summary: Dict[str, Any],
    analytics: Dict[str, Any],
    portfolio: Optional[Dict[str, Any]],
) -> str:
    parts = [f"Ticker: {ticker}", "Signal: " + json.dumps(signal_summary)]
    if analytics:
        compact = {
            "regime": (analytics.get("regime") or {}).get("label"),
            "trend": (analytics.get("regime") or {}).get("trend"),
            "gamma": (analytics.get("regime") or {}).get("gamma"),
            "probability": (analytics.get("probability") or {}).get("probability"),
            "direction": (analytics.get("probability") or {}).get("direction"),
            "grade": (analytics.get("rank") or {}).get("grade"),
            "confluence": (analytics.get("confluence") or {}),
            "key_features": {k: (analytics.get("features") or {}).get(k) for k in
                              ("composite_bias", "trend_bias", "flow_bullishness",
                               "gex_flip_distance", "volume_ratio", "dealer_regime",
                               "darkpool_bias", "news_sentiment")},
        }
        parts.append("Analytics: " + json.dumps(compact))
    if portfolio:
        compact_p = {
            "top_sector": portfolio.get("top_sector"),
            "top_sector_pct": portfolio.get("top_sector_pct"),
            "top_theme": portfolio.get("top_theme"),
            "top_theme_pct": portfolio.get("top_theme_pct"),
            "biggest_position": portfolio.get("biggest_position"),
            "correlation_clusters": portfolio.get("correlation_clusters"),
            "net_beta": portfolio.get("net_beta"),
            "macro_risk": portfolio.get("macro_risk"),
            "concentration_flags": portfolio.get("concentration_flags"),
        }
        parts.append("Portfolio: " + json.dumps(compact_p))
    parts.append("Return the JSON decision now.")
    return "\n\n".join(parts)


def _parse(text: str) -> dict:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in meta response")
    return json.loads(text[start: end + 1])


class MetaReasoner:
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

    def audit(
        self,
        ticker: str,
        signal_summary: Dict[str, Any],
        analytics: Dict[str, Any],
        portfolio_risk: Optional[Dict[str, Any]] = None,
    ) -> MetaDecision:
        """Run the audit. With no key, returns a safe pass-through approval.

        Verdicts are cached for 5 minutes keyed by a hash of the full
        input context — identical re-asks within the cycle (or across
        rapid successive cycles) collapse to a single Anthropic call.
        """
        if not self.available:
            return MetaDecision(
                approve=True, confidence=0.0, risk_modifier=1.0,
                reasoning=["meta-AI not configured — pass through"], source="pass_through",
            )
        cache_key = _audit_cache_key(
            ticker, signal_summary, analytics, portfolio_risk,
        )
        cached = _audit_cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            client = self._anthropic()
            model = getattr(TUNABLES, "meta_ai_model", TUNABLES.ai_brain_model)
            response = client.messages.create(
                model=model,
                max_tokens=getattr(TUNABLES, "meta_ai_max_tokens", 600),
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                          "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user",
                           "content": _user_payload(ticker, signal_summary, analytics, portfolio_risk)}],
            )
            try:
                from backend.bot.ai_cost import record_from_response
                record_from_response(surface="meta_ai", model=model, response=response,
                                        extra={"ticker": ticker})
            except Exception:
                pass
            text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
            parsed = _parse(text)
        except Exception as exc:
            logger.warning("meta-AI audit failed: %s", exc)
            return MetaDecision(
                approve=True, confidence=0.0, risk_modifier=1.0,
                reasoning=[f"meta audit error: {exc}"], source="error",
            )

        approve = bool(parsed.get("approve", True))
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0) or 0.0)))
        rm = float(parsed.get("risk_modifier", 1.0) or 1.0)
        rm = max(0.5, min(1.0, rm))
        reasoning = [str(r)[:200] for r in (parsed.get("reasoning") or [])][:6]
        verdict = MetaDecision(approve=approve, confidence=confidence, risk_modifier=rm,
                              reasoning=reasoning, source="claude")
        _audit_cache_put(cache_key, verdict)
        return verdict
