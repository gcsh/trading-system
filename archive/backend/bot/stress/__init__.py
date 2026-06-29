"""Stage-8 adversarial scenario library.

Synthetic market-state perturbations that test bot resilience BEFORE any
real-money exposure. Each scenario is a pure ``apply(snapshot)`` that
mutates a market snapshot in a documented way; the test harness drives the
engine against the mutated snapshot and verifies the bot:

  • Doesn't crash
  • Doesn't book a trade on degenerate data (zero price, etc.)
  • Honors audit invariants (Stage-1 hardening)

Coverage targets every realistic failure mode we've seen in market history:

  • flash_crash — 10% drop in one bar with partial recovery
  • halted — feed stops emitting bars (None/empty)
  • bad_quote — zero or negative prices, NaN volume
  • illiquid_chain — options chain empty / single strike
  • vix_spike — VIX doubles in a day
  • wide_spread — bid-ask widens 10× expected
  • stale_data — bar timestamp is older than threshold

Scenarios are declarative dictionaries so a test or canary run can spell
out which ones it cares about.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ScenarioResult:
    scenario: str
    description: str
    mutated_snapshot: Dict[str, Any] = field(default_factory=dict)
    expected_behaviour: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── scenario library ───────────────────────────────────────────────────────


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def flash_crash(snapshot: Dict[str, Any], *, drop_pct: float = 0.10
                 ) -> ScenarioResult:
    """10% drop in one bar with partial 50% recovery; VIX spikes."""
    out = dict(snapshot)
    price = _safe_float(snapshot.get("price"), 100.0)
    out["price"] = round(price * (1 - drop_pct), 4)
    out["high"] = round(price, 4)
    out["low"] = round(price * (1 - drop_pct * 1.2), 4)
    out["volume"] = float(snapshot.get("volume") or 1) * 5
    out["vix"] = round(_safe_float(snapshot.get("vix"), 18.0) * 2, 2)
    out["atr"] = round(_safe_float(snapshot.get("atr"), price * 0.02) * 3, 4)
    return ScenarioResult(
        scenario="flash_crash",
        description=f"{int(drop_pct*100)}% drop in one bar + 2× VIX",
        mutated_snapshot=out,
        expected_behaviour="bot should refuse to enter; existing positions hit stop-losses",
    )


def halted(snapshot: Dict[str, Any]) -> ScenarioResult:
    """Feed went silent — no new bars; force the snapshot to a stale state."""
    out = dict(snapshot)
    out["price"] = 0.0
    out["volume"] = 0.0
    out["volume_avg"] = _safe_float(snapshot.get("volume_avg"))
    out["halted"] = True
    return ScenarioResult(
        scenario="halted",
        description="Trading halt — no new bars",
        mutated_snapshot=out,
        expected_behaviour="bot must not place orders; missing price audit invariant",
    )


def bad_quote(snapshot: Dict[str, Any]) -> ScenarioResult:
    """Negative price, NaN volume — common upstream-data corruption."""
    out = dict(snapshot)
    out["price"] = -1.0
    out["bid"] = 0.0
    out["ask"] = -2.5
    out["volume"] = float("nan")
    return ScenarioResult(
        scenario="bad_quote",
        description="Negative price, NaN volume — corrupt upstream data",
        mutated_snapshot=out,
        expected_behaviour="snapshot validation rejects; no trade attempt",
    )


def illiquid_chain(snapshot: Dict[str, Any]) -> ScenarioResult:
    """Options chain reports no strikes — yfinance sometimes returns empty
    for thinly-traded names."""
    out = dict(snapshot)
    out["option_chain_strikes"] = []
    out["iv_rank"] = None
    out["implied_move"] = None
    out["has_options"] = False
    return ScenarioResult(
        scenario="illiquid_chain",
        description="Empty options chain",
        mutated_snapshot=out,
        expected_behaviour="options strategies must HOLD; equity strategies unaffected",
    )


def vix_spike(snapshot: Dict[str, Any], *, factor: float = 2.0
               ) -> ScenarioResult:
    """VIX doubles intraday."""
    out = dict(snapshot)
    current = _safe_float(snapshot.get("vix"), 18.0)
    out["vix"] = round(current * factor, 2)
    return ScenarioResult(
        scenario="vix_spike",
        description=f"VIX × {factor} (now {out['vix']})",
        mutated_snapshot=out,
        expected_behaviour="regime → high-vol; sizing cuts via vol-target + cross-asset hedge",
    )


def wide_spread(snapshot: Dict[str, Any], *, multiplier: float = 10.0
                  ) -> ScenarioResult:
    """Bid-ask widens (illiquidity / market panic)."""
    out = dict(snapshot)
    price = _safe_float(snapshot.get("price"), 100.0)
    # Force a noticeable percentage spread
    half = price * 0.01 * multiplier / 2
    out["bid"] = round(price - half, 4)
    out["ask"] = round(price + half, 4)
    out["spread_bps"] = round((half * 2 / price) * 1e4, 2)
    return ScenarioResult(
        scenario="wide_spread",
        description=f"bid-ask widened {multiplier}× (now {out['spread_bps']} bps)",
        mutated_snapshot=out,
        expected_behaviour="execution-cost gate trims size; high TCA cost recorded",
    )


def stale_data(snapshot: Dict[str, Any]) -> ScenarioResult:
    """Snapshot timestamp is older than the SLO."""
    out = dict(snapshot)
    out["timestamp"] = "2020-01-01T00:00:00"
    out["data_age_minutes"] = 60 * 24 * 7    # a week
    return ScenarioResult(
        scenario="stale_data",
        description="Snapshot timestamp ≥ 1 week old",
        mutated_snapshot=out,
        expected_behaviour="monitoring SLO breach; engine should skip the cycle",
    )


# ── registry + harness ────────────────────────────────────────────────────


SCENARIOS: Dict[str, Callable[..., ScenarioResult]] = {
    "flash_crash": flash_crash,
    "halted": halted,
    "bad_quote": bad_quote,
    "illiquid_chain": illiquid_chain,
    "vix_spike": vix_spike,
    "wide_spread": wide_spread,
    "stale_data": stale_data,
}


def available_scenarios() -> List[str]:
    return sorted(SCENARIOS)


def apply_scenario(name: str, snapshot: Dict[str, Any],
                     **kwargs) -> Optional[ScenarioResult]:
    fn = SCENARIOS.get(name)
    if fn is None:
        return None
    return fn(snapshot, **kwargs)


# ── batch suite runner ────────────────────────────────────────────────────


def run_suite(snapshot: Dict[str, Any], *, scenarios: Optional[List[str]] = None
                ) -> List[Dict[str, Any]]:
    """Apply every registered scenario to the same baseline snapshot. Caller
    decides what to do with the mutated dicts — typical use is to feed each
    one into a Strategy.analyze and assert no exception + sensible HOLD."""
    names = scenarios or available_scenarios()
    out: List[Dict[str, Any]] = []
    for name in names:
        result = apply_scenario(name, snapshot)
        if result is not None:
            out.append(result.to_dict())
    return out
