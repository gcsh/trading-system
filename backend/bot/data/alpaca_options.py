"""Alpaca options chain provider.

Sits in the options provider chain between ThetaData (primary) and
yfinance (free fallback). Alpaca's Options Algo Trader tier provides:

  - OPRA NBBO quotes for liquid US options
  - Greeks + IV per contract
  - Authenticated REST so we don't get rate-limited like yfinance

This wires the same atm-dict shape the other providers return, so
``options_snapshot`` can swap it in transparently.

Requires ``ALPACA_API_KEY`` + ``ALPACA_API_SECRET`` in the environment.
When creds are missing, ``atm_from_alpaca`` silently returns None so the
provider chain skips it without noise.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Brenner-Subrahmanyam straddle constant — same as the ThetaData path.
_BS_STRADDLE_K = math.sqrt(2 * math.pi) / 2


def _client_or_none():
    try:
        from backend.config import SETTINGS
        if not (SETTINGS.alpaca_api_key and SETTINGS.alpaca_api_secret):
            return None
        from alpaca.data.historical.option import OptionHistoricalDataClient
        return OptionHistoricalDataClient(
            api_key=SETTINGS.alpaca_api_key,
            secret_key=SETTINGS.alpaca_api_secret,
        )
    except Exception:
        logger.debug("alpaca options client init failed", exc_info=True)
        return None


def _atm_contract_symbols(ticker: str, spot: float,
                          target_dte: int = 30
                          ) -> Optional[tuple]:
    """Walk Alpaca's active contracts for ``ticker``, pick the expiry
    closest to ``target_dte``, then return (call_symbol, put_symbol,
    strike, expiration) for the contract whose strike is closest to spot.

    Pulling the actual symbols from the contracts list (instead of
    constructing OCC codes ad-hoc) means we always hit live contracts
    and don't have to know the exact strike-tick layout per ticker.
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOptionContractsRequest
        from backend.config import SETTINGS
        tc = TradingClient(
            api_key=SETTINGS.alpaca_api_key,
            secret_key=SETTINGS.alpaca_api_secret,
            paper=True,
        )
        today = date.today()
        from datetime import timedelta
        wanted_ord = today.toordinal() + max(1, int(target_dte))
        # Filter to expirations between tomorrow and +90 days. Alpaca's
        # un-filtered contracts list is sorted by expiration and the first
        # page lands on TODAY (intraday expirations) before reaching the
        # future contracts we actually want. Bounding the window keeps
        # the response small and forces relevant contracts to the front.
        gte = today + timedelta(days=1)
        lte = today + timedelta(days=max(60, int(target_dte) + 30))
        all_contracts = []
        page_token = None
        for _ in range(6):
            req_kwargs = dict(
                underlying_symbols=[ticker.upper()],
                status="active",
                limit=10000,
                expiration_date_gte=gte,
                expiration_date_lte=lte,
            )
            if page_token:
                req_kwargs["page_token"] = page_token
            page = tc.get_option_contracts(GetOptionContractsRequest(**req_kwargs))
            contracts = getattr(page, "option_contracts", None) or []
            all_contracts.extend(contracts)
            page_token = getattr(page, "next_page_token", None)
            if not page_token:
                break
        if not all_contracts:
            return None

        # Pick the expiration closest to target.
        best_exp = None
        best_gap = None
        exp_map: dict = {}
        for c in all_contracts:
            exp_raw = getattr(c, "expiration_date", None)
            if not exp_raw:
                continue
            try:
                exp = (exp_raw if isinstance(exp_raw, date)
                       else datetime.strptime(str(exp_raw), "%Y-%m-%d").date())
            except Exception:
                continue
            if exp <= today:
                continue
            exp_map.setdefault(exp, []).append(c)
            gap = abs(exp.toordinal() - wanted_ord)
            if best_gap is None or gap < best_gap:
                best_exp, best_gap = exp, gap
        if best_exp is None:
            return None

        # From contracts at the chosen expiration, find the call + put
        # whose strike sits closest to spot.
        contracts_at_exp = exp_map[best_exp]
        best_call = None
        best_put = None
        best_call_gap = best_put_gap = None
        for c in contracts_at_exp:
            try:
                strike = float(getattr(c, "strike_price", 0) or 0)
            except Exception:
                continue
            if strike <= 0:
                continue
            gap = abs(strike - spot)
            # c.type is a ContractType enum — extract its string value.
            # Fall back to parsing the OCC symbol (C/P after the 6-digit
            # date) so we don't depend on a specific SDK version.
            ctype = getattr(c, "type", None)
            kind = str(getattr(ctype, "value", ctype) or "").lower()
            if kind not in ("call", "put"):
                sym = str(getattr(c, "symbol", "") or "")
                if len(sym) >= 8:
                    char = sym[-9] if len(sym) >= 9 else ""
                    kind = "call" if char == "C" else "put" if char == "P" else ""
            if kind == "call" and (best_call_gap is None or gap < best_call_gap):
                best_call, best_call_gap = c, gap
            elif kind == "put" and (best_put_gap is None or gap < best_put_gap):
                best_put, best_put_gap = c, gap
        if best_call is None or best_put is None:
            return None
        call_sym = getattr(best_call, "symbol", None)
        put_sym = getattr(best_put, "symbol", None)
        strike = float(getattr(best_call, "strike_price", 0) or 0)
        if not (call_sym and put_sym and strike > 0):
            return None
        return call_sym, put_sym, strike, best_exp
    except Exception:
        logger.debug("alpaca contract lookup failed for %s",
                     ticker, exc_info=True)
        return None


