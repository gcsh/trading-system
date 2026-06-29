"""Asset-class market profiles.

Lets the same engine trade US stocks (market hours, SPY-anchored regime,
equity fees) and crypto (24/7, BTC-anchored regime, crypto fees) by swapping a
per-symbol profile instead of hardcoding equity assumptions. Detection is
config-driven (``TB_CRYPTO_QUOTE_CCYS``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from backend.config import TUNABLES

# yfinance crypto pairs look like BTC-USD / ETH-USDT. Built from config so the
# accepted quote currencies aren't hardcoded.
_CRYPTO_RE = re.compile(
    r"^[A-Z0-9]{2,6}-(" + "|".join(re.escape(c) for c in TUNABLES.crypto_quote_currencies) + r")$"
)


def is_crypto(symbol: str) -> bool:
    return bool(_CRYPTO_RE.match((symbol or "").upper()))


@dataclass(frozen=True)
class MarketProfile:
    asset_class: str       # "crypto" | "equity"
    trades_247: bool       # True → no market-hours / weekend gating
    regime_anchor: str     # symbol used to read the "market" regime
    fee_bps: float         # round-trip-ish fee assumption


def profile(symbol: str) -> MarketProfile:
    if is_crypto(symbol):
        return MarketProfile("crypto", True, TUNABLES.crypto_regime_anchor, TUNABLES.crypto_fee_bps)
    return MarketProfile("equity", False, "SPY", TUNABLES.backtest_commission_bps)


def regime_anchor(symbol: str) -> str:
    return profile(symbol).regime_anchor
