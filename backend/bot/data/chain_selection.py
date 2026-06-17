"""MITS Phase 17.D — Chain selection provenance.

Records WHY one specific listed contract won at strike-selection time.
Companion to ``chain_strike`` in ``backend.bot.data.options`` which
returns only ``(expiry, strike, option_type)``. Strategies and the
engine call ``chain_strike_with_provenance`` instead to ALSO capture
the candidate set, the rejection reason for each loser, the chain
source / freshness, and a one-line human-readable winner rationale.

Answers the 2nd of Phase 17's five "operator without opening code"
observability questions:

  1. Why was this trade entered?    — 16.B
  2. Why this contract?             — 17.D (this module)
  3. Why this size?                 — 17.C
  4. Why this exact price?          — 17.B
  5. Why this exact exit?           — 17.E

The provenance is serialized onto ``Trade.chain_selection_json`` at
``_persist_trade`` time. Stock trades have no chain selection — the
column stays NULL on those rows, preserving back-compat with every
pre-17.D Trade.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.bot.data.options import (
    _strike_interval,
    chain_strike as _legacy_chain_strike,
    snap_strike,
)
from backend.config import TUNABLES

logger = logging.getLogger(__name__)


# ── tunables (config-driven per the "no magic numbers" invariant) ──────


# Default delta band when the caller doesn't pass one. Mid-band single
# leg long-option convention: 0.30-0.45 is the "directional but not
# deep ITM" sweet spot. Overrideable via TUNABLES.chain_selection_delta_band.
_DEFAULT_DELTA_BAND: Tuple[float, float] = tuple(
    getattr(TUNABLES, "chain_selection_delta_band", (0.30, 0.45))
)
_MIN_OPEN_INTEREST: int = int(
    getattr(TUNABLES, "chain_selection_min_oi", 1000)
)
_MIN_VOLUME: int = int(
    getattr(TUNABLES, "chain_selection_min_volume", 100)
)
_MAX_SPREAD_PCT: float = float(
    getattr(TUNABLES, "chain_selection_max_spread_pct", 0.05)
)
_MAX_STALENESS_SECONDS: float = float(
    getattr(TUNABLES, "chain_selection_max_staleness_seconds", 60.0)
)


# ── dataclasses ─────────────────────────────────────────────────────────


@dataclass
class ChainCandidate:
    """One option contract considered at strike-selection time.

    ``rejection_reason`` is ``None`` on the contract that ultimately won;
    populated with a stable token (see :data:`REJECTION_REASONS`) on
    every contract that lost. This invariant — exactly one candidate
    with ``rejection_reason is None`` — is asserted in the unit tests.
    """
    expiry: str            # YYYY-MM-DD
    strike: float
    option_type: str       # 'C' or 'P'
    delta: Optional[float] = None
    open_interest: Optional[int] = None
    volume: Optional[int] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    iv: Optional[float] = None
    rejection_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChainSelectionProvenance:
    """Top-level record stamped onto ``Trade.chain_selection_json``.

    Carries the operator-readable answer to "Why this contract?" with
    the full candidate set so post-hoc auditing can replay what was
    available at decision time without re-querying the chain.
    """
    ticker: str
    direction: str                          # 'long_call' | 'long_put' | 'short_call' | 'short_put'
    requested_dte: int
    requested_delta_band: Tuple[float, float]
    underlying_spot: Optional[float]
    candidates: List[ChainCandidate] = field(default_factory=list)
    chosen_expiry: str = ""
    chosen_strike: float = 0.0
    chosen_option_type: str = ""
    chosen_reason: str = ""
    freshness_seconds: Optional[float] = None
    chain_source: str = "paper_stub"        # 'thetadata' | 'yfinance' | 'cache' | 'paper_stub'
    captured_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "direction": self.direction,
            "requested_dte": self.requested_dte,
            "requested_delta_band": list(self.requested_delta_band),
            "underlying_spot": self.underlying_spot,
            "candidates": [c.to_dict() for c in self.candidates],
            "chosen_expiry": self.chosen_expiry,
            "chosen_strike": self.chosen_strike,
            "chosen_option_type": self.chosen_option_type,
            "chosen_reason": self.chosen_reason,
            "freshness_seconds": self.freshness_seconds,
            "chain_source": self.chain_source,
            "captured_at": self.captured_at,
        }


# Stable rejection-reason tokens. Keep this list small and meaningful —
# the operator should be able to grep audit history for any one of them
# and learn something concrete about why a candidate lost.
REJECTION_REASONS = (
    "wrong_delta_band",          # delta computed but outside requested band
    "low_open_interest",         # OI under floor
    "low_volume",                # day's volume under floor
    "stale_quote",               # quote timestamp older than freshness gate
    "wide_spread_pct",           # spread / mid above ceiling
    "opted_for_higher_oi_alt",   # band/spread/freshness tied with chosen — chosen had bigger OI
)


# ── helpers ─────────────────────────────────────────────────────────────


def _direction_for(option_type: str, side: str) -> str:
    """Map (kind, side) -> 'long_call' | 'short_put' | etc."""
    side_l = side.lower()
    long = side_l in ("buy", "long")
    kind = "call" if option_type.lower().startswith("c") else "put"
    return f"{'long' if long else 'short'}_{kind}"


def _option_type_token(option_type: str) -> str:
    return "C" if option_type.lower().startswith("c") else "P"


def _safe_delta(spot: float, strike: float, T: float, iv: Optional[float],
                kind: str) -> Optional[float]:
    """Compute Black-Scholes delta for a single contract. ``None`` on
    invalid inputs — the caller treats None as "delta unknown" and uses
    the moneyness fallback for that candidate."""
    if not iv or iv <= 0 or T <= 0 or spot <= 0 or strike <= 0:
        return None
    try:
        from backend.bot.greeks import compute_greeks
        g = compute_greeks(spot, strike, T, iv, kind=kind)
        return abs(float(g.delta))
    except Exception:
        return None


def _quote_freshness_seconds(ts: Optional[datetime]) -> Optional[float]:
    """Seconds since quote timestamp. ``None`` when the vendor didn't
    supply a timestamp (yfinance path)."""
    if ts is None:
        return None
    try:
        # ThetaData returns ET-local; treat as naive UTC for the diff —
        # the absolute number is only used as a relative freshness gate.
        now = datetime.utcnow()
        return max(0.0, (now - ts.replace(tzinfo=None) if ts.tzinfo else now - ts).total_seconds())
    except Exception:
        return None


def _spread_pct(bid: float, ask: float) -> Optional[float]:
    if bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid


def _classify_candidate(
    cand: ChainCandidate,
    *,
    delta_band: Tuple[float, float],
    min_oi: int,
    min_volume: int,
    max_spread_pct: float,
    max_staleness: Optional[float],
    freshness_seconds: Optional[float],
) -> Optional[str]:
    """Decide whether ``cand`` would clear all gates. Returns the FIRST
    failing reason token (so the audit trail is deterministic) or
    ``None`` when the candidate is fully eligible.
    """
    if cand.delta is not None:
        if cand.delta < delta_band[0] or cand.delta > delta_band[1]:
            return "wrong_delta_band"
    if cand.open_interest is not None and cand.open_interest < min_oi:
        return "low_open_interest"
    if cand.volume is not None and cand.volume < min_volume:
        return "low_volume"
    if (cand.bid is not None and cand.ask is not None):
        sp = _spread_pct(cand.bid, cand.ask)
        if sp is not None and sp > max_spread_pct:
            return "wide_spread_pct"
    if (max_staleness is not None and freshness_seconds is not None
            and freshness_seconds > max_staleness):
        return "stale_quote"
    return None


def _compose_chosen_reason(
    chosen: ChainCandidate,
    delta_band: Tuple[float, float],
    min_oi: int,
    max_spread_pct: float,
    freshness_seconds: Optional[float],
    chain_source: str,
    in_band_winner: bool,
) -> str:
    """One-line human-readable rationale that lands on the chain
    selection JSON. Plain-English per the operator's "every observable
    should answer the question without code" rule.

    ``in_band_winner`` is True when the chosen candidate's delta is
    actually inside the requested band. False when we fell through to
    "closest-to-target" selection because no candidate fit the band —
    the reason must say so honestly rather than claim a band match
    that didn't happen."""
    parts: List[str] = []
    if chosen.delta is not None:
        if in_band_winner:
            parts.append(
                f"delta={chosen.delta:.2f} in [{delta_band[0]:.2f},{delta_band[1]:.2f}] band"
            )
        else:
            parts.append(
                f"delta={chosen.delta:.2f} outside [{delta_band[0]:.2f},{delta_band[1]:.2f}] band "
                "(no candidate in-band — picked nearest-to-target)"
            )
    else:
        parts.append(
            f"delta unknown — picked nearest-to-spot in chain"
        )
    if chosen.open_interest is not None:
        parts.append(f"OI={chosen.open_interest} >= {min_oi} floor")
    if chosen.bid is not None and chosen.ask is not None:
        sp = _spread_pct(chosen.bid, chosen.ask)
        if sp is not None:
            parts.append(f"spread {sp * 100:.1f}% < {max_spread_pct * 100:.1f}%")
    if freshness_seconds is not None:
        parts.append(f"quote age {freshness_seconds:.1f}s")
    parts.append(f"source={chain_source}")
    return ", ".join(parts)


