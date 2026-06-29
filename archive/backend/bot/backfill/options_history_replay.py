"""Options-strategy historical replay (P2.1-FU).

The stock-replay pipeline in :mod:`historical_replay` grades signals
against the underlying's forward bar close. That's correct for
BUY_STOCK / BUY_CALL / BUY_PUT (single-leg directional bets), but it
doesn't capture the strategy-specific P&L of:

  * **cash_secured_put** — premium received now, intrinsic loss at exit
    if underlying < strike
  * **covered_call_wheel** — premium received now, capped upside on
    underlying gain past strike
  * **bull_call_spread** — long lower call + short higher call (debit)
  * **iron_condor** — short put spread + short call spread (credit)

This module pulls **historical EOD option prices** from ThetaData
(``/v3/option/history/eod``) so each synthetic trade's P&L reflects
what the option position actually would have made between entry and
exit. Falls back to an intrinsic-value approximation when ThetaData
has a gap (delisted strike, missing date) so the pipeline never
crashes a backfill mid-run.

Like the stock replay, synthetic rows are tagged with
``signal_source="historical_replay"`` so live P&L surfaces exclude them
while calibration / cohort / journal include them.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, select

logger = logging.getLogger(__name__)


HISTORICAL_REPLAY_SOURCE = "historical_replay"

# Strategy-level defaults (mirrors all_strategies.py choices).
SUPPORTED_STRATEGIES = {
    "cash_secured_put":  {"target_dte": 30, "right": "P", "moneyness": -0.05,
                             "kind": "short_put"},
    "covered_call_wheel": {"target_dte": 30, "right": "C", "moneyness": 0.03,
                             "kind": "short_call"},
    "bull_call_spread":  {"target_dte": 30, "buy_money": 0.0, "sell_money": 0.05,
                             "kind": "debit_call_spread"},
    "iron_condor":       {"target_dte": 30,
                             "call_short_m": 0.04, "call_long_m": 0.06,
                             "put_short_m": -0.04, "put_long_m": -0.06,
                             "kind": "iron_condor"},
}


DEFAULT_PERIOD_YEARS = 2
DEFAULT_FORWARD_DAYS = 21       # ~3 trading weeks — holds to ~10 DTE
DEFAULT_NOTIONAL = 1000.0
DEFAULT_PACE_SEC = 0.02         # politeness between ThetaData calls


@dataclass
class OptionsReplayStats:
    ticker: str
    strategy: str
    period_years: int
    dates_scanned: int = 0
    trades_written: int = 0
    skipped_no_expiry: int = 0
    skipped_no_strike: int = 0
    skipped_no_eod: int = 0
    skipped_existing: int = 0
    intrinsic_fallback: int = 0
    win_rate: Optional[float] = None
    avg_pnl: Optional[float] = None
    errors: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── ThetaData helpers ───────────────────────────────────────────────────


def _historical_eod_close(client, *, symbol: str, expiration: date,
                              strike: float, right: str,
                              target_date: date) -> Optional[float]:
    """Fetch the EOD close for one option contract on ``target_date``.
    Returns the mid of bid/ask if both present, or close, or None."""
    payload = client._get_json(  # noqa: SLF001 — internal access acceptable
        "/v3/option/history/eod",
        {
            "symbol": symbol.upper(),
            "expiration": expiration.isoformat(),
            "strike": f"{float(strike):.3f}",
            "right": "C" if right.upper().startswith("C") else "P",
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
            "format": "json",
        },
    )
    if not payload:
        return None
    rows = payload.get("response") or []
    if not rows:
        return None
    first = rows[0]
    data_arr = first.get("data") or []
    if not data_arr:
        return None
    row = data_arr[0]
    # ThetaData EOD bars include open/high/low/close + (optional) bid/ask.
    bid = row.get("bid")
    ask = row.get("ask")
    if bid is not None and ask is not None:
        try:
            b, a = float(bid), float(ask)
            if b > 0 and a > 0:
                return (b + a) / 2.0
        except (TypeError, ValueError):
            pass
    close = row.get("close")
    if close is not None:
        try:
            c = float(close)
            if c > 0:
                return c
        except (TypeError, ValueError):
            pass
    return None


def _intrinsic_value(*, kind: str, strike: float, spot: float) -> float:
    """Fallback option valuation = intrinsic value only. No time value
    estimate. Used when ThetaData has a gap."""
    if spot <= 0 or strike <= 0:
        return 0.0
    if kind.lower().startswith("c"):
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


# ── per-strategy outcome math ───────────────────────────────────────────


def _csp_pnl(*, entry_premium: float, exit_premium: Optional[float],
                 exit_spot: float, strike: float,
                 contracts: int) -> Tuple[float, str]:
    """SELL_CSP P&L. Returns (pnl, source_tag).

    Logic:
      - Entry: receive premium (credit). collateral = strike × 100 × contracts
        but we measure P&L on the premium pair only.
      - Exit before expiry: buy back at exit_premium → PnL = (entry - exit) × 100 × contracts.
      - Exit at expiry (no ThetaData → intrinsic fallback): PnL = (entry_premium
        - intrinsic_at_exit) × 100 × contracts.
    """
    if exit_premium is not None:
        return ((entry_premium - exit_premium) * 100 * contracts, "thetadata_eod")
    # Fallback: intrinsic at exit
    intrinsic = max(0.0, strike - exit_spot)
    return ((entry_premium - intrinsic) * 100 * contracts, "intrinsic_approx")


def _cc_pnl(*, entry_premium: float, exit_premium: Optional[float],
                exit_spot: float, strike: float,
                contracts: int) -> Tuple[float, str]:
    """SELL_COVERED_CALL P&L on the call leg only (the long-stock side
    is owner-equivalent and not part of the strategy outcome for
    calibration purposes)."""
    if exit_premium is not None:
        return ((entry_premium - exit_premium) * 100 * contracts, "thetadata_eod")
    intrinsic = max(0.0, exit_spot - strike)
    return ((entry_premium - intrinsic) * 100 * contracts, "intrinsic_approx")


def _bull_call_spread_pnl(*, entry_buy: float, entry_sell: float,
                                 exit_buy: Optional[float],
                                 exit_sell: Optional[float],
                                 exit_spot: float,
                                 buy_strike: float, sell_strike: float,
                                 contracts: int) -> Tuple[float, str]:
    """Long lower call + short upper call (debit spread)."""
    debit = entry_buy - entry_sell
    if exit_buy is not None and exit_sell is not None:
        credit = exit_buy - exit_sell
        return ((credit - debit) * 100 * contracts, "thetadata_eod")
    intrinsic_long = max(0.0, exit_spot - buy_strike)
    intrinsic_short = max(0.0, exit_spot - sell_strike)
    credit = intrinsic_long - intrinsic_short
    return ((credit - debit) * 100 * contracts, "intrinsic_approx")


def _iron_condor_pnl(*,
                          entries: Dict[str, float], exits: Dict[str, Optional[float]],
                          strikes: Dict[str, float], exit_spot: float,
                          contracts: int) -> Tuple[float, str]:
    """Iron condor: short call + long call (above spot) + short put + long put
    (below spot). Entry is credit (sum of shorts minus longs).
    """
    credit_in = (entries["call_short"] - entries["call_long"]
                    + entries["put_short"] - entries["put_long"])
    if all(exits.get(k) is not None for k in ("call_short", "call_long",
                                                       "put_short", "put_long")):
        cost_to_close = (exits["call_short"] - exits["call_long"]
                              + exits["put_short"] - exits["put_long"])
        return ((credit_in - cost_to_close) * 100 * contracts, "thetadata_eod")
    cost_call = (max(0.0, exit_spot - strikes["call_short"])
                    - max(0.0, exit_spot - strikes["call_long"]))
    cost_put = (max(0.0, strikes["put_short"] - exit_spot)
                   - max(0.0, strikes["put_long"] - exit_spot))
    cost_to_close = cost_call + cost_put
    return ((credit_in - cost_to_close) * 100 * contracts, "intrinsic_approx")


# ── orchestrator ────────────────────────────────────────────────────────


def replay_options_strategy(
    *,
    ticker: str,
    strategy_name: str,
    period_years: int = DEFAULT_PERIOD_YEARS,
    forward_days: int = DEFAULT_FORWARD_DAYS,
    notional: float = DEFAULT_NOTIONAL,
    pace_sec: float = DEFAULT_PACE_SEC,
    max_dates: Optional[int] = None,
    overwrite: bool = False,
    monthly_only: bool = True,
) -> OptionsReplayStats:
    """Replay one (ticker, options-strategy) pair via historical EOD chains.

    Steps per sampled entry date D:
      1. Get underlying spot at D (yfinance close).
      2. Pick expiration nearest D + target_dte (from ThetaData full list).
      3. Pick strike(s) per strategy spec.
      4. EOD price on D (entry) + EOD price on D + forward_days (exit).
      5. Compute strategy-specific P&L.
      6. Write Trade + DecisionLog.

    To keep API volume sane we sample one entry per month by default
    (``monthly_only=True``). That gives ~24 entries over 2 years per
    strategy — meaningful sample, manageable cost.
    """
    from backend.bot.data.thetadata import get_client
    from backend.bot.data.iv_history import _historical_closes
    from backend.db import session_scope
    from backend.models.trade import Trade
    from backend.models.decision_log import DecisionLog

    spec = SUPPORTED_STRATEGIES.get(strategy_name)
    if spec is None:
        return OptionsReplayStats(
            ticker=ticker.upper(), strategy=strategy_name,
            period_years=period_years, errors=1,
        )

    stats = OptionsReplayStats(
        ticker=ticker.upper(), strategy=strategy_name,
        period_years=period_years,
    )
    client = get_client()

    end_date = date.today() - timedelta(days=1)  # avoid today's incomplete bar
    start_date = end_date - timedelta(days=365 * period_years)
    closes = _historical_closes(ticker, start_date, end_date)
    if not closes:
        stats.errors = 1
        return stats

    sorted_dates = sorted(closes.keys())
    # Sample one entry per calendar month for breadth.
    if monthly_only:
        seen_months: set = set()
        picked: List[date] = []
        for d in sorted_dates:
            key = (d.year, d.month)
            if key in seen_months:
                continue
            seen_months.add(key)
            picked.append(d)
        sorted_dates = picked
    if max_dates:
        sorted_dates = sorted_dates[:max_dates]
    stats.dates_scanned = len(sorted_dates)

    all_expirations = client.list_expirations(ticker)
    target_dte = int(spec["target_dte"])

    with session_scope() as session:
        if overwrite:
            doomed_ids = session.execute(
                select(Trade.id)
                .where(Trade.ticker == ticker.upper())
                .where(Trade.strategy == strategy_name)
                .where(Trade.signal_source == HISTORICAL_REPLAY_SOURCE)
            ).scalars().all()
            if doomed_ids:
                session.execute(
                    delete(DecisionLog).where(DecisionLog.trade_id.in_(doomed_ids))
                )
                session.execute(
                    delete(Trade).where(Trade.id.in_(doomed_ids))
                )
        else:
            # Skip if any synthetic rows already exist for this pair.
            from sqlalchemy import func
            existing = session.execute(
                select(func.count(Trade.id))
                .where(Trade.ticker == ticker.upper())
                .where(Trade.strategy == strategy_name)
                .where(Trade.signal_source == HISTORICAL_REPLAY_SOURCE)
            ).scalar() or 0
            if existing:
                stats.skipped_existing = existing
                stats.trades_written = existing
                return stats

        wins = 0
        sum_pnl = 0.0

        for entry_date in sorted_dates:
            spot = closes.get(entry_date)
            if not spot or spot <= 0:
                continue
            # Pick expiration nearest target DTE from entry.
            candidates = [
                e for e in all_expirations
                if (e - entry_date).days >= 7    # min DTE
            ]
            if not candidates:
                stats.skipped_no_expiry += 1
                continue
            expiry = min(candidates, key=lambda e: abs((e - entry_date).days - target_dte))

            list_strikes = client.list_strikes(ticker, expiry)
            if not list_strikes:
                stats.skipped_no_strike += 1
                continue

            exit_date = entry_date + timedelta(days=forward_days)
            if exit_date > expiry:
                exit_date = expiry  # don't sample past expiry

            # Compute exit spot for fallback intrinsic.
            # Find the actual close on exit_date (or nearest later trading day).
            exit_spot = closes.get(exit_date)
            if exit_spot is None:
                later = [d for d in sorted_dates if d >= exit_date]
                exit_spot = closes.get(later[0]) if later else spot
                if exit_spot is None:
                    exit_spot = spot

            # Strategy-specific strike picks + outcome.
            try:
                pnl, src = _replay_one(
                    client=client, spec=spec,
                    ticker=ticker.upper(), expiry=expiry,
                    spot=spot, exit_spot=exit_spot,
                    entry_date=entry_date, exit_date=exit_date,
                    list_strikes=list_strikes, notional=notional,
                )
            except Exception:
                logger.debug("replay_one failed for %s/%s @ %s",
                                   ticker, strategy_name, entry_date, exc_info=True)
                stats.errors += 1
                if pace_sec:
                    time.sleep(pace_sec)
                continue
            if pnl is None:
                stats.skipped_no_eod += 1
                if pace_sec:
                    time.sleep(pace_sec)
                continue
            if src == "intrinsic_approx":
                stats.intrinsic_fallback += 1

            # Confidence model: pretend the live strategy fired with its
            # own default confidence (no live data available historically;
            # use a strategy-typical baseline so calibration metrics have
            # signal).
            baseline_conf = 0.65

            # Volatility/regime tags: cheap defaults.
            rt = "unknown"
            rv = "normal"

            trade = Trade(
                timestamp=datetime.combine(entry_date, datetime.min.time()),
                ticker=ticker.upper(),
                action=spec["kind"].upper(),
                quantity=1.0,
                price=round(spot, 4),
                strategy=strategy_name,
                signal_source=HISTORICAL_REPLAY_SOURCE,
                confidence=baseline_conf,
                reason=f"[hist_replay {src}] {strategy_name} expiry={expiry.isoformat()}",
                paper=1,
                pnl=round(pnl, 2),
                status="closed",
                instrument="option",
                strike=None,
                expiration=expiry.isoformat(),
                contracts=1,
                # P1.5 — synthetic options use ThetaData EOD prices; the
                # underlying close came from ThetaData stock EOD. Tag
                # accordingly. Accounting model is still v1 because the
                # PnL math (intrinsic + close diff) is the v1 stub.
                pricing_source=("thetadata_eod" if str(src).startswith("thetadata")
                                    else "paper_stub"),
                accounting_version=1,
            )
            session.add(trade)
            session.flush()

            decision = DecisionLog(
                timestamp=trade.timestamp,
                ticker=ticker.upper(),
                action=spec["kind"].upper(),
                strategy=strategy_name,
                confidence=baseline_conf,
                status=f"{HISTORICAL_REPLAY_SOURCE}_closed",
                regime_trend=rt,
                regime_volatility=rv,
                regime_gamma="unknown",
                grade="",
                win_probability=baseline_conf,
                trade_id=trade.id,
                outcome_pnl=round(pnl, 2),
                outcome_status="closed",
                signal_source=HISTORICAL_REPLAY_SOURCE,
            )
            session.add(decision)
            stats.trades_written += 1
            if pnl > 0:
                wins += 1
            sum_pnl += pnl

            if pace_sec:
                time.sleep(pace_sec)

        session.commit()
        if stats.trades_written:
            stats.win_rate = round(wins / stats.trades_written, 4)
            stats.avg_pnl = round(sum_pnl / stats.trades_written, 2)
    return stats


def _replay_one(*, client, spec: Dict[str, Any], ticker: str, expiry: date,
                    spot: float, exit_spot: float,
                    entry_date: date, exit_date: date,
                    list_strikes: List[float],
                    notional: float) -> Tuple[Optional[float], str]:
    """Dispatch to the per-strategy pricing function. Returns
    (pnl_or_None, source_tag)."""
    kind = spec["kind"]
    if kind == "short_put":
        strike = _closest(list_strikes, spot * (1 + spec["moneyness"]))
        if strike is None:
            return None, "no_strike"
        entry_p = _historical_eod_close(
            client, symbol=ticker, expiration=expiry,
            strike=strike, right="P", target_date=entry_date,
        )
        if entry_p is None:
            return None, "no_entry"
        exit_p = _historical_eod_close(
            client, symbol=ticker, expiration=expiry,
            strike=strike, right="P", target_date=exit_date,
        )
        pnl, src = _csp_pnl(
            entry_premium=entry_p, exit_premium=exit_p,
            exit_spot=exit_spot, strike=strike, contracts=1,
        )
        return pnl, src
    if kind == "short_call":
        strike = _closest(list_strikes, spot * (1 + spec["moneyness"]))
        if strike is None:
            return None, "no_strike"
        entry_p = _historical_eod_close(
            client, symbol=ticker, expiration=expiry,
            strike=strike, right="C", target_date=entry_date,
        )
        if entry_p is None:
            return None, "no_entry"
        exit_p = _historical_eod_close(
            client, symbol=ticker, expiration=expiry,
            strike=strike, right="C", target_date=exit_date,
        )
        pnl, src = _cc_pnl(
            entry_premium=entry_p, exit_premium=exit_p,
            exit_spot=exit_spot, strike=strike, contracts=1,
        )
        return pnl, src
    if kind == "debit_call_spread":
        buy_strike = _closest(list_strikes, spot * (1 + spec["buy_money"]))
        sell_strike = _closest(list_strikes, spot * (1 + spec["sell_money"]))
        if buy_strike is None or sell_strike is None:
            return None, "no_strike"
        entry_buy = _historical_eod_close(
            client, symbol=ticker, expiration=expiry,
            strike=buy_strike, right="C", target_date=entry_date,
        )
        entry_sell = _historical_eod_close(
            client, symbol=ticker, expiration=expiry,
            strike=sell_strike, right="C", target_date=entry_date,
        )
        if entry_buy is None or entry_sell is None:
            return None, "no_entry"
        exit_buy = _historical_eod_close(
            client, symbol=ticker, expiration=expiry,
            strike=buy_strike, right="C", target_date=exit_date,
        )
        exit_sell = _historical_eod_close(
            client, symbol=ticker, expiration=expiry,
            strike=sell_strike, right="C", target_date=exit_date,
        )
        pnl, src = _bull_call_spread_pnl(
            entry_buy=entry_buy, entry_sell=entry_sell,
            exit_buy=exit_buy, exit_sell=exit_sell,
            exit_spot=exit_spot,
            buy_strike=buy_strike, sell_strike=sell_strike, contracts=1,
        )
        return pnl, src
    if kind == "iron_condor":
        cs = _closest(list_strikes, spot * (1 + spec["call_short_m"]))
        cl = _closest(list_strikes, spot * (1 + spec["call_long_m"]))
        ps = _closest(list_strikes, spot * (1 + spec["put_short_m"]))
        pl = _closest(list_strikes, spot * (1 + spec["put_long_m"]))
        if any(v is None for v in (cs, cl, ps, pl)):
            return None, "no_strike"
        entries = {}
        exits: Dict[str, Optional[float]] = {}
        for leg, strike, right in (
            ("call_short", cs, "C"), ("call_long", cl, "C"),
            ("put_short", ps, "P"), ("put_long", pl, "P"),
        ):
            e = _historical_eod_close(
                client, symbol=ticker, expiration=expiry,
                strike=strike, right=right, target_date=entry_date,
            )
            if e is None:
                return None, "no_entry"
            entries[leg] = e
            exits[leg] = _historical_eod_close(
                client, symbol=ticker, expiration=expiry,
                strike=strike, right=right, target_date=exit_date,
            )
        pnl, src = _iron_condor_pnl(
            entries=entries, exits=exits,
            strikes={"call_short": cs, "call_long": cl,
                          "put_short": ps, "put_long": pl},
            exit_spot=exit_spot, contracts=1,
        )
        return pnl, src
    return None, "unsupported_kind"


def _closest(items: Sequence[float], target: float) -> Optional[float]:
    if not items or target <= 0:
        return None
    return min(items, key=lambda x: abs(x - target))


def replay_options_universe(
    *,
    tickers: Sequence[str],
    strategies: Sequence[str],
    **kwargs,
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for t in tickers:
        out[t.upper()] = {}
        for s in strategies:
            try:
                stats = replay_options_strategy(
                    ticker=t, strategy_name=s, **kwargs,
                )
            except Exception:
                logger.warning("options replay %s/%s failed", t, s, exc_info=True)
                stats = OptionsReplayStats(
                    ticker=t.upper(), strategy=s,
                    period_years=kwargs.get("period_years",
                                                 DEFAULT_PERIOD_YEARS),
                    errors=1,
                )
            out[t.upper()][s] = stats.to_dict()
    return out


def _load_env_file(path: str = "/opt/trading-bot/.env") -> None:
    """SSM/ops invocations don't inherit systemd's EnvironmentFile, so
    Alpaca/ThetaData/Anthropic creds are silently empty when running
    under bare ``sudo -u tradingbot``. Parse the .env file ourselves —
    handles unquoted values with parens (e.g. SEC User-Agent) that
    bash sourcing chokes on."""
    import os, pathlib
    p = pathlib.Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k and not os.environ.get(k):
            os.environ[k] = v


if __name__ == "__main__":  # pragma: no cover — manual ops helper
    _load_env_file()
    import argparse, json
    parser = argparse.ArgumentParser(
        description="Options-strategy historical replay via ThetaData EOD")
    parser.add_argument("--ticker", action="append", required=True)
    parser.add_argument("--strategy", action="append", required=True)
    parser.add_argument("--years", type=int, default=DEFAULT_PERIOD_YEARS)
    parser.add_argument("--forward-days", type=int, default=DEFAULT_FORWARD_DAYS)
    parser.add_argument("--max-dates", type=int, default=None)
    parser.add_argument("--all-dates", action="store_true",
                            help="Sample every trading day instead of monthly.")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    result = replay_options_universe(
        tickers=args.ticker,
        strategies=args.strategy,
        period_years=args.years,
        forward_days=args.forward_days,
        max_dates=args.max_dates,
        monthly_only=not args.all_dates,
        overwrite=args.overwrite,
    )
    print(json.dumps(result, indent=2, default=str))
