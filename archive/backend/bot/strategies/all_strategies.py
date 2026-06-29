"""The 15 strategies + STRATEGY_REGISTRY + get_strategy.

Each strategy takes a ``(ticker, data)`` pair where ``data`` is a flat dict produced by
the MarketDataAdapter. Strategies are pure functions of that dict — no I/O.

All thresholds and scoring weights are tuned against the test fixtures in
``tests/unit/test_all_strategies.py``. Edit those tests first if you change behavior.
"""
from __future__ import annotations

from typing import Any, Dict

from backend.config import TUNABLES
from backend.bot.data.options import (
    chain_strike, chain_strike_with_drift, resolve_expiry_dte, snap_strike,
)
from backend.bot.strategies.base import Action, Signal, Strategy


# ─────────────────────────────────────────────────────────────────────────────
# 01. Bull Call Spread — bullish trend, low-to-mid IV
# ─────────────────────────────────────────────────────────────────────────────
class BullCallSpread(Strategy):
    name = "bull_call_spread"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        price = data.get("price", 0.0)
        ma50 = data.get("ma50", 0.0)
        ma200 = data.get("ma200", 0.0)
        rsi = data.get("rsi", 50.0)
        iv_rank = data.get("iv_rank", 50)
        adx = data.get("adx", 20)

        if price <= ma50 or ma50 <= ma200:
            return Signal.hold(ticker, self.name, "not in uptrend")
        if not (40 <= rsi <= 65):
            return Signal.hold(ticker, self.name, f"RSI {rsi:.0f} outside band")

        conf = 0.5
        conf += 0.15 if adx >= 25 else 0.0
        if iv_rank < 40:
            conf += 0.2
        elif iv_rank < 55:
            conf += 0.05
        conf = min(0.95, conf)

        exp_iso, dte = resolve_expiry_dte(ticker, target_dte=30)
        buy_strike = chain_strike(ticker, price, "call", target_dte=dte)
        # GEX: cap the short leg at the call wall (gamma resistance) when known.
        call_wall = data.get("call_wall")
        if call_wall and call_wall > buy_strike:
            sell_strike, sell_drift = chain_strike_with_drift(
                ticker, call_wall, "call", target_dte=dte,
            )
        else:
            sell_strike, sell_drift = chain_strike_with_drift(
                ticker, price, "call", moneyness=0.05, target_dte=dte,
            )
        return Signal(
            action=Action.BULL_CALL_SPREAD,
            ticker=ticker,
            confidence=conf,
            reason=f"bullish uptrend, RSI {rsi:.0f}, IV rank {iv_rank}",
            strategy=self.name,
            stop_loss=25.0,
            take_profit=50.0,
            dte=dte,
            metadata={"buy_strike": buy_strike, "sell_strike": sell_strike,
                          "dte": dte, "expiration": exp_iso,
                          "target_dte": 30,
                          "sell_strike_drift": sell_drift},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 02. RSI Mean Reversion — long-only, only in uptrend
# ─────────────────────────────────────────────────────────────────────────────
class RSIMeanReversion(Strategy):
    name = "rsi_mean_reversion"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        rsi = data.get("rsi", 50.0)
        price = data.get("price", 0.0)
        ma200 = data.get("ma200", 0.0)
        earnings_days = data.get("earnings_days", 999)

        if rsi >= 30:
            return Signal.hold(ticker, self.name, f"RSI {rsi:.0f} not oversold")
        if price <= ma200:
            return Signal.hold(ticker, self.name, "below 200MA, not in uptrend")
        if earnings_days <= 3:
            return Signal.hold(ticker, self.name, "earnings too close")

        conf = min(0.9, 0.5 + (30 - rsi) / 30 * 0.4)
        return Signal(
            action=Action.BUY_STOCK,
            ticker=ticker,
            confidence=conf,
            reason=f"RSI {rsi:.0f} oversold in uptrend",
            strategy=self.name,
            stop_loss=4.0,
            take_profit=8.0,
            metadata={"rsi_target": 50},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 03. Opening Range Breakout — first 30 min, breakout + premarket volume
# ─────────────────────────────────────────────────────────────────────────────
class OpeningRangeBreakout(Strategy):
    name = "opening_range_breakout"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        if data.get("earnings_today", False):
            return Signal.hold(ticker, self.name, "earnings today")
        if data.get("premarket_volume", 0) < 100_000:
            return Signal.hold(ticker, self.name, "insufficient premarket volume")

        price = data.get("price", 0.0)
        # GEX: use the gamma flip as a dynamic breakout pivot when intraday ORB
        # levels aren't available.
        gamma_flip = data.get("gamma_flip")
        orb_high = data.get("orb_high", 0.0) or (gamma_flip or 0.0)
        orb_low = data.get("orb_low", 0.0) or (gamma_flip or 0.0)
        volume = data.get("volume", 0)
        avg_volume = data.get("avg_volume", 1)

        # Flow: pre-market bullish sweeps are an early conviction tell for the
        # opening drive — they lift an upside breakout's confidence (#6).
        premarket_sweeps = data.get("premarket_bullish_sweeps", 0)
        if price > orb_high and volume / max(avg_volume, 1) > 1.2:
            conf = min(0.85, 0.55 + (volume / avg_volume - 1.2) * 0.2)
            morning_conviction = premarket_sweeps >= 2
            if morning_conviction:
                conf = min(0.95, conf + TUNABLES.flow_sweep_boost)
            return Signal(
                action=Action.BUY_STOCK,
                ticker=ticker,
                confidence=conf,
                reason=f"upside breakout above {orb_high:.2f}" + (
                    " + pre-market sweeps" if morning_conviction else ""),
                strategy=self.name,
                stop_loss=2.0,
                take_profit=4.0,
                metadata={"orb_high": orb_high, "time_exit": "15:30"},
            )
        if price < orb_low and volume / max(avg_volume, 1) > 1.2:
            conf = min(0.85, 0.55 + (volume / avg_volume - 1.2) * 0.2)
            return Signal(
                action=Action.SELL_STOCK,
                ticker=ticker,
                confidence=conf,
                reason=f"downside breakout below {orb_low:.2f}",
                strategy=self.name,
                stop_loss=2.0,
                take_profit=4.0,
                metadata={"orb_low": orb_low, "time_exit": "15:30"},
            )
        return Signal.hold(ticker, self.name, "no breakout")


# ─────────────────────────────────────────────────────────────────────────────
# 04. MACD Momentum Cross — bullish crossover with confirmation
# ─────────────────────────────────────────────────────────────────────────────
class MACDMomentumCross(Strategy):
    name = "macd_momentum"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        price = data.get("price", 0.0)
        ma50 = data.get("ma50", 0.0)
        macd = data.get("macd", 0.0)
        macd_signal = data.get("macd_signal", 0.0)
        hist = data.get("macd_hist", 0.0)
        prev_hist = data.get("prev_macd_hist", 0.0)
        adx = data.get("adx", 20)

        if price <= ma50:
            return Signal.hold(ticker, self.name, "below 50MA")
        if macd <= macd_signal:
            return Signal.hold(ticker, self.name, "MACD below signal")
        if hist <= prev_hist:
            return Signal.hold(ticker, self.name, "histogram weakening")

        conf = 0.55
        conf += 0.2 if adx >= 25 else 0.0
        conf += 0.1 if hist > 0 else 0.0
        conf = min(0.9, conf)
        return Signal(
            action=Action.BUY_STOCK,
            ticker=ticker,
            confidence=conf,
            reason=f"MACD cross with ADX {adx}",
            strategy=self.name,
            stop_loss=4.0,
            take_profit=8.0,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 05. Earnings Straddle — buy vol when historical move exceeds implied
# ─────────────────────────────────────────────────────────────────────────────
class EarningsStraddle(Strategy):
    name = "earnings_straddle"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        earnings_days = data.get("earnings_days", 999)
        if earnings_days > 3:
            return Signal.hold(ticker, self.name, "too far from earnings")
        hist_move = data.get("hist_earnings_move_avg", 0.0)
        implied = data.get("implied_move", 0.0)
        if implied >= hist_move:
            return Signal.hold(ticker, self.name, "implied >= historical move")

        edge = (hist_move - implied) / max(implied, 1e-6)
        conf = min(0.85, 0.55 + edge)
        price = data.get("price", 0.0)
        return Signal(
            action=Action.BUY_STRADDLE,
            ticker=ticker,
            confidence=conf,
            reason=f"hist {hist_move:.2%} > implied {implied:.2%}",
            strategy=self.name,
            stop_loss=40.0,
            take_profit=80.0,
            strike=chain_strike(ticker,price, "call"),
            dte=earnings_days + 1,
            metadata={
                "call_strike": chain_strike(ticker,price, "call"),
                "put_strike": chain_strike(ticker,price, "put"),
                "exit_trigger": "post_earnings_open",
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# 06. Trend Pullback — buy a dip in an established uptrend
# ─────────────────────────────────────────────────────────────────────────────
class TrendPullback(Strategy):
    name = "trend_pullback"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        price = data.get("price", 0.0)
        ma50 = data.get("ma50", 0.0)
        ma200 = data.get("ma200", 0.0)
        rsi = data.get("rsi", 50.0)
        high_52w = data.get("high_52w", price)

        if not (price > ma50 > ma200):
            return Signal.hold(ticker, self.name, "not in uptrend")
        if rsi >= 65:
            return Signal.hold(ticker, self.name, "RSI overbought")
        pullback = (high_52w - price) / max(high_52w, 1e-6)
        if pullback < 0.02:
            return Signal.hold(ticker, self.name, "pullback too small")
        if pullback > 0.12:
            return Signal.hold(ticker, self.name, "pullback too deep — possible trend break")

        conf = min(0.85, 0.55 + (pullback - 0.02) * 3.0)
        exp_iso, dte = resolve_expiry_dte(ticker, target_dte=30)
        return Signal(
            action=Action.BUY_CALL,
            ticker=ticker,
            confidence=conf,
            reason=f"{pullback:.1%} pullback from 52w high in uptrend",
            strategy=self.name,
            stop_loss=2.0,
            take_profit=5.0,
            strike=chain_strike(ticker, price, "call", target_dte=dte),
            dte=dte,
            metadata={"expiration": exp_iso, "target_dte": 30},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 07. News Catalyst Momentum — fresh strong news only
# ─────────────────────────────────────────────────────────────────────────────
class NewsCatalystMomentum(Strategy):
    name = "news_catalyst_momentum"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        score = data.get("news_score", 0.0)
        age = data.get("news_age_hours", 999)
        if abs(score) < 0.7:
            return Signal.hold(ticker, self.name, "weak sentiment")
        if age > 3:
            return Signal.hold(ticker, self.name, "stale news")

        action = Action.BUY_CALL if score > 0 else Action.BUY_PUT
        conf = min(0.9, 0.55 + abs(score) * 0.35)
        # Flow: corroborating institutional bullish sweeps add conviction. A
        # >$1M dark-pool print on the same name lifts the boost further (#7).
        sweep_boost = data.get("bullish_sweeps", 0) >= 2 and score > 0
        darkpool_confirms = sweep_boost and data.get("darkpool_confirms", False)
        if sweep_boost:
            boost = TUNABLES.flow_darkpool_boost if darkpool_confirms else TUNABLES.flow_sweep_boost
            conf = min(0.95, conf + boost)
        return Signal(
            action=action,
            ticker=ticker,
            confidence=conf,
            reason=f"fresh catalyst, sentiment {score:+.2f}" + (
                " + 2+ bullish sweeps + dark-pool" if darkpool_confirms
                else (" + 2+ bullish sweeps" if sweep_boost else "")),
            strategy=self.name,
            stop_loss=30.0,
            take_profit=50.0,
            strike=chain_strike(ticker,data.get("price", 0.0), "call" if score > 0 else "put"),
            dte=7,
            metadata={"time_exit": "13:00", "catalyst_type": data.get("catalyst_type")},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 08. Iron Condor — high IV, range-bound, no near-term earnings
# ─────────────────────────────────────────────────────────────────────────────
class IronCondor(Strategy):
    name = "iron_condor"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        iv_rank = data.get("iv_rank", 0)
        adx = data.get("adx", 30)
        earnings_days = data.get("earnings_days", 999)

        if iv_rank < 40:
            return Signal.hold(ticker, self.name, "IV rank too low")
        if adx >= 25:
            return Signal.hold(ticker, self.name, "market trending, not ranging")
        if earnings_days <= 14:
            return Signal.hold(ticker, self.name, "earnings within DTE")
        # GEX: condors want positive/long-gamma (mean-reverting) regimes.
        if data.get("dealer_regime") == "short_gamma":
            return Signal.hold(ticker, self.name, "short-gamma regime — skip condor")

        price = data.get("price", 0.0)
        conf = min(0.85, 0.55 + (iv_rank - 40) / 100)
        exp_iso, dte = resolve_expiry_dte(ticker, target_dte=30)
        # Iron condor convention: short wings near 20-delta, long wings
        # near 10-delta (insurance). Moneyness retained as fallback when
        # delta math fails.
        call_short, cs_drift = chain_strike_with_drift(
            ticker, price, "call",
            moneyness=0.04, target_dte=dte, target_delta=0.20)
        call_long, cl_drift = chain_strike_with_drift(
            ticker, price, "call",
            moneyness=0.06, target_dte=dte, target_delta=0.10)
        put_short, ps_drift = chain_strike_with_drift(
            ticker, price, "put",
            moneyness=-0.04, target_dte=dte, target_delta=0.20)
        put_long, pl_drift = chain_strike_with_drift(
            ticker, price, "put",
            moneyness=-0.06, target_dte=dte, target_delta=0.10)
        return Signal(
            action=Action.IRON_CONDOR,
            ticker=ticker,
            confidence=conf,
            reason=f"IV rank {iv_rank}, ADX {adx} ranging",
            strategy=self.name,
            stop_loss=200.0,
            take_profit=50.0,
            dte=dte,
            metadata={
                "call_short": call_short, "call_long": call_long,
                "put_short": put_short, "put_long": put_long,
                "dte": dte, "expiration": exp_iso, "target_dte": 30,
                "leg_drift": {
                    "call_short": cs_drift, "call_long": cl_drift,
                    "put_short": ps_drift, "put_long": pl_drift,
                },
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# 09. Covered Call Wheel — sell calls against existing shares
# ─────────────────────────────────────────────────────────────────────────────
class CoveredCallWheel(Strategy):
    name = "covered_call_wheel"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        shares = data.get("shares_owned", 0)
        if shares < 100:
            return Signal.hold(ticker, self.name, "need >=100 shares to sell call")
        if data.get("earnings_days", 999) <= 7:
            return Signal.hold(ticker, self.name, "earnings within DTE")

        price = data.get("price", 0.0)
        iv_rank = data.get("iv_rank", 30)
        if iv_rank < 25:
            return Signal.hold(ticker, self.name, "premium too low")

        exp_iso, dte = resolve_expiry_dte(ticker, target_dte=30)
        # Institutional convention: 30-delta short call. Falls through to
        # moneyness=0.03 if delta math fails (illiquid quotes).
        strike, drift = chain_strike_with_drift(
            ticker, price, "call",
            moneyness=0.03, target_dte=dte, target_delta=0.30,
        )
        conf = min(0.85, 0.6 + (iv_rank - 25) / 200)
        return Signal(
            action=Action.SELL_COVERED_CALL,
            ticker=ticker,
            confidence=conf,
            reason=f"sell {strike} call against {shares} shares",
            strategy=self.name,
            strike=strike,
            dte=dte,
            take_profit=50.0,
            metadata={"strike": strike, "dte": dte, "expiration": exp_iso,
                          "target_dte": 30, "contracts": shares // 100,
                          "strike_drift": drift},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 10. Cash-Secured Put — sell puts on quality stocks you'd own
# ─────────────────────────────────────────────────────────────────────────────
class CashSecuredPut(Strategy):
    name = "cash_secured_put"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        iv_rank = data.get("iv_rank", 0)
        if iv_rank < 30:
            return Signal.hold(ticker, self.name, "IV rank too low")
        pe = data.get("pe_ratio", 999)
        if pe is None or pe <= 0 or pe > 35:
            return Signal.hold(ticker, self.name, "valuation outside quality band")
        if data.get("earnings_days", 999) <= 7:
            return Signal.hold(ticker, self.name, "earnings within DTE")

        price = data.get("price", 0.0)
        exp_iso, dte = resolve_expiry_dte(ticker, target_dte=30)
        # Institutional convention: 30-delta short put. Falls through to
        # moneyness=-0.05 if delta math fails (illiquid quotes).
        strike, drift = chain_strike_with_drift(
            ticker, price, "put",
            moneyness=-0.05, target_dte=dte, target_delta=0.30,
        )
        conf = min(0.85, 0.6 + (iv_rank - 30) / 200)
        return Signal(
            action=Action.SELL_CSP,
            ticker=ticker,
            confidence=conf,
            reason=f"sell {strike} put, IV rank {iv_rank}",
            strategy=self.name,
            strike=strike,
            dte=dte,
            metadata={
                "strike": strike,
                "dte": dte,
                "expiration": exp_iso,
                "target_dte": 30,
                "strike_drift": drift,
                "assignment_plan": "wheel into covered calls if assigned",
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# 11. VWAP Reversion — intraday mean reversion to VWAP
# ─────────────────────────────────────────────────────────────────────────────
class VWAPReversion(Strategy):
    name = "vwap_reversion"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        price = data.get("price", 0.0)
        vwap = data.get("vwap", price)
        if vwap <= 0:
            return Signal.hold(ticker, self.name, "no VWAP")
        dist = (price - vwap) / vwap

        if abs(dist) < 0.01:
            return Signal.hold(ticker, self.name, "too close to VWAP")
        if abs(dist) > 0.05:
            return Signal.hold(ticker, self.name, "too far — possible trend, not reversion")

        market_trend = data.get("market_trend", "neutral")
        if dist < 0 and market_trend == "bearish":
            return Signal.hold(ticker, self.name, "bearish trend, skip long")
        if dist > 0 and market_trend == "bullish":
            return Signal.hold(ticker, self.name, "bullish trend, skip short")

        conf = min(0.8, 0.5 + abs(dist) * 6)
        action = Action.BUY_STOCK if dist < 0 else Action.SELL_STOCK
        return Signal(
            action=action,
            ticker=ticker,
            confidence=conf,
            reason=f"{abs(dist):.1%} {'below' if dist < 0 else 'above'} VWAP",
            strategy=self.name,
            stop_loss=1.5,
            take_profit=2.5,
            metadata={"target": vwap, "vwap": vwap},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 12. Gap Fill — fade unsupported overnight gaps
# ─────────────────────────────────────────────────────────────────────────────
class GapFill(Strategy):
    name = "gap_fill"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        gap = data.get("gap_pct", 0.0)
        if abs(gap) < 0.015:
            return Signal.hold(ticker, self.name, "gap too small")
        if abs(gap) > 0.08:
            return Signal.hold(ticker, self.name, "gap too large to fade")
        if data.get("has_catalyst", False) or data.get("earnings_today", False):
            return Signal.hold(ticker, self.name, "gap has a real catalyst")

        action = Action.SELL_STOCK if gap > 0 else Action.BUY_STOCK
        conf = min(0.8, 0.55 + abs(gap) * 5)
        prev_close = data.get("prev_close", data.get("price", 0.0))
        return Signal(
            action=action,
            ticker=ticker,
            confidence=conf,
            reason=f"unsupported gap {gap:+.1%}",
            strategy=self.name,
            stop_loss=1.5,
            take_profit=3.0,
            metadata={"target": prev_close, "fill_target": prev_close},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 13. 0DTE Scalp — SPY/QQQ-only, mid-day directional
# ─────────────────────────────────────────────────────────────────────────────
SUPPORTED_0DTE = {"SPY", "QQQ", "IWM"}


class ZeroDTEScalp(Strategy):
    name = "zero_dte_scalp"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        if ticker.upper() not in SUPPORTED_0DTE:
            return Signal.hold(ticker, self.name, "ticker not supported for 0DTE")
        tod = data.get("time_of_day", "09:30")
        if not ("10:00" <= tod <= "14:30"):
            return Signal.hold(ticker, self.name, "outside intraday window")
        if data.get("vix", 0) > 30:
            return Signal.hold(ticker, self.name, "VIX too high")
        # GEX: 0DTE scalps need short-gamma (dealers amplify moves). Skip when
        # the regime is explicitly long-gamma (dealers dampen moves).
        if data.get("dealer_regime") == "long_gamma":
            return Signal.hold(ticker, self.name, "long-gamma regime — moves get pinned")

        momentum = data.get("momentum_5m", 0.0)
        rsi_5m = data.get("rsi_5m", 50)
        if abs(momentum) < 0.4:
            return Signal.hold(ticker, self.name, "no momentum")

        if momentum > 0 and rsi_5m > 50:
            action = Action.BUY_CALL
        elif momentum < 0 and rsi_5m < 50:
            action = Action.BUY_PUT
        else:
            return Signal.hold(ticker, self.name, "momentum/RSI mismatch")

        conf = min(0.8, 0.55 + abs(momentum) * 0.25)
        price = data.get("price", 0.0)
        return Signal(
            action=action,
            ticker=ticker,
            confidence=conf,
            reason=f"5m momentum {momentum:+.2f}",
            strategy=self.name,
            stop_loss=40.0,
            take_profit=60.0,
            strike=chain_strike(ticker,price, "call" if action == Action.BUY_CALL else "put"),
            dte=0,
            metadata={"time_exit": "15:30"},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 14. Ratio Spread — moderate IV directional with skew
# ─────────────────────────────────────────────────────────────────────────────
class RatioSpread(Strategy):
    name = "ratio_spread"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        iv_rank = data.get("iv_rank", 0)
        if iv_rank < 30:
            return Signal.hold(ticker, self.name, "IV rank too low")
        adx = data.get("adx", 30)
        if adx >= 30:
            return Signal.hold(ticker, self.name, "trend too strong for ratio")
        if data.get("earnings_days", 999) <= 7:
            return Signal.hold(ticker, self.name, "earnings within DTE")

        price = data.get("price", 0.0)
        buy_strike = chain_strike(ticker,price, "call")
        sell_strike = chain_strike(ticker,price, "call", moneyness=0.05)
        conf = min(0.8, 0.55 + (iv_rank - 30) / 200)
        return Signal(
            action=Action.RATIO_SPREAD,
            ticker=ticker,
            confidence=conf,
            reason=f"1x2 ratio spread, IV rank {iv_rank}",
            strategy=self.name,
            stop_loss=100.0,
            take_profit=40.0,
            dte=45,
            metadata={
                "ratio": "1x2",
                "buy_strike": buy_strike,
                "sell_strike": sell_strike,
                "dte": 45,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# 15. Collar — hedge large equity position
# ─────────────────────────────────────────────────────────────────────────────
class Collar(Strategy):
    name = "collar"

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        shares = data.get("shares_owned", 0)
        if shares < 100:
            return Signal.hold(ticker, self.name, "need >=100 shares")
        position_value = data.get("position_value", 0)
        portfolio_value = data.get("portfolio_value", 1)
        pos_pct = position_value / max(portfolio_value, 1)
        vix = data.get("vix", 15)
        earnings_days = data.get("earnings_days", 999)
        unrealized = data.get("unrealized_gain_pct", 0.0)

        wants_collar = pos_pct >= 0.10 and (
            vix >= 22 or earnings_days <= 14 or unrealized >= 0.20
        )
        if not wants_collar:
            return Signal.hold(ticker, self.name, "no hedging trigger")

        price = data.get("price", 0.0)
        put_strike = chain_strike(ticker,price, "put", moneyness=-0.05)
        call_strike = chain_strike(ticker,price, "call", moneyness=0.05)
        conf = min(0.85, 0.55 + pos_pct + (vix - 20) / 50)
        return Signal(
            action=Action.COLLAR,
            ticker=ticker,
            confidence=conf,
            reason=f"hedge {pos_pct:.0%} position, VIX {vix}",
            strategy=self.name,
            dte=60,
            metadata={
                "buy_put_strike": put_strike,
                "sell_call_strike": call_strike,
                "contracts": shares // 100,
                "dte": 60,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# 16. EMA50 Momentum Continuation — deterministic trend-follower (STRAT.1)
# ─────────────────────────────────────────────────────────────────────────────
# Pine Script provenance: classic 4-condition continuation rule.
#
#   Entry (all four required):
#     c1: close > EMA50
#     c2: EMA50 > EMA200          (golden-cross regime)
#     c3: RSI(14) > 50            (momentum positive)
#     c4: volume > SMA(volume,20) (participation confirms)
#   Regime filter: close > EMA200 (never trade in bear markets)
#   Cooldown:      configurable seconds between entries per ticker
#
#   Exit / risk:
#     stop:   entry - 1.5 × ATR
#     target: entry + 3.0 × ATR    (2:1 reward/risk)
#     soft:   close < EMA50        (trailing exit signal — engine layer)
#
# Why include it: covers the "buy the rip" regime our existing TrendPullback
# (buy-the-dip) doesn't address. Deterministic, so it serves as a baseline
# the AI Brain must beat. Wired into the council as MechanicalTrendAgent so
# its verdict is one of the votes feeding consensus.
class EMA50MomentumContinuation(Strategy):
    name = "ema50_momentum"

    # Process-level cooldown per ticker — prevents same-cycle re-entry
    # after the engine accepts a signal. Cleared on engine restart.
    _last_entry_at: Dict[str, float] = {}

    @classmethod
    def _cooldown_seconds(cls) -> float:
        return float(getattr(TUNABLES, "ema50_strategy_cooldown_sec", 1800.0))

    @classmethod
    def mark_entry(cls, ticker: str) -> None:
        """Called by the engine after a signal from this strategy is
        accepted, so the cooldown window is anchored to actual entries,
        not every cycle that scored a candidate."""
        import time
        cls._last_entry_at[ticker.upper()] = time.monotonic()

    def analyze(self, ticker: str, data: Dict[str, Any]) -> Signal:
        price = data.get("price", 0.0)
        # Prefer the EMA fields added in market_data.py; fall back to the
        # SMA columns so this strategy still runs against backtest fixtures
        # that pre-date the EMA addition.
        ema50 = data.get("ema50") or data.get("ma50") or 0.0
        ema200 = data.get("ema200") or data.get("ma200") or 0.0
        rsi = data.get("rsi", 50.0)
        volume = data.get("volume", 0.0)
        avg_volume = data.get("avg_volume", 0.0)
        atr = data.get("atr") or 0.0

        # Hard guards — missing data ⇒ HOLD with the actual missing field
        # in the reason so the operator can see WHY we sat out.
        if price <= 0 or ema50 <= 0 or ema200 <= 0:
            return Signal.hold(ticker, self.name, "EMA data unavailable")

        # 4 conditions + regime filter.
        if price <= ema50:
            return Signal.hold(ticker, self.name,
                              f"price ${price:.2f} ≤ EMA50 ${ema50:.2f}")
        if ema50 <= ema200:
            return Signal.hold(ticker, self.name,
                              "EMA50 below EMA200 — not in uptrend")
        if rsi <= 50:
            return Signal.hold(ticker, self.name,
                              f"RSI {rsi:.0f} ≤ 50 — momentum not positive")
        if avg_volume > 0 and volume <= avg_volume:
            return Signal.hold(ticker, self.name,
                              f"volume {volume/avg_volume:.2f}× ≤ 20-day avg")
        if price <= ema200:
            return Signal.hold(ticker, self.name,
                              "price below EMA200 — regime filter")

        # Per-ticker cooldown.
        import time
        now = time.monotonic()
        last = self._last_entry_at.get(ticker.upper())
        cooldown = self._cooldown_seconds()
        if last is not None and (now - last) < cooldown:
            remaining = cooldown - (now - last)
            return Signal.hold(ticker, self.name,
                              f"cooldown {remaining:.0f}s remaining")

        # ATR-based exits. If ATR is unavailable (warm-up / vendor gap),
        # fall back to a 2%/4% stock-sized stop/TP so we don't ship with
        # zero-distance protection. Confidence drops in this case.
        atr_available = atr > 0
        if not atr_available:
            atr = price * 0.0133  # ≈ 1.33% so 1.5×ATR ≈ 2% stop
        stop_distance = 1.5 * atr
        target_distance = 3.0 * atr
        stop_pct = (stop_distance / price) * 100.0
        target_pct = (target_distance / price) * 100.0

        # Confidence shape — rule-based, not ML. Hold the boundaries hard
        # so we never claim Sonnet-grade conviction:
        #   - base 0.55 (above abstain threshold)
        #   - bonus for RSI strength (50→0, 70→+0.10)
        #   - bonus for trend separation (0% gap→0, 5% gap→+0.10)
        #   - bonus for volume confirmation (1×→0, 2×→+0.10)
        #   - clip to 0.85 ceiling (mechanical never beats LLM by spec)
        rsi_bonus = min(0.10, max(0.0, (rsi - 50.0) / 200.0))
        trend_gap_pct = (ema50 - ema200) / ema200 if ema200 > 0 else 0.0
        trend_bonus = min(0.10, max(0.0, trend_gap_pct * 2.0))
        vol_ratio = (volume / avg_volume) if avg_volume > 0 else 1.0
        vol_bonus = min(0.10, max(0.0, (vol_ratio - 1.0) * 0.10))
        conf = 0.55 + rsi_bonus + trend_bonus + vol_bonus
        if not atr_available:
            conf -= 0.10  # honesty discount when ATR is stale
        conf = max(0.35, min(0.85, conf))

        reason = (
            f"price ${price:.2f} > EMA50 > EMA200 "
            f"({trend_gap_pct*100:.1f}% gap), RSI {rsi:.0f}, "
            f"vol {vol_ratio:.1f}× avg, stop {stop_pct:.1f}%/"
            f"TP {target_pct:.1f}% (2:1 R)"
        )
        return Signal(
            action=Action.BUY_STOCK,
            ticker=ticker,
            confidence=round(conf, 3),
            reason=reason,
            strategy=self.name,
            stop_loss=round(stop_pct, 2),
            take_profit=round(target_pct, 2),
            metadata={
                "ema50": round(ema50, 4),
                "ema200": round(ema200, 4),
                "atr": round(atr, 4),
                "atr_available": atr_available,
                "stop_distance_dollars": round(stop_distance, 4),
                "target_distance_dollars": round(target_distance, 4),
                # Soft-trailing exit signal — engine layer reads this to
                # close the position when price closes back below EMA50.
                "soft_exit_level": round(ema50, 4),
                "rr_ratio": 2.0,
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────────────

STRATEGY_REGISTRY: Dict[str, Strategy] = {
    s.name: s
    for s in (
        BullCallSpread(),
        RSIMeanReversion(),
        OpeningRangeBreakout(),
        MACDMomentumCross(),
        EarningsStraddle(),
        TrendPullback(),
        NewsCatalystMomentum(),
        IronCondor(),
        CoveredCallWheel(),
        CashSecuredPut(),
        VWAPReversion(),
        GapFill(),
        ZeroDTEScalp(),
        RatioSpread(),
        Collar(),
        EMA50MomentumContinuation(),
    )
}


def get_strategy(name: str) -> Strategy:
    if name not in STRATEGY_REGISTRY:
        raise ValueError(f"unknown strategy: {name}")
    return STRATEGY_REGISTRY[name]
