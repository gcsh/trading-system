"""MITS Phase 7.2 — Opportunity Brain.

A Claude-driven discretionary reasoner that activates ONLY when the
intraday regime is non-normal. On panic / capitulation / squeeze /
trending days it OVERRIDES the standard statistical consensus with a
single, focused asymmetric trade hypothesis.

Operating contract:

  * The Bayesian + cohort + memory layers are bypassed for these
    trades — the operator's whole point is that on crisis days the
    statistical layer is too cautious. The Opportunity Brain's job is
    to spot the convex payoff the tape is offering RIGHT NOW.
  * Caching is aggressive — one Claude call per
    ``(regime_state, 5-minute wall-clock bucket)`` so worst-case spend
    during a 4-hour panic stays around 48 calls.
  * Returns ``None`` on ``normal`` regime → statistical layer leads.
  * Conviction floor honored at the engine: if
    ``conviction < TUNABLES.opportunity_brain_min_conviction``, the
    hypothesis is logged but does not override consensus.
  * Telegram and other messaging surfaces stay untouched.
"""
from __future__ import annotations

import json
import logging
import time
from sqlalchemy import select
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the OPPORTUNITY BRAIN — a discretionary options trader who steps in ONLY on non-normal intraday regimes (panic, capitulation, squeeze, trending). On normal days the statistical layer trades; you are silent. On crisis days the statistical layer is too cautious — your job is to spot the convex payoff with controlled downside that's hiding in the tape.

Think like a discretionary trader, NOT a quant. You're allowed to act on the edge that you see in the tape. The system has already verified the regime is non-normal — your only job is to recommend the asymmetric trade right now.

You will be given:
  1) The current intraday regime state (panic / capitulation / squeeze / trending_up / trending_down) and the inputs that triggered it.
  2) A compact live-tape JSON: SPY 5-min ticks, sector rotation, VIX curve, top unusual flow, dealer GEX flip, breadth, put/call.
  3) The watchlist's current intraday range.

Return ONE recommendation as JSON of EXACTLY this shape:

{
  "ticker": "SPY|QQQ|IWM|<single watchlist ticker>",
  "direction": "long_put|long_call|iron_condor|long_straddle|skip",
  "dte_bucket": "0d|1d|3-5d|7-14d",
  "conviction": 0.0-1.0,
  "thesis": "one tight paragraph (3-5 sentences) explaining: where the convexity is, why now, what kills the trade, EOD exit plan.",
  "notes": "1-2 sentences on what would invalidate the thesis intraday."
}

Rules:
  * For ``panic`` / ``capitulation`` regimes, prefer ``long_put`` on bouncing strength OR ``long_call`` on washout V-bottoms — 0DTE or 1DTE.
  * For ``squeeze`` regimes, prefer ``long_call`` 0-1DTE on the lead sector.
  * For ``trending_up`` / ``trending_down``, prefer 3-5DTE directional with controlled downside.
  * Conviction below 0.65 means the tape is unclear — return ``direction: "skip"`` with the thesis explaining why.
  * Never recommend short-DTE options into a single ticker's earnings — the catalyst gate enforces this, but the Brain should not propose it either.

