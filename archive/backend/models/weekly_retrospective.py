"""MITS Phase 6 (P6.4) — Sunday weekly retrospective.

Single row per (week_start_date). The Sunday 11:00 ET cron walks the
prior trading week (Mon-Fri) and assembles a structured recap:

  * Headline P&L + trade counts + win rate + average hold.
  * Top winning + losing TICKERS for the week.
  * Top winning + losing PATTERNS (from `Trade.detail_json.eod_bias`
    or `Trade.strategy`).
  * Family-level P&L attribution (which detector family carried the
    week + which dragged).
  * Catalyst-gate saves: how many trades the catalyst gate skipped +
    a $ estimate of avoided drawdown using the cohort's
    avg_return_pct.
  * Conviction-multiplier P&L effect: realized P&L split by the
    conviction sizing multiplier that fired (rank_1 / rank_2_3 /
    rank_4_plus).
  * Claude-composed summary paragraph (cached on the row so we don't
    re-call Claude on every UI fetch).

UPSERT-keyed on `week_start_date` so re-running the pass overwrites.
"""
from __future__ import annotations

from datetime import date as _date, datetime
from typing import Optional

from sqlalchemy import (
    Date, DateTime, Float, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class WeeklyRetrospective(Base):
    __tablename__ = "weekly_retrospectives"
    __table_args__ = (
        UniqueConstraint("week_start_date",
                              name="uq_weekly_retro_week_start"),
        Index("ix_weekly_retro_week_start", "week_start_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    week_start_date: Mapped[_date] = mapped_column(Date, index=True)
    week_end_date: Mapped[_date] = mapped_column(Date)

    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    closed_trades: Mapped[int] = mapped_column(Integer, default=0)
    realized_pnl_dollars: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_hold_minutes: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)

    # JSON-encoded top-N lists. Each entry is {key, pnl_dollars,
    # trade_count}. `key` is the ticker / pattern / family name.
    top_winning_tickers_json: Mapped[str] = mapped_column(
        String, default="[]")
    top_losing_tickers_json: Mapped[str] = mapped_column(
        String, default="[]")
    top_winning_patterns_json: Mapped[str] = mapped_column(
        String, default="[]")
    top_losing_patterns_json: Mapped[str] = mapped_column(
        String, default="[]")
    family_pnl_attribution_json: Mapped[str] = mapped_column(
        String, default="[]")

    catalyst_gate_saves_count: Mapped[int] = mapped_column(
        Integer, default=0)
    catalyst_gate_saves_dollars_estimated: Mapped[float] = mapped_column(
        Float, default=0.0)

    # JSON: {"rank_1": {trade_count, pnl_dollars}, "rank_2_3": {...},
    # "rank_4_plus": {...}, "no_eod_bias": {...}}
    conviction_multiplier_pnl_effect_json: Mapped[str] = mapped_column(
        String, default="{}")

    # Plain-English narrative (Claude-composed when an API key is
    # configured, deterministic fallback otherwise). Cached on the row.
    summary_paragraph: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    # Whether the summary was AI-composed (true) or rule-based fallback.
    summary_source: Mapped[str] = mapped_column(String, default="fallback")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        import json as _json
        def _decode(blob: Optional[str], default):
            if not blob:
                return default
            try:
                return _json.loads(blob)
            except Exception:
                return default
        return {
            "id": self.id,
            "week_start_date": (self.week_start_date.isoformat()
                                       if self.week_start_date else None),
            "week_end_date": (self.week_end_date.isoformat()
                                     if self.week_end_date else None),
            "total_trades": int(self.total_trades or 0),
            "closed_trades": int(self.closed_trades or 0),
            "realized_pnl_dollars": float(self.realized_pnl_dollars or 0.0),
            "win_rate": self.win_rate,
            "avg_hold_minutes": self.avg_hold_minutes,
            "top_winning_tickers": _decode(
                self.top_winning_tickers_json, []),
            "top_losing_tickers": _decode(
                self.top_losing_tickers_json, []),
            "top_winning_patterns": _decode(
                self.top_winning_patterns_json, []),
            "top_losing_patterns": _decode(
                self.top_losing_patterns_json, []),
            "family_pnl_attribution": _decode(
                self.family_pnl_attribution_json, []),
            "catalyst_gate_saves_count": int(
                self.catalyst_gate_saves_count or 0),
            "catalyst_gate_saves_dollars_estimated": float(
                self.catalyst_gate_saves_dollars_estimated or 0.0),
            "conviction_multiplier_pnl_effect": _decode(
                self.conviction_multiplier_pnl_effect_json, {}),
            "summary_paragraph": self.summary_paragraph,
            "summary_source": self.summary_source,
            "created_at": (self.created_at.isoformat()
                                  if self.created_at else None),
            "updated_at": (self.updated_at.isoformat()
                                  if self.updated_at else None),
        }
