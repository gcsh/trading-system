"""Item #1 — Memory-rich agent context assembler.

Pure functions stay pure: the 5 agents (``agent_market``,
``agent_microstructure``, ``agent_macro``, ``agent_portfolio_risk``,
``agent_devils_advocate``) take a context dict and return a vote. They
read whatever you put in the dict.

This module is the **one place** that packs memory (journal lessons,
similar trades, per-agent recent performance) into that dict so:

  • the same context produces the same vote (reproducibility),
  • adding a new memory source means one new field here, not 5 agent edits,
  • the chairman + Claude Chairman (Stage 21) get the same memory view.

Plumbed into ``engine.run_cycle`` at the point where ``agents_ctx`` is
built, just before ``run_consensus()`` is called.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Knowledge graph evidence loader (MITS Phase 1) ────────────────────


_INTRADAY_STRATEGY_HINTS = (
    "scalp", "intraday", "vwap", "opening", "scalper",
    "momo_intraday", "0dte",
)


def _horizon_for_strategy(strategy: Optional[str]) -> str:
    """Map a strategy label to the cohort horizon we prefer to query.

    Intraday-leaning strategies prefer the 60min horizon; everything
    else prefers 1d. Always falls back to whatever the corpus actually
    has populated.
    """
    if not strategy:
        return "1d"
    s = str(strategy).lower()
    for hint in _INTRADAY_STRATEGY_HINTS:
        if hint in s:
            return "60min"
    return "1d"


def _fmt_summary(cells: List[Dict[str, Any]],
                       outcomes: List[Dict[str, Any]]) -> str:
    """Format the "N analogs, WR X% (post Y%), avg move Z%" line."""
    if not cells:
        return ""
    n = sum(int(c.get("sample_size") or 0) for c in cells)
    if n == 0:
        return ""
    # Weighted means by sample size.
    wr_w = 0.0
    pwr_w = 0.0
    ret_w = 0.0
    hold_w = 0.0
    denom = 0
    for c in cells:
        s = int(c.get("sample_size") or 0)
        if not s:
            continue
        wr_w += float(c.get("win_rate") or 0.0) * s
        pwr_w += float(c.get("posterior_win_rate") or 0.0) * s
        ret_w += float(c.get("avg_return_pct") or 0.0) * s
        hold_w += float(c.get("avg_hold_minutes") or 0.0) * s
        denom += s
    if denom == 0:
        return ""
    wr = wr_w / denom * 100.0
    pwr = pwr_w / denom * 100.0
    ret_pct = ret_w / denom * 100.0
    hold_min = hold_w / denom
    parts = [
        f"{n} analogs",
        f"WR {wr:.0f}% (posterior {pwr:.0f}%)",
        f"avg move {ret_pct:+.1f}%",
    ]
    if hold_min > 0:
        if hold_min >= 60:
            parts.append(f"avg hold {hold_min / 60:.1f}h")
        else:
            parts.append(f"avg hold {hold_min:.0f} min")
    if outcomes:
        recent_wins = sum(1 for o in outcomes if o.get("was_winner"))
        parts.append(f"last {len(outcomes)}: {recent_wins}W/{len(outcomes) - recent_wins}L")
    return ", ".join(parts)


def load_knowledge_evidence(
    *,
    ticker: str,
    regime: Optional[str] = None,
    vol_state: Optional[str] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    strategy: Optional[str] = None,
    top_cells: int = 5,
    top_outcomes: int = 20,
) -> Dict[str, Any]:
    """Return the knowledge-graph evidence block for the current cohort.

    Shape:
        {
          "cells": [...top-N cohort cells matching (ticker, regime,
                       vol_state, time_bucket), ranked by posterior WR],
          "summary": "N analogs, WR X% (posterior Y%), avg move Z%, ...",
          "most_similar_outcomes": [...20 most-recent obs + outcomes
                                              matching the cohort],
        }

    Fail-open: returns the empty shape if the corpus has no cells, the
    DB isn't reachable, or any sub-query crashes. Callers should treat
    `cells == []` as "no evidence available".
    """
    empty = {"cells": [], "summary": "", "most_similar_outcomes": []}
    if not ticker:
        return empty
    tkr = str(ticker).upper().strip()
    horizon_pref = _horizon_for_strategy(strategy)

    try:
        from sqlalchemy import and_, desc, or_, select
        from backend.bot.detectors.base import _time_bucket
        from backend.db import session_scope
        from backend.models.knowledge_graph_cell import KnowledgeGraphCell
        from backend.models.market_observation import MarketObservation
        from backend.models.market_outcome import MarketOutcome
    except Exception:
        logger.debug("knowledge_evidence import failed", exc_info=True)
        return empty

    # Resolve the current time bucket from snapshot timestamp when
    # available; otherwise fall back to the current wallclock. This
    # mirrors what the detectors stamp on observations at fire-time.
    bucket = "rth"
    try:
        ts = None
        if snapshot:
            ts = snapshot.get("timestamp") or snapshot.get("as_of")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                ts = None
        if ts is None:
            ts = datetime.now()
        bucket = _time_bucket(ts) if hasattr(ts, "hour") else "rth"
    except Exception:
        bucket = "rth"

    # MITS Phase 3 — pull the operator-disabled detector set so we
    # never surface evidence for masked patterns. Fail-open: empty set
    # on error means everything passes through.
    try:
        from backend.bot.detectors import disabled_patterns as _disabled_fn
        disabled_set = _disabled_fn()
    except Exception:
        disabled_set = set()

    cells_out: List[Dict[str, Any]] = []
    try:
        with session_scope() as s:
            # Build cohort filter — prefer exact match, fall back to
            # (ticker only) if the exact cohort yields nothing so the
            # Brain still sees ticker-level evidence.
            base = select(KnowledgeGraphCell).where(
                KnowledgeGraphCell.ticker == tkr,
            )
            filters = []
            if regime and regime != "unknown":
                filters.append(KnowledgeGraphCell.regime == regime)
            if vol_state and vol_state != "normal":
                filters.append(KnowledgeGraphCell.vol_state == vol_state)
            if bucket and bucket != "rth":
                filters.append(KnowledgeGraphCell.time_bucket == bucket)
            q = base
            if filters:
                q = q.where(and_(*filters))
            q = (q.where(KnowledgeGraphCell.horizon == horizon_pref)
                       .order_by(desc(KnowledgeGraphCell.posterior_win_rate),
                                       desc(KnowledgeGraphCell.sample_size))
                       .limit(int(top_cells)))
            rows = s.execute(q).scalars().all()

            if not rows:
                # Loose fallback: ticker + horizon only, ordered by samples.
                q2 = (select(KnowledgeGraphCell)
                            .where(KnowledgeGraphCell.ticker == tkr)
                            .where(KnowledgeGraphCell.horizon == horizon_pref)
                            .order_by(desc(KnowledgeGraphCell.sample_size))
                            .limit(int(top_cells)))
                rows = s.execute(q2).scalars().all()

            cells_out = [r.to_dict() for r in rows]
    except Exception:
        logger.debug("knowledge cells query failed for %s", tkr, exc_info=True)

    # MITS Phase 12.2 — when the local cells are all thin (max N<30),
    # backfill the evidence using the hierarchical fallback so the
    # Brain doesn't reason over noise. We replace each thin cell's
    # posterior/n with the pooled parent value, marking it via
    # ``cohort_source`` so the UI/log can show "pooled across all
    # tickers" instead of "ticker-specific".
    try:
        from backend.bot.corpus.knowledge_graph import (
            get_posterior_with_fallback, MIN_N_LOCAL,
        )
        promoted: List[Dict[str, Any]] = []
        for c in cells_out:
            n = int(c.get("sample_size") or 0)
            if n >= MIN_N_LOCAL:
                c.setdefault("cohort_source", "cell")
                promoted.append(c)
                continue
            pat = c.get("pattern")
            if not pat:
                promoted.append(c)
                continue
            entry = get_posterior_with_fallback(
                ticker=tkr, pattern=pat,
                regime=(c.get("regime") or regime or "unknown"),
                vol_state=(c.get("vol_state") or vol_state or "normal"),
                horizon=horizon_pref,
                sample_split=(c.get("sample_split") or "combined"),
            )
            if entry is None:
                promoted.append(c)
                continue
            # Promote thin local with the pooled-parent values when the
            # parent has more samples than the local cell.
            new_n = int(entry.get("n") or 0)
            if new_n > n:
                c["sample_size"] = new_n
                c["posterior_win_rate"] = entry.get("posterior")
                c["win_rate"] = entry.get("win_rate")
                c["avg_return_pct"] = entry.get("avg_return_pct")
                c["confidence_level"] = entry.get("confidence_level")
                c["cohort_source"] = entry.get("source")
                if entry.get("confidence_lower") is not None:
                    c["confidence_lower"] = entry.get("confidence_lower")
                if entry.get("confidence_upper") is not None:
                    c["confidence_upper"] = entry.get("confidence_upper")
            else:
                c.setdefault("cohort_source", "local_thin")
            # MITS Phase 13 Fix 8 — surface CI width + warning. Brain
            # / agents / EOD compose their prompts off this dict, so
            # adding ci_width here means every downstream consumer
            # sees the same "wide CI" hint.
            lo = c.get("confidence_lower")
            hi = c.get("confidence_upper")
            if lo is not None and hi is not None:
                try:
                    width = round(float(hi) - float(lo), 4)
                    c["ci_width"] = width
                    try:
                        from backend.config import TUNABLES as _T
                        thresh = float(_T.cohort_ci_width_warn_threshold)
                    except Exception:
                        thresh = 0.20
                    if width > thresh:
                        post = c.get("posterior_win_rate")
                        post_pct = (round(float(post) * 100.0, 1)
                                    if post is not None else None)
                        width_pp = round(width * 100.0 / 2.0, 1)
                        c["ci_warning"] = (
                            f"posterior {post_pct}% (wide CI ±{width_pp}pp "
                            "— use with caution)"
                        )
                except Exception:
                    pass
            promoted.append(c)
        cells_out = promoted
    except Exception:
        logger.debug("knowledge fallback promotion failed for %s", tkr,
                     exc_info=True)

    # MITS Phase 3 — filter out cells for any pattern the operator has
    # disabled. Existing cells stay on disk; this just masks them from
    # the live evidence path.
    if disabled_set and cells_out:
        cells_out = [c for c in cells_out
                       if c.get("pattern") not in disabled_set]

    if not cells_out:
        return empty

    # Pull the matching most-recent observations + outcomes — limit to
    # the patterns surfaced in the top cells so we stay on-cohort.
    patterns = sorted({c["pattern"] for c in cells_out if c.get("pattern")})
    outcomes_out: List[Dict[str, Any]] = []
    try:
        with session_scope() as s:
            obs_q = (select(MarketObservation)
                          .where(MarketObservation.ticker == tkr)
                          .where(MarketObservation.pattern.in_(patterns))
                          .order_by(desc(MarketObservation.timestamp))
                          .limit(int(top_outcomes) * 3))
            obs_rows = s.execute(obs_q).scalars().all()
            obs_ids = [r.id for r in obs_rows]
            if obs_ids:
                outcomes = s.execute(
                    select(MarketOutcome)
                    .where(MarketOutcome.observation_id.in_(obs_ids))
                    .where(MarketOutcome.horizon == horizon_pref)
                ).scalars().all()
                outcome_by_id = {o.observation_id: o for o in outcomes}
                for r in obs_rows:
                    oc = outcome_by_id.get(r.id)
                    if oc is None:
                        continue
                    outcomes_out.append({
                        "observation_id": r.id,
                        "pattern": r.pattern,
                        "timestamp": (r.timestamp.isoformat()
                                                if r.timestamp else None),
                        "regime": r.regime,
                        "vol_state": r.vol_state,
                        "time_bucket": r.time_bucket,
                        "horizon": oc.horizon,
                        "return_pct": oc.return_pct,
                        "was_winner": oc.was_winner,
                    })
                    if len(outcomes_out) >= int(top_outcomes):
                        break
    except Exception:
        logger.debug("knowledge outcomes query failed for %s", tkr,
                          exc_info=True)

    return {
        "cells": cells_out,
        "summary": _fmt_summary(cells_out, outcomes_out),
        "most_similar_outcomes": outcomes_out,
    }


def build_agent_context(
    *,
    ticker: str,
    action: str,
    strategy: str,
    snapshot: Optional[Dict[str, Any]] = None,
    analytics: Optional[Dict[str, Any]] = None,
    portfolio_risk: Optional[Dict[str, Any]] = None,
    optimizer: Optional[Dict[str, Any]] = None,
    cross_asset: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    agent_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Assemble the rich context dict that the 5-agent council reads.

    Adds these new fields beyond the legacy ``agents_ctx`` shape:

      ``journal_lessons`` — list of Lesson dicts whose conditions match
                            the current strategy + regime. Only populated
                            when ``config['ai']['use_journal_lessons']``
                            is enabled (we don't want stale lessons
                            during the corpus warm-up). Soft cap at 5.
      ``similar_trades`` — up to 5 most-recent closed trades that match
                           on ticker / regime / strategy. Lets each agent
                           see prior outcomes for the same conditioning.
      ``recent_performance`` — per-agent calibration over the last 30
                                trades. Each agent reads ITS own row so
                                it can self-temper ("I've been over-
                                confident in this regime, dial back").

    All loaders fail-open: when a source is empty or unavailable, the
    field is ``[]`` or ``{}`` so agents that don't yet read these fields
    are unaffected.
    """
    features = (analytics or {}).get("features") or {}
    regime = (analytics or {}).get("regime") or {}
    regime_trend = regime.get("trend") or "unknown"
    regime_volatility = regime.get("volatility") or "normal"
    regime_gamma = regime.get("gamma") or "unknown"

    ctx: Dict[str, Any] = {
        "ticker": ticker,
        "action": action,
        "strategy": strategy,
        "analytics": analytics,
        "features": features,
        "snapshot": snapshot,
        "portfolio_risk": portfolio_risk,
        "optimizer": optimizer,
        "cross_asset": cross_asset,
        # MITS Phase 14.B — correlation-aware portfolio block. Built
        # below; empty until the loader fires so downstream consumers
        # that don't yet read it stay unaffected.
        "portfolio": {},
        # New fields default to empty so downstream never NPEs.
        "journal_lessons": [],
        "similar_trades": [],
        "recent_performance": {},
        # P2.3 — IV regime classification for the underlying. Computed
        # from the iv_history corpus; cached 1h. Agents read this as a
        # higher-order context above the per-bar iv_rank.
        "iv_regime": None,
        # MITS Phase 1 — knowledge graph evidence block. Populated below
        # from `knowledge_graph` + `market_observations` + `market_outcomes`
        # when the corpus has cells matching the current cohort. Empty
        # when the corpus is cold so downstream agents fail-open.
        "knowledge_evidence": {
            "cells": [],
            "summary": "",
            "most_similar_outcomes": [],
        },
        # MITS Phase 11.I — Brain prompt enrichment: insider activity +
        # 13F top funds + similar-regime analogs via pgvector. All
        # fail-open empty so existing agents (which don't read these
        # fields yet) are unaffected.
        "insider_recent": [],
        "smart_money": {
            "top_funds": [],
            "smart_money_direction": "flat",
            "smart_money_flow_pct": 0.0,
            "latest_quarter": None,
        },
        "similar_regime_days": [],
        # MITS Phase 14.E — thesis-health snapshot for every open paper
        # position. Devil's Advocate reads this to argue against opening
        # new trades while the existing book is bleeding.
        "open_positions_thesis_health": [],
    }

    # ── IV regime (P2.3) ──────────────────────────────────────────────
    try:
        from backend.bot.iv_regime import classify_ticker
        ctx["iv_regime"] = classify_ticker(ticker).to_dict()
    except Exception as exc:
        logger.debug("iv_regime context load failed for %s: %s", ticker, exc)

    # ── Regime vector (MITS Phase 15.A) ───────────────────────────────
    # Single consolidated view across trend / vol / iv_rank / iv_regime /
    # intraday / gamma / macro. ``intraday_classifier`` is not threaded
    # into agent context yet — that dim degrades to yellow until the
    # engine starts passing the classifier handle through.
    ctx["regime_vector"] = None
    try:
        from backend.bot.regime.vector import build_regime_vector
        rv = build_regime_vector(
            ticker=ticker,
            snapshot=(snapshot or {}),
            intraday_classifier=None,
        )
        ctx["regime_vector"] = rv.to_dict()
    except Exception as exc:
        logger.debug("regime_vector context load failed for %s: %s", ticker, exc)

    # ── Portfolio context (MITS Phase 14.B) ───────────────────────────
    # Pairwise return correlations across the open book + the candidate,
    # SPY-3% stress projection, sector/theme dollar weights. The
    # correlation-cap gate in the engine reads this block to refuse to
    # pile a fresh long onto a position that is statistically the same
    # trade.
    try:
        from sqlalchemy import select
        from backend.bot.portfolio_intel.portfolio_context import (
            build_portfolio_context,
        )
        from backend.db import session_scope
        positions = (portfolio_risk or {}).get("positions") or []
        equity = float((portfolio_risk or {}).get("equity") or 0.0)
        if not positions or equity <= 0:
            try:
                from backend.models.paper import (
                    PaperPosition, get_or_create_account,
                )
                with session_scope() as s:
                    if not positions:
                        rows = s.execute(select(PaperPosition)).scalars().all()
                        positions = [r.to_dict() for r in rows]
                    if equity <= 0:
                        account = get_or_create_account(s)
                        equity = float(account.last_portfolio_value or 0.0)
            except Exception:
                logger.debug("paper position/equity fallback failed",
                             exc_info=True)
        cand_dir = "LONG"
        a = (action or "").upper()
        if a.startswith("SELL") or a == "BUY_PUT" or "SHORT" in a:
            cand_dir = "SHORT"
        pctx = build_portfolio_context(
            positions=positions,
            equity=equity,
            candidate_ticker=ticker,
            candidate_direction=cand_dir,
        )
        ctx["portfolio"] = pctx.to_dict()
    except Exception:
        logger.debug("portfolio context load failed for %s", ticker,
                     exc_info=True)

    # ── Knowledge graph evidence (MITS Phase 1 — MITS.4 unlock) ──────
    # Queries the populated knowledge-graph cells that match the current
    # ticker + regime + vol_state + time_bucket, ordered by posterior
    # win-rate. Each agent (and the Brain) can then reason OVER
    # historical evidence instead of from first principles.
    try:
        ctx["knowledge_evidence"] = load_knowledge_evidence(
            ticker=ticker,
            regime=regime_trend,
            vol_state=regime_volatility,
            snapshot=snapshot,
            strategy=strategy,
        )
    except Exception:
        logger.debug("knowledge_evidence load failed for %s", ticker,
                          exc_info=True)

    # ── Insider activity (last 90 days, top-5 by transaction value) ──
    # Form 4 rows feed the Brain prompt so the AI can cite "Insider X
    # bought $1.2M on 2026-04-15" instead of inventing the claim.
    try:
        from datetime import date as _date, timedelta as _td
        from backend.models.insider_trade import InsiderTrade
        cutoff = _date.today() - _td(days=90)
        cluster_cutoff = _date.today() - _td(days=30)
        with session_scope() as s:
            rows = s.execute(
                select(InsiderTrade)
                .where(InsiderTrade.ticker == ticker)
                .where(InsiderTrade.transaction_date >= cutoff)
                .order_by(InsiderTrade.transaction_date.desc())
                .limit(50)
            ).scalars().all()
            recent = [r.to_dict() for r in rows]
        # Compress to top-5 by absolute total_value, dropping noisy
        # non-trading codes (A/F/G).
        meaningful = [r for r in recent
                          if (r.get("transaction_code") or "").upper()
                              in ("P", "S", "M")]
        meaningful.sort(
            key=lambda r: abs(float(r.get("total_value") or 0.0)),
            reverse=True,
        )
        cluster_buyers = set()
        for r in recent:
            if (r.get("transaction_code") or "").upper() != "P":
                continue
            try:
                txn = datetime.fromisoformat(r["transaction_date"]).date()
            except Exception:
                continue
            if txn >= cluster_cutoff:
                cluster_buyers.add(r.get("insider_name") or "")
        ctx["insider_recent"] = meaningful[:5]
        ctx["insider_cluster_buy_30d"] = (len(cluster_buyers) >= 3)
        ctx["insider_cluster_distinct_buyers_30d"] = len(cluster_buyers)
    except Exception:
        logger.debug("insider context load failed for %s", ticker,
                          exc_info=True)

    # ── 13F top-funds (latest quarter) ──────────────────────────────
    try:
        from backend.models.fund_holding import FundHolding
        with session_scope() as s:
            latest_q = s.execute(
                select(FundHolding.quarter_end_date)
                .where(FundHolding.ticker == ticker)
                .order_by(FundHolding.quarter_end_date.desc())
                .limit(1)
            ).scalar()
            if latest_q is not None:
                rows = s.execute(
                    select(FundHolding)
                    .where(FundHolding.ticker == ticker)
                    .where(FundHolding.quarter_end_date == latest_q)
                    .order_by(FundHolding.value_usd.desc())
                    .limit(5)
                ).scalars().all()
                top_funds = [r.to_dict() for r in rows]
                flow_rows = s.execute(
                    select(FundHolding.shares,
                              FundHolding.change_from_prior_qtr)
                    .where(FundHolding.ticker == ticker)
                    .where(FundHolding.quarter_end_date == latest_q)
                    .order_by(FundHolding.value_usd.desc())
                    .limit(25)
                ).all()
                total_shares = sum(float(r[0] or 0.0) for r in flow_rows)
                total_change = sum(float(r[1] or 0.0) for r in flow_rows)
                flow_pct = (total_change / total_shares
                                  if total_shares else 0.0)
                direction = ("added" if total_change > 0
                                  else ("trimmed" if total_change < 0
                                          else "flat"))
                ctx["smart_money"] = {
                    "top_funds": top_funds,
                    "smart_money_direction": direction,
                    "smart_money_flow_pct": round(flow_pct, 4),
                    "latest_quarter": (latest_q.isoformat()
                                              if latest_q else None),
                }
    except Exception:
        logger.debug("13F context load failed for %s", ticker,
                          exc_info=True)

    # ── Similar-regime days via pgvector ────────────────────────────
    # Top-3 most-similar historical days via regime_snapshot vector
    # space. Fail-open empty if pgvector isn't reachable. Used by the
    # Brain prompt to cite "today most resembles 2020-03-12, "
    # 2022-09-13, ...".
    try:
        from backend.bot.ai import vector_store as _vs
        text_parts = [
            f"ticker={ticker}",
            f"regime={regime_trend}",
            f"vol={regime_volatility}",
            f"gamma={regime_gamma}",
        ]
        snap = snapshot or {}
        for key in ("vix", "spy_30m_change_pct", "breadth_pct_above_50d",
                     "put_call_ratio"):
            v = snap.get(key)
            if v is not None:
                text_parts.append(f"{key}={v}")
        qv = _vs.embed(" | ".join(text_parts))
        if qv:
            try:
                hits = _vs.similarity_search(
                    "regime_snapshots", qv, k=5, min_cosine=0.65,
                )
            except Exception:
                hits = []
            ctx["similar_regime_days"] = [
                {
                    "date": (h.metadata or {}).get("date") or "unknown",
                    "regime": (h.metadata or {}).get("regime") or "unknown",
                    "cosine": round(float(h.cosine), 3),
                }
                for h in (hits[:3] if hits else [])
            ]
    except Exception:
        logger.debug("similar_regime_days load failed for %s", ticker,
                          exc_info=True)

    cfg = config or {}
    ai_cfg = cfg.get("ai") or {}

    # ── Journal lessons ──────────────────────────────────────────────
    # Curated (P2.2) ALWAYS fire — they're hand-coded guardrails that
    # don't depend on having mined a corpus. Organic (mined from closed
    # trades) are gated on ``ai.use_journal_lessons`` so noisy
    # warm-up findings don't influence trading until trust is built.
    try:
        # Pull macro signal once — used by both curated rules and engine.
        yield_curve_inverted = None
        try:
            from backend.bot.data.fred import yield_curve_inverted as _yci
            yield_curve_inverted = _yci()
        except Exception:
            pass

        from backend.bot.journal.curated import applicable_curated_lessons
        curated = applicable_curated_lessons(
            strategy=strategy,
            regime_trend=regime_trend,
            volatility=regime_volatility,
            gamma=regime_gamma,
            earnings_days=features.get("earnings_days"),
            iv_rank=features.get("iv_rank"),
            vix=(snapshot or {}).get("vix"),
            iv_regime=ctx.get("iv_regime"),
            yield_curve_inverted=yield_curve_inverted,
        )
        organic = []
        if ai_cfg.get("use_journal_lessons", False):
            from backend.bot.journal import applicable_lessons
            # applicable_lessons returns curated + organic; we'd double-count
            # if we used it. Call the organic-only path indirectly: just
            # accept that when the flag is on, curated lessons appear in
            # applicable_lessons too. To preserve the curated-always-on
            # semantics without duplication, dedup by pattern below.
            all_matches = applicable_lessons(
                strategy=strategy,
                regime_trend=regime_trend,
                volatility=regime_volatility,
                gamma=regime_gamma,
                earnings_days=features.get("earnings_days"),
                iv_rank=features.get("iv_rank"),
                vix=(snapshot or {}).get("vix"),
                iv_regime=ctx.get("iv_regime"),
                yield_curve_inverted=yield_curve_inverted,
            )
            curated_patterns = {l.pattern for l in curated}
            organic = [l for l in all_matches if l.pattern not in curated_patterns]
        merged = list(curated) + list(organic)
        ctx["journal_lessons"] = [
            {
                "pattern": getattr(l, "pattern", None),
                "source": (getattr(l, "condition_keys", {}) or {}).get("source", "organic"),
                "rule_id": (getattr(l, "condition_keys", {}) or {}).get("rule_id"),
                "size_multiplier": getattr(l, "size_multiplier", 1.0),
                "sample_size": getattr(l, "sample_size", 0),
                "expectancy": getattr(l, "expectancy", 0.0),
                "win_rate": getattr(l, "win_rate", 0.0),
                "suggested_action": getattr(l, "suggested_action", "unchanged"),
                "severity": getattr(l, "severity", "info"),
            }
            for l in merged[:8]
        ]
    except Exception:
        logger.debug("journal lessons load failed for %s", ticker, exc_info=True)

    # ── Similar trades (always on — empty when corpus is thin) ────────
    try:
        from backend.bot.journal import similar_trades
        ctx["similar_trades"] = similar_trades(
            ticker=ticker,
            regime_trend=regime_trend,
            regime_volatility=regime_volatility,
            strategy=strategy,
            k=5,
        )
    except Exception:
        logger.debug("similar_trades failed for %s", ticker, exc_info=True)

    # ── Per-agent recent performance (gives each agent self-awareness) ─
    if agent_names is None:
        # Sensible canonical roster — matches AGENT_FUNCS in
        # backend.bot.agents but kept loose to avoid a hard coupling that
        # would block this module on agents/__init__.py imports.
        agent_names = [
            "agent_market", "agent_microstructure", "agent_macro",
            "agent_portfolio_risk", "agent_devils_advocate",
        ]
    perf: Dict[str, Dict[str, Any]] = {}
    try:
        from backend.bot.agents.scorecard import recent_performance
        for name in agent_names:
            try:
                perf[name] = recent_performance(name, window=30)
            except Exception:
                continue
    except Exception:
        logger.debug("recent_performance loader failed", exc_info=True)
    ctx["recent_performance"] = perf

    # ── Open-position thesis-health (MITS Phase 14.E) ─────────────────
    # Walk every paper position with a usable (pattern, regime) hint and
    # score it against the historical winner profile. Devil's Advocate
    # reads this list to raise its voice when the book is degrading.
    ctx["open_positions_thesis_health"] = _open_positions_thesis_health()

    return ctx


