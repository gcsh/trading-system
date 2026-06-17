"""Alpaca IEX websocket stream → in-memory tick cache + broadcast.

The stream runs as an asyncio task off the main event loop. Each trade /
quote update updates a per-ticker tick buffer and broadcasts a tagged event
on the WebSocket hub so the UI can render real-time sparklines.

Free-tier Alpaca paper accounts include IEX-feed streaming for US equities
(no options). When credentials are missing the runner short-circuits and the
rest of the app keeps working with polled yfinance data.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Deque, Dict, Iterable, Optional

from backend.config import SETTINGS

logger = logging.getLogger(__name__)

MAX_TICKS_PER_TICKER = 240  # ~4 minutes at 1-sec cadence


@dataclass
class TickBuffer:
    """Bounded deque of recent ticks for a single ticker."""

    ticker: str
    ticks: Deque[dict] = field(default_factory=lambda: deque(maxlen=MAX_TICKS_PER_TICKER))

    def append(self, price: float, timestamp: Optional[datetime] = None) -> None:
        self.ticks.append(
            {
                "ticker": self.ticker,
                "price": price,
                "timestamp": (timestamp or datetime.utcnow()).isoformat(),
            }
        )

    def to_list(self) -> list:
        return list(self.ticks)

    @property
    def last_price(self) -> Optional[float]:
        return self.ticks[-1]["price"] if self.ticks else None


class StreamHub:
    """Hold per-ticker tick buffers + broadcast callback. Threadsafe-ish.

    The engine reads ``last_price`` here when it wants a real-time fill price.
    The UI reads the buffer via the /stream endpoints. Both are best-effort:
    if streaming isn't running, everything still works against yfinance.
    """

    def __init__(self, broadcast: Optional[Callable[[dict], Any]] = None) -> None:
        self.buffers: Dict[str, TickBuffer] = {}
        self.broadcast = broadcast
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.subscribed: set[str] = set()

    def buffer(self, ticker: str) -> TickBuffer:
        return self.buffers.setdefault(ticker.upper(), TickBuffer(ticker.upper()))

    def last_price(self, ticker: str) -> Optional[float]:
        buf = self.buffers.get(ticker.upper())
        return buf.last_price if buf else None

    def history(self, ticker: str) -> list:
        buf = self.buffers.get(ticker.upper())
        return buf.to_list() if buf else []

    def sample_to_lake(self) -> int:
        """MITS Phase 8.2 — sample every per-ticker buffer's last tick
        and write a single bronze row. Called by the scheduler every
        ``lake_alpaca_sample_sec`` seconds (default 30s). Returns the
        count of tickers sampled."""
        rows = []
        for ticker, buf in list(self.buffers.items()):
            last = buf.last_price
            if last is None:
                continue
            rows.append({
                "ticker": ticker,
                "last": float(last),
                "buffer_size": len(buf.ticks),
                "sampled_at": datetime.utcnow().isoformat(),
            })
        if not rows:
            return 0
        try:
            from backend.bot.data import lake as _lake
            _lake.write_bronze(
                "alpaca_stream", "ticks", rows,
                request_url="alpaca://stream/iex",
                source_version=__name__,
            )
        except Exception:
            pass
        return len(rows)

    async def _handle_trade(self, data: Any) -> None:
        """Alpaca SDK trade callback (Trade object or dict)."""
        ticker = getattr(data, "symbol", None) or (data.get("S") if isinstance(data, dict) else None)
        price = getattr(data, "price", None) or (data.get("p") if isinstance(data, dict) else None)
        if not ticker or price is None:
            return
        ts_attr = getattr(data, "timestamp", None) or (
            data.get("t") if isinstance(data, dict) else None
        )
        timestamp = None
        if isinstance(ts_attr, datetime):
            timestamp = ts_attr
        self.buffer(ticker).append(float(price), timestamp)
        if self.broadcast:
            try:
                result = self.broadcast(
                    {
                        "kind": "tick",
                        "ticker": ticker,
                        "price": float(price),
                        "timestamp": (timestamp or datetime.utcnow()).isoformat(),
                    }
                )
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("broadcast failed for tick")

    async def _run(self, tickers: Iterable[str]) -> None:
        if not SETTINGS.alpaca_api_key or not SETTINGS.alpaca_api_secret:
            logger.info("alpaca stream: credentials missing, skipping")
            return
        try:
            from alpaca.data.live import StockDataStream  # type: ignore
        except Exception:
            logger.warning("alpaca-py not importable; cannot stream")
            return

        stream = StockDataStream(SETTINGS.alpaca_api_key, SETTINGS.alpaca_api_secret)
        symbols = [t.upper() for t in tickers]
        if not symbols:
            return
        self.subscribed = set(symbols)
        stream.subscribe_trades(self._handle_trade, *symbols)
        logger.info("alpaca stream subscribed to %s", symbols)
        try:
            await stream._run_forever()  # type: ignore[attr-defined]
        except Exception:
            logger.exception("alpaca stream crashed")
        finally:
            try:
                await stream.stop_ws()
            except Exception:
                pass

    def start(self, tickers: Iterable[str]) -> None:
        if self._running:
            return
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        if not loop.is_running():
            logger.info("alpaca stream: event loop not running, cannot start")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(tickers))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


STREAM_HUB: Optional[StreamHub] = None