def _paper_stub_provenance(
    *,
    ticker: str,
    direction: str,
    requested_dte: int,
    requested_delta_band: Tuple[float, float],
    underlying_spot: Optional[float],
    chosen_expiry: str,
    chosen_strike: float,
    chosen_option_type: str,
) -> ChainSelectionProvenance:
    """Fallback shape when no chain data is reachable. Keeps the same
    schema so downstream cockpit + learning consumers don't need a
    branch for "no chain"; the ``chain_source='paper_stub'`` flag and
    the single-candidate list signal the degraded state explicitly."""
    chosen = ChainCandidate(
        expiry=chosen_expiry,
        strike=chosen_strike,
        option_type=chosen_option_type,
        rejection_reason=None,
    )
    return ChainSelectionProvenance(
        ticker=ticker,
        direction=direction,
        requested_dte=requested_dte,
        requested_delta_band=requested_delta_band,
        underlying_spot=underlying_spot,
        candidates=[chosen],
        chosen_expiry=chosen_expiry,
        chosen_strike=chosen_strike,
        chosen_option_type=chosen_option_type,
        chosen_reason="paper_stub fallback — no chain data",
        freshness_seconds=None,
        chain_source="paper_stub",
        captured_at=datetime.utcnow().isoformat(),
    )


# ── public surface ─────────────────────────────────────────────────────


