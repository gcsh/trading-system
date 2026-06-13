"""Best-effort TradingView Pine Script → custom-rule translator.

Pine only truly runs inside TradingView, so this is NOT a Pine interpreter.
It scans pasted Pine source for the common indicator/condition idioms we
support and emits rules in this app's custom-rule DSL (see
``backend/bot/strategies`` custom handling). Anything it can't recognize is
reported back so the user knows what was dropped.

Recognized patterns (case-insensitive):
  - ta.crossover(macd, signal) / macd line cross  -> "buy when macd crosses above signal"
  - ta.crossunder(...)                            -> sell variant
  - ta.rsi(...) < N  / rsi < N                     -> "buy when rsi < N"
  - rsi > N                                        -> "sell when rsi > N"
  - close > ta.sma(close, 50) / price > ma50       -> "buy when price above ma50"
  - close < ta.sma(close, 200)                     -> "sell when price below ma200"
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class PineImportResult:
    rules: List[str] = field(default_factory=list)
    recognized: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    @property
    def rules_text(self) -> str:
        return "\n".join(self.rules)


def translate_pine(source: str) -> PineImportResult:
    result = PineImportResult()
    if not source:
        return result
    text = source

    # --- MACD crosses ---
    if re.search(r"crossover\s*\(\s*macd", text, re.I) or re.search(r"macd.*crossover.*signal", text, re.I):
        result.rules.append("buy when macd crosses above signal")
        result.recognized.append("MACD bullish crossover")
    if re.search(r"crossunder\s*\(\s*macd", text, re.I) or re.search(r"macd.*crossunder.*signal", text, re.I):
        result.rules.append("sell when macd crosses below signal")
        result.recognized.append("MACD bearish crossunder")

    # --- RSI thresholds ---
    for m in re.finditer(r"rsi[^<>\n]{0,30}<\s*(\d{1,3})", text, re.I):
        n = int(m.group(1))
        result.rules.append(f"buy when rsi < {n}")
        result.recognized.append(f"RSI oversold < {n}")
    for m in re.finditer(r"rsi[^<>\n]{0,30}>\s*(\d{1,3})", text, re.I):
        n = int(m.group(1))
        result.rules.append(f"sell when rsi > {n}")
        result.recognized.append(f"RSI overbought > {n}")

    # --- Price vs moving average ---
    for m in re.finditer(r"(close|price)[^<>\n]{0,40}>\s*[^,\n]*sma\s*\([^,]*,\s*(\d{1,3})", text, re.I):
        window = int(m.group(2))
        result.rules.append(f"buy when price above ma{window}")
        result.recognized.append(f"price above {window}-MA")
    for m in re.finditer(r"(close|price)[^<>\n]{0,40}<\s*[^,\n]*sma\s*\([^,]*,\s*(\d{1,3})", text, re.I):
        window = int(m.group(2))
        result.rules.append(f"sell when price below ma{window}")
        result.recognized.append(f"price below {window}-MA")

    # Dedup rules while preserving order.
    seen = set()
    deduped = []
    for r in result.rules:
        if r not in seen:
            seen.add(r)
            deduped.append(r)
    result.rules = deduped

    # Report lines that look like logic but weren't translated.
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        looks_like_logic = any(tok in s.lower() for tok in ("strategy.entry", "strategy.exit", "alertcondition", "plotshape", "ta.", "crossover", "crossunder"))
        already = any(kw in s.lower() for kw in ("macd", "rsi", "sma", "close", "price"))
        if looks_like_logic and not already:
            result.skipped.append(s[:160])

    return result
