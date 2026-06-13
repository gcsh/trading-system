"""MITS Phase 3 — End-of-Day analysis batch.

Runs after the close (16:30 ET weekdays). For each ticker in the
watchlist + canonical ETF benchmarks:

  1. Pull today's intraday bars + last 10d daily bars.
  2. Run `detect_all` (respecting operator-disabled detectors).
  3. For every pattern that fired, query the knowledge graph for the
     cohort posterior + sample size.
  4. Pick the top-3 patterns by `posterior * log(1 + sample_size)`.
  5. Compose ONE Claude call producing the per-ticker thesis + suggested
     options setup.
  6. Persist as `EodAnalysis` (UPSERT on (ticker, analysis_date)).

Idempotent — re-running the same day overwrites the day's row.

Tomorrow's setup digest pulls the top-N rows ordered by rank_score and
formats them for the Telegram + the /tomorrow UI page.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date as _date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401

from sqlalchemy import desc, select

from backend.bot.corpus.knowledge_graph import (
    get_posterior_with_fallback,
)
from backend.bot.data.bars import bars_to_dataframe, fetch_bars as _shared_fetch_bars
from backend.bot.detectors import (
    DETECTOR_REGISTRY, detect_all, disabled_patterns,
)
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.brain_prediction import BrainPrediction
from backend.models.eod_analysis import EodAnalysis
from backend.models.knowledge_graph_cell import KnowledgeGraphCell
from backend.models.watchlist import WatchlistItem

logger = logging.getLogger(__name__)


# Canonical ETF benchmarks always covered, regardless of watchlist.
ETF_BENCHMARKS = ("SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE")

# Suggested-action gating (same thresholds as analysis route).
SUGGESTED_ACTION_MIN_POSTERIOR = 0.60
SUGGESTED_ACTION_MIN_SAMPLES = 30

# Phase 12.2 — EOD-pass cohort gate is intentionally looser than the
# suggested-action gate. We still want to RANK a pattern that has
# only N=15 in the cohort (so the operator sees it in the digest), we
# just don't push a suggested option strike unless it clears the
# 30-N / 60% threshold above.
EOD_COHORT_MIN_SAMPLES = int(
    getattr(TUNABLES, "eod_cohort_min_samples", 15) or 15
)


# ── data helpers ──────────────────────────────────────────────────────


def _fetch_intraday_df(ticker: str):
    """Pull today's intraday bars (5min) for the ticker. ThetaData-first
    (MITS Phase 4 P4.3), falls back to yfinance."""
    try:
        payload = _shared_fetch_bars(
            ticker, window="today", interval="5m", lookback_days=1,
        )
        df = bars_to_dataframe(payload.get("bars") or [])
        return df
    except Exception:
        logger.debug("eod intraday fetch failed for %s", ticker, exc_info=True)
        return None


def _fetch_daily_df(ticker: str):
    """Pull last 10d daily bars for context. ThetaData-first."""
    try:
        payload = _shared_fetch_bars(
            ticker, window="all", interval="1d", lookback_days=10,
        )
        df = bars_to_dataframe(payload.get("bars") or [])
        return df
    except Exception:
        logger.debug("eod daily fetch failed for %s", ticker, exc_info=True)
        return None


def _resolve_universe() -> List[str]:
    tickers: List[str] = []
    try:
        with session_scope() as s:
            tickers.extend(
                w.ticker.upper().strip()
                for w in s.query(WatchlistItem).all()
                if w.ticker and w.ticker.strip()
            )
    except Exception:
        logger.debug("eod watchlist fetch failed", exc_info=True)
    for bench in ETF_BENCHMARKS:
        if bench not in tickers:
            tickers.append(bench)
    return tickers


# ── scoring + cohort fetch ────────────────────────────────────────────


def _rank_score(posterior: float, sample_size: int) -> float:
    """Confidence-weighted ranking.

    posterior * log(1 + sample_size) — rewards strong posteriors AND
    statistical depth simultaneously. A 70% posterior on N=500 beats
    a 90% posterior on N=10 (which is statistical noise).
    """
    try:
        p = max(0.0, min(1.0, float(posterior or 0.0)))
        n = max(0, int(sample_size or 0))
        return p * math.log1p(n)
    except Exception:
        return 0.0


def _cohort_lookup(
    ticker: str, patterns: List[str],
    regime: str = "unknown", vol_state: str = "normal",
) -> Dict[str, Dict[str, Any]]:
    """Per-pattern best cohort posterior for the ticker.

    Phase 12.2 — now uses the hierarchical fallback
    (``get_posterior_with_fallback``) so the EOD pass surfaces
    setups even when the direction-aware split halved per-cell N
    below 30. Previously the direct DB select returned the local
    cell at any sample size, but the suggested-action gate at N≥30
    + posterior≥0.60 dropped almost every pattern → 622 patterns
    fired, 0 setups landed.

    The fallback chain (cell → pattern_regime parent → pattern
    parent) returns the most specific posterior available with
    enough samples, so most patterns now have a real cohort signal.

    Skips operator-disabled patterns.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not patterns:
        return out
    disabled = disabled_patterns()
    for pat in patterns:
        if pat in disabled:
            continue
        try:
            entry = get_posterior_with_fallback(
                ticker=ticker, pattern=pat,
                regime=regime or "unknown",
                vol_state=vol_state or "normal",
                horizon="5d",
                sample_split="combined",
            )
        except Exception:
            logger.debug("eod fallback lookup failed for %s/%s",
                         ticker, pat, exc_info=True)
            entry = None
        if entry is None:
            continue
        n = int(entry.get("n") or 0)
        if n < EOD_COHORT_MIN_SAMPLES:
            # Skip patterns where even the global parent is too thin
            # — those would produce noise-grade thesis text.
            continue
        # MITS Phase 13 Fix 8 — pass CI bounds through so the EOD
        # composer / Brain prompt can flag wide-CI posteriors.
        lo = entry.get("confidence_lower")
        hi = entry.get("confidence_upper")
        ci_width = None
        ci_warning = None
        if lo is not None and hi is not None:
            try:
                ci_width = round(float(hi) - float(lo), 4)
                try:
                    thresh = float(TUNABLES.cohort_ci_width_warn_threshold)
                except Exception:
                    thresh = 0.20
                if ci_width > thresh:
                    post_pct = round(
                        float(entry.get("posterior") or 0.0) * 100.0, 1)
                    width_pp = round(ci_width * 100.0 / 2.0, 1)
                    ci_warning = (
                        f"posterior {post_pct}% (wide CI ±{width_pp}pp "
                        "— use with caution)"
                    )
            except Exception:
                ci_width = None
        out[pat] = {
            "sample_size": n,
            "posterior_win_rate": float(entry.get("posterior") or 0.0),
            "win_rate": float(entry.get("win_rate") or 0.0),
            "regime": entry.get("regime"),
            "vol_state": entry.get("vol_state"),
            "horizon": entry.get("horizon") or "5d",
            "avg_return_pct": entry.get("avg_return_pct"),
            "avg_hold_minutes": entry.get("avg_hold_minutes"),
            "confidence_lower": lo,
            "confidence_upper": hi,
            "ci_width": ci_width,
            "ci_warning": ci_warning,
            "cohort_source": entry.get("source"),
        }
    return out


