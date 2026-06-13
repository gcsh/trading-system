"""Risk manager: position sizing, daily loss circuit breaker, stops, EOD checks."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    quantity: float = 0.0
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None


@dataclass
class AccountState:
    buying_power: float
    portfolio_value: float
    open_positions: int
    daily_pnl: float = 0.0


class RiskManager:
    """Enforces all configurable risk limits.

    The manager is stateless across cycles — it expects an :class:`AccountState`
    on every call. The bot engine builds one from broker queries (or mocks)
    before invoking :meth:`evaluate`.
    """

    INTRADAY_EOD = time(15, 45)

    def __init__(self, config: dict) -> None:
        risk = config.get("risk", {}) or {}
        self.max_position_size_usd = float(risk.get("max_position_size_usd", 1000))
        self.max_open_positions = int(risk.get("max_open_positions", 5))
        self.daily_loss_limit_usd = float(risk.get("daily_loss_limit_usd", 300))
        self.stop_loss_pct = float(risk.get("stop_loss_pct", 5)) / 100.0
        self.take_profit_pct = float(risk.get("take_profit_pct", 10)) / 100.0
        self.max_cash_usage_pct = float(risk.get("max_cash_usage_pct", 50)) / 100.0

    def circuit_breaker_tripped(self, account: AccountState) -> bool:
        return account.daily_pnl <= -abs(self.daily_loss_limit_usd)

    def is_after_eod_cutoff(self, now: datetime, trade_style: str = "intraday") -> bool:
        if trade_style != "intraday":
            return False
        return now.time() >= self.INTRADAY_EOD

    def evaluate(
        self,
        action: str,
        price: float,
        account: AccountState,
        trade_style: str = "intraday",
        now: Optional[datetime] = None,
        is_paper: bool = False,
    ) -> RiskDecision:
        if price <= 0:
            return RiskDecision(False, "invalid price")
        if self.circuit_breaker_tripped(account):
            return RiskDecision(False, "daily loss circuit breaker tripped")
        if account.open_positions >= self.max_open_positions:
            return RiskDecision(False, "max open positions reached")

        now = now or datetime.utcnow()
        # Skip the EOD cutoff for paper accounts — there's no overnight risk
        # to manage. Live trading still enforces it.
        if (
            action.startswith("BUY")
            and not is_paper
            and self.is_after_eod_cutoff(now, trade_style)
        ):
            return RiskDecision(False, "past intraday EOD cutoff")

        max_dollars_by_position = self.max_position_size_usd
        max_dollars_by_cash = account.portfolio_value * self.max_cash_usage_pct
        max_dollars = min(max_dollars_by_position, max_dollars_by_cash, account.buying_power)
        if max_dollars <= 0:
            return RiskDecision(False, "no buying power available")

        # OPEX days: dealers re-hedge violently into expiry, so shave size (#2).
        if action.startswith("BUY"):
            try:
                from backend.bot.signals.gex import is_opex_day

                if is_opex_day(now.date()):
                    from backend.config import TUNABLES

                    max_dollars *= TUNABLES.opex_size_factor
            except Exception:
                pass

        if action.startswith("BUY"):
            quantity = max_dollars / price
            stop = price * (1 - self.stop_loss_pct) if self.stop_loss_pct > 0 else None
            take = price * (1 + self.take_profit_pct) if self.take_profit_pct > 0 else None
        else:
            quantity = max_dollars / price
            stop = price * (1 + self.stop_loss_pct) if self.stop_loss_pct > 0 else None
            take = price * (1 - self.take_profit_pct) if self.take_profit_pct > 0 else None

        quantity = round(quantity, 4)
        if quantity <= 0:
            return RiskDecision(False, "computed quantity <= 0")
        return RiskDecision(
            approved=True,
            reason="ok",
            quantity=quantity,
            stop_loss_price=round(stop, 2) if stop else None,
            take_profit_price=round(take, 2) if take else None,
        )
