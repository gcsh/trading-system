"""Local paper-trading executor.

Tracks a fake cash balance and a positions table in the same SQLite DB the
rest of the app uses. Fills happen instantly at the current yfinance market
price. Options "fills" book the premium debit/credit as a cash flow and
record a metadata-only position — full options P&L (greeks, time decay)
isn't simulated.

Interface is interchangeable with :class:`Executor` and
:class:`AlpacaExecutor` — the engine never needs to know which one it has.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from backend.db import session_scope
from backend.models.paper import (
    PaperAccount,
    PaperPosition,
    get_or_create_account,
)

logger = logging.getLogger(__name__)


# ── P1.8 Execution realism helpers ──────────────────────────────────────


def _stock_commission(qty: float) -> float:
    """IBKR-equivalent stock commission. $0.005/share, $1.00 minimum.
    Config keys override the defaults."""
    from backend.config import TUNABLES
    raw = abs(qty) * float(TUNABLES.broker_stock_commission_per_share)
    return max(float(TUNABLES.broker_stock_commission_min), raw)


def _option_commission(contracts: float) -> float:
    """IBKR-equivalent option commission. $0.65/contract, $1.00 minimum."""
    from backend.config import TUNABLES
    raw = abs(contracts) * float(TUNABLES.broker_option_commission_per_contract)
    return max(float(TUNABLES.broker_option_commission_min), raw)


def _apply_stock_spread(mid_price: float, side: str) -> float:
    """Half-spread cost. BUY pays mid + spread/2, SELL receives mid - spread/2.
    Default 1 basis point per side (very tight; large-cap reality)."""
    from backend.config import TUNABLES
    bps = float(TUNABLES.broker_stock_spread_bps)
    half = mid_price * (bps / 10_000.0) / 2.0
    return mid_price + half if side.upper() == "BUY" else mid_price - half


def _apply_option_spread(mid_price: float, side: str) -> float:
    """Half-spread for options. Default 2% per side — retail reality."""
    from backend.config import TUNABLES
    pct = float(TUNABLES.broker_option_spread_pct)
    half = mid_price * pct / 2.0
    return mid_price + half if side.upper() == "BUY" else mid_price - half


@dataclass
class PaperOrderResult:
    success: bool
    order_id: Optional[str]
    paper: bool = True
    raw: dict = field(default_factory=dict)
    error: Optional[str] = None


def _parse_meta(blob: Optional[str]) -> dict:
    if not blob:
        return {}
    try:
        return json.loads(blob)
    except Exception:
        return {}


def _lookup_entry_grade(session, ticker: str) -> Optional[str]:
    """Pull the ranker grade from the most recent OPEN Trade row for a
    ticker. Returns None when no trade row exists or none carries an
    ``analytics.rank.grade`` payload (e.g. pre-14.A entries). The engine
    persists analytics + rank under detail_json's ``analytics`` key.
    """
    try:
        from sqlalchemy import select
        from backend.models.trade import Trade
    except Exception:
        return None
    try:
        row = session.execute(
            select(Trade)
            .where(Trade.ticker == ticker)
            .where(Trade.status == "open")
            .order_by(Trade.timestamp.desc())
            .limit(1)
        ).scalar_one_or_none()
    except Exception:
        return None
    if row is None or not getattr(row, "detail_json", None):
        return None
    try:
        detail = json.loads(row.detail_json)
    except Exception:
        return None
    analytics = detail.get("analytics") or {}
    rank = analytics.get("rank") or {}
    grade = rank.get("grade")
    if not grade or grade == "Reject":
        return None
    return str(grade)


def _yf_last_price(ticker: str) -> float:
    """Return the most recent traded price; 0.0 on failure."""
    try:
        import yfinance as yf

        df = yf.download(
            ticker, period="1d", interval="1m", progress=False, auto_adjust=False
        )
        if df is None or df.empty:
            df = yf.download(
                ticker, period="5d", interval="1d", progress=False, auto_adjust=False
            )
        if df is None or df.empty:
            return 0.0
        close = df["Close"]
        if hasattr(close, "iloc"):
            value = close.iloc[-1]
            if hasattr(value, "iloc"):  # MultiIndex column
                value = value.iloc[-1]
            return float(value)
        return 0.0
    except Exception:
        logger.exception("yfinance price fetch failed for %s", ticker)
        return 0.0


class PaperExecutor:
    """Fills orders against an in-DB ledger using real market prices."""

    def __init__(
        self,
        starting_cash: float = 1000.0,
        price_fn: Optional[Callable[[str], float]] = None,
    ) -> None:
        self.starting_cash = starting_cash
        self.paper = True
        self._price_fn = price_fn or _yf_last_price
        # Eagerly seed the account row.
        with session_scope() as session:
            get_or_create_account(session, starting_cash=starting_cash)

    # -- session ------------------------------------------------------------
    def login(self) -> bool:
        return True

    # -- pricing ------------------------------------------------------------
    def _price(self, ticker: str) -> float:
        try:
            return float(self._price_fn(ticker))
        except Exception:
            return 0.0

    # -- account ------------------------------------------------------------
    def get_account_state(self, positions: Optional[list] = None) -> dict:
        """Account snapshot. Equity is derived by summing market_value
        from the same positions() call the snapshot reconciler uses, so
        portfolio_value and the snapshot's position sum always agree
        (no spurious drift from two independent price passes).

        ``positions`` accepts a pre-fetched marked-positions list — pass
        this when the caller is going to use the same list for downstream
        reconciliation (e.g. the engine's snapshot recorder). Eliminates
        the cents-level drift that came from fetching MTM twice per cycle.
        """
        marked = positions if positions is not None else self.positions()
        with session_scope() as session:
            account = get_or_create_account(session, self.starting_cash)
            equity = account.cash
            for p in marked:
                if p.get("kind", "stock") == "stock":
                    mv = p.get("market_value")
                    if mv is not None:
                        equity += float(mv)
                    else:
                        # No live mark — fall back to cost basis so we
                        # don't silently zero out the position.
                        equity += (float(p.get("quantity") or 0)
                                   * float(p.get("avg_cost") or 0))
                else:
                    # Options + complex: market_value already includes
                    # the contract multiplier and sign. Long positions
                    # add equity, shorts subtract (the broker's mark
                    # convention is encoded in market_value via the
                    # is_long branch in positions()).
                    mv = p.get("market_value")
                    if mv is None:
                        mv = (abs(float(p.get("quantity") or 0))
                              * float(p.get("avg_cost") or 0))
                    qty = float(p.get("quantity") or 0)
                    if p.get("kind") == "option" and qty < 0:
                        equity -= float(mv)
                    else:
                        equity += float(mv)
            account.last_portfolio_value = equity
            return {
                "buying_power": account.cash,
                "portfolio_value": equity,
                "open_positions": len(marked),
                "cash": account.cash,
                "starting_cash": account.starting_cash,
                "realized_pnl": account.realized_pnl,
            }

    def reset(self, starting_cash: Optional[float] = None) -> dict:
        """Wipe positions and reset cash. Used by the UI 'reset paper' button."""
        with session_scope() as session:
            account = get_or_create_account(session, self.starting_cash)
            if starting_cash is not None:
                account.starting_cash = float(starting_cash)
            account.cash = account.starting_cash
            account.realized_pnl = 0.0
            account.last_portfolio_value = account.starting_cash
            session.query(PaperPosition).delete()
            return account.to_dict()

    def positions(self) -> list[dict]:
        """Open positions, marked to market with current price + unrealized P&L."""
        try:
            from zoneinfo import ZoneInfo
            _PT = ZoneInfo("America/Los_Angeles")
        except Exception:
            _PT = None  # falls back to UTC ISO string if zoneinfo unavailable
        with session_scope() as session:
            rows = session.query(PaperPosition).all()
            out = []
            for p in rows:
                d = p.to_dict()
                # EXIT.3 — stamp entry time in PT for the operator's UI.
                # The DB stores opened_at as naive UTC; localize → PT for
                # display, but also keep the original UTC ISO available.
                if p.opened_at and _PT is not None:
                    try:
                        from datetime import timezone
                        utc_dt = p.opened_at.replace(tzinfo=timezone.utc)
                        pt_dt = utc_dt.astimezone(_PT)
                        d["entry_time_pt"] = pt_dt.strftime(
                            "%Y-%m-%d %H:%M:%S %Z"
                        )
                        d["entry_time_utc"] = utc_dt.isoformat()
                    except Exception:
                        d["entry_time_pt"] = None
                if p.kind == "stock":
                    price = self._price(p.ticker)
                    if price > 0:
                        d["current_price"] = round(price, 2)
                        d["market_value"] = round(price * p.quantity, 2)
                        d["unrealized_pnl"] = round((price - p.avg_cost) * p.quantity, 2)
                        d["unrealized_pnl_pct"] = round(
                            (price - p.avg_cost) / p.avg_cost * 100, 2
                        ) if p.avg_cost else 0.0
                        # MITS Phase 17.A item #10 — stamp MTM cadence on
                        # stock rows as well. Used by the data-quality
                        # surface to confirm the engine is actually
                        # repricing positions each cycle.
                        now_utc = datetime.utcnow()
                        p.last_marked_at = now_utc
                        d["last_marked_at"] = now_utc.isoformat()
                elif p.kind == "option":
                    # P2.3 — Real MTM via live chain + BS fallback. The
                    # chain quote (when fresh ≤ 600s) is preferred; the
                    # BS fallback reprices using ``stored_iv`` so the
                    # mark stays meaningful when ThetaData is sluggish.
                    # The legacy stub (``intrinsic + 0.005×strike``) is
                    # the last-resort path inside ``price_for_mark``.
                    from backend.bot.options.pricing import price_for_mark
                    meta = _parse_meta(p.meta)
                    spot = self._price(p.ticker)
                    strike = float(p.strike or meta.get("strike") or 0.0)
                    expiration = (p.expiration
                                          or meta.get("expiration") or "")
                    right = (p.option_type
                                 or ("call"
                                       if (meta.get("action") or "").upper().endswith("CALL")
                                       else "put"))
                    is_call = right.lower().startswith("c")
                    is_long = p.quantity > 0
                    stored_iv = p.stored_iv or p.entry_iv
                    mark_result = price_for_mark(
                        symbol=p.ticker, spot=float(spot or 0),
                        strike=strike, expiration=expiration,
                        right=right, stored_iv=stored_iv,
                    )
                    mark_per_share = mark_result.mid
                    contracts = abs(p.quantity)
                    mark = mark_per_share * 100 * contracts
                    entry = abs(p.avg_cost) * contracts
                    pnl = (mark - entry) if is_long else (entry - mark)
                    d["current_price"] = round(mark_per_share, 4)
                    d["market_value"] = round(mark, 2)
                    d["unrealized_pnl"] = round(pnl, 2)
                    d["unrealized_pnl_pct"] = round((pnl / entry * 100) if entry else 0.0, 2)
                    d["strike"] = strike
                    d["expiration"] = expiration
                    d["option_type"] = "call" if is_call else "put"
                    d["side"] = "LONG" if is_long else "SHORT"
                    # P1.5 — propagate the mark source so the equity
                    # snapshot's ``pricing_source_mix`` is honest.
                    d["pricing_source"] = mark_result.source
                    d["mark"] = round(mark_per_share, 4)
                    d["quote_age_seconds"] = mark_result.age_seconds
                    # MITS Phase 17.A item #10 — stamp last_marked_at on
                    # every MTM pass. Persisted so the operator can see
                    # "how recently did the engine repricer touch this
                    # position?" without having to scrape logs.
                    now_utc = datetime.utcnow()
                    p.last_marked_at = now_utc
                    d["last_marked_at"] = now_utc.isoformat()
                    # MITS Phase 17.A item #2 — surface IV-refresh lag
                    # when the chain repriced this position. The mark
                    # may be fresh but the IV under it can be hours old.
                    if mark_result.source == "thetadata":
                        iv_age = None
                        if p.stored_iv_at is not None:
                            iv_age = (now_utc - p.stored_iv_at).total_seconds()
                        d["mtm"] = {
                            "stored_iv_age_seconds": iv_age,
                            "source": mark_result.source,
                        }
                    # P2.4 — refresh stored_iv when the chain is fresh.
                    if (mark_result.source == "thetadata"
                            and mark_result.iv and mark_result.iv > 0):
                        # We compute IV opportunistically when chain mid +
                        # spot + dte allow recovery via implied_iv. The
                        # pricing module sets it on chain marks now.
                        try:
                            p.stored_iv = float(mark_result.iv)
                            p.stored_iv_at = now_utc
                        except Exception:
                            pass
                    # EXIT.1 — surface the live exit-manager state so the
                    # UI can render the trailing floor, hard stop, peak,
                    # and drawdown-from-peak alongside the position. The
                    # decision itself is consulted by the engine each
                    # cycle; here we just expose its diagnostic numbers.
                    try:
                        from backend.bot.options.exit_manager import (
                            decide_exit, compute_dte,
                        )
                        dte_now = compute_dte(p.expiration or "")
                        entry_per_share = (float(p.avg_cost) / 100.0
                                           if p.avg_cost else 0.0)
                        if entry_per_share > 0 and mark_per_share > 0:
                            decision = decide_exit(
                                entry_premium_per_share=entry_per_share,
                                current_premium_per_share=mark_per_share,
                                peak_premium_per_share=p.peak_premium_per_share,
                                dte=dte_now,
                                entry_iv=p.entry_iv,
                                current_iv=(p.stored_iv or p.last_iv_seen),
                            )
                            d["exit_state"] = {
                                "gain_pct": round(decision.gain_pct, 2),
                                "drawdown_from_peak_pct": round(
                                    decision.drawdown_from_peak_pct, 2,
                                ),
                                "monitor_active": decision.monitor_active,
                                "trailing_floor_pct": (
                                    round(decision.trailing_floor_pct, 2)
                                    if decision.trailing_floor_pct is not None
                                    else None
                                ),
                                "hard_stop_pct": round(decision.hard_stop_pct, 2),
                                "iv_crush_detected": decision.iv_crush_detected,
                                "dte": dte_now,
                                "reason": decision.reason,
                            }
                    except Exception:
                        pass
                elif p.kind == "complex":
                    # Multi-leg / SELL_CSP / SELL_COVERED_CALL / IRON_CONDOR
                    # land here. Mark synthetically: intrinsic per leg ×
                    # multiplier × contracts, sign-flipped for shorts.
                    # Eliminates the $0.00 fall-through that left
                    # CurrentlyHoldingStrip and topbar "invested" disagreeing.
                    meta = _parse_meta(p.meta)
                    spot = self._price(p.ticker)
                    contracts = abs(p.quantity) or 1
                    action_raw = (meta.get("action") or "").upper()
                    # Per-leg intrinsic helper.
                    def _leg_intrinsic(strike: float, kind: str) -> float:
                        if spot <= 0 or strike <= 0:
                            return 0.0
                        if kind.startswith("c"):
                            return max(0.0, spot - strike)
                        return max(0.0, strike - spot)
                    mark = 0.0
                    if action_raw in ("SELL_CSP", "SELL_COVERED_CALL"):
                        # Single short leg: liability = intrinsic.
                        k = float(meta.get("strike") or 0.0)
                        kind = "put" if action_raw == "SELL_CSP" else "call"
                        mark = -_leg_intrinsic(k, kind) * 100 * contracts
                    elif action_raw in ("IRON_CONDOR",):
                        # Four legs: short call+put (negative), long call+put (positive).
                        cs = float(meta.get("call_short") or 0.0)
                        cl = float(meta.get("call_long") or 0.0)
                        ps = float(meta.get("put_short") or 0.0)
                        pl = float(meta.get("put_long") or 0.0)
                        mark = (
                            -_leg_intrinsic(cs, "call")
                            + _leg_intrinsic(cl, "call")
                            - _leg_intrinsic(ps, "put")
                            + _leg_intrinsic(pl, "put")
                        ) * 100 * contracts
                    elif action_raw in ("BULL_CALL_SPREAD",):
                        # Long lower + short higher (debit spread).
                        bk = float(meta.get("buy_strike") or 0.0)
                        sk = float(meta.get("sell_strike") or 0.0)
                        mark = (
                            _leg_intrinsic(bk, "call")
                            - _leg_intrinsic(sk, "call")
                        ) * 100 * contracts
                    else:
                        # Unknown complex shape — surface 0 with a flag so
                        # the UI can show "—" instead of stale numbers.
                        mark = 0.0
                    entry = float(p.avg_cost or 0.0) * contracts
                    # For shorts (premium received), avg_cost is the credit;
                    # mark is negative (liability). PnL = entry + mark.
                    is_short = action_raw.startswith("SELL_") or action_raw == "IRON_CONDOR"
                    if is_short:
                        pnl = entry + mark  # entry credit + (negative) mark
                    else:
                        pnl = mark - entry
                    d["current_price"] = round(mark / (100 * contracts), 4) if contracts else 0.0
                    d["market_value"] = round(mark, 2)
                    d["unrealized_pnl"] = round(pnl, 2)
                    d["unrealized_pnl_pct"] = round(
                        (pnl / entry * 100) if entry else 0.0, 2,
                    )
                    d["action"] = action_raw or "COMPLEX"
                    d["expiration"] = meta.get("expiration")
                    d["side"] = "SHORT" if is_short else "LONG"
                    d["contracts"] = contracts
                # MITS Phase 14.E — lift the entry grade from the most
                # recent open Trade row for this ticker so the UI strip
                # can show an A/B/C chip next to each held position.
                d["entry_grade"] = _lookup_entry_grade(session, p.ticker)
                out.append(d)
            return out

    def close_option(self, ticker: str, strike: float, expiration: str,
                      reason: str = "expiry") -> PaperOrderResult:
        """Force-close an open option position at its current intrinsic value.

        Used by the engine's exit manager when DTE hits zero. Realizes P&L as
        ``(intrinsic - entry)`` for longs / ``(entry - intrinsic)`` for shorts,
        deletes the position, settles cash, and returns the realized P&L on
        ``raw.pnl`` so the engine can persist it.

        P3.1 — at expiry, a SHORT ITM option is assigned rather than
        cash-settled at intrinsic. For SHORT PUT (CSP): we receive stock
        at the strike (cash debit = strike × 100 × contracts, stock added
        at avg_cost = strike). For SHORT CALL (CC): stock is removed at
        the strike (cash credit = strike × 100 × contracts).
        Net P&L matches the cash-settled close, but the resulting account
        state correctly reflects the assignment — critical for wheel
        strategies that chain CSP → assignment → CC.
        """
        spot = self._price(ticker)
        with session_scope() as session:
            account = get_or_create_account(session, self.starting_cash)
            row = (session.query(PaperPosition)
                    .filter_by(ticker=ticker.upper(), kind="option").first())
            if row is None:
                return PaperOrderResult(False, None, error=f"no open option for {ticker}")
            meta = _parse_meta(row.meta)
            # Match the specific contract (strike + expiry) — a ticker can hold
            # multiple option legs in principle.
            if (strike and float(meta.get("strike") or 0.0) != float(strike)) or (
                expiration and meta.get("expiration") != expiration
            ):
                return PaperOrderResult(False, None,
                    error=f"open option for {ticker} doesn't match {strike}/{expiration}")
            is_call = (meta.get("action") or "").upper().endswith("CALL")
            is_long = row.quantity > 0
            contract_strike = float(meta.get("strike") or 0.0)
            intrinsic = (max(0.0, (spot - contract_strike) if is_call
                                else (contract_strike - spot))
                            if spot > 0 else 0.0)
            contracts = abs(row.quantity)
            mark = intrinsic * 100 * contracts
            entry = abs(row.avg_cost) * contracts
            pnl = (mark - entry) if is_long else (entry - mark)

            # MITS Phase 17.A item #12 — also compute P&L against the
            # LIVE chain/BS mark, not just intrinsic. Closes-at-expiry
            # cash-settle at intrinsic (correct), but at-mid closes
            # leave a gap between "what we realized" and "what MTM said
            # the position was worth one cycle ago". Surfacing the
            # delta exposes hidden bleed from late marks / wide spreads.
            marked_pnl = None
            realized_vs_marked_delta = None
            try:
                from backend.bot.options.pricing import price_for_mark
                stored_iv = row.stored_iv or row.entry_iv
                mark_result = price_for_mark(
                    symbol=row.ticker, spot=float(spot or 0),
                    strike=contract_strike,
                    expiration=meta.get("expiration") or "",
                    right="call" if is_call else "put",
                    stored_iv=stored_iv,
                )
                mark_per_share = float(mark_result.mid)
                marked_total = mark_per_share * 100 * contracts
                marked_pnl = (
                    (marked_total - entry) if is_long
                    else (entry - marked_total)
                )
                realized_vs_marked_delta = pnl - marked_pnl
            except Exception:
                marked_pnl = None
                realized_vs_marked_delta = None

            # P3.1 — Assignment path: SHORT, ITM, reason=expiry/expiry-close.
            is_assignment = (
                not is_long
                and intrinsic > 0
                and ("expiry" in (reason or "").lower())
            )
            assignment_meta = None
            if is_assignment:
                shares = 100 * contracts
                if is_call:
                    # SHORT CALL (CC) ITM at expiry → stock is called away.
                    # Cash credit at strike. We assume the operator held
                    # the underlying as a covered call; if not, the books
                    # still need to record the share-removal — best
                    # effort find the underlying stock row.
                    account.cash += contract_strike * shares
                    stock_row = (session.query(PaperPosition)
                                     .filter_by(ticker=ticker.upper(),
                                                    kind="stock").first())
                    if stock_row is not None and stock_row.quantity >= shares:
                        stock_row.quantity -= shares
                        if stock_row.quantity <= 1e-6:
                            session.delete(stock_row)
                    assignment_meta = {
                        "kind": "call_assignment",
                        "shares_removed": shares,
                        "strike": contract_strike,
                        "cash_credit": contract_strike * shares,
                    }
                else:
                    # SHORT PUT (CSP) ITM at expiry → assigned the stock.
                    # Cash debit at strike. Stock added at strike as cost.
                    account.cash -= contract_strike * shares
                    stock_row = (session.query(PaperPosition)
                                     .filter_by(ticker=ticker.upper(),
                                                    kind="stock").first())
                    if stock_row is None:
                        session.add(PaperPosition(
                            ticker=ticker.upper(), kind="stock",
                            quantity=shares, avg_cost=contract_strike,
                        ))
                    else:
                        new_qty = stock_row.quantity + shares
                        stock_row.avg_cost = (
                            (stock_row.avg_cost * stock_row.quantity
                                 + contract_strike * shares) / new_qty
                        )
                        stock_row.quantity = new_qty
                    assignment_meta = {
                        "kind": "put_assignment",
                        "shares_received": shares,
                        "strike": contract_strike,
                        "cash_debit": contract_strike * shares,
                    }
                # Realized PnL on the option position itself is still
                # ``entry - mark`` (entry premium received minus intrinsic
                # owed). The downstream stock will mark separately.
                account.realized_pnl += pnl
            else:
                # Cash settlement (non-assignment): long → receive mark;
                # short → pay mark (we kept the premium at entry).
                if is_long:
                    account.cash += mark
                else:
                    account.cash -= mark
                account.realized_pnl += pnl
            session.delete(row)
            logger.info("[paper-local] CLOSE_OPT %s strike=%.2f exp=%s spot=%.2f "
                         "intrinsic=%.2f pnl=%.2f reason=%s%s",
                         ticker, contract_strike, meta.get("expiration"),
                         spot, intrinsic, pnl, reason,
                         f" assignment={assignment_meta['kind']}"
                         if assignment_meta else "")
            return PaperOrderResult(
                success=True,
                order_id=f"paper-opt-close-{ticker}-{strike}",
                paper=True,
                raw={"ticker": ticker.upper(), "qty": contracts, "price": intrinsic,
                      "action": "CLOSE_OPTION", "pnl": round(pnl, 2),
                      # MITS Phase 17.A item #12 — also surface the
                      # mark-derived counterpart and their delta.
                      "marked_pnl": (round(float(marked_pnl), 2)
                                     if marked_pnl is not None else None),
                      "realized_vs_marked_delta": (
                          round(float(realized_vs_marked_delta), 2)
                          if realized_vs_marked_delta is not None else None
                      ),
                      "entry_price": abs(row.avg_cost) / 100, "reason": reason,
                      "assignment": assignment_meta,
                      "pricing_source": "paper_stub"},
            )

    def open_position(self, ticker: str, kind: str = "stock") -> Optional[dict]:
        """Return the open position dict for a ticker, or None."""
        with session_scope() as session:
            p = (
                session.query(PaperPosition)
                .filter_by(ticker=ticker.upper(), kind=kind)
                .first()
            )
            return p.to_dict() if p else None

    # -- orders -------------------------------------------------------------
    def place_stock_order(
        self,
        ticker: str,
        action: str,
        quantity: float,
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> PaperOrderResult:
        action = action.upper()
        mid_price = limit_price if (order_type == "limit" and limit_price) else self._price(ticker)
        if mid_price <= 0:
            return PaperOrderResult(False, None, error=f"no market price for {ticker}")

        qty = max(0.0, float(quantity))
        if qty <= 0:
            return PaperOrderResult(False, None, error="quantity must be > 0")

        # P1.8 — execution realism: fill at bid (sell) or ask (buy) rather
        # than at mid, plus per-share commission with a per-order minimum.
        price = _apply_stock_spread(mid_price, action)
        commission = _stock_commission(qty)
        # MITS Phase 17.A item #4 — fill-vs-mid slippage in bps. Mirrors
        # the option path; same definition.
        slippage_bps = (
            abs(price - mid_price) / mid_price * 10_000.0
            if mid_price > 0 else None
        )

        # MITS Phase 17.B — capture the quote that priced this fill so
        # FillSnapshot carries source + age provenance. Limit orders carry
        # the price the caller chose ("limit_order" tag, no age). Market
        # orders pull a fresh Quote from the unified hierarchy.
        from backend.bot.data.quote_source import Quote, get_quote
        if order_type == "limit" and limit_price:
            quote = Quote(price=float(mid_price), source="limit_order",
                          age_seconds=None)
        else:
            quote = get_quote(ticker)
            if quote.price <= 0:
                quote = Quote(price=float(mid_price), source="unknown",
                              age_seconds=None)

        with session_scope() as session:
            account = get_or_create_account(session, self.starting_cash)
            existing = (
                session.query(PaperPosition)
                .filter_by(ticker=ticker.upper(), kind="stock")
                .first()
            )
            cost = qty * price + commission

            if action == "BUY":
                if cost > account.cash + 1e-6:
                    return PaperOrderResult(
                        False, None, error=f"insufficient cash (need ${cost:.2f}, have ${account.cash:.2f})"
                    )
                account.cash -= cost
                if existing is None:
                    session.add(
                        PaperPosition(
                            ticker=ticker.upper(),
                            kind="stock",
                            quantity=qty,
                            avg_cost=price,
                        )
                    )
                else:
                    new_qty = existing.quantity + qty
                    existing.avg_cost = (
                        existing.avg_cost * existing.quantity + price * qty
                    ) / new_qty
                    existing.quantity = new_qty
            elif action == "SELL":
                if existing is None or existing.quantity < qty - 1e-6:
                    return PaperOrderResult(
                        False, None, error="cannot short — no shares to sell"
                    )
                pnl = (price - existing.avg_cost) * qty - commission
                entry = existing.avg_cost
                # SELL credits price * qty MINUS commission.
                account.cash += qty * price - commission
                account.realized_pnl += pnl
                existing.quantity -= qty
                if existing.quantity <= 1e-6:
                    session.delete(existing)
                order_id = f"paper-{ticker.upper()}-{action}-{int(price * 100)}"
                logger.info(
                    "[paper-local] SELL %s %s @ %.2f entry=%.2f pnl=%.2f cash=%.2f commission=%.2f",
                    qty, ticker, price, entry, pnl, account.cash, commission,
                )
                # MITS Phase 17.B — fill provenance snapshot.
                from backend.bot.execution.fill_snapshot import FillSnapshot
                snapshot = FillSnapshot.from_stock_quote(
                    quote,
                    commission=float(commission),
                    fill_price=float(price),
                    slippage_bps=float(slippage_bps) if slippage_bps is not None else 0.0,
                )
                return PaperOrderResult(
                    success=True,
                    order_id=order_id,
                    paper=True,
                    raw={
                        "ticker": ticker.upper(), "qty": qty, "price": price,
                        "mid_price": mid_price,
                        "commission": round(commission, 2),
                        # MITS Phase 17.A items #4, #5, #11.
                        "slippage_bps": slippage_bps,
                        "total_commission": round(float(commission), 2),
                        "pricing_source": "paper_stub",
                        "action": action, "pnl": round(pnl, 2), "entry_price": entry,
                        # MITS Phase 17.B.
                        "fill_snapshot_json": snapshot.to_json(),
                    },
                )
            else:
                return PaperOrderResult(False, None, error=f"unknown action {action}")

            order_id = f"paper-{ticker.upper()}-{action}-{int(price * 100)}"
            logger.info("[paper-local] %s %s %s @ %.2f cash=%.2f", action, qty, ticker, price, account.cash)
            # MITS Phase 17.B — fill provenance snapshot.
            from backend.bot.execution.fill_snapshot import FillSnapshot
            snapshot = FillSnapshot.from_stock_quote(
                quote,
                commission=float(commission),
                fill_price=float(price),
                slippage_bps=float(slippage_bps) if slippage_bps is not None else 0.0,
            )
            return PaperOrderResult(
                success=True,
                order_id=order_id,
                paper=True,
                raw={
                    "ticker": ticker.upper(), "qty": qty, "price": price,
                    "action": action,
                    # MITS Phase 17.A items #4, #5, #11.
                    "slippage_bps": slippage_bps,
                    "total_commission": round(float(commission), 2),
                    "pricing_source": "paper_stub",
                    # MITS Phase 17.B.
                    "fill_snapshot_json": snapshot.to_json(),
                },
            )

    def place_options_order(
        self,
        ticker: str,
        action: str,
        quantity: int,
        strike: float,
        expiration: str,
    ) -> PaperOrderResult:
        """Single-leg options fill via real ThetaData chain quote with
        Black-Scholes fallback (P2.2).

        Pricing pipeline:
          1. ``price_at_entry`` resolves the live chain mid (and Greeks).
             Half-spread + commission still applied on top of that.
          2. If the chain is stale (> 60s) or unavailable, BS prices the
             contract using the IV hint (or 30% default).
          3. Last-resort stub matches the legacy behavior — only reached
             when both ThetaData and BS fail.

        Stores entry_bid/ask/mid/iv/delta/gamma/theta/vega on the
        PaperPosition row so the MTM repricer (P2.3) can compute a
        consistent mark when the chain is unavailable mid-cycle.
        """
        from backend.bot.options.pricing import price_at_entry
        action = action.upper()
        # Right is derived from the action (BUY_CALL / SELL_PUT / etc.)
        right = "call" if "CALL" in action else (
            "put" if "PUT" in action else "call"
        )
        # Pull a real mid from ThetaData (BS fallback if stale/missing).
        spot = self._price(ticker)
        mark = price_at_entry(
            symbol=ticker.upper(),
            spot=float(spot or strike),
            strike=float(strike),
            expiration=expiration,
            right=right,
        )
        # P1.8 — half-spread on top of the source mid (chain mid already
        # reflects the market spread, but the operator pays slightly worse
        # than mid; BS-fallback mid is theoretical so spread is necessary).
        premium_per_share = _apply_option_spread(mark.mid, action)
        premium_total = premium_per_share * 100 * quantity
        # P1.8 — IBKR-equivalent option commission.
        commission = _option_commission(quantity)

        # MITS Phase 17.A item #4 — premium slippage in bps. Compares the
        # fill we actually book (premium_per_share, post half-spread) to
        # the source mid we started from. Surfaces broker-style execution
        # quality even on paper fills.
        slippage_bps = (
            abs(premium_per_share - mark.mid) / mark.mid * 10_000.0
            if mark.mid > 0 else None
        )

        # MITS Phase 17.A item #1 — capture chain-quote age at entry +
        # tag the entry as "stale chain" when ThetaData was older than
        # 60s or we fell through to the BS fallback. The Greeks stored
        # on the position came from one of these two paths; without the
        # tag, downstream "entry_delta" looks authoritative either way.
        entry_chain_stale = bool(
            (mark.age_seconds is not None and mark.age_seconds > 60)
            or mark.source == "bs_fallback"
        )
        chain_freshness_at_entry_sec = (
            float(mark.age_seconds) if mark.age_seconds is not None else None
        )

        with session_scope() as session:
            account = get_or_create_account(session, self.starting_cash)
            is_long = action.startswith("BUY")
            if is_long:
                cost = premium_total + commission
                if cost > account.cash + 1e-6:
                    return PaperOrderResult(
                        False, None, error=f"insufficient cash for {quantity}x premium ${cost:.2f}"
                    )
                account.cash -= cost
            else:
                # Selling premium: credit cash MINUS commission.
                account.cash += premium_total - commission
            # MITS Phase 17.A item #11 — pricing_source is the trade-row
            # source of truth (lifted into plan via order.raw on the
            # engine side). Position meta keeps the entry_chain_stale
            # flag so the operator can spot fills that touched the BS
            # fallback. We deliberately do NOT carry "pricing_source"
            # in meta any more — the column on PaperPosition + the
            # Trade row column are the canonical readers.
            meta = {
                "strike": strike,
                "expiration": expiration,
                "action": action,
                "premium_per_share": premium_per_share,
                "commission": commission,
                "quote_age_seconds": mark.age_seconds,
                "entry_mid": mark.mid,
                "entry_iv": mark.iv,
                "entry_chain_stale": entry_chain_stale,
            }
            session.add(
                PaperPosition(
                    ticker=ticker.upper(),
                    kind="option",
                    quantity=quantity if is_long else -quantity,
                    avg_cost=premium_per_share * 100 if is_long else -premium_per_share * 100,
                    meta=json.dumps(meta),
                    # P2.2 — store entry greeks + IV for MTM repricing.
                    strike=float(strike),
                    expiration=str(expiration),
                    option_type=right,
                    entry_bid=mark.bid,
                    entry_ask=mark.ask,
                    entry_mid=mark.mid,
                    entry_iv=mark.iv,
                    entry_delta=mark.delta,
                    entry_gamma=mark.gamma,
                    entry_theta=mark.theta,
                    entry_vega=mark.vega,
                    entry_underlying=mark.underlying or float(spot or 0),
                    pricing_source=mark.source,
                    stored_iv=mark.iv,
                    stored_iv_at=datetime.utcnow(),
                    chain_freshness_at_entry_sec=chain_freshness_at_entry_sec,
                )
            )
            logger.info(
                "[paper-local] %s %sx %s %.2f %s cash=%.2f mid=%.2f src=%s",
                action, quantity, ticker, strike, expiration, account.cash,
                mark.mid, mark.source,
            )
            # MITS Phase 17.B — capture the full FillSnapshot. spread_paid
            # is the signed distance between the fill premium and the source
            # mid (positive on BUY, negative on SELL).
            from backend.bot.execution.fill_snapshot import FillSnapshot
            snapshot = FillSnapshot.from_option_mark(
                mark,
                commission=float(commission),
                fill_price=float(premium_per_share),
                slippage_bps=float(slippage_bps) if slippage_bps is not None else 0.0,
                spread_paid=float(premium_per_share - mark.mid),
            )
            return PaperOrderResult(
                success=True,
                order_id=f"paper-opt-{ticker}-{action}-{strike}",
                paper=True,
                raw={
                    "ticker": ticker.upper(), "qty": quantity,
                    "premium": premium_total,
                    # MITS Phase 17.A item #11 — promote pricing_source
                    # to a first-class raw field so the engine can lift
                    # it into ``plan["pricing_source"]`` BEFORE the
                    # Trade row is persisted.
                    "pricing_source": mark.source,
                    # MITS Phase 17.A item #4 — execution-quality fields.
                    "slippage_bps": slippage_bps,
                    # MITS Phase 17.A item #5 — surface total_commission
                    # in a single, predictable place. Single-leg orders
                    # only book one commission; multi-leg path sums them.
                    "total_commission": round(float(commission), 2),
                    # MITS Phase 17.B — structured fill provenance.
                    "fill_snapshot_json": snapshot.to_json(),
                    **meta,
                },
            )

    def place_complex_order(self, signal) -> PaperOrderResult:
        """Book a complex options position with per-leg pricing.

        P1.9 — replaces the prior flat ``max(50, 0.01 × strike × 100)``
        approximation with per-leg pricing. Each leg gets its own premium
        + option commission. Net cash flow = sum(per-leg credit/debit) -
        sum(per-leg commission). Iron condor 4-leg → 4 × $0.65 = $2.60
        commission (with the per-order minimum applied across all legs).
        """
        from backend.bot.execution.fill_snapshot import FillSnapshot
        from backend.bot.options.pricing import OptionMark
        action_str = signal.action.value
        legs = self._extract_legs(signal)
        leg_snapshots: list = []
        if not legs:
            # Fall back to the previous approximation when the strategy
            # didn't provide per-leg metadata (older Signal builders).
            strike = float(signal.strike or signal.metadata.get("buy_strike") or 0.0)
            if strike <= 0:
                return PaperOrderResult(False, None,
                                            error=f"no strike for {action_str}")
            is_credit = action_str.startswith("SELL") or action_str in {
                "IRON_CONDOR", "SELL_CSP", "SELL_COVERED_CALL",
            }
            approx_premium = max(50.0, 0.01 * strike * 100)
            net_credit = approx_premium if is_credit else -approx_premium
            total_contracts = 1
        else:
            # Per-leg pricing path. Each leg is a dict:
            # {kind: call|put, side: BUY|SELL, strike, expiration, contracts}
            net_credit = 0.0
            total_contracts = 0
            for leg in legs:
                leg_strike = float(leg.get("strike") or 0.0)
                leg_contracts = int(leg.get("contracts") or 1)
                leg_side = str(leg.get("side", "BUY")).upper()
                # P1.8 — option half-spread per leg.
                leg_mid = max(0.05, 0.03 * leg_strike)
                leg_fill = _apply_option_spread(leg_mid, leg_side)
                leg_premium = leg_fill * 100 * leg_contracts
                if leg_side == "SELL":
                    net_credit += leg_premium
                else:
                    net_credit -= leg_premium
                total_contracts += leg_contracts
                # MITS Phase 17.B — per-leg fill snapshot. The complex-order
                # path doesn't hit ThetaData per leg today, so we wrap the
                # approximation in a synthetic OptionMark with paper_stub
                # provenance; field shape matches single-leg snapshots so
                # downstream readers don't branch.
                per_leg_commission = (
                    _option_commission(leg_contracts) / max(1, total_contracts)
                )
                leg_slippage_bps = (
                    abs(leg_fill - leg_mid) / leg_mid * 10_000.0
                    if leg_mid > 0 else 0.0
                )
                synthetic_mark = OptionMark(
                    bid=None, ask=None, mid=round(leg_mid, 4),
                    iv=None, delta=None, gamma=None, theta=None, vega=None,
                    source="paper_stub", age_seconds=None,
                    underlying=None,
                )
                leg_snap = FillSnapshot.from_option_mark(
                    synthetic_mark,
                    commission=float(per_leg_commission),
                    fill_price=float(leg_fill),
                    slippage_bps=float(leg_slippage_bps),
                    spread_paid=float(leg_fill - leg_mid),
                )
                leg_dict = leg_snap.to_dict()
                leg_dict["kind"] = leg.get("kind")
                leg_dict["side"] = leg_side
                leg_dict["strike"] = leg_strike
                leg_dict["contracts"] = leg_contracts
                leg_snapshots.append(leg_dict)
        # P1.8/1.9 — commission applies to total contracts.
        commission = _option_commission(total_contracts)
        # net_credit is positive if we receive cash; negative if we pay.
        # cash_flow = net_credit - commission (commission is always a drag).
        cash_flow = net_credit - commission
        cost = -cash_flow  # debit if positive

        with session_scope() as session:
            account = get_or_create_account(session, self.starting_cash)
            if cost > 0 and cost > account.cash + 1e-6:
                return PaperOrderResult(
                    False, None,
                    error=f"insufficient cash for {action_str} (need ${cost:.2f})"
                )
            account.cash -= cost
            session.add(
                PaperPosition(
                    ticker=signal.ticker,
                    kind="complex",
                    quantity=1,
                    avg_cost=cost,
                    meta=json.dumps({
                        "action": action_str,
                        "legs": legs,
                        "net_credit": net_credit,
                        "commission": commission,
                        **signal.metadata,
                    }),
                )
            )
            logger.info(
                "[paper-local-complex] %s %s cash=%.2f", signal.action.value, signal.ticker, account.cash,
            )
            return PaperOrderResult(
                success=True,
                order_id=f"paper-complex-{signal.ticker}-{signal.action.value}",
                paper=True,
                raw={
                    "action": signal.action.value, "approx_premium": cost,
                    # MITS Phase 17.A item #5 — sum of per-leg
                    # commissions. The engine lifts this into
                    # Trade.total_commission so multi-leg fee drag is
                    # finally attributable per row.
                    "total_commission": round(float(commission), 2),
                    "pricing_source": "paper_stub",
                    # MITS Phase 17.B — per-leg snapshots under a single
                    # envelope. Empty list when the legacy approximation
                    # branch fired (no per-leg metadata available).
                    "fill_snapshot_json": json.dumps({"legs": leg_snapshots}),
                    **signal.metadata,
                },
            )

    def _extract_legs(self, signal) -> list:
        """Extract per-leg specs from a complex Signal's metadata.

        Supports:
          * IRON_CONDOR — expects ``call_short_strike``, ``call_long_strike``,
            ``put_short_strike``, ``put_long_strike`` in metadata.
          * BULL_CALL_SPREAD — ``buy_strike``, ``sell_strike``.
          * BEAR_PUT_SPREAD — ``buy_strike``, ``sell_strike``.

        Returns a list of leg dicts:
          {kind: call|put, side: BUY|SELL, strike, expiration, contracts}
        """
        meta = signal.metadata or {}
        action = signal.action.value
        expiry = meta.get("expiration") or meta.get("expiry")
        contracts = int(meta.get("contracts") or 1)
        legs: list = []
        if action == "IRON_CONDOR":
            for fld, kind, side in (
                ("call_short_strike", "call", "SELL"),
                ("call_long_strike",  "call", "BUY"),
                ("put_short_strike",  "put",  "SELL"),
                ("put_long_strike",   "put",  "BUY"),
            ):
                k = meta.get(fld)
                if k:
                    legs.append({"kind": kind, "side": side,
                                     "strike": float(k),
                                     "expiration": expiry,
                                     "contracts": contracts})
        elif action == "BULL_CALL_SPREAD":
            if meta.get("buy_strike") and meta.get("sell_strike"):
                legs = [
                    {"kind": "call", "side": "BUY",
                         "strike": float(meta["buy_strike"]),
                         "expiration": expiry, "contracts": contracts},
                    {"kind": "call", "side": "SELL",
                         "strike": float(meta["sell_strike"]),
                         "expiration": expiry, "contracts": contracts},
                ]
        elif action == "BEAR_PUT_SPREAD":
            if meta.get("buy_strike") and meta.get("sell_strike"):
                legs = [
                    {"kind": "put", "side": "BUY",
                         "strike": float(meta["buy_strike"]),
                         "expiration": expiry, "contracts": contracts},
                    {"kind": "put", "side": "SELL",
                         "strike": float(meta["sell_strike"]),
                         "expiration": expiry, "contracts": contracts},
                ]
        return legs

    def cancel_all_orders(self) -> None:
        # Paper executor fills immediately, so there's nothing to cancel.
        logger.info("[paper-local] cancel_all is a no-op (fills are instant)")
