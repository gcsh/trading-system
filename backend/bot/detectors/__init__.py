"""MITS Phase 0 — pattern detector registry.

Single entry point for the historical replay framework + live engine:

    from backend.bot.detectors import detect_all
    observations = detect_all("SPY", bars)

Adding a new detector:
  1. Implement in its own module (subclass `Detector`, set `pattern`).
  2. Register in the `DETECTOR_REGISTRY` dict below.
  3. Add unit tests under tests/unit/test_detectors_*.py.

MITS Phase 3 — every detector carries a `family` (one of the 7 groups
the operator-facing UI buckets them into) and a `description`. The
family is assigned at registry-build time so we don't have to edit
every individual detector class to surface the new metadata.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from backend.bot.detectors.base import Detector, Observation
from backend.bot.detectors.direction import resolve_direction
from backend.bot.detectors.catalyst import build_catalyst_detectors
from backend.bot.detectors.flow_intel import build_flow_intel_detectors
from backend.bot.detectors.liquidity import build_liquidity_detectors
from backend.bot.detectors.macro_regime import build_macro_regime_detectors
from backend.bot.detectors.market_structure import build_market_structure_detectors
from backend.bot.detectors.options_intel import build_options_intel_detectors
from backend.bot.detectors.pine_custom import (
    PINE_FAMILY_SLUG, build_pine_custom_detectors,
)
from backend.bot.detectors.price_action import build_price_action_detectors
from backend.bot.detectors.quantitative import build_quantitative_detectors
from backend.bot.detectors.smc import build_smc_detectors
from backend.bot.detectors.talib_patterns import build_talib_detectors
from backend.bot.detectors.volume_profile import build_volume_profile_detectors
from backend.bot.detectors.volume_profile_v2 import build_volume_profile_v2_detectors
from backend.bot.detectors.vwap import build_vwap_detectors
from backend.bot.detectors.wyckoff import build_wyckoff_detectors

logger = logging.getLogger(__name__)


# ── descriptions for every built-in detector (operator UI tooltip) ────

_DETECTOR_DESCRIPTIONS: Dict[str, str] = {
    # Candlesticks
    "bull_flag": "Strong upward thrust followed by tight sideways consolidation — continuation signal.",
    "bear_flag": "Sharp downward thrust followed by sideways consolidation — bearish continuation.",
    "pennant": "Converging triangle after a thrust — energy compression before next move.",
    "consolidation": "Tight sideways action — coiled spring before a directional break.",
    # Price action
    "breakout": "Close above prior 20-bar high with expanding volume.",
    "pullback": "Brief dip in an uptrend, holding above key moving averages.",
    "failed_breakout": "Breakout that reverses back below the prior high within 3 bars — bull trap.",
    "failed_breakdown": "Breakdown that reverses back above the prior low within 3 bars — bear trap.",
    # Market structure
    "break_of_structure": "Higher high (or lower low) breaking a recent pivot — trend continuation.",
    "change_of_character": "Trend reversal — first lower-low after sustained uptrend (or vice versa).",
    # Liquidity
    "liquidity_sweep": "Quick wick beyond a known liquidity pool then close back inside — stop hunt.",
    "stop_hunt": "Aggressive wick beyond support/resistance taking out clustered stops.",
    # VWAP
    "vwap_reclaim": "Price re-crosses above VWAP after being below — institutional support reclaim.",
    "vwap_rejection": "Price rejected at VWAP from below — sellers defended the level.",
    # Volume profile
    "hvn_acceptance": "Price accepted into a high-volume node — fair-value area.",
    "lvn_rejection": "Price rejected from a low-volume node — fast-move zone.",
    # Options intel
    "iv_expansion": "Implied volatility jumped sharply — vol expansion regime.",
    "iv_compression": "Implied volatility collapsed — vol compression regime.",
    "gex_acceleration": "Dealer gamma exposure shifted abruptly — regime change in pinning behavior.",
    # Flow intel (MITS Phase 5 / P5.4)
    "flow_call_sweep_unusual": "Aggressive bullish call sweeps clearing premium + urgency floors — institutional lifting offers.",
    "flow_put_sweep_unusual": "Aggressive bearish put sweeps clearing premium + urgency floors — institutional hitting bids.",
    "flow_call_block_buy": "Large single-print call buy at ask — institutional positioning, not flow chasing.",
    "flow_put_block_buy": "Large single-print put buy at ask — institutional positioning, not flow chasing.",
    "flow_dark_pool_call_lean": "Sustained bullish call sweeps + dark-pool confirmation in same conviction window.",
    "flow_dark_pool_put_lean": "Sustained bearish put sweeps + dark-pool confirmation in same conviction window.",
    # MITS Phase 12 — SMC (Smart Money Concepts) detectors.
    "order_block": "Last opposite-color candle before a >=1 ATR impulse; retest entry. ICT.",
    "fair_value_gap": "3-bar imbalance; emits when price returns to fill the gap. ICT.",
    "liquidity_sweep_v2": "Equal-highs/lows pool swept by a wick that closes back inside.",
    "stop_hunt_v2": "Failed sweep of prior swing high/low with volume > 1.5x MA(20).",
    "premium_discount_zone": "Trend-aligned entry inside the discount/premium half of the last impulse.",
    "market_structure_shift_v2": "ZigZag-pivot HH/HL — LH/LL state-machine trend flip.",
    # Wyckoff method detectors.
    "wyckoff_accumulation_phase": "Wyckoff Phase A-E accumulation tag.",
    "wyckoff_distribution_phase": "Wyckoff distribution schematic (buying climax — upthrust — markdown).",
    "wyckoff_spring": "False breakdown below trading-range support, recovers on rising volume.",
    "wyckoff_sos": "Sign of Strength: strong rally on expanding volume breaking above range.",
    "wyckoff_upthrust": "False breakout above resistance; declining volume on break + rising on reversal.",
    # Volume Profile v2 detectors.
    "poc_retest": "Price returns to the rolling-window POC after a >=1 ATR excursion.",
    "value_area_rejection": "Reversal candle at VAH or VAL of the 70 percent value area.",
    "composite_value_area": "Price inside the 5d / 20d / 60d value-area overlap.",
    # Catalyst detectors (Phase 11 data sources).
    "pead_drift": "Post-Earnings Announcement Drift: >=2 sigma surprise, 60-day forward window.",
    "insider_cluster": ">=3 distinct insider open-market buys within 30 days.",
    "smart_money_inflow": ">=5 top-50 funds add the same ticker in one 13F quarter.",
    "earnings_revision_shift": "Direction flip in analyst-estimate / guidance revisions.",
    # Macro regime detectors (FRED).
    "yield_curve_inversion": "DGS10 - DGS2 spread crosses zero (or steepens back).",
    "credit_spread_widening": "HY OAS rises by >=50bp in 30 days (or symmetric tightening).",
    "dollar_strength_shift": "Broad USD index z-score crosses plus/minus 2 sigma.",
    "composite_macro_regime": "Composite 0-100 risk-off score crossing defensive (>=60) or risk-on (<=30).",
    # Quantitative detectors.
    "cross_sectional_momentum": "12-1 month cross-sectional momentum (top + bottom quintile).",
    "mean_reversion_z": "3-day return z-score vs 60-day stdev > 2 / < -2.",
    "sector_dispersion": "Sector-ETF return-dispersion z-score: stock-picker vs passive-index regime.",
}


# Family assignment for each built-in family slug.
_FAMILY_MAP: Dict[str, str] = {
    "talib": "candlesticks",
    "price_action_flags": "candlesticks",
    "price_action_breakouts": "price_action",
    "market_structure": "market_structure",
    "liquidity": "liquidity",
    "vwap": "vwap",
    "volume_profile": "volume_profile",
    "options_intel": "options_intel",
    # MITS Phase 4 (P4.2) — Pine-imported custom detectors. The 8th
    # family in the operator UI palette.
    "pine_custom": PINE_FAMILY_SLUG,
    # MITS Phase 5 (P5.4) — flow-intel detectors derived from the live
    # FlowSeeker stream. 9th family.
    "flow_intel": "flow_intel",
    # MITS Phase 12 — institutional-grade detection layer (17 new
    # detectors across 6 new families).
    "smc": "smc",
    "wyckoff": "wyckoff",
    "volume_profile_v2": "volume_profile_v2",
    "catalyst": "catalyst",
    "macro_regime": "macro_regime",
    "quantitative": "quantitative",
}


# Patterns from the price_action module that are actually flag-shaped
# candlestick aggregates; the rest are true price-action signals.
_PRICE_ACTION_FLAG_PATTERNS = {
    "bull_flag", "bear_flag", "pennant", "consolidation",
}


def _build_registry() -> Dict[str, Detector]:
    """Construct the registry once at import time. Tolerates per-group
    construction failures so one broken family never wedges the rest.

    Family tagging: each detector gets `family` and `description` set
    via `_FAMILY_MAP` / `_DETECTOR_DESCRIPTIONS`. The price_action module
    is split into 'candlesticks' (flag-shaped aggregates) vs
    'price_action' (breakouts / pullbacks) because the operator UI
    groups them differently.
    """
    registry: Dict[str, Detector] = {}
    families = [
        ("talib", build_talib_detectors),
        ("price_action", build_price_action_detectors),
        ("market_structure", build_market_structure_detectors),
        ("liquidity", build_liquidity_detectors),
        ("vwap", build_vwap_detectors),
        ("volume_profile", build_volume_profile_detectors),
        ("options_intel", build_options_intel_detectors),
        # MITS Phase 4 (P4.2) — Pine-imported detectors dynamically
        # constructed from DetectorConfig rows.
        ("pine_custom", build_pine_custom_detectors),
        # MITS Phase 5 (P5.4) — flow-intel detectors that read the
        # live options-flow stream rather than bar data.
        ("flow_intel", build_flow_intel_detectors),
        # MITS Phase 12 — institutional rebuild families.
        ("smc", build_smc_detectors),
        ("wyckoff", build_wyckoff_detectors),
        ("volume_profile_v2", build_volume_profile_v2_detectors),
        ("catalyst", build_catalyst_detectors),
        ("macro_regime", build_macro_regime_detectors),
        ("quantitative", build_quantitative_detectors),
    ]
    for family_slug, builder in families:
        try:
            for det in builder():
                if not det.pattern:
                    continue
                # Resolve family — price_action splits across two UI groups.
                if family_slug == "price_action":
                    if det.pattern in _PRICE_ACTION_FLAG_PATTERNS:
                        det.family = _FAMILY_MAP["price_action_flags"]
                    else:
                        det.family = _FAMILY_MAP["price_action_breakouts"]
                elif family_slug == "pine_custom":
                    # Pine custom detectors set family in their constructor.
                    det.family = PINE_FAMILY_SLUG
                else:
                    det.family = _FAMILY_MAP.get(family_slug, "uncategorized")
                # Attach human-readable description when one is curated.
                if not det.description:
                    det.description = _DETECTOR_DESCRIPTIONS.get(
                        det.pattern, "",
                    )
                registry[det.pattern] = det
        except Exception:
            logger.exception("detector family %s failed to load", family_slug)
    return registry


DETECTOR_REGISTRY: Dict[str, Detector] = _build_registry()


def rebuild_registry() -> None:
    """Re-scan detector sources and refresh ``DETECTOR_REGISTRY``.

    Called from the Pine import endpoint so a freshly-persisted custom
    detector starts firing on the next cycle without an app restart.
    Safe to call repeatedly; idempotent.
    """
    global DETECTOR_REGISTRY
    DETECTOR_REGISTRY = _build_registry()


# ── runtime config cache (MITS Phase 3) ───────────────────────────────


# Cache the (disabled_set, params_by_name) tuple from `detector_config`
# for ~30s so the hot engine path doesn't re-query SQLite every cycle.
_CONFIG_CACHE_TTL_SEC = 30.0
_config_cache: Dict[str, Any] = {
    "loaded_at": 0.0,
    "disabled": set(),
    "params": {},
}
_config_cache_lock = threading.Lock()


def _load_detector_config(force: bool = False) -> Dict[str, Any]:
    """Return ({disabled_patterns}, {pattern: params_dict}).

    Cached for 30s. Idempotent + fail-open: when the table doesn't
    exist yet (fresh DB) or the query crashes we return empty sets so
    every detector stays enabled.
    """
    now = time.time()
    with _config_cache_lock:
        if (not force
                and now - _config_cache["loaded_at"] < _CONFIG_CACHE_TTL_SEC):
            return _config_cache
        try:
            from sqlalchemy import select
            from backend.db import session_scope
            from backend.models.detector_config import DetectorConfig
            disabled: Set[str] = set()
            params: Dict[str, Dict[str, Any]] = {}
            with session_scope() as s:
                rows = s.execute(select(DetectorConfig)).scalars().all()
                for row in rows:
                    if not row.enabled:
                        disabled.add(row.name)
                    try:
                        p = json.loads(row.params_json or "{}")
                        if isinstance(p, dict) and p:
                            params[row.name] = p
                    except Exception:
                        pass
            _config_cache["loaded_at"] = now
            _config_cache["disabled"] = disabled
            _config_cache["params"] = params
        except Exception:
            logger.debug("detector_config load failed", exc_info=True)
            # Leave the cache as-is so we don't flap between empty
            # (everything enabled) and stale.
            _config_cache["loaded_at"] = now
        return _config_cache


def disabled_patterns() -> Set[str]:
    """Return the current set of operator-disabled detector names.

    Used by `recompute_cells` and `load_knowledge_evidence` to mask
    cells / observations whose pattern is disabled.
    """
    return set(_load_detector_config()["disabled"])


def clear_detector_config_cache() -> None:
    """Force a re-read on next access. Called after every PATCH."""
    with _config_cache_lock:
        _config_cache["loaded_at"] = 0.0


def all_detectors() -> List[Detector]:
    """Return every detector in stable name-sorted order."""
    return [DETECTOR_REGISTRY[k] for k in sorted(DETECTOR_REGISTRY.keys())]


def detect_all(ticker: str, bars, *,
                  iv_series: Optional[List[float]] = None,
                  gex_series: Optional[List[float]] = None,
                  **kwargs: Any) -> List[Observation]:
    """Run every ENABLED detector against ``bars`` and concatenate
    the resulting observation lists.

    `iv_series` / `gex_series` get forwarded to detectors that need them
    (the options-intel family). Detectors that don't take those kwargs
    quietly ignore them.

    MITS Phase 3: skips detectors whose `pattern` appears in the
    operator-disabled set (from `detector_config` table). Param
    overrides per detector are merged on top of `default_params()` and
    passed via the `params` kwarg, which detectors may consult.
    """
    out: List[Observation] = []
    if bars is None or len(bars) < 5:
        return out
    cfg = _load_detector_config()
    disabled = cfg["disabled"]
    params_map = cfg["params"]
    enabled_fired = 0
    for det in all_detectors():
        if det.pattern in disabled:
            continue
        try:
            # Merge default params + operator overrides. Detectors that
            # don't read `params` quietly ignore the kwarg.
            merged_params = dict(det.default_params() or {})
            override = params_map.get(det.pattern)
            if override:
                merged_params.update(override)
            obs_list = det.detect(
                ticker, bars,
                iv_series=iv_series,
                gex_series=gex_series,
                params=merged_params,
                **kwargs,
            )
        except Exception:
            logger.debug("detector %s failed", det.pattern, exc_info=True)
            continue
        if obs_list:
            # MITS Phase 12.1 — central direction tagging. Every emitted
            # observation gets its `direction` field set from the
            # authoritative resolver. Detectors that already populate
            # `direction` win — the resolver only fills the gap.
            for o in obs_list:
                if getattr(o, "direction", None) is None:
                    try:
                        o.direction = resolve_direction(o.pattern, o.features)
                    except Exception:
                        pass
            out.extend(obs_list)
            enabled_fired += 1
    # MITS Phase 12.1 Fix 8 — single INFO log per cycle confirming
    # detect_all wired up correctly. Avoids per-detector chatter while
    # still giving the operator a confirmation line.
    enabled_total = sum(1 for d in all_detectors()
                            if d.pattern not in disabled)
    logger.info(
        "detect_all: %d enabled detectors fired %d observations on %s",
        enabled_total, len(out), ticker,
    )
    return out


__all__ = [
    "Detector",
    "Observation",
    "DETECTOR_REGISTRY",
    "all_detectors",
    "detect_all",
    "disabled_patterns",
    "clear_detector_config_cache",
    "rebuild_registry",
    "PINE_FAMILY_SLUG",
]
