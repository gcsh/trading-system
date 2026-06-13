"""Autonomous AI Brain — a Claude-driven portfolio trader.

Unlike the rule strategies (each of which encodes one fixed setup) the brain is
handed the full market snapshot for every candidate ticker plus the live
portfolio state, and is free to reason however it likes — any style, any blend
of technicals / options flow / gamma exposure / news / macro, and (optionally)
live web research — then returns one decision per ticker.

It only ever proposes PAPER actions; the engine still runs every decision
through the RiskManager before anything executes. With no API key it is simply
unavailable and the engine falls back to the rule strategies.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from backend.bot.data.options import chain_strike, snap_strike
from backend.bot.strategies.base import Action, Signal
from backend.config import SETTINGS, TUNABLES

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an autonomous trader running a small PAPER (simulated) account. You are NOT limited to any fixed strategy list — use whatever reasoning you judge best and combine signals freely: trend/momentum, mean-reversion, support/resistance, dealer gamma exposure (GEX) walls & flip, unusual options flow, volatility, earnings, news and macro context.

When a ticker carries a 'Memory says:' line in its snapshot, that is the system's knowledge-graph evidence for similar past setups (sample size, win rate, posterior, average move). REASON OVER THAT EVIDENCE — when posterior win rates are healthy on a meaningful sample, weight that toward action; when they are weak or thin, demand more from live signals before acting. Do not over-ride good evidence on a hunch, and do not invent confidence the corpus doesn't support.

The user message gives you the account state (cash, open positions, today's P&L, limits) and a compact market snapshot for each candidate ticker.

Decide what to do for EACH ticker. Be selective — most of the time the right move is HOLD. Only propose a BUY/SELL when the evidence genuinely lines up, and reflect your conviction honestly in `confidence` (reserve >0.75 for multi-signal confirmation). For any non-HOLD action, set a sensible stop_loss_pct and take_profit_pct (percent numbers, e.g. 4 means 4%). Respect risk: never imply more size than the account allows; the system enforces sizing and limits separately.

The user audits every call, so your `reasoning` must be a clear, complete, plain-English explanation of WHY — name the specific signals you weighed (trend, momentum, dealer gamma walls/flip & regime, options flow/sweeps, IV/volatility, news, earnings, macro), how they combine for or against the trade, the entry logic, why the stop/target levels, and why now. Aim for 3-5 tight sentences per ticker — specific, no filler. Explain HOLDs too (what's missing or conflicting).

Return ONLY a JSON object — no prose before or after — of exactly this shape:
{"decisions": [
  {"ticker": "AAA", "action": "BUY_STOCK|SELL_STOCK|BUY_CALL|BUY_PUT|HOLD",
   "confidence": 0.0-1.0, "stop_loss_pct": number, "take_profit_pct": number,
   "approach": "short label of the angle you used",
   "reasoning": "the full, auditable explanation described above"}
]}
Include every ticker you were given. Do not invent tickers or any other action values."""

# Snapshot fields worth showing the model (compact — keeps tokens/cost low).
_SNAP_KEYS = [
    "price", "prev_close", "rsi", "macd", "macd_signal", "ma50", "ma200",
    "adx", "atr", "vix", "iv_rank", "volume", "avg_volume",
    "dealer_regime", "gamma_flip", "call_wall", "put_wall", "opex_day",
    "bullish_sweeps", "bearish_sweeps", "premarket_bullish_sweeps", "darkpool_confirms",
    "news_score", "earnings_days", "pe_ratio", "spy_trend", "market_trend", "high_52w",
]

_OPTION_ACTIONS = {Action.BUY_CALL, Action.BUY_PUT}