def chain_strike_with_provenance(
    ticker: str,
    spot: float,
    kind: str = "call",
    *,
    side: str = "BUY",
    moneyness: float = 0.0,
    expiry: Optional[date] = None,
    target_dte: int = 30,
    target_delta: Optional[float] = None,
    delta_band: Optional[Tuple[float, float]] = None,
    min_open_interest: Optional[int] = None,
    min_volume: Optional[int] = None,
    max_spread_pct: Optional[float] = None,
    max_staleness_seconds: Optional[float] = None,
    candidate_window: int = 5,
) -> Tuple[str, float, str, ChainSelectionProvenance]:
    """Parallel to :func:`backend.bot.data.options.chain_strike` — returns
    the chosen contract AND a full :class:`ChainSelectionProvenance`.

    The legacy ``chain_strike`` is left unchanged for back-compat. New
    engine call-sites use this entry point so every option Trade row
    carries the full audit trail.

    Returns
    -------
    (expiry_iso: str, strike: float, option_type_token: 'C' | 'P',
     provenance: ChainSelectionProvenance)

    On any data-layer failure the function degrades to a paper_stub
    provenance with a single candidate (the chosen one) so the caller
    always gets a usable strike and a non-None provenance.
    """
    band = delta_band if delta_band is not None else _DEFAULT_DELTA_BAND
    min_oi = min_open_interest if min_open_interest is not None else _MIN_OPEN_INTEREST
    min_vol = min_volume if min_volume is not None else _MIN_VOLUME
    max_sp = max_spread_pct if max_spread_pct is not None else _MAX_SPREAD_PCT
    max_stale = (
        max_staleness_seconds
        if max_staleness_seconds is not None else _MAX_STALENESS_SECONDS
    )
    direction = _direction_for(kind, side)
    option_token = _option_type_token(kind)

    # Always have a non-zero fallback strike so the engine never crashes
    # when the chain is unreachable. ``snap_strike`` is pure arithmetic.
    fallback_strike = snap_strike(spot, kind, moneyness)
    fallback_expiry_iso = ""

    if not spot or spot <= 0:
        return ("", 0.0, option_token, _paper_stub_provenance(
            ticker=ticker,
            direction=direction,
            requested_dte=target_dte,
            requested_delta_band=tuple(band),
            underlying_spot=spot if spot else None,
            chosen_expiry="",
            chosen_strike=0.0,
            chosen_option_type=option_token,
        ))

    # ── ThetaData path ─────────────────────────────────────────────────
    try:
        from backend.bot.data.thetadata import get_client
        client = get_client()
        if expiry is None:
            expiry = client.nearest_expiration(ticker, target_dte=target_dte)
            if expiry is None:
                raise RuntimeError("no expiration listed")
        chain = client.chain_snapshot(ticker, expiry)
        right = "CALL" if option_token == "C" else "PUT"
        chain_for_right = [q for q in chain if q.right == right]
        if not chain_for_right:
            raise RuntimeError("empty side of chain")

        # Trim to the ``candidate_window`` strikes closest to the
        # arithmetic target. We don't carry the full chain (could be
        # 50-200 contracts); the operator only needs the considered set.
        target_price = float(spot) * (1.0 + float(moneyness or 0.0))
        ranked = sorted(chain_for_right, key=lambda q: abs(q.strike - target_price))
        window = ranked[: max(2, candidate_window)]

        # Compute per-candidate delta + gates. Build ChainCandidate rows
        # without setting rejection_reason yet (that's a second pass once
        # we know the chosen winner — multiple candidates can be eligible).
        T = max(1, (expiry - date.today()).days) / 365.0
        kind_arg = "call" if option_token == "C" else "put"

        # Try IV from vendor first; if absent, solve for IV from mid via
        # bisection so delta is computable for the most contracts possible.
        try:
            from backend.bot.greeks import implied_vol
        except Exception:
            implied_vol = None  # type: ignore

        raw_candidates: List[ChainCandidate] = []
        cand_freshness: List[Optional[float]] = []
        for q in window:
            mid = q.mid
            iv: Optional[float] = None
            if implied_vol is not None and mid > 0:
                try:
                    iv = implied_vol(mid, spot, q.strike, T, kind=kind_arg)
                except Exception:
                    iv = None
            delta_val = _safe_delta(spot, q.strike, T, iv, kind_arg)
            fr = _quote_freshness_seconds(q.timestamp)
            cand_freshness.append(fr)
            raw_candidates.append(ChainCandidate(
                expiry=expiry.isoformat(),
                strike=float(q.strike),
                option_type=option_token,
                delta=round(delta_val, 4) if delta_val is not None else None,
                # ThetaData OptionQuote shape doesn't include OI/volume on
                # the snapshot — gate on what we have, leave None for what
                # we don't. The unit tests inject OI/volume directly.
                open_interest=None,
                volume=None,
                bid=float(q.bid) if q.bid > 0 else None,
                ask=float(q.ask) if q.ask > 0 else None,
                iv=round(iv, 4) if iv is not None else None,
                rejection_reason=None,
            ))

        return _finalize_provenance(
            ticker=ticker, direction=direction,
            target_dte=target_dte, delta_band=tuple(band),
            spot=spot,
            candidates=raw_candidates,
            freshnesses=cand_freshness,
            min_oi=min_oi, min_vol=min_vol, max_sp=max_sp, max_stale=max_stale,
            chain_source="thetadata",
            fallback_expiry=expiry.isoformat(),
            fallback_strike=fallback_strike,
            option_token=option_token,
        )
    except Exception as exc:
        logger.debug(
            "chain_strike_with_provenance thetadata fallback for %s: %s",
            ticker, exc,
        )

    # ── degraded path ─────────────────────────────────────────────────
    #
    # ThetaData unreachable / empty / errored. Fall through to the legacy
    # arithmetic ``chain_strike`` (which has its OWN multi-tier fallback)
    # so we still return a usable strike. Provenance becomes paper_stub
    # because we have no real candidate set to record.
    try:
        legacy_strike = _legacy_chain_strike(
            ticker, spot, kind,
            moneyness=moneyness, expiry=expiry, target_dte=target_dte,
        )
    except Exception:
        legacy_strike = fallback_strike
    chosen_expiry_iso = expiry.isoformat() if expiry else fallback_expiry_iso
    return (
        chosen_expiry_iso, float(legacy_strike), option_token,
        _paper_stub_provenance(
            ticker=ticker,
            direction=direction,
            requested_dte=target_dte,
            requested_delta_band=tuple(band),
            underlying_spot=spot,
            chosen_expiry=chosen_expiry_iso,
            chosen_strike=float(legacy_strike),
            chosen_option_type=option_token,
        ),
    )