def _open_positions_thesis_health() -> List[Dict[str, Any]]:
    """Score each open PaperPosition against its winner-profile.

    Returns one entry per position whose stored ``meta`` carries a
    pattern hint that resolves to a trustworthy winner profile. Empty
    list when no positions exist, the corpus is too thin, or any sub-
    call fails. Never raises.
    """
    out: List[Dict[str, Any]] = []
    try:
        from sqlalchemy import select
        from backend.bot.thesis import build_winner_profile, calculate_health
        from backend.db import session_scope
        from backend.models.paper import PaperPosition
    except Exception:
        logger.debug("open-position thesis-health imports failed",
                          exc_info=True)
        return out

    try:
        with session_scope() as s:
            rows = s.execute(select(PaperPosition)).scalars().all()
            positions = [r.to_dict() for r in rows]
    except Exception:
        logger.debug("open-position fetch failed", exc_info=True)
        return out

    for pos in positions:
        meta = pos.get("meta") or {}
        if not isinstance(meta, dict):
            continue
        pattern = (meta.get("pattern")
                       or meta.get("detector_pattern")
                       or "")
        if not pattern:
            continue
        regime = meta.get("regime") or ""
        ticker = pos.get("ticker") or ""
        try:
            profile = build_winner_profile(
                pattern=str(pattern), regime=str(regime),
                horizon="1d", ticker=str(ticker) or None,
            )
        except Exception:
            logger.debug("winner profile build failed for %s/%s",
                              ticker, pattern, exc_info=True)
            continue
        if profile is None or not profile.is_trustworthy:
            continue
        position_ctx = {
            "ticker": ticker,
            "current_price": meta.get("current_price") or meta.get("mark"),
            "entry_price": pos.get("avg_cost"),
            "vwap": meta.get("vwap") or meta.get("current_vwap"),
            "flag_low": meta.get("flag_low"),
            "bos_pivot": meta.get("bos_pivot"),
            "peak_premium": pos.get("peak_premium_per_share"),
            "entry_iv": pos.get("entry_iv"),
            "stored_iv": pos.get("stored_iv") or pos.get("last_iv_seen"),
            "hold_minutes": meta.get("hold_minutes"),
            "peak_reached_minutes": meta.get("peak_reached_minutes"),
            "meta": meta,
        }
        try:
            health = calculate_health(
                open_position=position_ctx, current_bars=None,
                winner_profile=profile,
            )
        except Exception:
            logger.debug("health calc failed for %s/%s",
                              ticker, pattern, exc_info=True)
            continue
        if health.abstain:
            continue
        out.append({
            "ticker": ticker,
            "pattern": str(pattern),
            "score": float(health.score),
            "degraded_traits": list(health.degraded_traits),
        })
    return out