def _phase11_ticker_signals(ticker: str) -> Dict[str, Any]:
    """MITS Phase 11.I — fetch insider cluster + smart-money + parity-warn
    signals for ``ticker``. Used to boost/demote EOD setup rank.

    Returns:
      {
        "insider_cluster_buy": bool — 3+ insiders bought (last 30d)
        "smart_money_added": bool   — top-25 funds net-added shares (latest Q)
        "parity_warn_share": float  — share of recent obs flagged parity_warn
      }
    Empty/zero on any failure. Pure read-only.
    """
    out = {
        "insider_cluster_buy": False,
        "smart_money_added": False,
        "parity_warn_share": 0.0,
    }
    try:
        from datetime import date as _date, timedelta as _td
        from sqlalchemy import func as _func
        from backend.models.insider_trade import InsiderTrade
        from backend.models.fund_holding import FundHolding
        from backend.models.market_observation import MarketObservation
        with session_scope() as s:
            cutoff = _date.today() - _td(days=30)
            buyers = s.execute(
                select(_func.count(_func.distinct(
                    InsiderTrade.insider_name)))
                .where(InsiderTrade.ticker == ticker)
                .where(InsiderTrade.transaction_code == "P")
                .where(InsiderTrade.transaction_date >= cutoff)
            ).scalar() or 0
            out["insider_cluster_buy"] = int(buyers) >= 3

            latest_q = s.execute(
                select(_func.max(FundHolding.quarter_end_date))
                .where(FundHolding.ticker == ticker)
            ).scalar()
            if latest_q is not None:
                delta_sum = s.execute(
                    select(_func.sum(FundHolding.change_from_prior_qtr))
                    .where(FundHolding.ticker == ticker)
                    .where(FundHolding.quarter_end_date == latest_q)
                ).scalar() or 0.0
                out["smart_money_added"] = float(delta_sum) > 0

            obs_window = _date.today() - _td(days=180)
            total_obs = s.execute(
                select(_func.count(MarketObservation.id))
                .where(MarketObservation.ticker == ticker)
                .where(MarketObservation.timestamp >= datetime.combine(
                    obs_window, datetime.min.time()))
            ).scalar() or 0
            warn_obs = 0
            try:
                warn_obs = s.execute(
                    select(_func.count(MarketObservation.id))
                    .where(MarketObservation.ticker == ticker)
                    .where(MarketObservation.parity_warn.is_(True))
                    .where(MarketObservation.timestamp >= datetime.combine(
                        obs_window, datetime.min.time()))
                ).scalar() or 0
            except Exception:
                # parity_warn column may not exist on very old DBs.
                warn_obs = 0
            if total_obs > 0:
                out["parity_warn_share"] = float(warn_obs) / float(total_obs)
    except Exception:
        logger.debug("phase11 signals load failed for %s", ticker,
                          exc_info=True)
    return out


