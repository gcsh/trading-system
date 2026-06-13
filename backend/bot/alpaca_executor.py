"""Alpaca paper-trading executor.

Drop-in for :class:`backend.bot.executor.Executor`. Uses the official
``alpaca-py`` SDK (https://alpaca.markets/docs/python-sdk/) and the paper
trading endpoint by default. Single-leg options orders are routed if your
paper account has options approval; complex multi-leg structures are still
logged-only — Alpaca's multi-leg endpoint requires a different request shape
that depends on your account tier.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"


@dataclass
class AlpacaOrderResult:
    success: bool
    order_id: Optional[str]
    paper: bool
    raw: dict = field(default_factory=dict)
    error: Optional[str] = None


class AlpacaExecutor:
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        paper: bool = True,
        client=None,
    ) -> None:
        self.paper = paper
        self.api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self.api_secret = api_secret or os.getenv("ALPACA_API_SECRET", "")
        self._client = client
        self._logged_in = False

    # -- session ------------------------------------------------------------
    def _trading_client(self):
        if self._client is not None:
            return self._client
        from alpaca.trading.client import TradingClient

        self._client = TradingClient(
            api_key=self.api_key,
            secret_key=self.api_secret,
            paper=self.paper,
        )
        return self._client

    def login(self) -> bool:
        if not self.api_key or not self.api_secret:
            logger.warning("Alpaca API key/secret not set")
            return False
        try:
            # Single round-trip to confirm credentials.
            self._trading_client().get_account()
            self._logged_in = True
            return True
        except Exception as exc:
            logger.exception("Alpaca login failed: %s", exc)
            return False

    # -- account ------------------------------------------------------------
    def get_account_state(self) -> dict:
        try:
            account = self._trading_client().get_account()
            positions = self._trading_client().get_all_positions()
            return {
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value),
                "open_positions": len(positions),
            }
        except Exception as exc:
            logger.exception("get_account_state failed: %s", exc)
            return {"buying_power": 0.0, "portfolio_value": 0.0, "open_positions": 0}

    # -- orders -------------------------------------------------------------
    def place_stock_order(
        self,
        ticker: str,
        action: str,
        quantity: float,
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> AlpacaOrderResult:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        side = OrderSide.BUY if action.upper() == "BUY" else OrderSide.SELL
        try:
            quantity_int = max(1, int(quantity))
            if order_type == "limit" and limit_price is not None:
                req = LimitOrderRequest(
                    symbol=ticker, qty=quantity_int, side=side,
                    time_in_force=TimeInForce.DAY, limit_price=limit_price,
                )
            else:
                req = MarketOrderRequest(
                    symbol=ticker, qty=quantity_int, side=side,
                    time_in_force=TimeInForce.DAY,
                )
            order = self._trading_client().submit_order(req)
            # MITS Phase 17.B — fill snapshot. Use the same quote_source
            # the paper path uses so the snapshot field set is identical
            # regardless of executor. Limit orders snapshot the chosen
            # price with source="limit_order".
            from backend.bot.data.quote_source import Quote, get_quote
            from backend.bot.execution.fill_snapshot import FillSnapshot
            if order_type == "limit" and limit_price is not None:
                quote = Quote(price=float(limit_price),
                              source="limit_order", age_seconds=None)
            else:
                quote = get_quote(ticker)
            fill_price = float(getattr(order, "filled_avg_price", None)
                               or limit_price or quote.price or 0.0)
            slippage_bps = (
                abs(fill_price - quote.price) / quote.price * 10_000.0
                if quote.price > 0 else 0.0
            )
            snapshot = FillSnapshot.from_stock_quote(
                quote, commission=0.0, fill_price=fill_price,
                slippage_bps=slippage_bps,
            )
            return AlpacaOrderResult(
                success=True, order_id=str(order.id), paper=self.paper,
                raw={
                    "symbol": ticker, "qty": quantity_int, "side": side.value,
                    "fill_snapshot_json": snapshot.to_json(),
                },
            )
        except Exception as exc:
            logger.exception("Alpaca stock order failed")
            return AlpacaOrderResult(False, None, self.paper, error=str(exc))

    def place_options_order(
        self,
        ticker: str,
        action: str,
        quantity: int,
        strike: float,
        expiration: str,
    ) -> AlpacaOrderResult:
        """Submit a single-leg options order.

        Requires options trading approval on your Alpaca account (paper or
        live). ``action`` is BUY_CALL / BUY_PUT / SELL_CALL / SELL_PUT.
        ``expiration`` should be YYYY-MM-DD.
        """
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        # OCC option symbol: ROOT + YYMMDD + C/P + 8-digit strike (x1000)
        if not expiration:
            return AlpacaOrderResult(False, None, self.paper, error="no expiration")
        try:
            yymmdd = expiration.replace("-", "")[2:]
            opt_type = "C" if "CALL" in action.upper() else "P"
            strike_token = f"{int(round(strike * 1000)):08d}"
            occ = f"{ticker.upper()}{yymmdd}{opt_type}{strike_token}"
            side = OrderSide.BUY if action.upper().startswith("BUY") else OrderSide.SELL
            req = MarketOrderRequest(
                symbol=occ, qty=quantity, side=side, time_in_force=TimeInForce.DAY,
            )
            order = self._trading_client().submit_order(req)
            # MITS Phase 17.B — fill snapshot. Pull a chain mark via the
            # same pricing module the paper path uses; if it fails we still
            # emit a snapshot using a synthetic OptionMark with whatever
            # the Alpaca order response carries.
            from backend.bot.data.quote_source import get_quote
            from backend.bot.execution.fill_snapshot import FillSnapshot
            from backend.bot.options.pricing import OptionMark, price_at_entry
            underlying_quote = get_quote(ticker)
            mark = price_at_entry(
                symbol=ticker.upper(),
                spot=float(underlying_quote.price or strike),
                strike=float(strike), expiration=expiration,
                right="call" if "CALL" in action.upper() else "put",
            )
            fill_price = float(getattr(order, "filled_avg_price", None)
                               or mark.mid or 0.0)
            slippage_bps = (
                abs(fill_price - mark.mid) / mark.mid * 10_000.0
                if mark.mid > 0 else 0.0
            )
            snapshot = FillSnapshot.from_option_mark(
                mark, commission=0.0, fill_price=fill_price,
                slippage_bps=slippage_bps,
                spread_paid=float(fill_price - mark.mid),
            )
            return AlpacaOrderResult(
                success=True, order_id=str(order.id), paper=self.paper,
                raw={
                    "symbol": occ, "qty": quantity, "side": side.value,
                    "fill_snapshot_json": snapshot.to_json(),
                },
            )
        except Exception as exc:
            logger.exception("Alpaca options order failed")
            return AlpacaOrderResult(False, None, self.paper, error=str(exc))

    def place_complex_order(self, signal) -> AlpacaOrderResult:
        """Multi-leg structures (spreads, condors, straddles, collars).

        Alpaca's multi-leg endpoint exists for paper + cleared accounts but
        the request shape differs by structure. For now we log the intent and
        return a paper success so downstream logging still records the trade.
        """
        logger.info(
            "[alpaca-complex stub] %s %s legs=%s",
            signal.action.value, signal.ticker, signal.metadata,
        )
        return AlpacaOrderResult(
            success=True,
            order_id=f"alpaca-paper-complex-{signal.ticker}-{signal.action.value}",
            paper=True,
            raw={"action": signal.action.value, **signal.metadata},
        )

    def cancel_all_orders(self) -> None:
        try:
            self._trading_client().cancel_orders()
        except Exception:
            logger.exception("Alpaca cancel_all failed")