def derive_bias_factor(
    posterior: float,
    sample_size: int,
    *,
    scale: Optional[float] = None,
    min_factor: Optional[float] = None,
    max_factor: Optional[float] = None,
    min_samples: Optional[int] = None,
) -> float:
    """MITS P2.3 — calibrated knowledge-graph bias.

    Maps a corpus-aggregate posterior (0..1) to a confidence multiplier
    via a smooth posterior-strength formula:

        raw   = 1.0 + (posterior - 0.5) * 2.0 * scale
        bias  = clamp(raw, min_factor, max_factor)

    Thin corpora (`sample_size < min_samples`) return 1.0 (neutral).
    All four parameters default to TUNABLES.memory_bias_* so the
    operator can tune via env vars without touching this code.

    Examples (with scale=0.20, the default):
      posterior=0.50, N=20  → 1.000  (neutral)
      posterior=0.75, N=20  → 1.100  (legacy +10% behaviour)
      posterior=1.00, N=20  → 1.200  (max support)
      posterior=0.25, N=20  → 0.900  (legacy -10% behaviour)
      posterior=0.00, N=20  → 0.800  (clamped floor)
      posterior=0.85, N=5   → 1.000  (thin corpus → neutral)
    """
    try:
        from backend.config import TUNABLES as _T
        scale_v = float(scale if scale is not None else _T.memory_bias_scale)
        min_v = float(min_factor if min_factor is not None else _T.memory_bias_min)
        max_v = float(max_factor if max_factor is not None else _T.memory_bias_max)
        min_n = int(min_samples if min_samples is not None
                                else _T.memory_bias_min_samples)
    except Exception:
        # Defaults match the docstring example.
        scale_v = float(scale if scale is not None else 0.20)
        min_v = float(min_factor if min_factor is not None else 0.80)
        max_v = float(max_factor if max_factor is not None else 1.25)
        min_n = int(min_samples if min_samples is not None else 20)

    try:
        n = int(sample_size or 0)
    except Exception:
        n = 0
    if n < min_n:
        return 1.0
    try:
        p = float(posterior)
    except Exception:
        return 1.0
    # NaN guard — float('nan') compares False with everything, so
    # the explicit check catches both NaN inputs and arithmetic results.
    if p != p:
        return 1.0
    p = max(0.0, min(1.0, p))
    raw = 1.0 + (p - 0.5) * 2.0 * scale_v
    if raw != raw:
        return 1.0
    if raw < min_v:
        return min_v
    if raw > max_v:
        return max_v
    return raw