def _pick_top_patterns(
    cohorts: Dict[str, Dict[str, Any]], top_n: int = 3,
    ticker: Optional[str] = None,
) -> List[Tuple[str, Dict[str, Any], float]]:
    """Return [(pattern, cohort_dict, rank_score)] sorted desc by rank_score.

    MITS Phase 11.I — when ``ticker`` is provided, factor in:
      - +15% boost if insider cluster-buy in last 30d
      - +10% boost if smart money (top-25 funds) net-added this quarter
      - −20% penalty when ≥30% of recent obs are parity_warn=True

    Multipliers are TUNABLES — defaults are conservative.
    """
    boost_cfg = {
        "insider_cluster": float(getattr(
            TUNABLES, "eod_rank_boost_insider_cluster", 1.15)),
        "smart_money": float(getattr(
            TUNABLES, "eod_rank_boost_smart_money", 1.10)),
        "parity_warn_penalty": float(getattr(
            TUNABLES, "eod_rank_penalty_parity_warn", 0.80)),
        "parity_warn_threshold": float(getattr(
            TUNABLES, "eod_rank_parity_warn_threshold", 0.30)),
    }
    signals = _phase11_ticker_signals(ticker) if ticker else {}

    ranked: List[Tuple[str, Dict[str, Any], float]] = []
    for pat, c in cohorts.items():
        score = _rank_score(c.get("posterior_win_rate") or 0.0,
                                c.get("sample_size") or 0)
        if signals.get("insider_cluster_buy"):
            score *= boost_cfg["insider_cluster"]
        if signals.get("smart_money_added"):
            score *= boost_cfg["smart_money"]
        warn_share = float(signals.get("parity_warn_share") or 0.0)
        if warn_share >= boost_cfg["parity_warn_threshold"]:
            score *= boost_cfg["parity_warn_penalty"]
        # Store the signals on the cohort so downstream consumers can
        # render the boosted reason ("ranked +12% — insider cluster").
        c.setdefault("phase11_signals", signals)
        ranked.append((pat, c, score))
    ranked.sort(key=lambda t: t[2], reverse=True)
    return ranked[:top_n]