def _fmt_snapshot(ticker: str, snap: Dict[str, Any]) -> str:
    lines = [f"- {ticker}:"]
    for k in _SNAP_KEYS:
        v = snap.get(k)
        if v is not None:
            lines.append(f"    {k}: {v}")
    news = snap.get("news") or snap.get("headlines")
    if isinstance(news, list) and news:
        heads = []
        for it in news[:3]:
            t = (it.get("headline") or it.get("title")) if isinstance(it, dict) else str(it)
            if t:
                heads.append(str(t)[:160])
        if heads:
            lines.append("    headlines: " + " | ".join(heads))
    # MITS Phase 1 — corpus evidence line per ticker. Reason OVER
    # evidence, not from first principles. The engine threads the
    # knowledge_evidence block into the snapshot under the key
    # ``knowledge_evidence`` for each ticker; falls through silently
    # when the corpus is cold.
    ke = snap.get("knowledge_evidence") or {}
    summary = (ke.get("summary") or "").strip()
    if summary:
        lines.append(f"    Memory says: {summary}")
        cells = ke.get("cells") or []
        if cells:
            top = cells[:3]
            top_str = "; ".join(
                f"{c.get('pattern')}@{c.get('regime')}/{c.get('vol_state')}"
                f" N={c.get('sample_size')} post={(c.get('posterior_win_rate') or 0) * 100:.0f}%"
                for c in top
            )
            lines.append(f"    Top analog cells: {top_str}")

    # MITS Phase 11.I — Recent activity enrichment: insider Form 4 +
    # 13F top-funds + similar-regime analogs. These are explicit
    # citations the operator can verify in SEC + the UI; the Brain
    # is encouraged to reason OVER them in its rationale.
    insider = snap.get("insider_recent") or []
    if insider:
        bits = []
        for r in insider[:3]:
            code = (r.get("transaction_code") or "?").upper()
            kind = ("buy" if code == "P" else
                       "sell" if code == "S" else
                       "exercise" if code == "M" else code)
            name = (r.get("insider_name") or "?")[:30]
            value = r.get("total_value")
            try:
                vstr = (f"${abs(float(value)) / 1000:.0f}k"
                          if value is not None else "?")
            except Exception:
                vstr = "?"
            bits.append(f"{name} {kind} {vstr} {r.get('transaction_date')}")
        lines.append("    Recent insider activity: " + " | ".join(bits))
        if snap.get("insider_cluster_buy_30d"):
            n = snap.get("insider_cluster_distinct_buyers_30d") or 0
            lines.append(f"    Insider cluster-buy 30d: {n} insiders bought")
    sm = snap.get("smart_money") or {}
    top_funds = sm.get("top_funds") or []
    if top_funds:
        fbits = []
        for f in top_funds[:3]:
            name = (f.get("fund_name") or "?")[:35]
            shares = f.get("shares")
            delta = f.get("change_from_prior_qtr")
            try:
                sstr = (f"{float(shares):,.0f}sh"
                           if shares is not None else "?")
            except Exception:
                sstr = "?"
            try:
                dstr = (f" Δ{float(delta):+,.0f}"
                           if delta is not None else "")
            except Exception:
                dstr = ""
            fbits.append(f"{name} {sstr}{dstr}")
        direction = sm.get("smart_money_direction") or "flat"
        lines.append(
            f"    Smart money ({sm.get('latest_quarter') or 'latest'} "
            f"13F, {direction}): " + " | ".join(fbits))
    sims = snap.get("similar_regime_days") or []
    if sims:
        sbits = [(f"{x.get('date')} ({x.get('regime')}, "
                   f"cos {x.get('cosine')})")
                   for x in sims[:3]]
        lines.append("    Today most resembles: " + " | ".join(sbits))
    return "\n".join(lines)


def _build_user_message(snapshots: Dict[str, Any], portfolio: Dict[str, Any]) -> str:
    held = ", ".join(portfolio.get("held") or []) or "none"
    acct = (
        f"Account (PAPER): cash ${portfolio.get('cash', 0):,.0f}, "
        f"portfolio ${portfolio.get('portfolio_value', 0):,.0f}, "
        f"open positions {portfolio.get('open_positions', 0)} (holding: {held}), "
        f"today's P&L ${portfolio.get('daily_pnl', 0):,.0f}. "
        f"Max position size ${portfolio.get('max_position_usd') or 0:,.0f}; "
        f"act only above confidence {portfolio.get('min_confidence', 0.4)}."
    )
    snaps = "\n".join(_fmt_snapshot(tk, s) for tk, s in snapshots.items())
    return (
        f"{acct}\n\n"
        f"Candidate tickers (each may carry a 'Memory says:' line "
        f"summarising historical analogs from the knowledge corpus — "
        f"weigh that evidence alongside the live signals):\n{snaps}\n\n"
        f"Return the JSON decisions now."
    )


def _parse(text: str) -> dict:
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in brain response")
    return json.loads(text[start : end + 1])