def apply_memory_bias(votes: List[Any], context: Dict[str, Any]) -> None:
    """In-place adjustment of vote confidences based on the memory fields
    in ``context``. Called from ``run_consensus`` AFTER each agent emits
    its base vote but BEFORE the chairman reconciles.

    Bias rules (multiplicative, clamped to [0.05, 1.00]):

      • If the agent's ``recent_performance.drift_flag`` is True
        (calibration_error > 0.20 over the last 30 closed trades), shave
        15% off this vote's confidence — the agent has been over-
        confident in this regime and should self-temper.

      • If ``similar_trades`` has 3+ matches and >60% are losers, shave
        10% off — comparable setups have been losing.

      • If ``similar_trades`` has 3+ matches and >70% are winners,
        boost 5% (capped at 1.0) — comparable setups have been winning.

      • A loud ``journal_lessons[*].size_multiplier < 1.0`` also drags
        confidence — multiply by ``max(0.5, lesson.size_multiplier)``.

    This is the single chokepoint that turns memory into vote impact
    without touching each agent's body. The chairman receives the
    biased votes plus the raw context, so it can also reason about
    *why* confidence shifted (memory_bias field on each vote).
    """
    similar = context.get("similar_trades") or []
    lessons = context.get("journal_lessons") or []
    perf = context.get("recent_performance") or {}
    knowledge = context.get("knowledge_evidence") or {}

    n_similar = len(similar)
    similar_winners = sum(1 for s in similar if s.get("was_winner"))
    similar_losers = n_similar - similar_winners

    lesson_drag = 1.0
    for lesson in lessons:
        try:
            lm = float(lesson.get("size_multiplier", 1.0))
        except Exception:
            continue
        if lm < 1.0:
            lesson_drag = min(lesson_drag, max(0.5, lm))

    # MITS Phase 2 (P2.3) — self-calibrated knowledge-graph bias.
    # Replaces the Phase 1 hardcoded ±10% factor with a smooth
    # posterior-strength formula. Thin corpora (< min_samples) skip the
    # bias and return 1.0 so a few stray observations can't sway
    # confidence. The formula:
    #
    #     raw  = 1.0 + (posterior - 0.5) * 2.0 * scale
    #     bias = clamp(raw, min_factor, max_factor)
    #
    # Tunables live in `TUNABLES.memory_bias_*` (see config.py).
    knowledge_bias = 1.0
    knowledge_reason = None
    knowledge_summary = ""
    try:
        cells = knowledge.get("cells") or []
        total_n = sum(int(c.get("sample_size") or 0) for c in cells)
        if total_n > 0:
            pw = 0.0
            for c in cells:
                s = int(c.get("sample_size") or 0)
                pw += float(c.get("posterior_win_rate") or 0.0) * s
            posterior_aggregate = pw / total_n
            knowledge_bias = derive_bias_factor(
                posterior=posterior_aggregate,
                sample_size=total_n,
            )
            if knowledge_bias > 1.0:
                knowledge_reason = (
                    f"knowledge_supports({posterior_aggregate:.0%}"
                    f"@N={total_n}@x{knowledge_bias:.2f})")
            elif knowledge_bias < 1.0:
                knowledge_reason = (
                    f"knowledge_opposes({posterior_aggregate:.0%}"
                    f"@N={total_n}@x{knowledge_bias:.2f})")
        knowledge_summary = knowledge.get("summary") or ""
    except Exception:
        pass

    for v in votes:
        agent_name = getattr(v, "agent", None) or (
            v.get("agent") if isinstance(v, dict) else None)
        if not agent_name:
            continue
        base_conf = float(getattr(v, "confidence", None)
                                  or (v.get("confidence") if isinstance(v, dict) else 0.5))
        bias = 1.0
        reasons: List[str] = []

        agent_perf = perf.get(agent_name) or {}
        if agent_perf.get("drift_flag"):
            bias *= 0.85
            reasons.append(f"drift({agent_perf.get('calibration_error', 0):.2f})")

        if n_similar >= 3:
            loss_share = similar_losers / n_similar
            win_share = similar_winners / n_similar
            if loss_share > 0.60:
                bias *= 0.90
                reasons.append(f"similar_losers({similar_losers}/{n_similar})")
            elif win_share > 0.70:
                bias *= 1.05
                reasons.append(f"similar_winners({similar_winners}/{n_similar})")

        if lesson_drag < 1.0:
            bias *= lesson_drag
            reasons.append(f"lesson_drag({lesson_drag:.2f})")

        if knowledge_bias != 1.0:
            bias *= knowledge_bias
            if knowledge_reason:
                reasons.append(knowledge_reason)

        # Clamp + apply.
        new_conf = max(0.05, min(1.0, base_conf * bias))
        # Annotate the vote's reasoning with the corpus summary the
        # FIRST time we touch it for this consensus run — gives the
        # operator (and the lossless Chairman) a visible trace of the
        # evidence the council was acting under.
        if knowledge_summary:
            existing = (getattr(v, "reasoning", None)
                              if not isinstance(v, dict)
                              else v.get("reasoning"))
            existing = str(existing or "")
            if "Memory says" not in existing and "knowledge:" not in existing:
                augmented = (existing + " | knowledge: "
                                  + knowledge_summary).strip(" |")
                if isinstance(v, dict):
                    v["reasoning"] = augmented
                else:
                    try:
                        v.reasoning = augmented
                    except Exception:
                        pass

        if isinstance(v, dict):
            v["confidence"] = new_conf
            if reasons:
                v["memory_bias"] = {
                    "factor": round(bias, 3),
                    "reasons": reasons,
                    "base_confidence": round(base_conf, 3),
                }
        else:
            try:
                v.confidence = new_conf
            except Exception:
                pass
            if reasons:
                try:
                    setattr(v, "memory_bias", {
                        "factor": round(bias, 3),
                        "reasons": reasons,
                        "base_confidence": round(base_conf, 3),
                    })
                except Exception:
                    pass