# ── claude composition (1 call per ticker) ────────────────────────────


_BULLISH_PATTERNS = {
    "bull_flag", "pennant", "consolidation", "breakout",
    "pullback", "failed_breakdown", "vwap_reclaim",
    "hvn_acceptance", "iv_expansion", "gex_acceleration",
    "break_of_structure",
}
_BEARISH_PATTERNS = {
    "bear_flag", "failed_breakout", "vwap_rejection",
    "lvn_rejection", "iv_compression", "change_of_character",
    "liquidity_sweep", "stop_hunt",
}


def _resolve_suggested_strike(
    ticker: str, spot: float, direction: str, dte_target: int,
) -> Tuple[Optional[float], str]:
    """MITS Phase 4 (P4.4) — listed-chain strike via chain_strike, with
    snap_strike fallback when ThetaData isn't reachable."""
    if not spot or spot <= 0:
        return None, "snap_fallback"
    kind = "call" if direction in ("long_call", "call_spread") else "put"
    sign = +1.0 if kind == "call" else -1.0
    target_delta = 0.40
    moneyness = 0.01 * sign
    try:
        from backend.bot.data.options import chain_strike, snap_strike
    except Exception:
        return None, "snap_fallback"
    try:
        listed = chain_strike(
            ticker, spot, kind,
            moneyness=moneyness,
            target_dte=int(dte_target),
            target_delta=target_delta,
        )
        if listed and listed > 0:
            arithmetic = snap_strike(spot, kind, moneyness)
            if abs(listed - arithmetic) < 1e-6:
                return float(listed), "snap_fallback"
            return float(listed), "chain"
    except Exception:
        pass
    try:
        return float(snap_strike(spot, kind, moneyness)), "snap_fallback"
    except Exception:
        return None, "snap_fallback"


def _suggested_action(
    pattern: str, cohort: Dict[str, Any], ticker: str,
    spot: Optional[float],
) -> Optional[Dict[str, Any]]:
    """Mirror analysis.py — gated on posterior + samples, chain-aware
    strike from MITS Phase 4 (P4.4)."""
    post = cohort.get("posterior_win_rate") or 0.0
    n = int(cohort.get("sample_size") or 0)
    if post < SUGGESTED_ACTION_MIN_POSTERIOR or n < SUGGESTED_ACTION_MIN_SAMPLES:
        return None
    if pattern in _BEARISH_PATTERNS:
        action = "BUY_PUT"
        direction = "long_put"
    elif pattern in _BULLISH_PATTERNS:
        action = "BUY_CALL"
        direction = "long_call"
    else:
        return None
    dte_target = 30
    strike, strike_source = _resolve_suggested_strike(
        ticker, float(spot or 0.0), direction, dte_target,
    )
    return {
        "action": action,
        "direction": direction,
        "strike": strike,
        "strike_source": strike_source,
        "dte": dte_target,
        "dte_target": dte_target,
        "target_premium_pct": 50,
        "stop_premium_pct": 30,
        "rationale": (
            f"{pattern} on {ticker}: posterior {post*100:.0f}% "
            f"(N={n})."
        ),
    }


