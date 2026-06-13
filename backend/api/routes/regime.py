"""MITS Phase 7.6 — intraday regime endpoint.

Endpoints:

  ``GET /regime/intraday`` →
      {state, severity, since, vix, vix_change_pct, breadth, put_call,
       mode, last_scan_at, current_hypothesis}

  ``GET /regime/opportunity-context?ticker=NVDA`` →
      {ticker, regime, prompt_summary, blocks: {analogs, insider,
       fund_changes, news, earnings}, prompt_chars}
      → MITS Phase 11.2 Gap 11. The full Claude prompt-context the
      Opportunity Brain would assemble for ``ticker`` right now. Lets
      the operator audit whether the pgvector + corpus pipeline is
      feeding the Brain real data.

``state`` and inputs come straight off the engine's
``_current_regime`` / classifier cache; ``mode`` reflects whether the
discretionary opportunistic layer is the active decision-maker
(``opportunistic``) or the statistical layer leads (``statistical``).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from backend.db import session_scope
from backend.models.fund_holding import FundHolding
from backend.models.insider_trade import InsiderTrade
from backend.models.intraday_regime_event import IntradayRegimeEvent
from backend.models.news_article import NewsArticle

router = APIRouter(prefix="/regime", tags=["regime"])


def _latest_event() -> Optional[IntradayRegimeEvent]:
    try:
        with session_scope() as s:
            row = (s.query(IntradayRegimeEvent)
                   .order_by(IntradayRegimeEvent.event_at.desc())
                   .first())
            if row is not None:
                s.expunge(row)
            return row
    except Exception:
        return None


@router.get("/intraday")
async def intraday_regime(request: Request) -> Dict[str, Any]:
    engine = getattr(request.app.state, "engine", None)
    state: str = "normal"
    state_dict: Optional[Dict[str, Any]] = None
    hypothesis: Optional[Dict[str, Any]] = None
    last_scan_at: Optional[str] = None
    if engine is not None:
        # Pull the cached classifier output without re-running (cheap).
        try:
            classifier = getattr(engine, "_intraday_classifier", None)
            if classifier is not None and classifier._cache is not None:
                state_dict = classifier._cache.to_dict()
                state = state_dict.get("state") or "normal"
                last_scan_at = state_dict.get("classified_at")
        except Exception:
            pass
        # Hypothesis surface — engine stashes the most recent one
        # produced by the Opportunity Brain.
        hypothesis = getattr(engine, "_last_opportunity_hypothesis", None)
        if hasattr(hypothesis, "to_dict"):
            hypothesis = hypothesis.to_dict()

    # Always include the latest persisted transition for "since" timing.
    last_event = _latest_event()
    since = (last_event.event_at.isoformat()
                if (last_event and last_event.event_at) else None)
    if state == "normal" and last_event is not None:
        state = last_event.new_state or state

    vix: Optional[float] = None
    vix_change_pct: Optional[float] = None
    breadth: Optional[float] = None
    put_call: Optional[float] = None
    if state_dict:
        vix = state_dict.get("vix_spot")
        vix_change_pct = state_dict.get("vix_1d_pct_change")
        breadth = state_dict.get("breadth_ratio")
        put_call = state_dict.get("put_call_ratio")
    elif last_event is not None:
        vix = last_event.vix_spot
        breadth = last_event.breadth_ratio
        put_call = last_event.put_call_ratio

    mode = "opportunistic" if state != "normal" else "statistical"
    return {
        "state": state,
        "severity": (state_dict or {}).get("severity") if state_dict else (
            last_event.severity if last_event else "low"
        ),
        "since": since,
        "vix": vix,
        "vix_change_pct": vix_change_pct,
        "breadth": breadth,
        "put_call": put_call,
        "mode": mode,
        "last_scan_at": last_scan_at or datetime.utcnow().isoformat(),
        "current_hypothesis": hypothesis,
    }


@router.get("/events")
async def recent_events(limit: int = 50) -> Dict[str, Any]:
    """Newest-first list of intraday regime transitions."""
    limit = max(1, min(int(limit or 50), 500))
    try:
        with session_scope() as s:
            rows = (s.query(IntradayRegimeEvent)
                    .order_by(IntradayRegimeEvent.event_at.desc())
                    .limit(limit)
                    .all())
            return {"events": [r.to_dict() for r in rows]}
    except Exception:
        return {"events": []}


# ── opportunity-context surface (MITS Phase 11.2 Gap 11) ──────────────


def _safe_session_block(builder):
    """Wrap a builder so a DB / pgvector failure degrades to an empty
    block instead of taking down the whole endpoint."""
    try:
        return builder()
    except Exception as exc:  # noqa: BLE001
        return {"items": [], "error": str(exc)[:240]}


def _live_context_from_engine(engine) -> Dict[str, Any]:
    if engine is None:
        return {}
    out: Dict[str, Any] = {}
    try:
        classifier = getattr(engine, "_intraday_classifier", None)
        if classifier is not None and getattr(classifier, "_cache", None):
            d = classifier._cache.to_dict() or {}
            for src_key, dst_key in (
                ("vix_spot", "vix"),
                ("vix_1d_pct_change", "vix_change_pct"),
                ("breadth_ratio", "breadth"),
                ("put_call_ratio", "put_call"),
                ("spy_30m_pct_change", "spy_30m_change_pct"),
                ("sector_dispersion", "sector_dispersion"),
            ):
                v = d.get(src_key)
                if v is not None:
                    out[dst_key] = v
            out["flow_summary"] = d.get("top_flow_summary") or ""
            out["state"] = d.get("state") or "normal"
    except Exception:
        pass
    return out


def _build_analog_block(regime_state: str,
                              live_context: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from backend.bot.ai.opportunity_brain import (
            _fetch_analogs, _format_analog_block,
        )
        analogs = _fetch_analogs(regime_state, live_context) or []
        return {
            "items": analogs,
            "rendered": _format_analog_block(analogs),
        }
    except Exception as exc:  # noqa: BLE001
        return {"items": [], "error": str(exc)[:240]}


def _build_insider_block(ticker: str, *, limit: int = 3) -> Dict[str, Any]:
    with session_scope() as s:
        rows = s.execute(
            select(InsiderTrade)
            .where(InsiderTrade.ticker == ticker)
            .order_by(InsiderTrade.transaction_date.desc())
            .limit(limit)
        ).scalars().all()
        items = []
        for r in rows:
            items.append({
                "transaction_date": (r.transaction_date.isoformat()
                                          if r.transaction_date else None),
                "insider_name": r.insider_name,
                "role": r.insider_role,
                "txn_code": r.transaction_code,
                "shares": r.shares,
                "price": r.price,
                "total_value": r.total_value,
                "is_director": bool(r.is_director),
                "is_officer": bool(r.is_officer),
            })
    return {"items": items}


def _build_fund_changes_block(ticker: str, *, limit: int = 3) -> Dict[str, Any]:
    """Top 13F position changes for the ticker, ranked by abs(value_usd)
    change in the most recent quarter."""
    cutoff = date.today() - timedelta(days=200)
    with session_scope() as s:
        rows = s.execute(
            select(FundHolding)
            .where(FundHolding.ticker == ticker)
            .where(FundHolding.quarter_end_date >= cutoff)
            .order_by(FundHolding.quarter_end_date.desc())
            .limit(200)
        ).scalars().all()
        # Group by fund_cik → keep the most recent quarter, sort by
        # absolute change magnitude.
        latest_by_fund: Dict[str, FundHolding] = {}
        for r in rows:
            existing = latest_by_fund.get(r.fund_cik)
            if existing is None or r.quarter_end_date > existing.quarter_end_date:
                latest_by_fund[r.fund_cik] = r
        sortable = []
        for r in latest_by_fund.values():
            magnitude = abs(float(r.change_from_prior_qtr or 0.0))
            sortable.append((magnitude, r))
        sortable.sort(key=lambda x: x[0], reverse=True)
        items = []
        for _, r in sortable[:limit]:
            items.append({
                "fund_name": r.fund_name,
                "fund_cik": r.fund_cik,
                "quarter_end": (r.quarter_end_date.isoformat()
                                  if r.quarter_end_date else None),
                "shares": r.shares,
                "value_usd": r.value_usd,
                "pct_of_portfolio": r.pct_of_portfolio,
                "change_from_prior_qtr": r.change_from_prior_qtr,
            })
    return {"items": items}


def _build_news_block(ticker: str, *, limit: int = 5) -> Dict[str, Any]:
    cutoff = datetime.utcnow() - timedelta(days=45)
    with session_scope() as s:
        rows = s.execute(
            select(NewsArticle)
            .where(NewsArticle.ticker == ticker)
            .where(NewsArticle.published_at >= cutoff)
            .order_by(NewsArticle.published_at.desc())
            .limit(limit)
        ).scalars().all()
        items = [{
            "published_at": (r.published_at.isoformat()
                              if r.published_at else None),
            "headline": r.headline,
            "source": r.source,
            "sentiment_label": r.sentiment_label,
            "sentiment_score": r.sentiment_score,
            "url": r.url,
        } for r in rows]
    return {"items": items}


def _build_earnings_block(ticker: str) -> Dict[str, Any]:
    try:
        from backend.models.earnings_transcript import EarningsTranscript
    except Exception as exc:
        return {"items": [], "error": str(exc)[:240]}
    with session_scope() as s:
        row = s.execute(
            select(EarningsTranscript)
            .where(EarningsTranscript.ticker == ticker)
            .order_by(EarningsTranscript.report_date.desc().nullslast())
            .limit(1)
        ).scalars().first()
        if row is None:
            return {"items": []}
        full = row.full_text or ""
        sentiment_blurb = full[:1200] if full else ""
        return {
            "items": [{
                "fiscal_year": row.fiscal_year,
                "fiscal_quarter": row.fiscal_quarter,
                "report_date": (row.report_date.isoformat()
                                  if row.report_date else None),
                "paragraph_count": row.paragraph_count,
                "summary_snippet": sentiment_blurb,
            }],
        }


@router.get("/opportunity-context")
async def opportunity_context(
    request: Request,
    ticker: str = Query(..., min_length=1, max_length=10),
) -> Dict[str, Any]:
    """Return the full Claude-prompt context the Opportunity Brain
    would build for ``ticker`` right now. Lets the operator audit that
    the corpus + pgvector pipeline is feeding real data into the
    Brain's prompt, not empty placeholders.
    """
    ticker = ticker.strip().upper()
    if not ticker.isalpha():
        raise HTTPException(status_code=400, detail="invalid ticker")
    engine = getattr(request.app.state, "engine", None)
    live_ctx = _live_context_from_engine(engine)
    regime_state = live_ctx.get("state") or "normal"

    analogs = _safe_session_block(
        lambda: _build_analog_block(regime_state, live_ctx))
    insider = _safe_session_block(lambda: _build_insider_block(ticker))
    fund_changes = _safe_session_block(
        lambda: _build_fund_changes_block(ticker))
    news = _safe_session_block(lambda: _build_news_block(ticker))
    earnings = _safe_session_block(lambda: _build_earnings_block(ticker))

    blocks: Dict[str, Any] = {
        "analogs": analogs,
        "insider": insider,
        "fund_changes": fund_changes,
        "news": news,
        "earnings": earnings,
    }

    # Render a one-shot text summary of the assembled prompt so the
    # operator can paste it into a debugger if the JSON is too noisy.
    lines: List[str] = []
    lines.append(f"=== Opportunity Brain context for {ticker} ===")
    lines.append(f"regime_state={regime_state}  vix={live_ctx.get('vix')}"
                       f"  breadth={live_ctx.get('breadth')}"
                       f"  put_call={live_ctx.get('put_call')}")
    lines.append("")
    lines.append("[Top historical analogs]")
    lines.append((analogs.get("rendered") or "  (none — pgvector empty or below floor)"))
    lines.append("")
    lines.append("[Recent insider trades — Form 4]")
    if insider.get("items"):
        for i in insider["items"]:
            lines.append(
                f"  {i.get('transaction_date')} {i.get('insider_name')} "
                f"({i.get('role') or '?'}) "
                f"{i.get('txn_code')} {i.get('shares')} @ {i.get('price')}"
            )
    else:
        lines.append("  (none in DB)")
    lines.append("")
    lines.append("[Top 13F position changes — last 2 quarters]")
    if fund_changes.get("items"):
        for i in fund_changes["items"]:
            lines.append(
                f"  {i.get('fund_name')} ({i.get('quarter_end')}): "
                f"value=${i.get('value_usd')} "
                f"Δ={i.get('change_from_prior_qtr')}"
            )
    else:
        lines.append("  (none in DB)")
    lines.append("")
    lines.append("[Recent news + sentiment — last 45d]")
    if news.get("items"):
        for i in news["items"]:
            lines.append(
                f"  {i.get('published_at')} [{i.get('sentiment_label') or '?'}] "
                f"{i.get('headline')}"
            )
    else:
        lines.append("  (none in DB)")
    lines.append("")
    lines.append("[Latest earnings release]")
    if earnings.get("items"):
        e = earnings["items"][0]
        lines.append(
            f"  FY{e.get('fiscal_year')} Q{e.get('fiscal_quarter')} "
            f"(report {e.get('report_date')}, "
            f"{e.get('paragraph_count')} paragraphs)"
        )
        snip = (e.get("summary_snippet") or "").replace("\n", " ")[:300]
        if snip:
            lines.append(f"  snippet: {snip}…")
    else:
        lines.append("  (no earnings transcripts in DB)")
    rendered = "\n".join(lines)

    return {
        "ticker": ticker,
        "regime_state": regime_state,
        "live_context": live_ctx,
        "blocks": blocks,
        "prompt_summary": rendered,
        "prompt_chars": len(rendered),
    }