class AutonomousBrain:
    """Claude-backed portfolio decision-maker. Never raises to the caller."""

    def __init__(self, api_key: Optional[str] = None, client: Any = None) -> None:
        self._api_key = api_key   # explicit override (tests / direct use)
        self._client = client

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

    def decide_portfolio(
        self,
        snapshots: Dict[str, Dict[str, Any]],
        portfolio: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Signal]:
        """Batched Claude calls → {TICKER: Signal} for every candidate.

        Each request handles a small batch of tickers (default 6). Asking
        Claude for ~12 decisions in one shot was hitting the response
        token budget and truncating the JSON mid-string, producing
        ``Expecting ',' delimiter`` parse errors that wiped the entire
        cycle. Batching makes each response comfortably fit under the
        max_tokens ceiling AND isolates failures: one bad batch no longer
        invalidates the others — the others' decisions still flow.
        """
        portfolio = portfolio or {}
        if not self.available or not snapshots:
            return {}
        batch_size = max(1, int(getattr(TUNABLES, "ai_brain_batch_size", 6)))
        items = list(snapshots.items())
        merged: Dict[str, Signal] = {}
        for i in range(0, len(items), batch_size):
            batch = dict(items[i:i + batch_size])
            merged.update(self._decide_one_batch(batch, portfolio))
        return merged

    def _decide_one_batch(
        self,
        snapshots: Dict[str, Dict[str, Any]],
        portfolio: Dict[str, Any],
    ) -> Dict[str, Signal]:
        """Single Claude call. Returns {} on any failure so the caller can
        merge a partial result without losing other batches."""
        try:
            client = self._anthropic()
            kwargs: Dict[str, Any] = dict(
                model=TUNABLES.ai_brain_model,
                max_tokens=TUNABLES.ai_brain_max_tokens,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": _build_user_message(snapshots, portfolio)}],
            )
            if portfolio.get("web_research"):
                kwargs["tools"] = [{
                    "type": "web_search_20250305", "name": "web_search",
                    "max_uses": TUNABLES.ai_brain_web_max_uses,
                }]
            response = client.messages.create(**kwargs)
            try:
                from backend.bot.ai_cost import record_from_response
                record_from_response(surface="brain", model=TUNABLES.ai_brain_model,
                                        response=response)
            except Exception:
                pass
            text = "".join(
                b.text for b in response.content if getattr(b, "type", None) == "text"
            )
            parsed = _parse(text)
        except Exception as exc:
            tickers = ",".join(sorted(snapshots.keys()))
            logger.warning("AI brain batch [%s] decide failed: %s", tickers, exc)
            if portfolio.get("web_research"):
                retry = dict(portfolio)
                retry["web_research"] = False
                return self._decide_one_batch(snapshots, retry)
            return {}
        return self._to_signals(snapshots, parsed)

    def _to_signals(self, snapshots: Dict[str, Any], parsed: dict) -> Dict[str, Signal]:
        known = {t.upper() for t in snapshots}
        out: Dict[str, Signal] = {}
        for dec in (parsed.get("decisions") or []):
            tk = str(dec.get("ticker") or "").upper()
            if tk not in known:
                continue
            try:
                action = Action(str(dec.get("action") or "HOLD").upper())
            except ValueError:
                action = Action.HOLD
            conf = max(0.0, min(1.0, float(dec.get("confidence", 0.0) or 0.0)))
            approach = str(dec.get("approach") or "ai brain")[:80]
            reasoning = str(dec.get("reasoning") or "").strip()[:1600]
            price = float(snapshots.get(tk, {}).get("price", 0.0) or 0.0)
            is_opt = action in _OPTION_ACTIONS
            out[tk] = Signal(
                ticker=tk,
                action=action,
                confidence=conf,
                reason=reasoning or approach,
                strategy="ai_brain",
                stop_loss=float(dec.get("stop_loss_pct") or 0.0),
                take_profit=float(dec.get("take_profit_pct") or 0.0),
                strike=(
                    chain_strike(tk, price, "call" if action == Action.BUY_CALL else "put")
                    if is_opt and price > 0 else None
                ),
                dte=7 if is_opt else None,
                metadata={"source": "ai_brain", "approach": approach, "raw": dec},
            )
        return out
