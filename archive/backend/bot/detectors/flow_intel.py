"""MITS Phase 5 (P5.4) — flow-intel detector family.

A 9th detector family that emits ``MarketObservation`` rows derived
from the live FlowSeeker alert stream. Six patterns:

  * flow_call_sweep_unusual   — bullish call sweeps clearing the
       conviction-window premium + urgency floor.
  * flow_put_sweep_unusual    — bearish put sweep equivalent.
  * flow_call_block_buy       — large single-print call buy at ask
       (institutional positioning, not flow chasing).
  * flow_put_block_buy        — large single-print put buy at ask.
  * flow_dark_pool_call_lean  — sustained call sweeps + dark-pool
       confirmation in the same conviction window.
  * flow_dark_pool_put_lean   — put-side equivalent.

These detectors don't read bar data; they synthesize one observation
per cluster of alerts from ``flow_for(ticker)``. The corpus pipeline
then forward-prices them via the standard outcome linker so the
knowledge graph picks up flow patterns the same way it picks up
candlestick patterns.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.bot.detectors.base import Detector, Observation
from backend.config import TUNABLES

logger = logging.getLogger(__name__)


FLOW_PATTERNS = (
    "flow_call_sweep_unusual",
    "flow_put_sweep_unusual",
    "flow_call_block_buy",
    "flow_put_block_buy",
    "flow_dark_pool_call_lean",
    "flow_dark_pool_put_lean",
)


def _alert_field(alert: Any, name: str, default=None):
    if isinstance(alert, dict):
        return alert.get(name, default)
    return getattr(alert, name, default)


def _emit(ticker: str, pattern: str, alerts: List[Any],
            timestamp: datetime,
            spot: Optional[float] = None,
            extra: Optional[Dict[str, Any]] = None) -> Observation:
    """Build a 1d-timeframe observation row that mirrors the structure
    of a chart-pattern detector. The corpus pipeline keys cohorts on
    pattern + regime, so flow patterns slot in alongside chart ones."""
    features: Dict[str, Any] = {
        "n_alerts": len(alerts),
        "total_premium": float(sum(
            float(_alert_field(a, "premium") or 0.0) for a in alerts
        )),
        "avg_urgency": (
            float(sum(float(_alert_field(a, "urgency_score") or 0.0)
                          for a in alerts)) / max(1, len(alerts))
        ),
    }
    if extra:
        features.update(extra)
    return Observation(
        ticker=ticker,
        pattern=pattern,
        timestamp=timestamp,
        timeframe="1d",
        regime="unknown",
        vol_state="normal",
        time_bucket="rth",
        spot=spot,
        features=features,
        source="live_engine",
    )


class _FlowDetectorBase(Detector):
    """Shared scaffolding for the flow-intel detectors.

    The ``detect`` contract takes ``bars`` (so it slots into ``detect_all``
    cleanly) but only consults the live FlowSeeker stream. When no
    flow data is available (degraded vendor, no key) it returns []
    instead of raising — same fail-open behavior as the IV detectors.
    """

    family = "flow_intel"
    # Subclasses set ``pattern``, ``direction`` (bullish|bearish), and
    # ``signal_kind`` (sweep|block|darkpool).
    direction: str = "bullish"
    signal_kind: str = "sweep"

    def _spot(self, bars) -> Optional[float]:
        try:
            if bars is not None and len(bars) > 0:
                return float(bars["close"].astype(float).iloc[-1])
        except Exception:
            return None
        return None

    def _alerts(self, ticker: str, alerts_override=None) -> List[Any]:
        """Fetch (or accept an injected list of) FlowAlert rows. Tests
        inject; production fetches from the live flow signal."""
        if alerts_override is not None:
            return list(alerts_override)
        try:
            from backend.bot.signals.flow import flow_for
            return list(flow_for(ticker) or [])
        except Exception:
            logger.debug("flow_intel: flow_for(%s) failed", ticker,
                              exc_info=True)
            return []


def _matches_sweep(alert, direction: str) -> bool:
    sentiment = (_alert_field(alert, "sentiment") or "").lower()
    trade_type = (_alert_field(alert, "trade_type") or "").lower()
    return trade_type == "sweep" and sentiment == direction


def _matches_block(alert, direction: str) -> bool:
    """A block is an unusually large single-print order that the
    aggregator tags as ``trade_type == 'block'`` OR a sweep whose
    premium clears the institutional block-size floor."""
    sentiment = (_alert_field(alert, "sentiment") or "").lower()
    trade_type = (_alert_field(alert, "trade_type") or "").lower()
    premium = float(_alert_field(alert, "premium") or 0.0)
    block_min = float(
        getattr(TUNABLES, "flow_intel_block_premium_min", 1_000_000.0)
    )
    if sentiment != direction:
        return False
    if trade_type == "block":
        return True
    return trade_type == "sweep" and premium >= block_min


def _matches_darkpool(alert) -> bool:
    return (_alert_field(alert, "trade_type") or "").lower() == "darkpool"


class _SweepDetector(_FlowDetectorBase):
    signal_kind = "sweep"

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  alerts: Optional[List[Any]] = None,
                  **kwargs) -> List[Observation]:
        p = params if params is not None else self.default_params()
        premium_floor = float(
            p.get("premium_min",
                  getattr(TUNABLES, "flow_intel_sweep_premium_min", 250_000.0))
        )
        urgency_floor = float(
            p.get("urgency_min",
                  getattr(TUNABLES, "flow_intel_sweep_urgency_min", 0.75))
        )
        raw = self._alerts(ticker, alerts_override=alerts)
        matched = [
            a for a in raw
            if _matches_sweep(a, self.direction)
            and float(_alert_field(a, "premium") or 0.0) >= premium_floor
            and float(_alert_field(a, "urgency_score") or 0.0) >= urgency_floor
        ]
        if not matched:
            return []
        spot = self._spot(bars)
        ts = datetime.utcnow()
        return [_emit(
            ticker, self.pattern, matched, timestamp=ts, spot=spot,
            extra={"direction": self.direction, "signal_kind": "sweep"},
        )]

    def default_params(self) -> Dict[str, Any]:
        return {
            "premium_min": float(
                getattr(TUNABLES, "flow_intel_sweep_premium_min", 250_000.0)
            ),
            "urgency_min": float(
                getattr(TUNABLES, "flow_intel_sweep_urgency_min", 0.75)
            ),
        }


class _BlockDetector(_FlowDetectorBase):
    signal_kind = "block"

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  alerts: Optional[List[Any]] = None,
                  **kwargs) -> List[Observation]:
        p = params if params is not None else self.default_params()
        block_min = float(
            p.get("premium_min",
                  getattr(TUNABLES, "flow_intel_block_premium_min", 1_000_000.0))
        )
        raw = self._alerts(ticker, alerts_override=alerts)
        matched = [a for a in raw if _matches_block(a, self.direction)]
        if not matched:
            return []
        # Filter again on the per-detector minimum (parametrizable).
        matched = [
            a for a in matched
            if float(_alert_field(a, "premium") or 0.0) >= block_min
        ]
        if not matched:
            return []
        spot = self._spot(bars)
        ts = datetime.utcnow()
        return [_emit(
            ticker, self.pattern, matched, timestamp=ts, spot=spot,
            extra={"direction": self.direction, "signal_kind": "block"},
        )]

    def default_params(self) -> Dict[str, Any]:
        return {
            "premium_min": float(
                getattr(TUNABLES, "flow_intel_block_premium_min", 1_000_000.0)
            ),
        }


class _DarkPoolLeanDetector(_FlowDetectorBase):
    signal_kind = "darkpool"

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  alerts: Optional[List[Any]] = None,
                  **kwargs) -> List[Observation]:
        p = params if params is not None else self.default_params()
        darkpool_min = float(
            p.get("darkpool_premium_min",
                  getattr(TUNABLES, "flow_intel_darkpool_min", 1_000_000.0))
        )
        sweep_premium_floor = float(
            p.get("sweep_premium_min",
                  getattr(TUNABLES, "flow_intel_sweep_premium_min", 250_000.0))
        )
        raw = self._alerts(ticker, alerts_override=alerts)
        sweeps = [
            a for a in raw
            if _matches_sweep(a, self.direction)
            and float(_alert_field(a, "premium") or 0.0) >= sweep_premium_floor
        ]
        darkpool = [
            a for a in raw
            if _matches_darkpool(a)
            and float(_alert_field(a, "premium") or 0.0) >= darkpool_min
        ]
        if not sweeps or not darkpool:
            return []
        spot = self._spot(bars)
        ts = datetime.utcnow()
        combined = sweeps + darkpool
        return [_emit(
            ticker, self.pattern, combined, timestamp=ts, spot=spot,
            extra={
                "direction": self.direction,
                "signal_kind": "darkpool_lean",
                "sweep_count": len(sweeps),
                "darkpool_count": len(darkpool),
            },
        )]

    def default_params(self) -> Dict[str, Any]:
        return {
            "darkpool_premium_min": float(
                getattr(TUNABLES, "flow_intel_darkpool_min", 1_000_000.0)
            ),
            "sweep_premium_min": float(
                getattr(TUNABLES, "flow_intel_sweep_premium_min", 250_000.0)
            ),
        }


# ── concrete detector classes ──────────────────────────────────────────


class CallSweepUnusualDetector(_SweepDetector):
    pattern = "flow_call_sweep_unusual"
    direction = "bullish"
    description = (
        "Aggressive bullish call sweeps clear the premium + urgency floor — "
        "institutional flow lifting the offer."
    )


class PutSweepUnusualDetector(_SweepDetector):
    pattern = "flow_put_sweep_unusual"
    direction = "bearish"
    description = (
        "Aggressive bearish put sweeps clear the premium + urgency floor — "
        "institutional flow hitting the bid."
    )


class CallBlockBuyDetector(_BlockDetector):
    pattern = "flow_call_block_buy"
    direction = "bullish"
    description = (
        "Large single-print call buy at ask — institutional positioning, "
        "not chasing flow."
    )


class PutBlockBuyDetector(_BlockDetector):
    pattern = "flow_put_block_buy"
    direction = "bearish"
    description = (
        "Large single-print put buy at ask — institutional positioning, "
        "not chasing flow."
    )


class DarkPoolCallLeanDetector(_DarkPoolLeanDetector):
    pattern = "flow_dark_pool_call_lean"
    direction = "bullish"
    description = (
        "Sustained bullish call sweeps PLUS dark-pool confirmation in the "
        "same conviction window."
    )


class DarkPoolPutLeanDetector(_DarkPoolLeanDetector):
    pattern = "flow_dark_pool_put_lean"
    direction = "bearish"
    description = (
        "Sustained bearish put sweeps PLUS dark-pool confirmation in the "
        "same conviction window."
    )


def build_flow_intel_detectors() -> List[Detector]:
    return [
        CallSweepUnusualDetector(),
        PutSweepUnusualDetector(),
        CallBlockBuyDetector(),
        PutBlockBuyDetector(),
        DarkPoolCallLeanDetector(),
        DarkPoolPutLeanDetector(),
    ]


__all__ = [
    "FLOW_PATTERNS",
    "CallSweepUnusualDetector",
    "PutSweepUnusualDetector",
    "CallBlockBuyDetector",
    "PutBlockBuyDetector",
    "DarkPoolCallLeanDetector",
    "DarkPoolPutLeanDetector",
    "build_flow_intel_detectors",
]