def _compose_thesis(
    ticker: str, top: List[Tuple[str, Dict[str, Any], float]],
    spot: Optional[float],
) -> Dict[str, Any]:
    """Phase 14.A — wrap the hybrid composer (force_deep=True so EOD
    always tries the deep Claude path on the single primary pattern).
    The returned dict carries the legacy keys + ``uncertainty_signal``
    so the suggested_action_json can ride it forward without a schema
    change.
    """
    if not top:
        return {
            "headline": "",
            "thesis_paragraph": "",
            "suggested_action": None,
            "invalidation": [],
            "uncertainty_signal": {},
        }
    primary_pat, primary_cohort, _ = top[0]
    knowledge = {primary_pat: primary_cohort}
    bars: List[Dict[str, Any]] = [{"close": spot}] if spot else []

    # MITS Phase 15.A — give the deep composer the consolidated regime view.
    regime_summary: Optional[str] = None
    try:
        from backend.bot.features import build_features
        from backend.bot.regime.vector import build_regime_vector
        snapshot = {"price": float(spot) if spot else 0.0}
        snapshot["features"] = build_features(snapshot)
        regime_summary = build_regime_vector(
            ticker=ticker, snapshot=snapshot,
        ).summary_text()
    except Exception:
        logger.debug("eod regime_vector build failed for %s", ticker,
                          exc_info=True)

    from backend.bot.analysis import compose_hybrid

    ensemble = compose_hybrid(
        ticker=ticker, window="today",
        knowledge=knowledge, observations=[],
        bars=bars, features=None,
        deep_top_n=1, force_deep=True,
        regime_vector_summary=regime_summary,
    )
    chosen = (ensemble.chosen or {}).get(primary_pat) or {}
    fallback_invalidation = [
        "Pattern fails on tomorrow's open (>1% counter-move)",
        "Volume dries up below 20-bar median",
        "Regime flips counter to the cohort regime",
    ]
    return {
        "headline": (chosen.get("headline") or "")[:240],
        "thesis_paragraph": (chosen.get("thesis_paragraph") or "")[:1600],
        "suggested_action": chosen.get("suggested_action"),
        "invalidation": (chosen.get("invalidation")
                              or fallback_invalidation)[:6],
        "uncertainty_signal": (
            (ensemble.uncertainty_signal or {}).get(primary_pat) or {}
        ),
    }


# ── persist ───────────────────────────────────────────────────────────


def _upsert_row(session, ticker: str, analysis_date: _date,
                  patterns_fired: List[str],
                  top: List[Tuple[str, Dict[str, Any], float]],
                  composed: Dict[str, Any]) -> str:
    row = session.execute(
        select(EodAnalysis)
        .where(EodAnalysis.ticker == ticker)
        .where(EodAnalysis.analysis_date == analysis_date)
    ).scalar_one_or_none()
    primary_pattern = top[0][0] if top else None
    primary_cohort = top[0][1] if top else {}
    primary_score = top[0][2] if top else 0.0
    action = "updated"
    if row is None:
        row = EodAnalysis(ticker=ticker, analysis_date=analysis_date)
        session.add(row)
        action = "inserted"
    row.patterns_fired = json.dumps(patterns_fired)
    row.top_pattern = primary_pattern
    row.top_posterior = primary_cohort.get("posterior_win_rate")
    row.top_sample_size = primary_cohort.get("sample_size")
    row.confidence = primary_cohort.get("posterior_win_rate")
    row.thesis_paragraph = composed.get("thesis_paragraph")
    row.headline = composed.get("headline")
    row.suggested_action_json = (
        json.dumps(composed.get("suggested_action"))
        if composed.get("suggested_action") is not None else None
    )
    row.invalidation_json = json.dumps(composed.get("invalidation") or [])
    # Phase 14.A — ride the per-pattern uncertainty signal in the
    # suggested_action_json envelope when an action exists. The EOD
    # schema doesn't gain a new column; consumers reading the JSON get
    # the extra key transparently.
    uncert = composed.get("uncertainty_signal") or {}
    sa_obj = composed.get("suggested_action")
    if isinstance(sa_obj, dict) and uncert:
        sa_obj = dict(sa_obj)
        sa_obj["uncertainty_signal"] = uncert
        row.suggested_action_json = json.dumps(sa_obj)
    row.rank_score = float(primary_score)
    return action