Return ONLY the JSON object. No prose before or after."""


@dataclass
class OpportunityHypothesis:
    ticker: str = ""
    direction: str = "skip"  # long_put | long_call | iron_condor | long_straddle | skip
    dte_bucket: str = "1d"
    conviction: float = 0.0
    thesis: str = ""
    notes: str = ""
    regime_state: str = "normal"
    from_cache: bool = False
    # MITS Phase 8.7 — historical analogs cited by the Brain. Each
    # entry: ``{date, regime, cosine, top_trades: [...]}``. Empty when
    # pgvector has no neighbors above the similarity floor.
    historical_analogs: list = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "direction": self.direction,
            "dte_bucket": self.dte_bucket,
            "conviction": round(float(self.conviction), 4),
            "thesis": self.thesis,
            "notes": self.notes,
            "regime_state": self.regime_state,
            "from_cache": self.from_cache,
            "historical_analogs": list(self.historical_analogs),
        }


def _five_min_bucket(now_ts: Optional[float] = None) -> int:
    """Return the integer wall-clock 5-min bucket id for cache keying."""
    ts = float(now_ts if now_ts is not None else time.time())
    bucket_sec = max(60, int(TUNABLES.opportunity_brain_cache_bucket_sec))
    return int(ts // bucket_sec)


def _parse(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in opportunity brain response")
    return json.loads(text[start: end + 1])


def _format_inputs(regime_state: str, live_context: Dict[str, Any]) -> str:
    """Compact context block for the Claude prompt."""
    try:
        ctx_json = json.dumps(live_context, default=str)
    except Exception:
        ctx_json = "{}"
    if len(ctx_json) > 3500:
        # Hard cap so prompt-cache stays cheap.
        ctx_json = ctx_json[:3500] + "...TRUNCATED"
    return (
        f"Regime: {regime_state}.\n\n"
        f"Live tape (JSON):\n{ctx_json}\n\n"
        f"What is the asymmetric trade RIGHT NOW? Where's the convex "
        f"payoff with controlled downside? Return the JSON object now."
    )


# MITS Phase 11.I — affected-tickers enrichment. When the regime is
# panic / squeeze, the Opportunity Brain prompt cites the top-3 insider
# transactions and the top-25 smart-money positioning shift for the
# affected tickers so the AI can reason "insiders bought $X across N
# names this quarter -> this is conviction, not noise".


def _format_affected_ticker_enrichment(
    live_context: Dict[str, Any],
) -> str:
    """Build the Insider Activity + Smart Money block.

    Inputs come from the live_context — the engine populates
    ``affected_tickers`` (e.g. ['NVDA', 'AAPL', 'XLK']) on
    panic/squeeze regime activations. Each ticker contributes the top
    insider txn (P/S code) in the last 30d + the top-25 13F shift.
    Fail-open empty when the candidates list is missing or no rows
    are on file yet.
    """
    affected = (live_context.get("affected_tickers") or
                  live_context.get("focus_tickers") or
                  live_context.get("scan_tickers") or [])
    if isinstance(affected, str):
        affected = [t.strip() for t in affected.split(",") if t.strip()]
    affected = [str(t).upper() for t in (affected or [])][:8]
    if not affected:
        return ""

    try:
        from datetime import date as _date, timedelta as _td
        from sqlalchemy import desc as _desc, func as _func
        from backend.db import session_scope as _scope
        from backend.models.fund_holding import FundHolding
        from backend.models.insider_trade import InsiderTrade
    except Exception:
        return ""

    cutoff = _date.today() - _td(days=30)
    lines: list = []
    try:
        with _scope() as s:
            insider_bits: list = []
            for tk in affected:
                rows = s.execute(
                    select(InsiderTrade)
                    .where(InsiderTrade.ticker == tk)
                    .where(InsiderTrade.transaction_date >= cutoff)
                    .where(InsiderTrade.transaction_code.in_(("P", "S")))
                    .order_by(
                        _desc(InsiderTrade.total_value).nullslast()
                            if hasattr(_desc(InsiderTrade.total_value),
                                            "nullslast")
                            else _desc(InsiderTrade.total_value))
                    .limit(1)
                ).scalars().all()
                if not rows:
                    continue
                r = rows[0]
                code = (r.transaction_code or "?").upper()
                kind = "BUY" if code == "P" else "SELL"
                v = r.total_value or 0.0
                try:
                    vstr = f"${abs(float(v)) / 1000:.0f}k"
                except Exception:
                    vstr = "?"
                insider_bits.append(
                    f"  - {tk}: {kind} by "
                    f"{(r.insider_name or '?')[:25]} {vstr} "
                    f"({r.transaction_date})"
                )
            if insider_bits:
                lines.append("Top insider activity (last 30d):")
                lines.extend(insider_bits)

            fund_bits: list = []
            for tk in affected:
                latest_q = s.execute(
                    select(_func.max(FundHolding.quarter_end_date))
                    .where(FundHolding.ticker == tk)
                ).scalar()
                if latest_q is None:
                    continue
                flow_rows = s.execute(
                    select(
                        _func.sum(FundHolding.change_from_prior_qtr))
                    .where(FundHolding.ticker == tk)
                    .where(FundHolding.quarter_end_date == latest_q)
                ).scalar()
                if flow_rows is None:
                    continue
                try:
                    delta = float(flow_rows)
                except Exception:
                    continue
                direction = ("added" if delta > 0
                                  else ("trimmed" if delta < 0 else "flat"))
                fund_bits.append(
                    f"  - {tk}: {direction} Δ {delta:+,.0f} sh "
                    f"({latest_q})"
                )
            if fund_bits:
                lines.append("Smart money positioning shift this quarter:")
                lines.extend(fund_bits)
    except Exception:
        return ""
    return ("\n".join(lines)) if lines else ""


# ── MITS Phase 8.7 — historical-analog block builder ──────────────────


def _fetch_analogs(regime_state: str,
                      live_context: Dict[str, Any]) -> list:
    """Pull the top-K similar historical days from pgvector.

    Returns ``[]`` on any failure (no model, no DB, no neighbors) so
    the Brain still runs — just without the analog citation, same
    behaviour as Phase 7.
    """
    try:
        from backend.bot.ai import vector_store
        from backend.config import TUNABLES as _T
    except Exception:
        return []

    def _f(k: str) -> Optional[float]:
        v = live_context.get(k)
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    spy_30m = _f("spy_30m_change_pct") or _f("spy_30m")
    vix_level = _f("vix") or _f("vix_level")
    breadth = _f("breadth") or _f("breadth_pct_above_50d")
    pcr = _f("put_call") or _f("put_call_ratio")
    sector_disp = _f("sector_dispersion")
    flow_summary = str(live_context.get("flow_summary")
                          or live_context.get("top_flow") or "")[:600]

    text_parts = [
        f"date=today",
        f"regime={regime_state}",
        f"spy_30m={spy_30m if spy_30m is not None else 'na'}",
        f"vix={vix_level if vix_level is not None else 'na'}",
        f"breadth={breadth if breadth is not None else 'na'}",
        f"put_call={pcr if pcr is not None else 'na'}",
        f"sector_dispersion={sector_disp if sector_disp is not None else 'na'}",
        f"flow={flow_summary[:200]}",
    ]
    text = " | ".join(text_parts)
    try:
        qv = vector_store.embed(text)
        if not qv:
            return []
        hits = vector_store.similarity_search(
            "regime_snapshots", qv,
            k=int(getattr(_T, "analog_top_k", 10)),
            min_cosine=float(getattr(_T, "analog_min_cosine", 0.70)),
        )
    except Exception:
        return []
    if not hits:
        return []

    # For each analog day, fetch a few closed_trades that occurred on
    # the same day (regime-cohort proxy: ticker + outcome).
    analogs: list = []
    try:
        for hit in hits[: int(getattr(_T, "analog_render_top_n", 3))]:
            meta = hit.metadata or {}
            day = meta.get("date") or ""
            # Pull a small set of correlated closed trades. We don't
            # have a true day-cohort index in pgvector yet, so we
            # similarity-search the closed_trades namespace using the
            # same vector — cheap and good enough as Phase 8.7 v1.
            try:
                trade_hits = vector_store.similarity_search(
                    "closed_trades", qv, k=5, min_cosine=0.55,
                )
            except Exception:
                trade_hits = []
            top_trades = []
            for th in trade_hits:
                tm = th.metadata or {}
                if not tm:
                    continue
                top_trades.append({
                    "ticker": tm.get("ticker") or "",
                    "strategy": tm.get("strategy") or "",
                    "outcome": tm.get("outcome") or "",
                    "pnl": tm.get("pnl"),
                })
            analogs.append({
                "date": day,
                "regime": meta.get("regime"),
                "cosine": round(float(hit.cosine), 3),
                "top_trades": top_trades[:3],
            })
    except Exception:
        return []
    return analogs


def _format_analog_block(analogs: list) -> str:
    if not analogs:
        return ""
    lines = ["Today most resembles these historical days:"]
    for idx, an in enumerate(analogs, 1):
        date_s = an.get("date") or "unknown"
        regime_s = an.get("regime") or "unknown"
        cosine_s = an.get("cosine")
        head = (f"  {idx}. {date_s} (cosine {cosine_s}, "
                  f"regime: {regime_s})")
        trades = an.get("top_trades") or []
        if trades:
            head += " — winners: " + ", ".join(
                f"{t.get('ticker') or '?'} {t.get('strategy') or ''} "
                f"{('+' + str(t['pnl'])) if (t.get('pnl') or 0) > 0 else (str(t.get('pnl')) if t.get('pnl') is not None else '')}".strip()
                for t in trades
            )
        lines.append(head)
    lines.append(
        "Given this historical pattern, what's the asymmetric trade "
        "RIGHT NOW? Cite at least one analog in your thesis when it "
        "informs the trade."
    )
    return "\n".join(lines)


class OpportunityBrain:
    """Claude-backed discretionary opportunism reasoner."""

    def __init__(self, api_key: Optional[str] = None,
                   client: Any = None) -> None:
        self._api_key = api_key
        self._client = client
        # Cache key = (regime_state, 5-min bucket id).
        self._cache: Dict[tuple, OpportunityHypothesis] = {}

    def _key(self) -> str:
        if self._api_key is not None:
            return self._api_key
        from backend.config import anthropic_key
        return anthropic_key()

    @property
    def available(self) -> bool:
        return self._client is not None or bool(self._key())

    def _anthropic(self):
        if self._client is None:
            from anthropic import Anthropic  # type: ignore
            self._client = Anthropic(api_key=self._key(), timeout=30.0)
        return self._client

    def analyze(self, regime_state: str,
                live_context: Dict[str, Any],
                ) -> Optional[OpportunityHypothesis]:
        """Return a hypothesis on non-normal regimes; ``None`` on normal.

        Cache hits within the same 5-min wall-clock bucket short-circuit
        the Claude call entirely. The cached hypothesis has
        ``from_cache=True`` set so callers (and the activity feed) can
        tell whether the row reflects fresh reasoning.
        """
        regime_state = (regime_state or "normal").lower()
        if regime_state == "normal":
            return None
        if not self.available:
            return None

        cache_key = (regime_state, _five_min_bucket())
        cached = self._cache.get(cache_key)
        if cached is not None:
            # Return a shallow copy marked from_cache so the caller can
            # tell whether the row is fresh.
            hit = OpportunityHypothesis(
                ticker=cached.ticker,
                direction=cached.direction,
                dte_bucket=cached.dte_bucket,
                conviction=cached.conviction,
                thesis=cached.thesis,
                notes=cached.notes,
                regime_state=cached.regime_state,
                from_cache=True,
                historical_analogs=list(cached.historical_analogs),
                raw=cached.raw,
            )
            return hit

        # MITS Phase 8.7 — pull historical analogs from pgvector and
        # weave them into the Claude prompt. Empty list ⇒ no neighbors
        # above the cosine floor; the Brain falls back to its Phase 7
        # discretionary path with a plain regime + live-tape prompt.
        analogs = _fetch_analogs(regime_state, live_context)
        analog_block = _format_analog_block(analogs)

        try:
            client = self._anthropic()
            user_msg = _format_inputs(regime_state, live_context)
            # MITS Phase 11.I — when crisis-regime, weave Form 4 +
            # 13F enrichment for the affected tickers.
            phase11_block = _format_affected_ticker_enrichment(live_context)
            if phase11_block:
                user_msg = phase11_block + "\n\n" + user_msg
            if analog_block:
                user_msg = analog_block + "\n\n" + user_msg
            response = client.messages.create(
                model=TUNABLES.opportunity_brain_model,
                max_tokens=int(TUNABLES.opportunity_brain_max_tokens),
                system=[{
                    "type": "text", "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_msg}],
            )
            # Record cost.
            try:
                from backend.bot.ai_cost import record_from_response
                record_from_response(
                    surface="opportunity_brain",
                    model=TUNABLES.opportunity_brain_model,
                    response=response,
                )
            except Exception:
                pass
            text = "".join(
                b.text for b in response.content
                if getattr(b, "type", None) == "text"
            )
            parsed = _parse(text)
        except Exception as exc:
            logger.warning("opportunity brain analyze failed: %s", exc)
            return None

        hypothesis = OpportunityHypothesis(
            ticker=str(parsed.get("ticker") or "SPY")[:10].upper(),
            direction=str(parsed.get("direction") or "skip")[:32].lower(),
            dte_bucket=str(parsed.get("dte_bucket") or "1d")[:16].lower(),
            conviction=max(0.0, min(1.0,
                                    float(parsed.get("conviction") or 0.0))),
            thesis=str(parsed.get("thesis") or "")[:2000],
            notes=str(parsed.get("notes") or "")[:600],
            regime_state=regime_state,
            from_cache=False,
            historical_analogs=analogs,
            raw=parsed,
        )
        self._cache[cache_key] = hypothesis
        return hypothesis


__all__ = ["OpportunityBrain", "OpportunityHypothesis"]
