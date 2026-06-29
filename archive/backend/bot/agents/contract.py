"""Stage-20a — Master agent contract surface.

Defines the structured data types every council agent emits and the
invariants the consensus engine enforces. The point is to make the
panel "think consistently, not harder": every agent answers the same
question with the same shape, so the Chairman (Stage-20b) can reweight,
reconcile, and surface dissent without inventing new reasoning.

Contract rules (enforced in ``AgentVote.__post_init__`` when
``reasoning_type`` is one of the new structured values):

  1. ``reasoning_type == "insufficient_signal"`` ⇒ ``key_drivers == []``
     AND ``stance == STANCE_ABSTAIN``
  2. ``reasoning_type ∈ {"contributing", "dissenting"}`` ⇒
     ``len(key_drivers) ≥ 1``
  3. ``confidence ≥ min_confidence_for_contribution`` AND
     ``key_drivers == []`` ⇒ ContractViolation (raises)
  4. ``stance != STANCE_ABSTAIN`` AND
     ``reasoning_type == "insufficient_signal"`` ⇒ ContractViolation

Legacy votes (pre-20a callers, tests with positional args) default to
``reasoning_type = "legacy"`` and bypass these checks — invariants only
fire for opt-in 20a-shaped votes. The Chairman in 20b will refuse to
operate on legacy votes; production agents in this module emit the
structured form.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


# ── source taxonomy ─────────────────────────────────────────────────────


# Nine source categories every KeyDriver MUST cite. The Chairman uses
# the union of categories across all contributing/dissenting agents to
# compute Jaccard overlap — five agents citing the same one category
# is a single signal, not five.
SOURCE_CATEGORIES = (
    "macro_liquidity",        # NFCI, fed funds rate change, financial conditions
    "credit",                 # HY OAS, IG spreads, credit stress
    "breadth",                # % above 50/200dma, advance/decline, breadth verdict
    "positioning",            # COT noncommercial net, dealer regime, sentiment
    "volatility",             # VIX, IV rank, vol phase
    "fundamentals",           # earnings call intel, guidance, margins
    "insider_flow",           # Form 4 burst, insider activity
    "price_structure",        # regime trend, momentum, levels
    "microstructure_flow",    # options flow, volume, spread, short interest
    "portfolio_state",        # OUR book: drawdown, theme heat, concentration, cohort
)


# ── reasoning-type taxonomy ─────────────────────────────────────────────


REASONING_CONTRIBUTING = "contributing"      # agent has evidence and votes for/against
REASONING_DISSENTING = "dissenting"          # agent counter-argues the consensus direction
REASONING_INSUFFICIENT_SIGNAL = "insufficient_signal"  # agent lacks evidence; abstains
REASONING_LEGACY = "legacy"                  # pre-Stage-20a vote, contract not applicable

REASONING_TYPES = (
    REASONING_CONTRIBUTING,
    REASONING_DISSENTING,
    REASONING_INSUFFICIENT_SIGNAL,
    REASONING_LEGACY,
)

STRUCTURED_REASONING_TYPES = (
    REASONING_CONTRIBUTING,
    REASONING_DISSENTING,
    REASONING_INSUFFICIENT_SIGNAL,
)


# ── direction taxonomy ──────────────────────────────────────────────────


DIRECTION_LONG = "supports_long"
DIRECTION_SHORT = "supports_short"
DIRECTION_ABSTAIN = "supports_abstain"

KEY_DRIVER_DIRECTIONS = (DIRECTION_LONG, DIRECTION_SHORT, DIRECTION_ABSTAIN)


# ── risk level ──────────────────────────────────────────────────────────


RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"
RISK_UNKNOWN = "UNKNOWN"

RISK_LEVELS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_UNKNOWN)


# ── errors ──────────────────────────────────────────────────────────────


class ContractViolation(ValueError):
    """Raised when an AgentVote violates the Stage-20a contract."""


# ── dataclasses ─────────────────────────────────────────────────────────


@dataclass
class KeyDriver:
    """One piece of structured evidence behind an agent's vote.

    Every contributing/dissenting agent must produce ≥ 1 driver. The
    Chairman aggregates drivers across the panel — by category for
    overlap, by direction for net bias, by ``time_sensitive`` for
    "why now" reasoning.
    """

    description: str               # short plain-English label, e.g. "HY spread 4.2% rising"
    source_category: str           # MUST be in SOURCE_CATEGORIES
    direction: str                 # one of KEY_DRIVER_DIRECTIONS
    weight: float = 0.5            # magnitude in [0, 1]
    time_sensitive: bool = False   # True if the driver decays in < 1 trading day

    def __post_init__(self) -> None:
        if self.source_category not in SOURCE_CATEGORIES:
            raise ContractViolation(
                f"KeyDriver.source_category must be one of {SOURCE_CATEGORIES}, "
                f"got {self.source_category!r}"
            )
        if self.direction not in KEY_DRIVER_DIRECTIONS:
            raise ContractViolation(
                f"KeyDriver.direction must be one of {KEY_DRIVER_DIRECTIONS}, "
                f"got {self.direction!r}"
            )
        try:
            w = float(self.weight)
        except (TypeError, ValueError) as exc:
            raise ContractViolation(
                f"KeyDriver.weight must be numeric, got {self.weight!r}") from exc
        if not (0.0 <= w <= 1.0):
            raise ContractViolation(
                f"KeyDriver.weight must be in [0, 1], got {w}")
        self.weight = w

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── invariant enforcement ──────────────────────────────────────────────


def enforce_vote_contract(
    *,
    reasoning_type: str,
    stance: str,
    confidence: float,
    key_drivers: List[KeyDriver],
    abstain_stance: str,
    min_confidence_for_contribution: float,
) -> None:
    """Apply the four 20a contract invariants. Pure validator — no side
    effects, raises ``ContractViolation`` on failure.

    Callable in isolation from tests so we can prove every rule.
    """
    if reasoning_type == REASONING_LEGACY:
        return                                                 # opt-out path

    if reasoning_type not in STRUCTURED_REASONING_TYPES:
        raise ContractViolation(
            f"reasoning_type must be one of {REASONING_TYPES}, "
            f"got {reasoning_type!r}"
        )

    # Invariant 1: insufficient_signal ⇒ empty key_drivers AND stance == abstain
    if reasoning_type == REASONING_INSUFFICIENT_SIGNAL:
        if key_drivers:
            raise ContractViolation(
                "insufficient_signal votes must have no key_drivers; "
                f"got {len(key_drivers)}"
            )
        if stance != abstain_stance:
            # Invariant 4
            raise ContractViolation(
                f"insufficient_signal votes must have stance={abstain_stance!r}; "
                f"got {stance!r}"
            )
        return

    # Invariant 2: contributing/dissenting ⇒ at least one key_driver
    if reasoning_type in (REASONING_CONTRIBUTING, REASONING_DISSENTING):
        if not key_drivers:
            raise ContractViolation(
                f"{reasoning_type} votes must have at least 1 key_driver; got 0"
            )

    # Invariant 3: confidence above threshold without evidence is a violation
    if confidence >= min_confidence_for_contribution and not key_drivers:
        raise ContractViolation(
            f"confidence {confidence:.2f} >= "
            f"{min_confidence_for_contribution:.2f} requires key_drivers; "
            "empty drivers + high confidence is forbidden"
        )
