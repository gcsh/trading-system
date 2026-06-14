"""Robinhood order execution with paper-mode safety net.

All ``robin_stocks`` calls are wrapped so tests can mock them without hitting
the live broker. In paper mode every order is short-circuited to a logged
result.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.config import SETTINGS

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]
    paper: bool
    raw: dict = field(default_factory=dict)
    error: Optional[str] = None


class Executor:
    """Thin wrapper around ``robin_stocks`` with session and paper-mode handling."""

    def __init__(self, paper_mode: Optional[bool] = None, rh_module: Any = None) -> None:
        self.paper_mode = SETTINGS.paper_mode if paper_mode is None else paper_mode
        self._rh = rh_module
        self._logged_in = False
        self._last_login_attempt = 0.0

    # -- session ------------------------------------------------------------
    def _client(self):
        if self._rh is None:
            import robin_stocks.robinhood as rh  # type: ignore

            self._rh = rh
        return self._rh

    def login(self) -> bool:
        """Login to Robinhood. Returns True on success, False if creds missing."""
        if self.paper_mode:
            self._logged_in = True
            return True
        if not SETTINGS.robinhood_username or not SETTINGS.robinhood_password:
            logger.warning("Robinhood credentials missing; cannot login")
            return False
        # Rate-limit login attempts.
        if time.time() - self._last_login_attempt < 30:
            return self._logged_in
        self._last_login_attempt = time.time()
        rh = self._client()
        mfa = None
        if SETTINGS.robinhood_mfa_secret:
            try:
                import pyotp  # type: ignore

                mfa = pyotp.TOTP(SETTINGS.robinhood_mfa_secret).now()
            except Exception:  # pragma: no cover - optional dep
                mfa = None
        try:
            rh.login(
                username=SETTINGS.robinhood_username,
                password=SETTINGS.robinhood_password,
                mfa_code=mfa,
                store_session=True,
            )
            self._logged_in = True
            return True
        except Exception as exc:
            logger.exception("Robinhood login failed: %s", exc)
            self._logged_in = False
            return False

    # -- account ------------------------------------------------------------
    def get_account_state(self) -> dict:
        """Return buying_power, portfolio_value, open_positions for the risk manager."""
        if self.paper_mode:
            return {
                "buying_power": 10000.0,
                "portfolio_value": 25000.0,
                "open_positions": 0,
            }
        rh = self._client()
        if not self._logged_in and not self.login():
            return {"buying_power": 0.0, "portfolio_value": 0.0, "open_positions": 0}
        profile = rh.profiles.load_account_profile()
        portfolio = rh.profiles.load_portfolio_profile()
        positions = rh.account.get_open_stock_positions() or []
        return {
            "buying_power": float(profile.get("buying_power", 0.0)),
            "portfolio_value": float(portfolio.get("equity", 0.0)),
            "open_positions": len([p for p in positions if float(p.get("quantity", 0)) > 0]),
        }

    # -- orders -------------------------------------------------------------
    def place_stock_order(
        self,
        ticker: str,
        action: str,
        quantity: float,
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        """Submit a stock order. ``action`` is BUY or SELL."""
        action = action.upper()
        if self.paper_mode:
            logger.info("[paper] %s %s %s", action, quantity, ticker)
            return OrderResult(True, f"paper-{ticker}-{action}", True, {"ticker": ticker})
        rh = self._client()
        if not self._logged_in and not self.login():
            return OrderResult(False, None, False, error="not logged in")
        try:
            if action == "BUY":
                if order_type == "limit" and limit_price is not None:
                    raw = rh.orders.order_buy_limit(ticker, quantity, limit_price)
                else:
                    raw = rh.orders.order_buy_market(ticker, quantity)
            elif action == "SELL":
                if order_type == "limit" and limit_price is not None:
                    raw = rh.orders.order_sell_limit(ticker, quantity, limit_price)
                else:
                    raw = rh.orders.order_sell_market(ticker, quantity)
            else:
                return OrderResult(False, None, False, error=f"unknown action {action}")
            return OrderResult(True, raw.get("id"), False, raw=raw or {})
        except Exception as exc:
            logger.exception("order failed")
            return OrderResult(False, None, False, error=str(exc))

    def place_options_order(
        self,
        ticker: str,
        action: str,  # BUY_CALL / BUY_PUT / SELL_CALL / SELL_PUT
        quantity: int,
        strike: float,
        expiration: str,
        option_type_override: Optional[str] = None,  # Fix N=1 — kept for signature parity
    ) -> OrderResult:
        action = action.upper()
        if self.paper_mode:
            logger.info("[paper] %s %s %s @ %s exp %s", action, quantity, ticker, strike, expiration)
            return OrderResult(
                True,
                f"paper-{ticker}-{action}-{strike}",
                True,
                {"ticker": ticker, "strike": strike, "expiration": expiration},
            )
        rh = self._client()
        if not self._logged_in and not self.login():
            return OrderResult(False, None, False, error="not logged in")
        try:
            option_type = "call" if "CALL" in action else "put"
            if action.startswith("BUY"):
                raw = rh.orders.order_buy_option_limit(
                    "open", "debit", price=0.05, symbol=ticker,
                    quantity=quantity, expirationDate=expiration,
                    strike=strike, optionType=option_type,
                )
            else:
                raw = rh.orders.order_sell_option_limit(
                    "close", "credit", price=0.05, symbol=ticker,
                    quantity=quantity, expirationDate=expiration,
                    strike=strike, optionType=option_type,
                )
            return OrderResult(True, raw.get("id"), False, raw=raw or {})
        except Exception as exc:
            logger.exception("options order failed")
            return OrderResult(False, None, False, error=str(exc))

    def place_complex_order(self, signal) -> "OrderResult":
        """Multi-leg options (spreads, condors, collars, straddles).

        Most brokers require dedicated multi-leg endpoints; this base
        implementation logs the legs in paper mode and returns a paper
        success. Concrete brokers should override.
        """
        logger.info(
            "[paper-complex] %s %s metadata=%s",
            signal.action.value, signal.ticker, signal.metadata,
        )
        return OrderResult(
            success=True,
            order_id=f"paper-complex-{signal.ticker}-{signal.action.value}",
            paper=True,
            raw={"signal": signal.action.value, **signal.metadata},
        )

    def cancel_all_orders(self) -> None:
        if self.paper_mode:
            logger.info("[paper] cancel all")
            return
        rh = self._client()
        try:
            rh.orders.cancel_all_stock_orders()
        except Exception:
            logger.exception("cancel_all_stock_orders failed")
        try:
            rh.orders.cancel_all_option_orders()
        except Exception:
            logger.exception("cancel_all_option_orders failed")
