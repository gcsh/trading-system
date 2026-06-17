"""Full-market symbol search backed by the SEC company-tickers list.

The SEC publishes every registered company's ticker + name at a free, no-key
endpoint. We fetch it once, cache in memory, and search locally. This makes
search cover ~10,000 US-listed companies instead of a tiny hardcoded list.
Common ETFs are merged in (the SEC list is light on funds).
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, List

logger = logging.getLogger(__name__)

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
# SEC requires a descriptive User-Agent.
_HEADERS = {"User-Agent": "trading-bot-dev contact@example.com"}

# Popular ETFs / indices not always in the SEC company list.
_ETFS: List[tuple[str, str]] = [
    ("SPY", "SPDR S&P 500 ETF"), ("QQQ", "Invesco QQQ Trust"),
    ("IWM", "iShares Russell 2000"), ("DIA", "SPDR Dow Jones"),
    ("VOO", "Vanguard S&P 500"), ("VTI", "Vanguard Total Market"),
    ("XLK", "Tech Sector SPDR"), ("XLF", "Financials Sector SPDR"),
    ("XLE", "Energy Sector SPDR"), ("XLV", "Healthcare Sector SPDR"),
    ("XLY", "Consumer Disc. SPDR"), ("XLP", "Consumer Staples SPDR"),
    ("XLI", "Industrials SPDR"), ("XLU", "Utilities SPDR"),
    ("ARKK", "ARK Innovation ETF"), ("GLD", "SPDR Gold"),
    ("SLV", "iShares Silver"), ("TLT", "20+ Year Treasury"),
    ("VIX", "CBOE Volatility Index"), ("SOXL", "Semiconductor Bull 3x"),
    ("TQQQ", "ProShares UltraPro QQQ"), ("SQQQ", "ProShares UltraPro Short QQQ"),
]

# Crypto pairs (yfinance convention) so the search can surface them — the SEC
# list is equities-only.
_CRYPTO: List[tuple[str, str]] = [
    ("BTC-USD", "Bitcoin"), ("ETH-USD", "Ethereum"), ("SOL-USD", "Solana"),
    ("XRP-USD", "XRP"), ("DOGE-USD", "Dogecoin"), ("ADA-USD", "Cardano"),
    ("AVAX-USD", "Avalanche"), ("LINK-USD", "Chainlink"), ("MATIC-USD", "Polygon"),
    ("LTC-USD", "Litecoin"), ("BCH-USD", "Bitcoin Cash"), ("DOT-USD", "Polkadot"),
]

_lock = threading.Lock()
_cache: List[Dict[str, str]] | None = None


def _load() -> List[Dict[str, str]]:
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is not None:
            return _cache
        rows: List[Dict[str, str]] = [
            {"symbol": s, "description": d, "type": "ETF"} for s, d in _ETFS
        ] + [
            {"symbol": s, "description": d, "type": "Crypto"} for s, d in _CRYPTO
        ]
        try:
            import httpx

            resp = httpx.get(SEC_URL, headers=_HEADERS, timeout=10.0)
            resp.raise_for_status()
            payload = resp.json()
            for entry in payload.values():
                ticker = (entry.get("ticker") or "").upper()
                title = entry.get("title") or ""
                if ticker:
                    rows.append({"symbol": ticker, "description": title.title(), "type": "Stock"})
            logger.info("loaded %d symbols from SEC", len(rows))
        except Exception:
            logger.warning("SEC symbol list unavailable; using ETF list only", exc_info=True)
        _cache = rows
        return _cache


def search(query: str, limit: int = 20) -> List[Dict[str, str]]:
    """Search symbols by ticker prefix or name substring."""
    q = (query or "").strip().upper()
    if not q:
        return []
    rows = _load()
    starts: List[Dict[str, str]] = []
    contains: List[Dict[str, str]] = []
    for r in rows:
        sym = r["symbol"]
        if sym == q:
            starts.insert(0, r)
        elif sym.startswith(q):
            starts.append(r)
        elif q in sym or q in r["description"].upper():
            contains.append(r)
    # De-dupe by symbol, preserving order (exact/prefix matches first).
    seen = set()
    out: List[Dict[str, str]] = []
    for r in starts + contains:
        if r["symbol"] in seen:
            continue
        seen.add(r["symbol"])
        out.append(r)
        if len(out) >= limit:
            break
    return out
