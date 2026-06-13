"""Stage-10 item 19 — strike quality scoring (OI + IV neighborhood).

A nominally-correct strike can still be a bad trade if:
  • Open interest is below a liquidity floor → wide bid/ask + slippage risk
  • Implied vol is discontinuous vs neighbors → mispricing / stale quote
  • Volume is near zero → no recent trades to anchor the quote

``score_strike`` returns a 0–1 quality score plus per-factor breakdown so
the engine / UI can show WHY a strike was downgraded. Strategies that pick
strikes through ``snap_strike`` can take the chain-aware path through
``nearest_available_strike`` AND score it here to decide between two
nearby candidates.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class StrikeQuality:
    strike: float
    kind: str                # "call" | "put"
    oi: int = 0
    volume: int = 0
    iv: Optional[float] = None
    iv_neighbor_gap: Optional[float] = None  # max |Δ| between IV and neighbors
    factors: Dict[str, float] = field(default_factory=dict)
    score: float = 0.0       # ∈ [0, 1]; 1.0 = clean, 0.0 = avoid
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── per-factor sub-scores ─────────────────────────────────────────────────


def _oi_score(oi: int, *, min_oi: int = 50, healthy_oi: int = 500) -> float:
    """Below ``min_oi`` → 0; above ``healthy_oi`` → 1; linear in between."""
    if oi <= 0:
        return 0.0
    if oi >= healthy_oi:
        return 1.0
    if oi < min_oi:
        return round(oi / min_oi * 0.3, 3)        # cap below min at 0.30
    span = max(1, healthy_oi - min_oi)
    return round(0.3 + 0.7 * (oi - min_oi) / span, 3)


def _volume_score(volume: int, *, healthy: int = 100) -> float:
    if volume <= 0:
        return 0.0
    if volume >= healthy:
        return 1.0
    return round(volume / healthy, 3)


def _iv_smoothness_score(iv: Optional[float],
                            neighbor_ivs: Sequence[Optional[float]],
                            *, max_gap: float = 0.10) -> float:
    """Smoothness vs neighbors. Returns 1.0 when IV sits within ``max_gap``
    of neighbors; drops linearly as the gap widens."""
    if iv is None:
        return 0.0
    valid_neighbors = [n for n in neighbor_ivs if n is not None]
    if not valid_neighbors:
        return 0.5     # no signal — neutral
    worst_gap = max(abs(iv - n) for n in valid_neighbors)
    if worst_gap >= max_gap:
        return 0.0
    return round(1.0 - worst_gap / max_gap, 3)


def _neighbor_ivs(quotes: Sequence[Dict[str, Any]],
                    strike: float, kind: str, *,
                    window: int = 2) -> List[Optional[float]]:
    """Pick the IV values of the N nearest-strike neighbors of the same
    kind. Excludes the target strike itself."""
    same_kind = [q for q in quotes if q.get("kind") == kind
                   and q.get("strike") != strike]
    sorted_q = sorted(same_kind, key=lambda q: abs(float(q.get("strike", 0)) - strike))
    return [q.get("iv") for q in sorted_q[:window * 2]]


# ── public scorer ────────────────────────────────────────────────────────


def score_strike(
    quotes: Sequence[Dict[str, Any]],
    *,
    strike: float,
    kind: str,
    expiration: Optional[str] = None,
) -> StrikeQuality:
    """Compose an OI × volume × IV-smoothness score for ``strike``."""
    pool = list(quotes)
    if expiration:
        pool = [q for q in pool if q.get("expiration") == expiration]
    target = next(
        (q for q in pool
          if q.get("strike") == strike and q.get("kind") == kind),
        None,
    )
    if target is None:
        return StrikeQuality(
            strike=strike, kind=kind, score=0.0,
            notes=[f"strike {strike} {kind} not in chain"],
        )
    oi = int(target.get("open_interest") or 0)
    vol = int(target.get("volume") or 0)
    iv = target.get("iv")

    neighbor_ivs = _neighbor_ivs(pool, strike, kind)
    iv_score = _iv_smoothness_score(iv, neighbor_ivs)
    oi_s = _oi_score(oi)
    vol_s = _volume_score(vol)
    # Composite: weight liquidity (OI+vol) more than smoothness; the
    # smoothness signal is noisier and we only really want to PENALIZE
    # discontinuities, not reward perfection.
    composite = round(0.45 * oi_s + 0.30 * vol_s + 0.25 * iv_score, 3)

    notes: List[str] = []
    if oi_s < 0.30:
        notes.append(f"low open interest ({oi})")
    if vol_s < 0.10:
        notes.append(f"low volume ({vol})")
    if iv_score < 0.20:
        notes.append(
            f"IV {iv} discontinuous vs neighbors "
            f"(gap {max((abs(iv - n) for n in neighbor_ivs if n is not None), default=0):.3f})"
            if iv is not None else "no IV recorded"
        )
    if not notes:
        notes.append("clean strike — passes all quality checks")

    worst_gap = (max((abs(iv - n) for n in neighbor_ivs if n is not None),
                       default=0) if iv is not None else None)
    return StrikeQuality(
        strike=float(strike), kind=kind, oi=oi, volume=vol, iv=iv,
        iv_neighbor_gap=round(worst_gap, 4) if worst_gap is not None else None,
        factors={"oi": oi_s, "volume": vol_s, "iv_smoothness": iv_score},
        score=composite, notes=notes,
    )
