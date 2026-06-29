"""Strategy-related endpoints: list registry, import Pine script."""
from __future__ import annotations

from fastapi import APIRouter

from backend.bot.pine_import import translate_pine
from backend.bot.strategies.all_strategies import STRATEGY_REGISTRY
from backend.db import session_scope
from backend.models.config import load_config, save_config

router = APIRouter(prefix="/strategies", tags=["strategies"])


# Single source of truth for UI labels + descriptions. Driven by the
# strategy registry keys so adding a new strategy in all_strategies.py
# auto-shows up in /strategies/catalog (with a fallback label derived
# from the slug). Categories: stock | long_option | defined_risk |
# premium_selling | complex.
_STRATEGY_META: dict[str, dict[str, str]] = {
    "bull_call_spread": {
        "label": "Bull Call Spread",
        "description": "Defined-risk bullish — long call + short higher call",
        "category": "defined_risk",
    },
    "rsi_mean_reversion": {
        "label": "RSI Mean Reversion",
        "description": "Buy oversold dips inside an uptrend",
        "category": "stock",
    },
    "opening_range_breakout": {
        "label": "Opening Range Breakout",
        "description": "Trade the first-30-min range break",
        "category": "stock",
    },
    "macd_momentum": {
        "label": "MACD Momentum Cross",
        "description": "Long when MACD crosses up; short on cross down",
        "category": "stock",
    },
    "earnings_straddle": {
        "label": "Earnings Straddle",
        "description": "Long straddle ahead of earnings — IV expansion play",
        "category": "long_option",
    },
    "trend_pullback": {
        "label": "Trend Pullback",
        "description": "Buy a 2-12% pullback inside an established uptrend",
        "category": "long_option",
    },
    "news_catalyst_momentum": {
        "label": "News Catalyst Momentum",
        "description": "Strong sentiment + fresh news → directional option",
        "category": "long_option",
    },
    "iron_condor": {
        "label": "Iron Condor",
        "description": "Range-bound, high IV — sell premium on both wings",
        "category": "complex",
    },
    "covered_call_wheel": {
        "label": "Covered Call Wheel",
        "description": "Hold stock, sell calls against it; roll on assignment",
        "category": "premium_selling",
    },
    "cash_secured_put": {
        "label": "Cash-Secured Put",
        "description": "Sell put on stock you'd be happy to own at the strike",
        "category": "premium_selling",
    },
    "vwap_reversion": {
        "label": "VWAP Reversion",
        "description": "Fade extreme moves back to VWAP intraday",
        "category": "stock",
    },
    "gap_fill": {
        "label": "Gap Fill",
        "description": "Fade overnight gaps that statistically retrace",
        "category": "stock",
    },
    "zero_dte_scalp": {
        "label": "0DTE Scalp",
        "description": "Same-day expiry directional scalp on SPY / QQQ",
        "category": "long_option",
    },
    "ratio_spread": {
        "label": "Ratio Spread",
        "description": "1×2 or 1×3 ratio for asymmetric payoff",
        "category": "complex",
    },
    "collar": {
        "label": "Collar",
        "description": "Long stock + protective put + sell upside call",
        "category": "complex",
    },
    "ema50_momentum": {
        "label": "EMA50 Momentum Continuation",
        "description": ("Long-only trend continuation: price > EMA50 > EMA200, "
                        "RSI > 50, volume > 20-day avg. 2:1 ATR-based RR. "
                        "Also votes in the council as MechanicalTrendAgent."),
        "category": "stock",
    },
}


def _strategy_entry(slug: str) -> dict[str, str]:
    """Return the catalog entry for a slug. Falls back to a label derived
    from the slug when metadata is missing (so a newly-added strategy is
    visible immediately even before its metadata is added here)."""
    meta = _STRATEGY_META.get(slug, {})
    return {
        "slug": slug,
        "label": meta.get("label") or slug.replace("_", " ").title(),
        "description": meta.get("description") or "",
        "category": meta.get("category") or "stock",
    }


@router.get("/list")
async def list_strategies() -> list[str]:
    """Legacy: just the slugs. Kept for backward compat."""
    return list(STRATEGY_REGISTRY.keys())


@router.get("/catalog")
async def strategy_catalog() -> list[dict[str, str]]:
    """Rich strategy catalog for the UI — slug + human label + description
    + category. The frontend reads this once on mount; new strategies in
    the registry auto-appear (with a fallback label) so we never have to
    touch the UI when adding a strategy."""
    return [_strategy_entry(slug) for slug in STRATEGY_REGISTRY.keys()]


@router.post("/import-pine")
async def import_pine(payload: dict) -> dict:
    """Translate pasted Pine source to custom rules.

    If ``apply`` is true, the translated rules are saved into config and the
    active strategy is switched to ``custom``.
    """
    source = payload.get("source", "")
    apply = bool(payload.get("apply", False))
    result = translate_pine(source)
    saved = False
    if apply and result.rules:
        with session_scope() as session:
            cfg = load_config(session)
            cfg["custom_rules"] = result.rules_text
            cfg["strategy"] = "custom"
            save_config(session, cfg)
            saved = True
    return {
        "rules": result.rules,
        "rules_text": result.rules_text,
        "recognized": result.recognized,
        "skipped": result.skipped,
        "applied": saved,
    }
