"""Stage-2 broker-constraint models.

Every broker has hard rules the bot must respect or the order is rejected:
  • Minimum lot size (some brokers: 1 share; some: 100 shares for old odd-lot
    handling; options always 1 contract = 100 shares of underlying)
  • Maximum order size / notional (regulatory + venue)
  • Allowed order types (market / limit / stop / extended-hours / etc.)
  • Multi-leg atomicity (does the broker fill spreads as one combo, or do you
    submit each leg sequentially and risk one leg failing after the other
    already filled?)

The validator returns a list of ``ConstraintViolation``. The engine treats
them like audit violations:
  • paper: block the order if any constraint fails
  • live:  log + alert but don't second-guess the broker's own validation
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class ConstraintViolation:
    name: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "message": self.message, **self.detail}


@dataclass
class BrokerProfile:
    """Static declarative description of broker rules."""
    name: str

    # Stocks
    stock_min_shares: float = 1.0
    stock_fractional_supported: bool = True
    stock_max_order_notional: float = 1_000_000.0

    # Options
    option_min_contracts: int = 1
    option_max_contracts: int = 1000

    # Order types
    allowed_order_types: Set[str] = field(default_factory=lambda: {"market", "limit"})
    extended_hours: bool = False

    # Multi-leg
    leg_atomicity_supported: bool = True
    max_legs: int = 4

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "stock_min_shares": self.stock_min_shares,
            "stock_fractional_supported": self.stock_fractional_supported,
            "stock_max_order_notional": self.stock_max_order_notional,
            "option_min_contracts": self.option_min_contracts,
            "option_max_contracts": self.option_max_contracts,
            "allowed_order_types": sorted(self.allowed_order_types),
            "extended_hours": self.extended_hours,
            "leg_atomicity_supported": self.leg_atomicity_supported,
            "max_legs": self.max_legs,
        }


# ── catalog ────────────────────────────────────────────────────────────────


BROKER_PROFILES: Dict[str, BrokerProfile] = {
    "local_paper": BrokerProfile(
        name="local_paper",
        stock_fractional_supported=True,
        leg_atomicity_supported=True,
    ),
    "alpaca_paper": BrokerProfile(
        name="alpaca_paper",
        stock_fractional_supported=True,
        allowed_order_types={"market", "limit", "stop", "stop_limit"},
        extended_hours=True,
        leg_atomicity_supported=True,
    ),
    "alpaca_live": BrokerProfile(
        name="alpaca_live",
        stock_fractional_supported=True,
        allowed_order_types={"market", "limit", "stop", "stop_limit"},
        extended_hours=True,
        leg_atomicity_supported=True,
        # Conservative caps for live trading.
        stock_max_order_notional=200_000.0,
        option_max_contracts=500,
    ),
    "robinhood": BrokerProfile(
        name="robinhood",
        stock_fractional_supported=True,
        allowed_order_types={"market", "limit", "stop", "stop_limit"},
        leg_atomicity_supported=False,        # Robinhood spreads are sequential
        max_legs=2,
    ),
    "ibkr_lite": BrokerProfile(
        name="ibkr_lite",
        stock_fractional_supported=True,
        allowed_order_types={"market", "limit", "stop", "stop_limit"},
        leg_atomicity_supported=True,
        max_legs=4,
    ),
    "ibkr_pro": BrokerProfile(
        name="ibkr_pro",
        stock_fractional_supported=False,
        stock_min_shares=1.0,
        allowed_order_types={"market", "limit", "stop", "stop_limit",
                              "moc", "loc", "trailing_stop"},
        extended_hours=True,
        leg_atomicity_supported=True,
        max_legs=4,
    ),
}


def get_profile(broker: str) -> BrokerProfile:
    return BROKER_PROFILES.get(broker, BROKER_PROFILES["local_paper"])


# ── validator ──────────────────────────────────────────────────────────────


def validate_order(plan: Dict[str, Any], broker: str = "local_paper",
                    order_type: str = "market") -> List[ConstraintViolation]:
    """Run every applicable constraint check against an order plan. Returns
    the violation list — empty means the order is broker-legal."""
    profile = get_profile(broker)
    violations: List[ConstraintViolation] = []

    instrument = plan.get("instrument", "stock")

    # Order-type allowed?
    if order_type not in profile.allowed_order_types:
        violations.append(ConstraintViolation(
            name="order_type_not_supported",
            message=f"{broker} doesn't accept '{order_type}' orders "
                     f"(allowed: {sorted(profile.allowed_order_types)})",
            detail={"order_type": order_type,
                     "allowed": sorted(profile.allowed_order_types)},
        ))

    if instrument == "stock":
        qty = float(plan.get("quantity") or 0)
        price = float(plan.get("price") or plan.get("limit_price") or 0)
        # Fractional check
        if not profile.stock_fractional_supported and qty != int(qty):
            violations.append(ConstraintViolation(
                name="fractional_not_supported",
                message=f"{broker} doesn't accept fractional shares "
                         f"(got {qty}; submit whole shares)",
                detail={"quantity": qty},
            ))
        # Min lot
        if qty != 0 and abs(qty) < profile.stock_min_shares:
            violations.append(ConstraintViolation(
                name="below_min_lot",
                message=f"{broker} min lot is {profile.stock_min_shares} shares; "
                         f"got {qty}",
                detail={"quantity": qty, "min_lot": profile.stock_min_shares},
            ))
        # Max notional
        notional = abs(qty * price) if price else 0
        if notional > profile.stock_max_order_notional:
            violations.append(ConstraintViolation(
                name="above_max_notional",
                message=f"order ${notional:.0f} exceeds {broker} max "
                         f"${profile.stock_max_order_notional:.0f}",
                detail={"notional": notional,
                         "cap": profile.stock_max_order_notional},
            ))

    elif instrument in ("option", "spread"):
        contracts = int(plan.get("contracts") or plan.get("quantity") or 0)
        if contracts and abs(contracts) < profile.option_min_contracts:
            violations.append(ConstraintViolation(
                name="below_option_min",
                message=f"{broker} min option order is "
                         f"{profile.option_min_contracts} contract(s); got {contracts}",
            ))
        if contracts and abs(contracts) > profile.option_max_contracts:
            violations.append(ConstraintViolation(
                name="above_option_max",
                message=f"{broker} max option order is "
                         f"{profile.option_max_contracts} contracts; got {contracts}",
            ))
        # Multi-leg checks
        legs = plan.get("legs") or []
        if isinstance(legs, list) and len(legs) > profile.max_legs:
            violations.append(ConstraintViolation(
                name="too_many_legs",
                message=f"{broker} supports up to {profile.max_legs} legs; "
                         f"plan has {len(legs)}",
                detail={"max_legs": profile.max_legs, "n_legs": len(legs)},
            ))
        if (instrument == "spread" and legs and not profile.leg_atomicity_supported):
            violations.append(ConstraintViolation(
                name="atomicity_not_supported",
                message=f"{broker} fills spread legs sequentially — risk of "
                         f"partial leg execution. Submit each leg as a single "
                         f"order or switch to a broker with combo orders.",
            ))

    return violations
