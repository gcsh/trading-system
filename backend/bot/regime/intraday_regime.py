"""MITS Phase 7.1 — Intraday Regime Classifier.

Reads a small handful of live tape inputs and labels the current
intraday environment with one of seven states:

  * ``normal``          — statistical layer leads (default)
  * ``trending_up``     — persistent directional move up
  * ``trending_down``   — persistent directional move down
  * ``panic``           — sharp drawdown + spiking VIX
  * ``capitulation``    — panic + crushed breadth + heavy hedging (PCR)
  * ``squeeze``         — sharp bounce off a panic / capitulation low
  * ``chop``            — narrow range + below-average realized vol

The classifier is intentionally simple and rule-based (no ML, no
network beyond the existing market-data adapter). Thresholds live in
``TUNABLES`` so the operator can re-tune without code changes. Pure
function: never raises; returns ``normal`` whenever inputs are missing.

The engine calls ``classify()`` at the top of each cycle, caches the
result for ``intraday_regime_classifier_cache_sec`` seconds inside the
classifier itself, and persists an ``IntradayRegimeEvent`` row on every
state TRANSITION (not every cycle) so the audit trail stays compact.
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.intraday_regime_event import IntradayRegimeEvent

logger = logging.getLogger(__name__)


STATES = (
    "normal",
    "trending_up",
    "trending_down",
    "panic",
    "capitulation",
    "squeeze",
    "chop",
)

# Sector ETFs used for dispersion measurement. Same list as live_tape.
_SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLY", "XLU")


@dataclass
class IntradayRegimeState:
    state: str = "normal"
    severity: str = "low"  # low | medium | high
    spy_pct_change_30m: Optional[float] = None
    vix_spot: Optional[float] = None
    vix_1d_pct_change: Optional[float] = None
    vix_curve_slope: Optional[float] = None
    breadth_ratio: Optional[float] = None
    put_call_ratio: Optional[float] = None
    sector_dispersion: Optional[float] = None
    reasons: List[str] = field(default_factory=list)
    classified_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "severity": self.severity,
            "spy_pct_change_30m": self.spy_pct_change_30m,
            "vix_spot": self.vix_spot,
            "vix_1d_pct_change": self.vix_1d_pct_change,
            "vix_curve_slope": self.vix_curve_slope,
            "breadth_ratio": self.breadth_ratio,
            "put_call_ratio": self.put_call_ratio,
            "sector_dispersion": self.sector_dispersion,
            "reasons": list(self.reasons),
            "classified_at": self.classified_at,
        }


@dataclass
class IntradayRegimeInputs:
    """Raw numeric inputs to the classifier. Public so tests can build
    synthetic snapshots without touching network code."""
    spy_pct_change_30m: Optional[float] = None
    spy_pct_change_60m: Optional[float] = None
    spy_realized_vol_10d: Optional[float] = None
    spy_intraday_realized_vol: Optional[float] = None
    vix_spot: Optional[float] = None
    vix_1d_pct_change: Optional[float] = None
    vix_curve_slope: Optional[float] = None  # negative = backwardation = stress
    breadth_ratio: Optional[float] = None  # advancers / (advancers+decliners)
    put_call_ratio: Optional[float] = None
    sector_dispersion: Optional[float] = None
    prior_state: Optional[str] = None


def _classify_from_inputs(inputs: IntradayRegimeInputs) -> IntradayRegimeState:
    """Pure decision logic. No I/O. Tested directly with synthetic inputs.

    Order matters: capitulation ⊃ panic, squeeze takes priority over
    trending_up when the prior bar was crisis. Chop is the lowest-energy
    fallback before ``normal``.
    """
    spy = inputs.spy_pct_change_30m
    vix = inputs.vix_spot
    vix_chg = inputs.vix_1d_pct_change
    pcr = inputs.put_call_ratio
    breadth = inputs.breadth_ratio
    spy_60 = inputs.spy_pct_change_60m
    realized = inputs.spy_intraday_realized_vol
    realized_10d = inputs.spy_realized_vol_10d
    prior = (inputs.prior_state or "").lower()

    reasons: List[str] = []
    state = "normal"
    severity = "low"

    panic_spy = float(TUNABLES.intraday_regime_panic_spy_30m)
    panic_vix = float(TUNABLES.intraday_regime_panic_vix_level)
    panic_vix_pct = float(TUNABLES.intraday_regime_panic_vix_1d_pct)
    cap_pcr = float(TUNABLES.intraday_regime_capitulation_pcr)
    cap_breadth = float(TUNABLES.intraday_regime_capitulation_breadth)
    sq_spy = float(TUNABLES.intraday_regime_squeeze_spy_30m)
    sq_breadth = float(TUNABLES.intraday_regime_squeeze_breadth)
    trending = float(TUNABLES.intraday_regime_trending_spy_30m)
    chop_60 = float(TUNABLES.intraday_regime_chop_spy_60m_abs)

    # ---- Panic / capitulation ------------------------------------------
    is_panic = (
        spy is not None and spy < panic_spy
        and vix is not None and vix > panic_vix
        and vix_chg is not None and vix_chg > panic_vix_pct
    )
    if is_panic:
        state = "panic"
        severity = "high"
        reasons.append(
            f"SPY 30m={spy:.2f}% < {panic_spy}% AND VIX={vix:.1f} > {panic_vix} "
            f"AND VIX 1d={vix_chg:.1f}% > {panic_vix_pct}%"
        )
        # Capitulation = panic + crushed breadth + heavy put hedging.
        if (pcr is not None and pcr > cap_pcr
                and breadth is not None and breadth < cap_breadth):
            state = "capitulation"
            reasons.append(
                f"PCR={pcr:.2f} > {cap_pcr} + breadth={breadth:.2f} < {cap_breadth}"
            )
        return IntradayRegimeState(
            state=state, severity=severity,
            spy_pct_change_30m=spy, vix_spot=vix,
            vix_1d_pct_change=vix_chg,
            vix_curve_slope=inputs.vix_curve_slope,
            breadth_ratio=breadth, put_call_ratio=pcr,
            sector_dispersion=inputs.sector_dispersion,
            reasons=reasons,
        )

    # ---- Squeeze (post-panic bounce) -----------------------------------
    prior_was_crisis = prior in ("panic", "capitulation")
    breadth_squeeze = (breadth is not None and breadth > sq_breadth)
    if spy is not None and spy > sq_spy and (prior_was_crisis or breadth_squeeze):
        reasons.append(
            f"SPY 30m=+{spy:.2f}% > {sq_spy}%"
            + (" + prior crisis bounce" if prior_was_crisis else "")
            + (f" + breadth {breadth:.2f} > {sq_breadth}"
               if breadth_squeeze else "")
        )
        return IntradayRegimeState(
            state="squeeze", severity="high",
            spy_pct_change_30m=spy, vix_spot=vix,
            vix_1d_pct_change=vix_chg,
            vix_curve_slope=inputs.vix_curve_slope,
            breadth_ratio=breadth, put_call_ratio=pcr,
            sector_dispersion=inputs.sector_dispersion,
            reasons=reasons,
        )

    # ---- Chop (low energy) ---------------------------------------------
    if (spy_60 is not None and abs(spy_60) < chop_60
            and realized is not None and realized_10d is not None
            and realized < realized_10d):
        reasons.append(
            f"|SPY 60m|={abs(spy_60):.2f}% < {chop_60}% AND "
            f"realized vol {realized:.2f} < 10d avg {realized_10d:.2f}"
        )
        return IntradayRegimeState(
            state="chop", severity="low",
            spy_pct_change_30m=spy, vix_spot=vix,
            vix_1d_pct_change=vix_chg,
            vix_curve_slope=inputs.vix_curve_slope,
            breadth_ratio=breadth, put_call_ratio=pcr,
            sector_dispersion=inputs.sector_dispersion,
            reasons=reasons,
        )

    # ---- Trending up / down --------------------------------------------
    if spy is not None and spy > trending:
        reasons.append(f"SPY 30m=+{spy:.2f}% > {trending}%")
        return IntradayRegimeState(
            state="trending_up", severity="medium",
            spy_pct_change_30m=spy, vix_spot=vix,
            vix_1d_pct_change=vix_chg,
            vix_curve_slope=inputs.vix_curve_slope,
            breadth_ratio=breadth, put_call_ratio=pcr,
            sector_dispersion=inputs.sector_dispersion,
            reasons=reasons,
        )
    if spy is not None and spy < -trending:
        reasons.append(f"SPY 30m={spy:.2f}% < -{trending}%")
        return IntradayRegimeState(
            state="trending_down", severity="medium",
            spy_pct_change_30m=spy, vix_spot=vix,
            vix_1d_pct_change=vix_chg,
            vix_curve_slope=inputs.vix_curve_slope,
            breadth_ratio=breadth, put_call_ratio=pcr,
            sector_dispersion=inputs.sector_dispersion,
            reasons=reasons,
        )

    # ---- Default: normal -----------------------------------------------
    return IntradayRegimeState(
        state="normal", severity="low",
        spy_pct_change_30m=spy, vix_spot=vix,
        vix_1d_pct_change=vix_chg,
        vix_curve_slope=inputs.vix_curve_slope,
        breadth_ratio=breadth, put_call_ratio=pcr,
        sector_dispersion=inputs.sector_dispersion,
        reasons=["no crisis / trending / chop signature"],
    )


def _persist_event(prior: str, new_state: IntradayRegimeState) -> None:
    """Persist a transition row. Best-effort — silent on failure."""
    try:
        with session_scope() as s:
            row = IntradayRegimeEvent(
                prior_state=prior or "unknown",
                new_state=new_state.state,
                severity=new_state.severity,
                spy_pct_change_30m=new_state.spy_pct_change_30m,
                vix_spot=new_state.vix_spot,
                vix_curve_slope=new_state.vix_curve_slope,
                breadth_ratio=new_state.breadth_ratio,
                put_call_ratio=new_state.put_call_ratio,
                sector_dispersion=new_state.sector_dispersion,
            )
            s.add(row)
            s.commit()
    except Exception:
        logger.debug("intraday_regime: persist failed", exc_info=True)


class IntradayRegimeClassifier:
    """The engine wires a single instance of this onto itself.

    ``classify()`` reads the live tape via the existing
    ``MarketDataAdapter`` snapshot for SPY (which already carries VIX,
    intraday history, etc.) plus the optional breadth and put-call
    inputs surfaced by ``_collect_inputs``. Returns the cached state
    when called within ``intraday_regime_classifier_cache_sec`` of the
    last computation, so multiple call-sites in a single cycle don't
    each hit yfinance.
    """

    def __init__(self, market_data: Any = None) -> None:
        self.market_data = market_data
        self._cache: Optional[IntradayRegimeState] = None
        self._cache_at: float = 0.0
        self._last_state: str = "unknown"

    def _now(self) -> float:
        return time.time()

    def _collect_inputs(self) -> IntradayRegimeInputs:
        """Best-effort pull of every classifier input. Each subsection
        is independently try/except'd so partial data still produces a
        usable classification (with the missing fields = None)."""
        inputs = IntradayRegimeInputs(prior_state=self._last_state)

        spy_snap: Optional[Dict[str, Any]] = None
        if self.market_data is not None and hasattr(self.market_data, "snapshot"):
            try:
                spy_snap = self.market_data.snapshot("SPY").data
            except Exception:
                spy_snap = None

        if spy_snap:
            try:
                # snapshot exposes intraday returns as `intraday_pct_change`
                # or via change_pct / open vs price. Fall back gracefully.
                spy_pct = spy_snap.get("intraday_30m_pct") or spy_snap.get(
                    "intraday_pct_change") or spy_snap.get("change_pct")
                if spy_pct is not None:
                    inputs.spy_pct_change_30m = float(spy_pct)
                spy_60 = spy_snap.get("intraday_60m_pct") or spy_pct
                if spy_60 is not None:
                    inputs.spy_pct_change_60m = float(spy_60)
                # Realized vol — best-effort fields.
                rv = spy_snap.get("intraday_realized_vol")
                if rv is not None:
                    inputs.spy_intraday_realized_vol = float(rv)
                rv10 = spy_snap.get("realized_vol_10d") or spy_snap.get(
                    "hv_10d")
                if rv10 is not None:
                    inputs.spy_realized_vol_10d = float(rv10)
                # VIX
                vix = spy_snap.get("vix")
                if vix is not None:
                    inputs.vix_spot = float(vix)
                vix_1d = spy_snap.get("vix_1d_pct") or spy_snap.get(
                    "vix_change_pct")
                if vix_1d is not None:
                    inputs.vix_1d_pct_change = float(vix_1d)
                vx_slope = spy_snap.get("vix_curve_slope")
                if vx_slope is not None:
                    inputs.vix_curve_slope = float(vx_slope)
            except (TypeError, ValueError):
                pass

        # Breadth — derive from the cached breadth_snapshot when available.
        try:
            from backend.bot.breadth import latest as _breadth_latest
            row = _breadth_latest()
            if row is not None and row.advancers is not None and row.decliners is not None:
                total = float(row.advancers) + float(row.decliners)
                if total > 0:
                    inputs.breadth_ratio = float(row.advancers) / total
        except Exception:
            pass

        # Put/call ratio — pull from the spy snapshot when present (the
        # Cboe data source surfaces it as `put_call_ratio`).
        if spy_snap and inputs.put_call_ratio is None:
            try:
                pcr = spy_snap.get("put_call_ratio") or spy_snap.get(
                    "pcc_ratio")
                if pcr is not None:
                    inputs.put_call_ratio = float(pcr)
            except (TypeError, ValueError):
                pass

        # Sector dispersion — std-dev of recent returns across the
        # 5-sector basket. Walks the same MarketDataAdapter so failures
        # collapse to None.
        if self.market_data is not None and hasattr(self.market_data, "snapshot"):
            returns: List[float] = []
            for sym in _SECTOR_ETFS:
                try:
                    snap = self.market_data.snapshot(sym).data
                    pct = snap.get("intraday_30m_pct") or snap.get(
                        "intraday_pct_change") or snap.get("change_pct")
                    if pct is not None:
                        returns.append(float(pct))
                except Exception:
                    continue
            if len(returns) >= 3:
                try:
                    inputs.sector_dispersion = float(statistics.pstdev(returns))
                except Exception:
                    pass

        return inputs

    def classify(self) -> IntradayRegimeState:
        """Return the current intraday regime state, using the in-process cache.

        The cache prevents redundant work when multiple engine
        sub-systems (the run_cycle entry, the /regime/intraday API
        route, the Opportunity Brain) all ask for the state within the
        same wall-clock window.
        """
        cache_sec = float(TUNABLES.intraday_regime_classifier_cache_sec)
        if (self._cache is not None
                and (self._now() - self._cache_at) < cache_sec):
            return self._cache

        try:
            inputs = self._collect_inputs()
        except Exception:
            logger.debug("intraday_regime: input collection failed",
                            exc_info=True)
            inputs = IntradayRegimeInputs(prior_state=self._last_state)
        state = _classify_from_inputs(inputs)
        state.classified_at = datetime.utcnow().isoformat()

        # Persist on transition only.
        if state.state != self._last_state:
            _persist_event(self._last_state, state)
            self._last_state = state.state

        self._cache = state
        self._cache_at = self._now()
        return state


__all__ = [
    "STATES",
    "IntradayRegimeClassifier",
    "IntradayRegimeInputs",
    "IntradayRegimeState",
    "_classify_from_inputs",  # exposed for unit tests
]