def _persist_brain_prediction_eod(
    session, *, ticker: str,
    top: List[Tuple[str, Dict[str, Any], float]],
    composed: Dict[str, Any],
    regime_vector: Optional[Dict[str, Any]] = None,
    top_strategy: Optional[Dict[str, Any]] = None,
) -> None:
    """MITS Phase 14.D — log the EOD-pass composition into the brain
    prediction ledger. Best-effort; failures must not block the EOD
    pass.

    MITS Phase 15.E — stamps decision-time JSON snapshots of the
    regime vector and top strategy when supplied.
    """
    try:
        sa = composed.get("suggested_action")
        if not isinstance(sa, dict) or not sa.get("action"):
            return
        primary_pat = top[0][0] if top else None
        primary_cohort = top[0][1] if top else {}
        invalidation = composed.get("invalidation") or []
        session.add(BrainPrediction(
            surface="eod_analysis",
            ticker=ticker,
            window=None,
            pattern=primary_pat,
            suggested_action=sa.get("action"),
            suggested_direction=sa.get("direction"),
            suggested_strike=sa.get("strike"),
            suggested_dte=sa.get("dte"),
            posterior_at_decision=primary_cohort.get("posterior_win_rate"),
            sample_size_at_decision=primary_cohort.get("sample_size"),
            confidence_self_assessment=None,
            invalidation_json=json.dumps(list(invalidation)),
            thesis_paragraph=composed.get("thesis_paragraph"),
            regime_at_decision=(
                json.dumps(regime_vector) if regime_vector else None
            ),
            top_strategy_at_decision=(
                json.dumps(top_strategy) if top_strategy else None
            ),
        ))
    except Exception:
        logger.debug("eod brain prediction persist failed", exc_info=True)


# ── public API ────────────────────────────────────────────────────────


