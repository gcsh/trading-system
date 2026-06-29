"""Trade log table used for history and analytics."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class Trade(Base):
    """A single trade decision and its outcome (open or closed).

    For options trades, ``instrument`` is 'option' or 'spread', and
    ``option_type`` / ``strike`` / ``expiration`` / ``contracts`` describe the
    contract. ``detail_json`` carries the full decision context (the indicator
    snapshot, stop/target, spread legs) so the UI can explain *why* the trade
    fired.
    """

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    strategy: Mapped[str] = mapped_column(String)
    signal_source: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String, default="")
    paper: Mapped[int] = mapped_column(Integer, default=1)
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    status: Mapped[str] = mapped_column(String, default="open")

    # Instrument detail
    instrument: Mapped[str] = mapped_column(String, default="stock")  # stock | option | spread
    option_type: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)  # call | put
    strike: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    expiration: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    contracts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=None)
    stop_loss_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    take_profit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=None)
    detail_json: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)

    # P1.5 — which data source priced this fill?
    # Values: thetadata | alpaca | yfinance | stale | bs_fallback | paper_stub
    # Enables post-hoc audit: "what % of fills came from real chain data
    # vs stubbed math?" When pricing_source == "paper_stub", the row is
    # under v1 accounting and should be excluded from v2-grade analytics.
    pricing_source: Mapped[str] = mapped_column(String, default="paper_stub",
                                                        index=True)

    # P1.7 — accounting model version. v1 = fake option premium / stub MTM.
    # v2 = real chain pricing + BS fallback (post-Phase 2 cutover).
    # Stamps every trade with the model used so equity comparisons across
    # accounting changes are mechanical, not guesswork.
    accounting_version: Mapped[int] = mapped_column(Integer, default=1)

    # MITS Phase 7 — opportunistic-flag for trial-scorecard separation.
    # True when the trade was routed through the Opportunity Brain +
    # opportunistic gate during a non-normal intraday regime. Lets
    # operators compare statistical-layer win rate vs discretionary-
    # layer win rate cleanly.
    opportunistic: Mapped[int] = mapped_column(Integer, default=0, index=True)

    # MITS Phase 7 finishing pass — must_exit_by_eod marker. Set on
    # opportunistic trades so the engine's 15:55 ET EOD sweep closes
    # them even when no other exit (stop / target / DTE) triggers. The
    # operator's hard rule: crisis-day discretionary positions are NOT
    # swing positions; they MUST be flat overnight.
    must_exit_by_eod: Mapped[int] = mapped_column(Integer, default=0, index=True)

    # MITS Phase 17.A — execution telemetry. All nullable so existing
    # rows survive the auto-migrate, and pre-17.A code paths can still
    # write without these fields.
    slippage_bps:               Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_commission:           Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_vs_marked_delta:   Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spot_at_emit:               Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spot_at_fill:               Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # MITS Phase 17.B — fill provenance snapshot. JSON blob carrying the
    # full FillSnapshot (or {"legs": [...]} on multi-leg structures). Feeds
    # Phase 18 Learning Layer fill-quality attribution.
    fill_snapshot_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # MITS Phase 17.C — sizing provenance chain. Records base_qty,
    # ordered multiplier steps (input * factor == output), final_qty,
    # and the rounded final integer used by the executor. Feeds Phase 18
    # Learning Layer per-multiplier outcome attribution.
    sizing_chain_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # MITS Phase 17.D — chain selection provenance. Records the candidate
    # contracts considered at strike-selection time (chosen + up to 4
    # rejected), per-candidate rejection reasons, the requested delta band
    # and DTE, the chain source (thetadata / yfinance / cache / paper_stub),
    # quote freshness, and a one-line human-readable ``chosen_reason``.
    # Answers "Why this contract?" — the 2nd of Phase 17's five
    # observability questions. NULL on stock trades by design.
    chain_selection_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # MITS Phase 17.E — exit policy provenance. Carries the
    # ExitPolicyResult.to_dict() for trades created on the CLOSE path
    # (the headline ExitTrigger plus every rule_evaluation row produced
    # the moment the position was judged). Answers "Why this exact
    # exit?" — the 5th of Phase 17's five observability questions. NULL
    # on entry trades + on closes that bypass the exit_manager (manual
    # close, fresh-start sweep, expiry assignment book-entry leg).
    exit_policy_result_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # MITS Phase 18-FU Gap 4 — provenance kind for the row source. Values:
    #   * "live"               — real engine cycle (default; covers all
    #                            historical rows pre-migration too).
    #   * "synthetic_backfill" — written by the flag-gated historical
    #                            replay backfill (backend.bot.learning.backfill)
    #                            ONLY when the operator has opted in.
    # Learning-layer aggregators (18.A attribution, 18.B counterfactual,
    # 18.C policy tuning, 18.D weight adaptation, 18.E rollback) default
    # to reading `source_kind IN ('live', NULL)`; synthetic rows are
    # included only when the caller passes `include_synthetic=True`.
    # NEVER write synthetic_backfill on the live engine path.
    source_kind: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, default="live", index=True,
    )

    def to_dict(self) -> dict:
        detail = None
        if self.detail_json:
            try:
                detail = json.loads(self.detail_json)
            except Exception:
                detail = None
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "ticker": self.ticker,
            "action": self.action,
            "quantity": self.quantity,
            "price": self.price,
            "strategy": self.strategy,
            "signal_source": self.signal_source,
            "confidence": self.confidence,
            "reason": self.reason,
            "paper": bool(self.paper),
            "pnl": self.pnl,
            "status": self.status,
            "instrument": self.instrument,
            "option_type": self.option_type,
            "strike": self.strike,
            "expiration": self.expiration,
            "contracts": self.contracts,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "detail": detail,
            "pricing_source": self.pricing_source,
            "accounting_version": self.accounting_version,
            "opportunistic": bool(self.opportunistic),
            "must_exit_by_eod": bool(self.must_exit_by_eod),
            "slippage_bps": self.slippage_bps,
            "total_commission": self.total_commission,
            "realized_vs_marked_delta": self.realized_vs_marked_delta,
            "spot_at_emit": self.spot_at_emit,
            "spot_at_fill": self.spot_at_fill,
            "fill_snapshot": (
                json.loads(self.fill_snapshot_json)
                if self.fill_snapshot_json else None
            ),
            "sizing_chain": (
                json.loads(self.sizing_chain_json)
                if self.sizing_chain_json else None
            ),
            "chain_selection": (
                json.loads(self.chain_selection_json)
                if self.chain_selection_json else None
            ),
            "exit_policy_result": (
                json.loads(self.exit_policy_result_json)
                if self.exit_policy_result_json else None
            ),
            "source_kind": self.source_kind,
        }
