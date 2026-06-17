"""Finnhub free-tier wrapper.

Provides quotes, company news, social sentiment, and earnings dates. The
caller (engine / MarketDataAdapter) reads ``FINNHUB_API_KEY`` from settings —
when missing, every method returns ``None`` so callers degrade gracefully.

Rate limit: 60 req/min on the free tier. We rely on the engine's own cycle
cadence (5-min scheduler) to stay well under it, plus a small in-memory
cache so repeat calls within a window don't re-hit the network.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.config import SETTINGS

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
DEFAULT_CACHE_TTL = 30  # seconds


@dataclass
class FinnhubQuote:
    """Subset of the /quote endpoint."""

    price: float
    high: float
    low: float
    open: float
    prev_close: float
    timestamp: int
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_response(cls, payload: dict) -> "FinnhubQuote":
        return cls(
            price=float(payload.get("c", 0) or 0),
            high=float(payload.get("h", 0) or 0),
            low=float(payload.get("l", 0) or 0),
            open=float(payload.get("o", 0) or 0),
            prev_close=float(payload.get("pc", 0) or 0),
            timestamp=int(payload.get("t", 0) or 0),
            raw=payload,
        )


class FinnhubClient:
    """Tiny HTTP wrapper using ``httpx`` (already a project dep)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        http_client: Any = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else SETTINGS.finnhub_api_key
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, tuple[float, Any]] = {}
        self._http = http_client

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    # -- low-level ----------------------------------------------------------
    def _http_client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=5.0)
        return self._http

    def _get(self, endpoint: str, params: Dict[str, Any]) -> Optional[Any]:
        if not self.available:
            return None
        full_params = {**params, "token": self.api_key}
        cache_key = f"{endpoint}|{sorted(full_params.items())}"
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < self.cache_ttl:
            return cached[1]
        try:
            response = self._http_client().get(f"{FINNHUB_BASE}{endpoint}", params=full_params)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            logger.exception("finnhub %s failed", endpoint)
            return None
        self._cache[cache_key] = (time.time(), payload)
        return payload

    # -- public -------------------------------------------------------------
    def quote(self, ticker: str) -> Optional[FinnhubQuote]:
        payload = self._get("/quote", {"symbol": ticker.upper()})
        if not payload:
            return None
        return FinnhubQuote.from_response(payload)

    def company_news(self, ticker: str, days_back: int = 7) -> List[dict]:
        from datetime import datetime, timedelta

        to_date = datetime.utcnow().date()
        from_date = to_date - timedelta(days=days_back)
        payload = self._get(
            "/company-news",
            {"symbol": ticker.upper(), "from": from_date.isoformat(), "to": to_date.isoformat()},
        )
        if not payload or not isinstance(payload, list):
            return []
        return payload[:25]

    def social_sentiment(self, ticker: str) -> Optional[dict]:
        """Aggregate Reddit + Twitter mentions and sentiment (premium endpoint).

        Returns None on free-tier accounts that don't have access — we treat
        it as missing data rather than an error.
        """
        payload = self._get(
            "/stock/social-sentiment",
            {"symbol": ticker.upper()},
        )
        if not payload or not isinstance(payload, dict):
            return None
        return payload

    def upcoming_earnings(self, ticker: str) -> Optional[dict]:
        """Next earnings event for ``ticker`` (or None)."""
        from datetime import datetime, timedelta

        today = datetime.utcnow().date()
        future = today + timedelta(days=90)
        payload = self._get(
            "/calendar/earnings",
            {"symbol": ticker.upper(), "from": today.isoformat(), "to": future.isoformat()},
        )
        if not payload or "earningsCalendar" not in payload:
            return None
        events = payload.get("earningsCalendar") or []
        return events[0] if events else None