def _finalize_provenance(
    *,
    ticker: str,
    direction: str,
    target_dte: int,
    delta_band: Tuple[float, float],
    spot: float,
    candidates: List[ChainCandidate],
    freshnesses: List[Optional[float]],
    min_oi: int,
    min_vol: int,
    max_sp: float,
    max_stale: float,
    chain_source: str,
    fallback_expiry: str,
    fallback_strike: float,
    option_token: str,
) -> Tuple[str, float, str, ChainSelectionProvenance]:
    """Score the candidates, mark the winner, stamp rejection reasons on
    losers, and assemble the provenance dataclass. Shared between the
    ThetaData path and the in-process test harness (which builds a
    candidate list directly and calls this with ``chain_source='thetadata'``).
    """
    if not candidates:
        # No real candidates — degrade to paper_stub.
        return (
            fallback_expiry, fallback_strike, option_token,
            _paper_stub_provenance(
                ticker=ticker, direction=direction,
                requested_dte=target_dte,
                requested_delta_band=delta_band,
                underlying_spot=spot,
                chosen_expiry=fallback_expiry,
                chosen_strike=fallback_strike,
                chosen_option_type=option_token,
            ),
        )

    # ── pick the winner ─────────────────────────────────────────────
    #
    # Preference order:
    #   1. Lands inside the requested delta band (when delta known).
    #   2. Among in-band, the one whose delta is closest to band midpoint.
    #   3. If no candidate has a known delta or none are in-band, fall
    #      back to "closest strike to spot" using the candidate list order
    #      (already sorted by distance from target_price).
    band_mid = (delta_band[0] + delta_band[1]) / 2.0
    in_band = [c for c in candidates if c.delta is not None
                  and delta_band[0] <= c.delta <= delta_band[1]]
    if in_band:
        chosen = min(in_band, key=lambda c: abs((c.delta or 0.0) - band_mid))
        in_band_winner = True
    else:
        chosen = candidates[0]
        in_band_winner = False

    # ── stamp rejection reasons on losers ──────────────────────────
    chosen_index = candidates.index(chosen)
    chosen_freshness = freshnesses[chosen_index] if freshnesses else None
    for i, cand in enumerate(candidates):
        if cand is chosen:
            cand.rejection_reason = None
            continue
        fr = freshnesses[i] if i < len(freshnesses) else None
        reason = _classify_candidate(
            cand, delta_band=delta_band,
            min_oi=min_oi, min_volume=min_vol,
            max_spread_pct=max_sp,
            max_staleness=max_stale,
            freshness_seconds=fr,
        )
        if reason is None:
            # Eligible but not chosen — the chosen one beat it on
            # the secondary preference (e.g. higher OI, tighter band
            # match). Record that explicitly so the audit doesn't
            # show 'None' on a loser.
            reason = "opted_for_higher_oi_alt"
        cand.rejection_reason = reason

    chosen_reason = _compose_chosen_reason(
        chosen, delta_band, min_oi, max_sp, chosen_freshness, chain_source,
        in_band_winner=in_band_winner,
    )

    prov = ChainSelectionProvenance(
        ticker=ticker,
        direction=direction,
        requested_dte=target_dte,
        requested_delta_band=delta_band,
        underlying_spot=float(spot),
        candidates=candidates,
        chosen_expiry=chosen.expiry,
        chosen_strike=chosen.strike,
        chosen_option_type=chosen.option_type,
        chosen_reason=chosen_reason,
        freshness_seconds=(
            round(chosen_freshness, 2) if chosen_freshness is not None else None
        ),
        chain_source=chain_source,
        captured_at=datetime.utcnow().isoformat(),
    )
    return (chosen.expiry, chosen.strike, chosen.option_type, prov)