def run_eod_pass(date: Optional[_date] = None,
                    tickers: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run the end-of-day analysis pipeline.

    Returns a stats dict:
        {
          "analysis_date": "2026-06-05",
          "tickers_analyzed": 13,
          "total_patterns_fired": 47,
          "rows_inserted": N,
          "rows_updated": M,
          "top_setups": [
              {"rank": 1, "ticker": "NVDA", "pattern": "bull_flag",
               "posterior": 0.72, "sample_size": 412, "rank_score": 4.34}
          ]
        }
    """
    analysis_date = date or datetime.utcnow().date()
    universe = tickers if tickers is not None else _resolve_universe()
    stats = {
        "analysis_date": analysis_date.isoformat(),
        "tickers_analyzed": 0,
        "total_patterns_fired": 0,
        "rows_inserted": 0,
        "rows_updated": 0,
        "top_setups": [],
    }
    ranked_setups: List[Dict[str, Any]] = []

    for ticker in universe:
        try:
            intraday_df = _fetch_intraday_df(ticker)
            daily_df = _fetch_daily_df(ticker)
            # Prefer intraday if available, fall back to daily.
            primary_df = intraday_df if intraday_df is not None and len(
                intraday_df) >= 5 else daily_df
            if primary_df is None or len(primary_df) < 5:
                continue
            try:
                obs = detect_all(ticker, primary_df)
            except Exception:
                logger.debug("eod detect_all failed for %s", ticker,
                                exc_info=True)
                obs = []
            patterns = sorted({o.pattern for o in obs})
            stats["total_patterns_fired"] += len(patterns)
            # Best-effort regime + vol from the most recent observation
            # so the cohort lookup hits the right (pattern,regime) parent
            # cell before falling back to global pattern.
            primary_regime = "unknown"
            primary_vol = "normal"
            try:
                if obs:
                    last = obs[-1]
                    primary_regime = (
                        getattr(last, "regime", None) or "unknown"
                    )
                    primary_vol = (
                        getattr(last, "vol_state", None) or "normal"
                    )
            except Exception:
                pass
            cohorts = _cohort_lookup(
                ticker, patterns,
                regime=primary_regime, vol_state=primary_vol,
            )
            top = _pick_top_patterns(cohorts, top_n=3, ticker=ticker)
            spot = None
            try:
                if primary_df is not None and len(primary_df) > 0:
                    spot = float(primary_df["Close"].iloc[-1]) if "Close" in primary_df.columns \
                        else float(primary_df["close"].iloc[-1])
            except Exception:
                spot = None
            composed = _compose_thesis(ticker, top, spot)
            # MITS Phase 15.E — stamp the consolidated regime vector
            # on the EOD BrainPrediction row so the nightly linker can
            # attribute outcomes to the regime call.
            regime_vector_dict: Optional[Dict[str, Any]] = None
            try:
                from backend.bot.features import build_features
                from backend.bot.regime.vector import build_regime_vector
                snapshot: Dict[str, Any] = {"price": float(spot or 0.0)}
                snapshot["features"] = build_features(snapshot)
                rv = build_regime_vector(ticker=ticker, snapshot=snapshot)
                regime_vector_dict = rv.to_dict()
            except Exception:
                logger.debug("eod regime_vector build failed for %s",
                             ticker, exc_info=True)
            with session_scope() as s:
                action = _upsert_row(
                    s, ticker, analysis_date, patterns, top, composed,
                )
                if action == "inserted":
                    stats["rows_inserted"] += 1
                else:
                    stats["rows_updated"] += 1
                _persist_brain_prediction_eod(
                    s, ticker=ticker, top=top, composed=composed,
                    regime_vector=regime_vector_dict,
                )
            stats["tickers_analyzed"] += 1
            if top:
                pat, cohort, score = top[0]
                ranked_setups.append({
                    "ticker": ticker,
                    "pattern": pat,
                    "posterior": cohort.get("posterior_win_rate"),
                    "sample_size": cohort.get("sample_size"),
                    "rank_score": score,
                })
        except Exception:
            logger.exception("eod pass failed for %s", ticker)
            continue

    ranked_setups.sort(key=lambda d: d.get("rank_score") or 0.0, reverse=True)
    for i, item in enumerate(ranked_setups[:10]):
        item["rank"] = i + 1
    stats["top_setups"] = ranked_setups[:10]
    return stats


def format_tomorrow_digest_text(
    analysis_date: Optional[_date] = None, limit: int = 3,
) -> Optional[str]:
    """Compose the Telegram digest body. Returns None when the day has
    no rows (graceful no-op for the scheduler)."""
    analysis_date = analysis_date or datetime.utcnow().date()
    try:
        with session_scope() as s:
            rows = s.execute(
                select(EodAnalysis)
                .where(EodAnalysis.analysis_date == analysis_date)
                .order_by(desc(EodAnalysis.rank_score))
                .limit(int(limit))
            ).scalars().all()
            entries = [r.to_dict() for r in rows]
    except Exception:
        logger.debug("eod digest fetch failed", exc_info=True)
        return None
    if not entries:
        return None
    import html as _html
    lines = [
        f"<b>Tomorrow's Setup — {analysis_date.isoformat()}</b>",
        f"<i>Top {len(entries)} opportunit"
        f"{'y' if len(entries) == 1 else 'ies'} ranked by historical edge.</i>",
    ]
    for i, r in enumerate(entries, start=1):
        ticker = r.get("ticker") or "?"
        pat = r.get("top_pattern") or "?"
        post = r.get("top_posterior")
        n = r.get("top_sample_size") or 0
        wr_str = f"{post*100:.0f}%" if post is not None else "n/a"
        block = [
            f"\n<b>{i}. {_html.escape(ticker)}</b> · "
            f"<code>{_html.escape(pat)}</code> · posterior <b>{wr_str}</b> "
            f"(N={n})",
        ]
        headline = r.get("headline")
        if headline:
            block.append(_html.escape(headline))
        sa = r.get("suggested_action")
        if isinstance(sa, dict):
            action = sa.get("action") or ""
            strike = sa.get("strike")
            dte = sa.get("dte")
            tgt = sa.get("target_premium_pct")
            stop = sa.get("stop_premium_pct")
            block.append(
                f"Action: <code>{_html.escape(str(action))}</code> "
                f"strike {strike} DTE {dte} · "
                f"target +{tgt}% / stop -{stop}%"
            )
        lines.extend(block)
    return "\n".join(lines)
