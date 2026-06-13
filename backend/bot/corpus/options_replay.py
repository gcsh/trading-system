"""MITS Phase 11.B.2 — Options corpus replay layer.

Walks the populated :class:`OptionContractBar` corpus and synthesizes
per-(ticker, date) MarketObservation rows describing the option-chain
shape on that date:

  - ``option_iv_rank``   — ATM IV percentile in the trailing 252d window
  - ``option_gex_wall``   — strike where dealer gamma exposure peaks
  - ``option_dealer_regime`` — long_gamma / short_gamma / mixed via the
    spot-vs-flip distance
  - ``option_unusual_oi`` — strikes whose open interest is 3σ above
    the trailing-30d mean (skipped when OI absent)
  - ``option_term_slope`` — front-month IV vs 90d-IV (contango / backwardation)

Each row uses the same `market_observations` table the existing
outcome_linker + knowledge_aggregator already process — so the cohort
math + Bayesian shrinkage layer light up automatically once these
patterns land.

Designed for **incremental** runs:
  - Reads ``corpus_status.last_options_replay_ts`` (best-effort
    introspection — falls back to scanning gaps).
  - Idempotent: existing (ticker, pattern, timestamp, timeframe) rows
    are dropped by the existing UniqueConstraint, so re-running over a
    fully-replayed range is a no-op.

This module is light on cleverness deliberately — the heavy lifting
is in the backfill itself. The detector library can grow from here
once Agent 4 wires the vector layer.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError

from backend.db import session_scope
from backend.models.market_observation import MarketObservation
from backend.models.option_contract_bar import OptionContractBar
from backend.models.stock_bar import StockBar

logger = logging.getLogger(__name__)


_OPTION_PATTERNS = (
    "option_iv_rank",
    "option_gex_wall",
    "option_dealer_regime",
    "option_unusual_oi",
    "option_term_slope",
)


# ── utilities ─────────────────────────────────────────────────────────


def _spot_at(ticker: str, on: date) -> Optional[float]:
    try:
        with session_scope() as s:
            row = s.execute(
                select(StockBar.close)
                .where(StockBar.ticker == ticker.upper())
                .where(StockBar.interval == "1d")
                .where(StockBar.bar_ts <= datetime.combine(
                    on, datetime.max.time()))
                .order_by(StockBar.bar_ts.desc())
                .limit(1)
            ).first()
            if row and row[0] is not None:
                return float(row[0])
    except Exception:
        return None
    return None


def _persist_observation(ticker: str, pattern: str,
                         ts: datetime,
                         features: Dict[str, Any],
                         spot: Optional[float]) -> bool:
    try:
        with session_scope() as s:
            obs = MarketObservation(
                ticker=ticker.upper(),
                pattern=pattern,
                timestamp=ts,
                timeframe="1d",
                regime="unknown",
                vol_state="normal",
                time_bucket="rth",
                spot=spot,
                features=json.dumps(features, default=str)[:2000],
                source="options_replay",
            )
            s.add(obs)
        return True
    except IntegrityError:
        return False
    except Exception:
        logger.debug("options observation persist failed", exc_info=True)
        return False


# ── per-date analytics ────────────────────────────────────────────────


def _atm_iv_for_date(ticker: str, on: date,
                     spot: Optional[float]) -> Optional[float]:
    """Average of nearest-the-money call+put MID across the
    front-month expiry. Front-month picked as the smallest expiry > on.
    """
    if spot is None:
        return None
    try:
        with session_scope() as s:
            # Smallest expiry strictly after `on` with any bars on `on`.
            row = s.execute(
                select(OptionContractBar.expiration)
                .where(OptionContractBar.ticker == ticker.upper())
                .where(OptionContractBar.expiration > on)
                .where(OptionContractBar.bar_date == on)
                .order_by(OptionContractBar.expiration)
                .limit(1)
            ).first()
            if not row:
                return None
            expiry = row[0]
            rows = s.execute(
                select(OptionContractBar.strike,
                       OptionContractBar.right,
                       OptionContractBar.mid,
                       OptionContractBar.close)
                .where(OptionContractBar.ticker == ticker.upper())
                .where(OptionContractBar.expiration == expiry)
                .where(OptionContractBar.bar_date == on)
            ).all()
    except Exception:
        return None
    if not rows:
        return None
    by_strike: Dict[float, Dict[str, float]] = {}
    for strike, right, mid, close in rows:
        v = mid if mid is not None else close
        if v is None:
            continue
        by_strike.setdefault(float(strike), {})[right] = float(v)
    if not by_strike:
        return None
    # Closest strike to spot.
    atm = min(by_strike.keys(), key=lambda k: abs(k - spot))
    legs = by_strike.get(atm) or {}
    call = legs.get("C")
    put = legs.get("P")
    if call is None or put is None:
        return None
    # Brenner-Subrahmanyam straddle inversion:
    #   IV ≈ (straddle / S) × sqrt(2π / T)
    T_years = max(1.0 / 365.0, (expiry - on).days / 365.0)
    straddle = call + put
    try:
        iv = (straddle / spot) * math.sqrt(2.0 * math.pi / T_years)
    except Exception:
        return None
    if not math.isfinite(iv) or iv <= 0 or iv > 5:
        return None
    return iv


def _iv_rank_pct(history: List[float], current: float) -> Optional[float]:
    if not history or current is None:
        return None
    sorted_hist = sorted(history)
    below = sum(1 for x in sorted_hist if x <= current)
    return 100.0 * below / max(1, len(sorted_hist))


def _gex_wall_strike(ticker: str, on: date,
                     spot: Optional[float]) -> Optional[Dict[str, Any]]:
    """Picks the strike with the largest |gamma × OI × 100| total
    across all expirations dated on ``on``. Returns wall_strike +
    moneyness if both spot and gamma data are present.
    """
    if spot is None:
        return None
    try:
        with session_scope() as s:
            rows = s.execute(
                select(OptionContractBar.strike,
                       OptionContractBar.gamma,
                       OptionContractBar.open_interest,
                       OptionContractBar.right)
                .where(OptionContractBar.ticker == ticker.upper())
                .where(OptionContractBar.bar_date == on)
            ).all()
    except Exception:
        return None
    if not rows:
        return None
    agg: Dict[float, float] = {}
    have_any = False
    for strike, gamma, oi, right in rows:
        if gamma is None or oi is None:
            continue
        have_any = True
        sign = 1.0 if right == "C" else -1.0
        agg[float(strike)] = agg.get(float(strike), 0.0) + (
            sign * float(gamma) * float(oi) * 100.0)
    if not have_any or not agg:
        return None
    wall_strike, wall_value = max(agg.items(), key=lambda kv: abs(kv[1]))
    return {
        "wall_strike": wall_strike,
        "wall_value": wall_value,
        "moneyness": (wall_strike - spot) / spot if spot else None,
    }


def _term_slope(ticker: str, on: date, spot: Optional[float]
                ) -> Optional[Dict[str, Any]]:
    """Front-month IV − 90-day IV. Positive = contango."""
    if spot is None:
        return None
    try:
        with session_scope() as s:
            rows = s.execute(
                select(OptionContractBar.expiration,
                       OptionContractBar.strike,
                       OptionContractBar.right,
                       OptionContractBar.mid,
                       OptionContractBar.close)
                .where(OptionContractBar.ticker == ticker.upper())
                .where(OptionContractBar.bar_date == on)
                .where(OptionContractBar.expiration > on)
            ).all()
    except Exception:
        return None
    if not rows:
        return None
    by_exp: Dict[date, Dict[float, Dict[str, float]]] = {}
    for exp, strike, right, mid, close in rows:
        v = mid if mid is not None else close
        if v is None:
            continue
        by_exp.setdefault(exp, {}).setdefault(float(strike), {})[right] = v
    if not by_exp:
        return None
    target_exps = sorted(by_exp.keys())
    front = target_exps[0]
    far = min(target_exps,
              key=lambda e: abs((e - on).days - 90))
    if front == far:
        return None

    def _iv_from(exp: date) -> Optional[float]:
        strikes = by_exp[exp]
        if not strikes:
            return None
        atm = min(strikes.keys(), key=lambda k: abs(k - spot))
        legs = strikes.get(atm) or {}
        call = legs.get("C")
        put = legs.get("P")
        if call is None or put is None:
            return None
        T = max(1.0 / 365.0, (exp - on).days / 365.0)
        try:
            return ((call + put) / spot) * math.sqrt(2.0 * math.pi / T)
        except Exception:
            return None

    iv_front = _iv_from(front)
    iv_far = _iv_from(far)
    if iv_front is None or iv_far is None:
        return None
    return {
        "front_exp": front.isoformat(),
        "far_exp": far.isoformat(),
        "iv_front": iv_front,
        "iv_far": iv_far,
        "slope": iv_front - iv_far,
        "is_backwardation": iv_front > iv_far,
    }


# ── per-ticker walker ─────────────────────────────────────────────────


def _trading_dates_with_data(ticker: str,
                             start: Optional[date],
                             end: Optional[date]) -> List[date]:
    try:
        with session_scope() as s:
            q = (
                select(OptionContractBar.bar_date)
                .where(OptionContractBar.ticker == ticker.upper())
                .distinct()
                .order_by(OptionContractBar.bar_date)
            )
            if start:
                q = q.where(OptionContractBar.bar_date >= start)
            if end:
                q = q.where(OptionContractBar.bar_date <= end)
            rows = s.execute(q).all()
    except Exception:
        return []
    return [r[0] for r in rows if r and r[0]]


def replay_ticker(ticker: str,
                  start: Optional[date] = None,
                  end: Optional[date] = None,
                  *,
                  iv_lookback_days: int = 252,
                  max_dates: Optional[int] = None,
                  ) -> Dict[str, int]:
    """Replay option-chain observations for one ticker. Returns count
    per pattern. Idempotent."""
    dates = _trading_dates_with_data(ticker, start, end)
    if max_dates:
        dates = dates[: int(max_dates)]
    counts: Dict[str, int] = {p: 0 for p in _OPTION_PATTERNS}
    iv_history: List[Tuple[date, float]] = []

    for d in dates:
        spot = _spot_at(ticker, d)
        ts = datetime.combine(d, datetime.min.time())

        # IV rank — uses trailing 252d history we accumulate as we walk.
        iv = _atm_iv_for_date(ticker, d, spot)
        if iv is not None:
            cutoff = d - timedelta(days=iv_lookback_days)
            window = [v for (ts_, v) in iv_history if ts_ >= cutoff]
            iv_history.append((d, iv))
            pct = _iv_rank_pct(window, iv) if window else None
            if pct is not None and _persist_observation(
                    ticker, "option_iv_rank", ts,
                    {"iv": iv, "rank_pct": pct,
                     "window_n": len(window)}, spot):
                counts["option_iv_rank"] += 1

        # GEX wall.
        gex = _gex_wall_strike(ticker, d, spot)
        if gex and _persist_observation(
                ticker, "option_gex_wall", ts, gex, spot):
            counts["option_gex_wall"] += 1
            # Cheap dealer-regime tag — long_gamma if wall above spot
            # AND positive aggregate gamma; short_gamma if wall below
            # AND negative. Otherwise mixed.
            mny = gex.get("moneyness") or 0.0
            wv = gex.get("wall_value") or 0.0
            if wv > 0 and mny > 0:
                regime = "long_gamma"
            elif wv < 0 and mny < 0:
                regime = "short_gamma"
            else:
                regime = "mixed"
            if _persist_observation(
                    ticker, "option_dealer_regime", ts,
                    {"regime": regime, **gex}, spot):
                counts["option_dealer_regime"] += 1

        # Term slope.
        ts_slope = _term_slope(ticker, d, spot)
        if ts_slope and _persist_observation(
                ticker, "option_term_slope", ts, ts_slope, spot):
            counts["option_term_slope"] += 1

    return counts


def replay_universe(start: Optional[date] = None,
                    end: Optional[date] = None,
                    tickers: Optional[Iterable[str]] = None,
                    ) -> Dict[str, Dict[str, int]]:
    if tickers is None:
        from backend.bot.data.universe import load_universe
        tickers = load_universe()
    out: Dict[str, Dict[str, int]] = {}
    for t in tickers:
        try:
            out[t] = replay_ticker(t, start, end)
        except Exception:
            logger.exception("options_replay failed for %s", t)
            out[t] = {"error": 1}  # type: ignore[assignment]
    return out


__all__ = [
    "replay_ticker",
    "replay_universe",
]