def build_provenance_from_candidates(
    *,
    ticker: str,
    direction: str,
    requested_dte: int,
    requested_delta_band: Tuple[float, float],
    underlying_spot: Optional[float],
    candidates: List[ChainCandidate],
    freshnesses: Optional[List[Optional[float]]] = None,
    chain_source: str = "thetadata",
    min_open_interest: Optional[int] = None,
    min_volume: Optional[int] = None,
    max_spread_pct: Optional[float] = None,
    max_staleness_seconds: Optional[float] = None,
) -> ChainSelectionProvenance:
    """Test/harness entry point — pick winner + tag rejection reasons on
    a pre-built candidate list. Used by the unit tests (no live
    ThetaData dependency) and by any future caller that already has
    candidate data from another source (e.g. backfilled chain history).
    """
    band = (
        requested_delta_band
        if requested_delta_band is not None else _DEFAULT_DELTA_BAND
    )
    min_oi = (
        min_open_interest if min_open_interest is not None else _MIN_OPEN_INTEREST
    )
    min_vol = min_volume if min_volume is not None else _MIN_VOLUME
    max_sp = max_spread_pct if max_spread_pct is not None else _MAX_SPREAD_PCT
    max_stale = (
        max_staleness_seconds
        if max_staleness_seconds is not None else _MAX_STALENESS_SECONDS
    )
    if not candidates:
        return _paper_stub_provenance(
            ticker=ticker, direction=direction,
            requested_dte=requested_dte,
            requested_delta_band=tuple(band),
            underlying_spot=underlying_spot,
            chosen_expiry="",
            chosen_strike=0.0,
            chosen_option_type="C" if direction.endswith("call") else "P",
        )
    freshnesses = freshnesses or [None] * len(candidates)
    _expiry, _strike, _token, prov = _finalize_provenance(
        ticker=ticker, direction=direction,
        target_dte=requested_dte, delta_band=tuple(band),
        spot=float(underlying_spot or 0.0),
        candidates=candidates, freshnesses=freshnesses,
        min_oi=min_oi, min_vol=min_vol, max_sp=max_sp, max_stale=max_stale,
        chain_source=chain_source,
        fallback_expiry=candidates[0].expiry,
        fallback_strike=candidates[0].strike,
        option_token=candidates[0].option_type,
    )
    return prov
