"""Portfolio Intelligence Layer.

Look across all open positions and ask the things a risk-aware trader would ask:
*are we too concentrated in one sector?*, *do these names rise and fall together?*,
*what's our net beta?*, *what would a 5% market move do to this book?* The output
is a single ``PortfolioRisk`` snapshot that the engine attaches to events, the
ranker can use as a modifier, and the UI can render as warnings.

Pure + deterministic — takes a list of position dicts (the shape
``PaperExecutor.positions()`` already returns) and computes everything from
small static sector/theme/beta tables. The tables are tiny on purpose; we lean
on a sensible default for unknown tickers instead of pretending to cover the
whole market. Move them to a data file once they grow.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# Tickers → sector. Default "Other" when missing.
_SECTOR: Dict[str, str] = {
    # Indices / ETFs
    "SPY": "Index", "QQQ": "Index", "IWM": "Index", "DIA": "Index", "VTI": "Index",
    # Mega-cap tech / semis / cloud
    "AAPL": "Tech", "MSFT": "Tech", "GOOG": "Tech", "GOOGL": "Tech", "META": "Tech",
    "AMZN": "Tech", "NFLX": "Tech", "ORCL": "Tech", "ADBE": "Tech", "CRM": "Tech",
    "NVDA": "Semis", "AMD": "Semis", "AVGO": "Semis", "MU": "Semis", "SMCI": "Semis",
    "INTC": "Semis", "TSM": "Semis", "QCOM": "Semis", "MRVL": "Semis", "ARM": "Semis",
    # EV / Auto / Industrials
    "TSLA": "Auto", "F": "Auto", "GM": "Auto", "RIVN": "Auto", "LCID": "Auto",
    # Financials
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials", "GS": "Financials",
    "MS": "Financials", "C": "Financials",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy",
    # Healthcare
    "JNJ": "Healthcare", "PFE": "Healthcare", "UNH": "Healthcare", "LLY": "Healthcare",
    # Crypto-linked
    "COIN": "Crypto", "MSTR": "Crypto", "MARA": "Crypto", "RIOT": "Crypto",
    "BTC-USD": "Crypto", "ETH-USD": "Crypto",
    # Consumer
    "WMT": "Consumer", "TGT": "Consumer", "HD": "Consumer", "COST": "Consumer",
}

# Thematic baskets — tickers that historically move together on the same story.
_THEMES: Dict[str, set] = {
    "AI infrastructure": {"NVDA", "AMD", "AVGO", "SMCI", "MU", "TSM", "MRVL", "ARM"},
    "Mag7":              {"AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA"},
    "Semis":             {"NVDA", "AMD", "AVGO", "MU", "TSM", "SMCI", "MRVL", "INTC", "QCOM", "ARM"},
    "Crypto-proxy":      {"COIN", "MSTR", "MARA", "RIOT", "BTC-USD", "ETH-USD"},
    "EV / Auto":         {"TSLA", "F", "GM", "RIVN", "LCID"},
    "Banks":             {"JPM", "BAC", "WFC", "GS", "C", "MS"},
    "Energy":            {"XOM", "CVX", "COP", "SLB"},
    "Cloud / software":  {"MSFT", "ORCL", "CRM", "ADBE", "NOW", "SNOW"},
}

# Rough beta to SPY. 1.0 = market; default for unknown is 1.0.
_BETA: Dict[str, float] = {
    "SPY": 1.0, "QQQ": 1.15, "IWM": 1.2, "DIA": 0.95, "VTI": 1.0,
    "AAPL": 1.2, "MSFT": 1.1, "GOOG": 1.05, "GOOGL": 1.05, "META": 1.3,
    "AMZN": 1.25, "NFLX": 1.4, "ORCL": 0.95, "ADBE": 1.1, "CRM": 1.2,
    "NVDA": 1.7, "AMD": 1.9, "AVGO": 1.3, "MU": 1.5, "SMCI": 2.0,
    "INTC": 1.1, "TSM": 1.2, "QCOM": 1.2, "MRVL": 1.5, "ARM": 1.7,
    "TSLA": 1.8, "F": 1.05, "GM": 1.0, "RIVN": 2.2, "LCID": 2.3,
    "JPM": 1.1, "BAC": 1.15, "WFC": 1.0, "GS": 1.1, "MS": 1.2, "C": 1.2,
    "XOM": 0.9, "CVX": 0.85, "COP": 1.1, "SLB": 1.3,
    "JNJ": 0.7, "PFE": 0.7, "UNH": 0.8, "LLY": 0.85,
    "MSTR": 2.5, "COIN": 2.3, "MARA": 2.7, "RIOT": 2.6,
    "BTC-USD": 1.6, "ETH-USD": 1.8,
    "WMT": 0.7, "TGT": 0.95, "HD": 1.0, "COST": 0.85,
}


def sector_of(ticker: str) -> str:
    return _SECTOR.get((ticker or "").upper(), "Other")


def beta_of(ticker: str) -> float:
    return float(_BETA.get((ticker or "").upper(), 1.0))


def themes_for(ticker: str) -> List[str]:
    tk = (ticker or "").upper()
    return [name for name, bucket in _THEMES.items() if tk in bucket]


@dataclass
class PortfolioRisk:
    total_market_value: float = 0.0
    positions_count: int = 0
    by_sector: Dict[str, Dict[str, float]] = field(default_factory=dict)   # sector → {value, pct}
    by_theme: Dict[str, Dict[str, Any]] = field(default_factory=dict)       # theme → {value, pct, tickers}
    by_kind: Dict[str, float] = field(default_factory=dict)                # stock/option/spread → pct
    top_sector: Optional[str] = None
    top_sector_pct: float = 0.0
    top_theme: Optional[str] = None
    top_theme_pct: float = 0.0
    biggest_position: Optional[Dict[str, Any]] = None
    correlation_clusters: List[Dict[str, Any]] = field(default_factory=list)
    net_beta: float = 0.0
    net_delta: float = 0.0
    diversification: float = 1.0          # 1 - HHI, 1=perfectly diversified, 0=one name
    concentration_flags: List[str] = field(default_factory=list)
    macro_risk: str = "LOW"               # HIGH | MODERATE | LOW

    def to_dict(self) -> dict:
        return asdict(self)


def _position_value(p: Dict[str, Any]) -> float:
    """Best available dollar value for a position. Stocks have market_value;
    options fall back to the cost basis (qty * avg_cost) since we don't mark
    them to market today."""
    mv = p.get("market_value")
    if isinstance(mv, (int, float)) and mv > 0:
        return float(mv)
    qty = float(p.get("quantity") or 0)
    px = float(p.get("current_price") or 0)
    if qty > 0 and px > 0:
        return qty * px
    return qty * float(p.get("avg_cost") or 0)


def _net_delta(p: Dict[str, Any], value: float) -> float:
    """Crude net-delta estimate: long stock contributes +value; long call/put
    contributes roughly ±0.5 × value (no per-position greeks yet)."""
    kind = (p.get("kind") or "stock").lower()
    if kind == "stock":
        return value
    opt = (p.get("option_type") or "").lower()
    if opt == "call":
        return 0.5 * value
    if opt == "put":
        return -0.5 * value
    return 0.0


def assess_portfolio(positions: List[Dict[str, Any]]) -> PortfolioRisk:
    """Compute the portfolio-wide risk snapshot. Never raises; empty positions
    → all-zero baseline."""
    if not positions:
        return PortfolioRisk()

    risk = PortfolioRisk(positions_count=len(positions))
    values: List[float] = []
    sec_value: Dict[str, float] = {}
    theme_value: Dict[str, Dict[str, Any]] = {}
    kind_value: Dict[str, float] = {}
    weighted_beta = 0.0
    net_delta = 0.0
    biggest = None
    cluster_index: Dict[str, List[str]] = {}   # sector → tickers
    theme_index: Dict[str, List[str]] = {}     # theme  → tickers

    for p in positions:
        ticker = (p.get("ticker") or "").upper()
        if not ticker:
            continue
        value = _position_value(p)
        if value <= 0:
            continue
        values.append(value)

        sector = sector_of(ticker)
        sec_value[sector] = sec_value.get(sector, 0.0) + value
        cluster_index.setdefault(sector, []).append(ticker)

        for theme in themes_for(ticker):
            slot = theme_value.setdefault(theme, {"value": 0.0, "tickers": []})
            slot["value"] += value
            if ticker not in slot["tickers"]:
                slot["tickers"].append(ticker)
            theme_index.setdefault(theme, []).append(ticker)

        kind = (p.get("kind") or "stock").lower()
        kind_value[kind] = kind_value.get(kind, 0.0) + value

        weighted_beta += value * beta_of(ticker)
        net_delta += _net_delta(p, value)

        if biggest is None or value > biggest["value"]:
            biggest = {"ticker": ticker, "value": round(value, 2)}

    total = sum(values)
    if total <= 0:
        return risk

    risk.total_market_value = round(total, 2)
    risk.by_sector = {s: {"value": round(v, 2), "pct": round(v / total, 4)} for s, v in sec_value.items()}
    risk.by_theme = {
        t: {"value": round(d["value"], 2), "pct": round(d["value"] / total, 4),
            "tickers": sorted(set(d["tickers"]))}
        for t, d in theme_value.items()
    }
    risk.by_kind = {k: round(v / total, 4) for k, v in kind_value.items()}

    top_sec = max(sec_value.items(), key=lambda kv: kv[1])
    risk.top_sector, risk.top_sector_pct = top_sec[0], round(top_sec[1] / total, 4)
    if theme_value:
        top_t = max(theme_value.items(), key=lambda kv: kv[1]["value"])
        risk.top_theme, risk.top_theme_pct = top_t[0], round(top_t[1]["value"] / total, 4)

    if biggest:
        biggest["pct"] = round(biggest["value"] / total, 4)
        risk.biggest_position = biggest

    risk.net_beta = round(weighted_beta / total, 2)
    risk.net_delta = round(net_delta, 2)

    # Correlation clusters: any sector OR theme with 2+ tickers. Themes are more
    # informative than the bare sector label, so we surface theme clusters first
    # and skip a same-ticker-set sector cluster (it would be redundant).
    cluster_seen: set = set()
    clusters: List[Dict[str, Any]] = []
    for source in (theme_index, cluster_index):
        for label, tickers in source.items():
            uniq = sorted(set(tickers))
            if len(uniq) < 2:
                continue
            key = tuple(uniq)
            if key in cluster_seen:
                continue
            cluster_seen.add(key)
            clusters.append({"label": label, "tickers": uniq})
    risk.correlation_clusters = clusters

    # Diversification (1 − HHI on sector weights). 1 = perfectly diverse.
    hhi = sum((v / total) ** 2 for v in sec_value.values())
    risk.diversification = round(max(0.0, 1.0 - hhi), 3)

    flags: List[str] = []
    if risk.top_sector_pct > 0.5:
        flags.append(f"{risk.top_sector} concentration {risk.top_sector_pct:.0%}")
    if risk.top_theme_pct > 0.5:
        flags.append(f"theme overlap '{risk.top_theme}' {risk.top_theme_pct:.0%}")
    if biggest and biggest["pct"] > 0.3:
        flags.append(f"{biggest['ticker']} single-name {biggest['pct']:.0%}")
    if risk.net_beta > 1.5:
        flags.append(f"high net beta {risk.net_beta}")
    risk.concentration_flags = flags

    # Thresholds picked to match common sense: a 5-position multi-sector book
    # where the biggest name is ≈25% is LOW; HIGH requires a real concentration.
    if (risk.top_sector_pct > 0.6 or (biggest and biggest["pct"] > 0.45)
            or risk.net_beta > 1.5):
        risk.macro_risk = "HIGH"
    elif (risk.top_sector_pct > 0.4 or (biggest and biggest["pct"] > 0.30)
            or risk.net_beta > 1.2):
        risk.macro_risk = "MODERATE"
    else:
        risk.macro_risk = "LOW"

    return risk
