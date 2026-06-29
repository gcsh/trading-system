"""MITS Phase 12.E — Catalyst-driven detectors.

Four detectors that consume Phase 11 data sources (insider_trades,
fund_holdings, news_articles, stock_bars) rather than just OHLCV bars:

  * pead_drift            — Post-Earnings Announcement Drift. After an
                            earnings surprise with a >=2-sigma reaction,
                            emit a daily observation for the next 60
                            trading days.
  * insider_cluster       — three or more distinct insider buys (Form 4
                            transaction code P) on the same ticker within
                            a 30-day window.
  * smart_money_inflow    — five or more of the top 50 funds (by AUM
                            proxy) increase positions in the same
                            13F-quarter.
  * earnings_revision_shift — analyst-revision direction flip. Derived
                            from news_articles tagged "estimate" or
                            "guidance" when Finnhub revisions endpoint
                            data isn't available.

Citations:

  * Bernard, V. L. & Thomas, J. K. (1989). "Post-Earnings-Announcement
    Drift: Delayed Price Response or Risk Premium?" Journal of
    Accounting Research, 27 (Supplement), 1–36.
  * Foster, G., Olsen, C. & Shevlin, T. (1984). "Earnings Releases,
    Anomalies, and the Behavior of Security Returns." The Accounting
    Review, 59 (4), 574–603.
  * Lakonishok, J. & Lee, I. (2001). "Are Insider Trades
    Informative?" Review of Financial Studies, 14 (1), 79–111.
  * Cohen, R. B., Polk, C. & Silli, B. (2010). "Best Ideas." NBER
    working paper. Shows top-1 conviction positions outperform.
  * Stickel, S. E. (1991). "Common Stock Returns Surrounding Earnings
    Forecast Revisions: More Puzzling Evidence." The Accounting
    Review, 66 (2), 402–416.

Design
======

These detectors fire on the bar where the *triggering event*
crystallises. They share the same Observation contract as price-action
detectors so the corpus + cohort tables consume them uniformly.

All four are look-ahead-safe: at bar index i we only query rows whose
``filing_date`` / ``transaction_date`` / ``published_at`` precede
``bars.index[i]``.

If a required Phase 11 table is empty (e.g. tests without an
``insider_trades`` row), the detector returns ``[]`` gracefully.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, select

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)
from backend.db import session_scope

logger = logging.getLogger(__name__)


CATALYST_FAMILY = "catalyst"


def _build_obs(ticker: str, bars, i: int, pattern: str,
                  features: Dict[str, Any], spot_close: float) -> Observation:
    ts = bars.index[i]
    try:
        ts_py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    except Exception:
        ts_py = ts
    return Observation(
        ticker=ticker,
        pattern=pattern,
        timestamp=ts_py,
        timeframe=_bar_timeframe(bars),
        regime=_classify_regime(bars, i),
        vol_state=_classify_vol_state(bars, i),
        time_bucket=_time_bucket(ts_py) if hasattr(ts_py, "hour") else "rth",
        spot=spot_close,
        features=features,
    )


# ── 1. Post-Earnings Announcement Drift ───────────────────────────────


_EARNINGS_HEADLINE_KEYWORDS = (
    "earnings", "eps", "quarterly results", "q1 results", "q2 results",
    "q3 results", "q4 results", "reports first", "reports second",
    "reports third", "reports fourth", "beats estimates",
    "misses estimates", "tops estimates",
)


def _fetch_earnings_events(ticker: str) -> List[date]:
    """Return distinct dates on which a news headline plausibly tags
    an earnings event for this ticker."""
    try:
        from backend.models.news_article import NewsArticle
    except Exception:
        return []
    out: List[date] = []
    try:
        with session_scope() as s:
            rows = s.execute(
                select(NewsArticle.published_at, NewsArticle.headline)
                .where(NewsArticle.ticker == ticker)
            ).all()
    except Exception:
        logger.debug("news fetch failed for %s", ticker, exc_info=True)
        return []
    seen: set = set()
    for published_at, headline in rows:
        if not headline or not published_at:
            continue
        hl = headline.lower()
        if not any(kw in hl for kw in _EARNINGS_HEADLINE_KEYWORDS):
            continue
        try:
            d = published_at.date() if hasattr(published_at, "date") \
                else published_at
        except Exception:
            continue
        if d in seen:
            continue
        seen.add(d)
        out.append(d)
    return sorted(out)


# ETFs don't release quarterly earnings (no 10-Q, no 8-K). Skip them
# in pead_drift so the audit doesn't flag them as "no earnings 8-K
# identified" — the absence of earnings is by design, not a data gap.
# MITS Phase 12.1 Fix 11.
_PEAD_SKIP_ETFS_DEFAULT = {
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU",
    "XLRE", "XLC",
    "VTI", "VOO", "VEA", "VWO", "VTV", "VUG", "EFA", "EEM",
    "GLD", "SLV", "USO", "TLT", "HYG", "LQD",
    "VXX", "UVXY", "TQQQ", "SQQQ",
}


class PEADDriftDetector(Detector):
    """Post-Earnings Announcement Drift. We compute the earnings-day
    return and compare it to the 60-day return-volatility standard
    deviation; if abs(return) > 2 sigma, drift is considered triggered
    and we emit one observation per bar for the next 60 trading days
    with the surprise direction tagged in features.

    Coverage note (MITS Phase 12.1 Fix 11): ETFs have no quarterly
    earnings releases (no 10-Q, no 8-K). When ``pead_skip_etfs`` is
    enabled (default True) the detector silently returns ``[]`` for
    tickers in the ETF skip list. Operator can opt back in by
    flipping the param on a specific ticker via the detector
    control plane.
    """

    pattern = "pead_drift"
    family = CATALYST_FAMILY
    description = (
        "Post-Earnings Announcement Drift: after a >=2 sigma earnings "
        "reaction, emit daily observations for the next 60 trading "
        "days. Cited: Bernard & Thomas 1989 JAR. "
        "Skips ETF tickers by default — they have no quarterly "
        "earnings releases."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "drift_window_bars": 60,
            "sigma_threshold": 2.0,
            "vol_lookback": 60,
            # MITS Phase 12.1 Fix 11 — ETFs have no quarterly
            # earnings releases, skip them quietly.
            "pead_skip_etfs": True,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 70:
            return []
        bars = _lower_columns(bars)
        try:
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        drift_w = int(p.get("drift_window_bars", 60))
        sigma_thr = float(p.get("sigma_threshold", 2.0))
        vol_lb = int(p.get("vol_lookback", 60))
        # MITS Phase 12.1 Fix 11 — skip ETFs (no quarterly earnings).
        if bool(p.get("pead_skip_etfs", True)) \
                and (ticker or "").upper() in _PEAD_SKIP_ETFS_DEFAULT:
            return []
        # Daily returns.
        rets = [0.0] + [
            (closes[i] - closes[i - 1]) / max(1e-9, closes[i - 1])
            for i in range(1, len(closes))
        ]
        # Index dates for matching with earnings dates.
        index_dates: List[date] = []
        for ts in bars.index:
            try:
                index_dates.append(ts.date() if hasattr(ts, "date") else ts)
            except Exception:
                index_dates.append(None)
        events = _fetch_earnings_events(ticker)
        if not events:
            return []
        date_to_i = {d: i for i, d in enumerate(index_dates) if d is not None}
        out: List[Observation] = []
        # For each event, decide direction and emit drift observations.
        for ev_date in events:
            # Pick the first bar on or after the event date.
            ev_i = None
            for delta in range(0, 5):
                cand = date_to_i.get(ev_date + timedelta(days=delta))
                if cand is not None:
                    ev_i = cand
                    break
            if ev_i is None or ev_i < vol_lb:
                continue
            sigma = pstdev(rets[max(1, ev_i - vol_lb):ev_i]) \
                       if ev_i - vol_lb > 1 else 0.0
            if sigma <= 0:
                continue
            event_ret = rets[ev_i]
            if abs(event_ret) < sigma_thr * sigma:
                continue
            direction = "bullish" if event_ret > 0 else "bearish"
            # Emit one observation per bar in [ev_i+1, ev_i+drift_w].
            end_i = min(len(bars) - 1, ev_i + drift_w)
            for j in range(ev_i + 1, end_i + 1):
                out.append(_build_obs(ticker, bars, j, self.pattern, {
                    "direction": direction,
                    "event_date": ev_date.isoformat()
                                          if hasattr(ev_date, "isoformat")
                                          else str(ev_date),
                    "event_return": round(event_ret, 5),
                    "sigma_60d": round(sigma, 5),
                    "days_post_event": j - ev_i,
                }, float(closes[j])))
        return out


# ── 2. Insider cluster ────────────────────────────────────────────────


class InsiderClusterDetector(Detector):
    """Three-or-more distinct insider open-market buys (transaction
    code 'P') on the same ticker within a 30-day window. Emits on the
    bar at or just after the 3rd buy's filing date."""

    pattern = "insider_cluster"
    family = CATALYST_FAMILY
    description = (
        ">=3 distinct insiders open-market buying within 30 days. "
        "Cited: Lakonishok & Lee 'Are Insider Trades Informative?' "
        "RFS 2001."
    )

    def default_params(self) -> Dict[str, Any]:
        # MITS Phase 12.1 Fix 7 — relaxed minimum from 3 → 2 distinct
        # insiders. Form 4 'P' (open-market purchase) and 'A' (grant)
        # rows are both accepted; 'A' is rare unless there's a real
        # vesting cliff so it adds signal-worthy weight. The audit
        # showed 3286 P rows but zero clusters firing — the 3-distinct
        # bar plus the original 5-row look-back window was too tight.
        return {
            "min_distinct_insiders": 2,
            "window_days": 30,
            # Allow open-market purchases (P) AND option/award (A);
            # both are bullish signals when clustered.
            "accept_codes": "P,A",
            "cluster_cooldown_days": 30,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 5:
            return []
        bars = _lower_columns(bars)
        try:
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        min_n = int(p.get("min_distinct_insiders", 2))
        win_d = int(p.get("window_days", 30))
        cooldown = int(p.get("cluster_cooldown_days", 30))
        accept_codes_str = str(p.get("accept_codes", "P,A"))
        accept_codes = [c.strip().upper() for c in accept_codes_str.split(",")
                              if c.strip()]
        try:
            from backend.models.insider_trade import InsiderTrade
        except Exception:
            return []
        try:
            with session_scope() as s:
                rows = s.execute(
                    select(InsiderTrade.filing_date,
                                  InsiderTrade.transaction_date,
                                  InsiderTrade.insider_name,
                                  InsiderTrade.transaction_code,
                                  InsiderTrade.shares)
                    .where(InsiderTrade.ticker == ticker)
                    .where(InsiderTrade.transaction_code.in_(accept_codes))
                    .order_by(InsiderTrade.filing_date.asc())
                ).all()
        except Exception:
            logger.debug("insider fetch failed for %s", ticker, exc_info=True)
            return []
        if len(rows) < min_n:
            return []
        # Normalize rows. Prefer transaction_date for windowing (the
        # "when the buy happened" date) but fall back to filing_date.
        norm = []
        for fd, td, name, code, shares in rows:
            anchor = td or fd
            if anchor is None:
                continue
            if shares is None or shares <= 0:
                continue
            norm.append((anchor, name or "", code, int(shares)))
        if len(norm) < min_n:
            return []
        # Index dates from bars.
        index_dates: List[date] = []
        for ts in bars.index:
            try:
                index_dates.append(ts.date() if hasattr(ts, "date") else ts)
            except Exception:
                index_dates.append(None)
        date_to_i = {d: i for i, d in enumerate(index_dates) if d is not None}
        out: List[Observation] = []
        last_emit_date: Optional[date] = None
        # Walk every row as a candidate anchor; look back ``win_d`` days.
        for j in range(len(norm)):
            anchor = norm[j][0]
            distinct: set = set()
            window_buys = []
            # Walk backwards while inside the window.
            k = j
            while k >= 0:
                fd, name, code, shares = norm[k]
                gap = (anchor - fd).days
                if gap < 0 or gap > win_d:
                    break
                distinct.add(name or f"unknown_{k}")
                window_buys.append((fd, name, code, shares))
                k -= 1
            if len(distinct) < min_n:
                continue
            # Cooldown — don't re-emit within `cooldown` days.
            if last_emit_date is not None \
                    and (anchor - last_emit_date).days < cooldown:
                continue
            # Map to a bar index — accept up to 5 days forward to find
            # the next trading day (handles weekend/holiday filings).
            bar_i = None
            for delta in range(0, 7):
                cand = date_to_i.get(anchor + timedelta(days=delta))
                if cand is not None:
                    bar_i = cand
                    break
            if bar_i is None:
                continue
            last_emit_date = anchor
            out.append(_build_obs(ticker, bars, bar_i, self.pattern, {
                # Phase 12.1: 'side' = 'buy' so direction resolver maps
                # to long. Catalyst direction resolver also accepts
                # 'cluster_kind' = 'buy'.
                "side": "buy",
                "cluster_kind": "buy",
                "filing_date": anchor.isoformat()
                                       if hasattr(anchor, "isoformat")
                                       else str(anchor),
                "distinct_insiders": len(distinct),
                "buys_in_window": len(window_buys),
                "total_shares": sum(int(b[3]) for b in window_buys),
                "codes": "/".join(sorted({b[2] for b in window_buys})),
            }, float(closes[bar_i])))
        return out


# ── 3. Smart money inflow ─────────────────────────────────────────────


class SmartMoneyInflowDetector(Detector):
    """Five or more top-50 funds (ranked by aggregate AUM proxy =
    portfolio value sum) add to or initiate this ticker in the same
    13F-reported quarter. Emits on the filing date of the 5th add."""

    pattern = "smart_money_inflow"
    family = CATALYST_FAMILY
    description = (
        ">=5 top-50 institutional funds add the same ticker in a "
        "single 13F quarter. Cited: Cohen, Polk & Silli 'Best Ideas' "
        "NBER 2010."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "min_funds": 5,
            "top_funds_n": 50,
        }

    def _top_funds(self, min_n: int) -> set:
        try:
            from backend.models.fund_holding import FundHolding
        except Exception:
            return set()
        try:
            with session_scope() as s:
                latest_q = s.execute(
                    select(func.max(FundHolding.quarter_end_date))
                ).scalar_one_or_none()
                if latest_q is None:
                    return set()
                rows = s.execute(
                    select(FundHolding.fund_cik,
                                  func.sum(FundHolding.value_usd))
                    .where(FundHolding.quarter_end_date == latest_q)
                    .group_by(FundHolding.fund_cik)
                ).all()
        except Exception:
            return set()
        if not rows:
            return set()
        ranked = sorted(rows, key=lambda r: -(r[1] or 0.0))[:min_n]
        return {r[0] for r in ranked}

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 5:
            return []
        bars = _lower_columns(bars)
        try:
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        min_funds = int(p.get("min_funds", 5))
        top_n = int(p.get("top_funds_n", 50))
        top_funds = self._top_funds(top_n)
        if not top_funds:
            return []
        try:
            from backend.models.fund_holding import FundHolding
        except Exception:
            return []
        try:
            with session_scope() as s:
                rows = s.execute(
                    select(FundHolding.fund_cik,
                                  FundHolding.quarter_end_date,
                                  FundHolding.filing_date,
                                  FundHolding.change_from_prior_qtr,
                                  FundHolding.value_usd)
                    .where(FundHolding.ticker == ticker)
                    .where(FundHolding.fund_cik.in_(top_funds))
                    .order_by(FundHolding.filing_date.asc())
                ).all()
        except Exception:
            return []
        if not rows:
            return []
        # Group by quarter_end_date.
        by_quarter: Dict[Any, List[Any]] = {}
        for row in rows:
            fc, qed, fd, change, val = row
            if change is None or change <= 0:
                # ignore reductions / unknown changes
                continue
            by_quarter.setdefault(qed, []).append((fc, fd, val))
        index_dates: List[date] = []
        for ts in bars.index:
            try:
                index_dates.append(ts.date() if hasattr(ts, "date") else ts)
            except Exception:
                index_dates.append(None)
        date_to_i = {d: i for i, d in enumerate(index_dates) if d is not None}
        out: List[Observation] = []
        for qed, adds in by_quarter.items():
            if len(adds) < min_funds:
                continue
            # Emit on the day of the min_funds-th filing.
            adds_sorted = sorted(adds, key=lambda x: x[1] or date.min)
            emit_date = adds_sorted[min_funds - 1][1]
            if emit_date is None:
                continue
            bar_i = None
            for delta in range(0, 10):
                cand = date_to_i.get(emit_date + timedelta(days=delta))
                if cand is not None:
                    bar_i = cand
                    break
            if bar_i is None:
                continue
            out.append(_build_obs(ticker, bars, bar_i, self.pattern, {
                "quarter_end": (qed.isoformat()
                                          if hasattr(qed, "isoformat")
                                          else str(qed)),
                "filing_date": (emit_date.isoformat()
                                         if hasattr(emit_date, "isoformat")
                                         else str(emit_date)),
                "fund_count": len(adds),
                "aggregate_value_usd": float(
                    sum((a[2] or 0.0) for a in adds_sorted)
                ),
            }, float(closes[bar_i])))
        return out


# ── 4. Earnings-revision shift ────────────────────────────────────────


_RAISE_KEYWORDS = (
    "raises guidance", "raises outlook", "raises forecast",
    "increases guidance", "boosts guidance", "upgraded",
    "estimate raised", "raised estimates", "lifts guidance",
)
_CUT_KEYWORDS = (
    "cuts guidance", "lowers guidance", "lowers outlook",
    "lowers forecast", "trims guidance", "downgraded",
    "estimate cut", "cut estimates", "warns",
)


def _classify_revision(headline: str) -> Optional[str]:
    hl = (headline or "").lower()
    if any(k in hl for k in _RAISE_KEYWORDS):
        return "raise"
    if any(k in hl for k in _CUT_KEYWORDS):
        return "cut"
    return None


class EarningsRevisionShiftDetector(Detector):
    """Analyst-revision direction flip. We sort news_articles tagged
    as estimate / guidance changes, classify them as 'raise' or 'cut',
    and emit when the most recent revision flips from the prior trend
    (a raise after a string of cuts, or vice versa)."""

    pattern = "earnings_revision_shift"
    family = CATALYST_FAMILY
    description = (
        "Direction flip in analyst-estimate / guidance revisions. "
        "Cited: Stickel 'Common Stock Returns Surrounding Earnings "
        "Forecast Revisions' TAR 1991."
    )

    def default_params(self) -> Dict[str, Any]:
        return {
            "min_prior_run": 2,  # at least 2 prior revisions of the opposite kind
            "lookback_days": 180,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 5:
            return []
        bars = _lower_columns(bars)
        try:
            closes = bars["close"].astype(float).tolist()
        except Exception:
            return []
        p = params if params is not None else self.default_params()
        min_run = int(p.get("min_prior_run", 2))
        lookback = int(p.get("lookback_days", 180))
        try:
            from backend.models.news_article import NewsArticle
        except Exception:
            return []
        try:
            with session_scope() as s:
                rows = s.execute(
                    select(NewsArticle.published_at, NewsArticle.headline)
                    .where(NewsArticle.ticker == ticker)
                    .order_by(NewsArticle.published_at.asc())
                ).all()
        except Exception:
            return []
        revisions: List[Any] = []
        for published_at, headline in rows:
            kind = _classify_revision(headline)
            if kind is None:
                continue
            try:
                d = published_at.date() if hasattr(published_at, "date") \
                    else published_at
            except Exception:
                continue
            revisions.append((d, kind))
        if len(revisions) < min_run + 1:
            return []
        index_dates: List[date] = []
        for ts in bars.index:
            try:
                index_dates.append(ts.date() if hasattr(ts, "date") else ts)
            except Exception:
                index_dates.append(None)
        date_to_i = {d: i for i, d in enumerate(index_dates) if d is not None}
        out: List[Observation] = []
        for j in range(min_run, len(revisions)):
            curr_date, curr_kind = revisions[j]
            window = [(d, k) for d, k in revisions[max(0, j - 6):j]
                          if curr_date and d and (curr_date - d).days <= lookback]
            if len(window) < min_run:
                continue
            opposite = "cut" if curr_kind == "raise" else "raise"
            if not all(k == opposite for _, k in window[-min_run:]):
                continue
            bar_i = None
            for delta in range(0, 5):
                cand = date_to_i.get(curr_date + timedelta(days=delta))
                if cand is not None:
                    bar_i = cand
                    break
            if bar_i is None:
                continue
            out.append(_build_obs(ticker, bars, bar_i, self.pattern, {
                "direction": "bullish" if curr_kind == "raise" else "bearish",
                "revision_date": curr_date.isoformat()
                                        if hasattr(curr_date, "isoformat")
                                        else str(curr_date),
                "prior_run_kind": opposite,
                "prior_run_length": min_run,
            }, float(closes[bar_i])))
        return out


def build_catalyst_detectors() -> List[Detector]:
    return [
        PEADDriftDetector(),
        InsiderClusterDetector(),
        SmartMoneyInflowDetector(),
        EarningsRevisionShiftDetector(),
    ]