def atm_from_alpaca(ticker: str, spot: float,
                    target_dte: int = 30) -> Optional[dict]:
    """ATM straddle snapshot via Alpaca options data.

    Returns the same shape as ``_atm_from_thetadata`` / ``_atm_from_yfinance``:

        {iv_atm, implied_move, dte, expiry, source, data_confidence,
         sanity_flags}

    Returns None when creds are missing, when no expiration is found, or
    when the bid/ask is degenerate. The caller handles None by falling
    through to the next provider in the chain.
    """
    if spot is None or spot <= 0:
        return None
    client = _client_or_none()
    if client is None:
        return None

    try:
        symbols = _atm_contract_symbols(ticker, spot, target_dte=target_dte)
        if symbols is None:
            return None
        call_sym, put_sym, strike, expiration = symbols

        from alpaca.data.requests import OptionLatestQuoteRequest
        req = OptionLatestQuoteRequest(symbol_or_symbols=[call_sym, put_sym])
        quotes = client.get_option_latest_quote(req)
        call_q = quotes.get(call_sym) if quotes else None
        put_q = quotes.get(put_sym) if quotes else None
        if call_q is None or put_q is None:
            return None

        def _mid(q):
            b = float(getattr(q, "bid_price", 0) or 0)
            a = float(getattr(q, "ask_price", 0) or 0)
            if b > 0 and a > 0:
                return (b + a) / 2
            return b or a or 0.0

        c_mid = _mid(call_q)
        p_mid = _mid(put_q)
        if c_mid <= 0 or p_mid <= 0:
            return None
        straddle = c_mid + p_mid

        dte = max(1, (expiration - date.today()).days)
        T = dte / 365.0
        if T <= 0 or spot <= 0:
            return None
        iv_atm = round(straddle / (_BS_STRADDLE_K * spot * math.sqrt(T)), 4)
        if iv_atm <= 0:
            return None
        implied_move = round(straddle / spot, 4)
        return {
            "iv_atm": iv_atm,
            "implied_move": implied_move,
            "dte": dte,
            "expiry": expiration.isoformat(),
            "source": "alpaca",
            "data_confidence": "medium",
            "sanity_flags": [],
        }
    except Exception as exc:
        logger.debug("alpaca options snapshot failed for %s: %s",
                     ticker, exc, exc_info=True)
        return None
