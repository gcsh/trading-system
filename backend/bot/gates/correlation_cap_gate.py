"""MITS Phase 14.B — Correlation-cap gate.

Refuses to open a fresh position whose return profile already lives in
the book. Two block conditions:

  1. Max |rho| against any existing SAME-DIRECTION position is at or
     above ``TUNABLES.correlation_cap_rho`` (default 0.85). The
     opposite-direction case is a hedge, not a duplicate, so it passes.

  2. The candidate's sector exposure would exceed
     ``TUNABLES.cluster_max_exposure`` (default 0.50) once we add the
     proposed trade.

Returns a verdict dataclass; the engine reads ``blocked`` and emits an
event when set.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from backend.bot.portfolio_intel import sector_of
from backend.bot.portfolio_intel.portfolio_context import (
    PortfolioContext,
    _position_direction,
)
from backend.config import TUNABLES


@dataclass
class CorrelationCapResult:
    blocked: bool
    reason: str
    worst_peer: Optional[str]
    worst_rho: float
    candidate_direction: str
    # MITS Phase 16.C — continuous-sizing extension. ``hard_block``
    # mirrors ``blocked`` (kept for explicit semantics in code that
    # discriminates the two). ``sizing_multiplier`` is the size haircut
    # the engine applies when the candidate sits in the soft-cap zone.
    sizing_multiplier: float = 1.0
    hard_block: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _normalize_direction(direction: str) -> str:
    """Map any action label / direction string to LONG or SHORT."""
    d = (direction or "").upper().strip()
    if d in {"LONG", "BUY", "BUY_CALL", "BUY_PUT_SPREAD",
             "BUY_CALL_SPREAD", "BUY_DEBIT_CALL_SPREAD"}:
        return "LONG"
    if d in {"SHORT", "SELL", "SELL_SHORT", "BUY_PUT", "SHORT_CALL",
             "SELL_CALL", "BUY_PUT_DEBIT"}:
        return "SHORT"
    # BUY_CALL → LONG, BUY_PUT → SHORT (already covered above);
    # everything else defaults to LONG so we err on the side of
    # applying the cap rather than letting an unknown action sneak in.
    if d.startswith("BUY_PUT"):
        return "SHORT"
    if d.startswith("BUY"):
        return "LONG"
    if d.startswith("SELL"):
        return "SHORT"
    return "LONG"


def check_correlation_cap(
    *,
    candidate_ticker: str,
    candidate_direction: str,
    portfolio_context: PortfolioContext,
    positions: Optional[list] = None,
    rho_threshold: Optional[float] = None,
    sector_cap: Optional[float] = None,
) -> CorrelationCapResult:
    """Evaluate the correlation cap + sector cap for ``candidate_ticker``.

    ``positions`` is the raw open-positions list (same shape the engine
    passes to ``build_portfolio_context``). It's used to look up the
    DIRECTION of each peer so we only block on same-direction pile-ups.
    """
    rho_thr = float(
        rho_threshold if rho_threshold is not None
        else TUNABLES.correlation_cap_rho
    )
    sec_cap = float(
        sector_cap if sector_cap is not None
        else TUNABLES.cluster_max_exposure
    )

    candidate_upper = candidate_ticker.upper()
    cand_dir = _normalize_direction(candidate_direction)

    # Build a ticker → direction map from the live positions so we can
    # distinguish hedge from pile-up.
    peer_direction: Dict[str, str] = {}
    for p in positions or []:
        tk = (p.get("ticker") or "").upper()
        if not tk:
            continue
        peer_direction[tk] = _position_direction(p)

    # Same-direction max |rho| sweep.
    worst_peer: Optional[str] = None
    worst_rho: float = 0.0
    candidate_rhos = (portfolio_context.pairwise_correlation or {}).get(
        candidate_upper, {}
    )
    for peer, rho in candidate_rhos.items():
        if peer == candidate_upper:
            continue
        pdir = peer_direction.get(peer)
        if pdir is None:
            continue
        if pdir != cand_dir:
            # Opposite direction = hedge, not pile-up.
            continue
        try:
            mag = abs(float(rho))
        except (TypeError, ValueError):
            continue
        if mag > abs(worst_rho):
            worst_rho = float(rho)
            worst_peer = peer

    if worst_peer is not None and abs(worst_rho) >= rho_thr:
        # MITS Phase 16.C — hard block keeps 14.B semantics intact
        # (blocked=True, sizing_multiplier=0). hard_block mirrors blocked
        # so callers can disambiguate later if the soft-cap and hard-cap
        # paths ever fork further.
        return CorrelationCapResult(
            blocked=True,
            hard_block=True,
            sizing_multiplier=0.0,
            reason=(
                f"correlation cap: |rho|={abs(worst_rho):.2f} vs "
                f"{worst_peer} ({cand_dir}) >= {rho_thr:.2f}"
            ),
            worst_peer=worst_peer,
            worst_rho=round(float(worst_rho), 3),
            candidate_direction=cand_dir,
        )

    # MITS Phase 16.C — soft cap zone: 0.5 < |rho| < rho_thr. Linear
    # interpolation from multiplier=1.0 at |rho|=0.5 down to 0.3 at
    # |rho|=rho_thr. The trade is NOT blocked; the engine multiplies
    # the proposed quantity by sizing_multiplier downstream.
    if worst_peer is not None and abs(worst_rho) > 0.5:
        span = max(rho_thr - 0.5, 1e-6)
        progress = (abs(worst_rho) - 0.5) / span
        multiplier = max(0.3, 1.0 - 0.7 * progress)
        multiplier = round(float(multiplier), 3)
        return CorrelationCapResult(
            blocked=False,
            hard_block=False,
            sizing_multiplier=multiplier,
            reason=(
                f"correlation soft cap: |rho|={abs(worst_rho):.2f} vs "
                f"{worst_peer} ({cand_dir}) → size×{multiplier:.2f}"
            ),
            worst_peer=worst_peer,
            worst_rho=round(float(worst_rho), 3),
            candidate_direction=cand_dir,
        )

    # Sector cap: the candidate's sector pct + the candidate's projected
    # weight must not exceed the cap. We don't know the candidate's
    # notional yet so we use the CURRENT sector exposure as a hard
    # ceiling — if we're already at the cap, do not add more.
    cand_sector = sector_of(candidate_upper)
    sector_pct = float(
        (portfolio_context.by_sector or {}).get(cand_sector, 0.0)
    )
    if sector_pct >= sec_cap:
        return CorrelationCapResult(
            blocked=True,
            hard_block=True,
            sizing_multiplier=0.0,
            reason=(
                f"sector cap: {cand_sector} already at "
                f"{sector_pct:.0%} >= {sec_cap:.0%}"
            ),
            worst_peer=worst_peer,
            worst_rho=round(float(worst_rho), 3) if worst_peer else 0.0,
            candidate_direction=cand_dir,
        )

    return CorrelationCapResult(
        blocked=False,
        hard_block=False,
        sizing_multiplier=1.0,
        reason="ok",
        worst_peer=worst_peer,
        worst_rho=round(float(worst_rho), 3) if worst_peer else 0.0,
        candidate_direction=cand_dir,
    )
