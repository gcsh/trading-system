"""MITS Phase 14.B — Portfolio-level context with return correlations.

Where ``PortfolioRisk`` (in this package's ``__init__``) reasons about
sector / theme / beta exposures using static tables, ``PortfolioContext``
goes one level deeper: it walks the ``stock_bars`` table for every open
position + the candidate ticker, computes daily-return Pearson rhos
across the lookback window, and surfaces the pairwise matrix + the
candidate's worst correlation against the existing book.

Used by the correlation-cap gate to refuse to pile a fresh long onto a
position that is statistically the same trade, and by the
``/portfolio/context`` endpoint so the UI can show "Net long $X · Lev
Yx · SPY-3% → -Z%".

Fail-open everywhere: missing bars fall back to a sector/theme proxy,
SPY-stress falls back to crude beta math.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from backend.bot.portfolio_intel import beta_of, sector_of, themes_for
from backend.db import session_scope
from backend.models.stock_bar import StockBar


@dataclass
class PortfolioContext:
    equity: float = 0.0
    net_long_notional: float = 0.0
    net_short_notional: float = 0.0
    leverage: float = 0.0
    by_sector: Dict[str, float] = field(default_factory=dict)
    by_theme: Dict[str, float] = field(default_factory=dict)
    pairwise_correlation: Dict[str, Dict[str, float]] = field(default_factory=dict)
    candidate_max_correlation: Optional[float] = None
    candidate_max_correlation_peer: Optional[str] = None
    stress_spy_down_3pct_pnl: float = 0.0
    stress_spy_down_3pct_pct: float = 0.0
    computed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── correlation helpers ───────────────────────────────────────────────


def _daily_returns(closes: List[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev <= 0:
            continue
        out.append((closes[i] - prev) / prev)
    return out


def _pearson(a: List[float], b: List[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 5:
        return None
    a = a[-n:]
    b = b[-n:]
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = 0.0
    var_a = 0.0
    var_b = 0.0
    for x, y in zip(a, b):
        da = x - mean_a
        db = y - mean_b
        num += da * db
        var_a += da * da
        var_b += db * db
    denom = math.sqrt(var_a * var_b)
    if denom == 0:
        return None
    return num / denom


def _fetch_close_series(
    session, ticker: str, lookback_days: int,
) -> List[float]:
    """Return the daily closes for ``ticker`` over the last
    ``lookback_days``, oldest first. Empty list when bars are missing."""
    cutoff = datetime.utcnow() - timedelta(days=lookback_days * 2 + 5)
    rows = session.execute(
        select(StockBar.close, StockBar.bar_ts)
        .where(StockBar.ticker == ticker.upper())
        .where(StockBar.interval == "1d")
        .where(StockBar.bar_ts >= cutoff)
        .order_by(StockBar.bar_ts.asc())
    ).all()
    closes = [float(r[0]) for r in rows
              if r[0] is not None and float(r[0]) > 0]
    if len(closes) > lookback_days + 1:
        closes = closes[-(lookback_days + 1):]
    return closes


def _proxy_correlation(a: str, b: str) -> float:
    """Theme / sector proxy when bars are missing. Same theme=0.85,
    same sector=0.50, otherwise 0.10."""
    if a.upper() == b.upper():
        return 1.0
    themes_a = set(themes_for(a))
    themes_b = set(themes_for(b))
    if themes_a & themes_b:
        return 0.85
    if sector_of(a) == sector_of(b) and sector_of(a) != "Other":
        return 0.50
    return 0.10


# ── notional / direction helpers ──────────────────────────────────────


def _position_signed_notional(p: Dict[str, Any]) -> float:
    """Signed dollar exposure. Long stock = +mv, short stock = -mv.
    Long call / short put = positive delta-equivalent notional;
    long put / short call = negative. Uses ``entry_delta`` for options
    when available, falls back to ±0.5 of mv otherwise."""
    kind = (p.get("kind") or "stock").lower()
    qty = float(p.get("quantity") or 0)
    mv = p.get("market_value")
    if mv is None:
        mv = qty * float(p.get("current_price") or p.get("avg_cost") or 0)
    mv = float(mv)

    if kind == "stock":
        return mv if qty >= 0 else -abs(mv)

    delta = p.get("entry_delta")
    if delta is None:
        opt = (p.get("option_type") or "").lower()
        delta = 0.5 if opt == "call" else (-0.5 if opt == "put" else 0.0)
    try:
        delta_f = float(delta)
    except (TypeError, ValueError):
        delta_f = 0.0
    sign = 1.0 if qty >= 0 else -1.0
    return abs(mv) * delta_f * sign


def _position_direction(p: Dict[str, Any]) -> str:
    """Reduce a position to LONG / SHORT for correlation-cap logic."""
    signed = _position_signed_notional(p)
    return "LONG" if signed >= 0 else "SHORT"


# ── builder ───────────────────────────────────────────────────────────


def build_portfolio_context(
    *,
    positions: List[Dict[str, Any]],
    equity: float,
    candidate_ticker: Optional[str] = None,
    candidate_direction: Optional[str] = None,
    lookback_days: int = 60,
) -> PortfolioContext:
    """Compute a correlation-aware portfolio snapshot.

    Pairwise rhos are built from the ``stock_bars`` table; gaps fall
    back to the sector/theme proxy so the dict is always populated.
    """
    ctx = PortfolioContext(
        equity=round(float(equity or 0.0), 2),
        computed_at=datetime.utcnow().isoformat() + "Z",
    )

    long_notional = 0.0
    short_notional = 0.0
    sector_notional: Dict[str, float] = {}
    theme_notional: Dict[str, float] = {}
    gross_notional = 0.0
    ticker_signed: Dict[str, float] = {}

    for p in positions or []:
        ticker = (p.get("ticker") or "").upper()
        if not ticker:
            continue
        signed = _position_signed_notional(p)
        if signed == 0:
            continue
        gross_notional += abs(signed)
        if signed > 0:
            long_notional += signed
        else:
            short_notional += abs(signed)
        ticker_signed[ticker] = ticker_signed.get(ticker, 0.0) + signed

        sec = sector_of(ticker)
        sector_notional[sec] = sector_notional.get(sec, 0.0) + abs(signed)
        for theme in themes_for(ticker):
            theme_notional[theme] = theme_notional.get(theme, 0.0) + abs(signed)

    ctx.net_long_notional = round(long_notional, 2)
    ctx.net_short_notional = round(short_notional, 2)
    ctx.leverage = round(
        (gross_notional / ctx.equity) if ctx.equity > 0 else 0.0, 3,
    )

    if gross_notional > 0:
        ctx.by_sector = {
            s: round(v / gross_notional, 4)
            for s, v in sector_notional.items()
        }
        ctx.by_theme = {
            t: round(v / gross_notional, 4)
            for t, v in theme_notional.items()
        }

    # Tickers in the universe that we need correlations for: every open
    # position + the candidate (if any). Build a returns map once.
    tickers = sorted(set(ticker_signed.keys()))
    candidate_upper = candidate_ticker.upper() if candidate_ticker else None
    if candidate_upper and candidate_upper not in tickers:
        tickers_for_returns = tickers + [candidate_upper]
    else:
        tickers_for_returns = tickers

    returns_map: Dict[str, List[float]] = {}
    with session_scope() as session:
        for tk in tickers_for_returns:
            closes = _fetch_close_series(session, tk, lookback_days)
            rets = _daily_returns(closes)
            if len(rets) >= 5:
                returns_map[tk] = rets

    # Pairwise rho across existing positions. Symmetric matrix; only
    # store the upper triangle (each pair once but mirrored for easy UI
    # lookup).
    pair: Dict[str, Dict[str, float]] = {}
    for i, a in enumerate(tickers):
        for b in tickers[i + 1:]:
            rho = None
            if a in returns_map and b in returns_map:
                rho = _pearson(returns_map[a], returns_map[b])
            if rho is None:
                rho = _proxy_correlation(a, b)
            rho = round(float(rho), 3)
            pair.setdefault(a, {})[b] = rho
            pair.setdefault(b, {})[a] = rho
    ctx.pairwise_correlation = pair

    # Candidate-aware max-correlation peer. Also extends ``pair`` with
    # candidate ↔ existing rows so the correlation-cap gate can read
    # the candidate's row directly off the matrix.
    if candidate_upper:
        worst_rho = None
        worst_peer = None
        for held in tickers:
            if held == candidate_upper:
                continue
            rho = None
            if (candidate_upper in returns_map and held in returns_map):
                rho = _pearson(
                    returns_map[candidate_upper], returns_map[held],
                )
            if rho is None:
                rho = _proxy_correlation(candidate_upper, held)
            rho = float(rho)
            rho_rounded = round(rho, 3)
            pair.setdefault(candidate_upper, {})[held] = rho_rounded
            pair.setdefault(held, {})[candidate_upper] = rho_rounded
            if worst_rho is None or abs(rho) > abs(worst_rho):
                worst_rho = rho
                worst_peer = held
        if worst_rho is not None:
            ctx.candidate_max_correlation = round(worst_rho, 3)
            ctx.candidate_max_correlation_peer = worst_peer

    # SPY -3% stress: project per-ticker drop via beta_of(ticker) * -0.03
    # against signed notional. For options, signed notional already
    # incorporates entry_delta so we don't double-scale.
    stress_pnl = 0.0
    for ticker, signed in ticker_signed.items():
        beta = beta_of(ticker)
        stress_pnl += signed * beta * (-0.03)
    ctx.stress_spy_down_3pct_pnl = round(stress_pnl, 2)
    ctx.stress_spy_down_3pct_pct = round(
        (stress_pnl / ctx.equity) if ctx.equity > 0 else 0.0, 4,
    )

    return ctx
