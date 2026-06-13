"""MITS Phase 17.B — FillSnapshot: structured fill-time observable capture.

One-shot snapshot of everything the executor saw at the instant a fill
booked: bid/ask/mid, IV + greeks (options), underlying, source +
freshness of the quote, plus the realized commission / spread / slippage.
Persisted as ``Trade.fill_snapshot_json``; consumed by Phase 18 Learning
Layer to attribute fill quality to vendor freshness, time-of-day, IV
regime, etc.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class FillSnapshot:
    """One-shot capture of every observable at fill time.

    Options fills populate the full 14 numeric fields. Stock fills
    leave iv/greeks/underlying and bid/ask/spread_pct/spread_paid as
    None — quote_source provides a single mid, not a two-sided book.
    """
    bid: Optional[float]
    ask: Optional[float]
    mid: float
    spread_pct: Optional[float]
    iv: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]
    theta: Optional[float]
    vega: Optional[float]
    underlying: Optional[float]
    source: str
    age_seconds: Optional[float]
    commission: float
    spread_paid: Optional[float]
    slippage_bps: Optional[float]
    captured_at: str

    @classmethod
    def from_option_mark(
        cls,
        mark: Any,  # OptionMark from backend.bot.options.pricing
        *,
        commission: float,
        fill_price: float,
        slippage_bps: float,
        spread_paid: float,
    ) -> "FillSnapshot":
        """Build snapshot from an OptionMark + fill-time metadata.

        ``fill_price`` is unused in the snapshot body (slippage_bps already
        carries the fill-vs-mid relationship) but kept on the signature so
        callers always pass it — Phase 18 may extend the snapshot with the
        raw fill price and we don't want to chase callsites then.
        """
        spread_pct: Optional[float] = None
        if mark.bid and mark.ask and mark.mid:
            spread_pct = round((mark.ask - mark.bid) / mark.mid * 100, 4)
        return cls(
            bid=mark.bid,
            ask=mark.ask,
            mid=mark.mid,
            spread_pct=spread_pct,
            iv=mark.iv,
            delta=mark.delta,
            gamma=mark.gamma,
            theta=mark.theta,
            vega=mark.vega,
            underlying=mark.underlying,
            source=mark.source,
            age_seconds=mark.age_seconds,
            commission=round(commission, 4),
            spread_paid=round(spread_paid, 4),
            slippage_bps=round(slippage_bps, 4),
            captured_at=datetime.utcnow().isoformat(),
        )

    @classmethod
    def from_stock_quote(
        cls,
        quote: Any,  # Quote from backend.bot.data.quote_source
        *,
        commission: float,
        fill_price: float,
        slippage_bps: float,
    ) -> "FillSnapshot":
        """Build snapshot from a Quote + fill-time metadata.

        Stock quote_source only exposes a single mid (price), so bid/ask/
        spread fields and the greeks are None by definition. The remaining
        fields (source + age_seconds + commission + slippage_bps) are the
        observability surface the Learning Layer needs to attribute stock
        fill quality.
        """
        return cls(
            bid=None,
            ask=None,
            mid=quote.price,
            spread_pct=None,
            iv=None,
            delta=None,
            gamma=None,
            theta=None,
            vega=None,
            underlying=None,
            source=quote.source,
            age_seconds=quote.age_seconds,
            commission=round(commission, 4),
            spread_paid=None,
            slippage_bps=round(slippage_bps, 4),
            captured_at=datetime.utcnow().isoformat(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)
