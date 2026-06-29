"""Andrews Median Line (Pitchfork) — MITS Phase 10 theory module.

Citation:

  * Dr. Alan H. Andrews, "Median Line Study" (Andrews Foundation,
    1960s) — the original 'Action / Reaction' course that defined
    the pitchfork. Andrews observed that ~80% of price action
    eventually returns to a median line drawn from three consecutive
    significant pivots:

        Pivot A (e.g. swing high)
        Pivot B (next swing low)
        Pivot C (next swing high)

        Median line     = ray from A through midpoint(B, C)
        Upper parallel  = line through B parallel to the median
        Lower parallel  = line through C parallel to the median
        Trigger line    = line through B and C

  * Patrick Mikula, "The Best Trendline Methods of Alan Andrews and
    Five New Trendline Techniques" (Mikula Forecasting, 2003) —
    modern formalisation + extensions used here.

Signals:

  * BUY  on bounce off the lower parallel.
  * SELL on rejection at the upper parallel.
  * EXIT when price closes through the trigger line (BC line) — Andrews
    called this an "Action/Reaction failure".
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

from ._zigzag import detect_pivots
from .schema import (
    Line, Marker, Signal, TheoryAnnotation,
    bar_close, bar_ts,
)


def _bar_index_to_ts(bars, i):
    if 0 <= i < len(bars):
        return bar_ts(bars[i])
    # Extrapolate by stepping past the last bar with the median delta.
    from datetime import datetime, timedelta
    if len(bars) < 2: return bar_ts(bars[-1])
    last = bars[-1]
    prev = bars[-2]
    try:
        a = datetime.fromisoformat(bar_ts(prev).replace("Z", "+00:00"))
        b = datetime.fromisoformat(bar_ts(last).replace("Z", "+00:00"))
        step = (b - a).total_seconds()
        overshoot = i - (len(bars) - 1)
        out = b + timedelta(seconds=step * overshoot)
        return out.isoformat()
    except Exception:
        return bar_ts(bars[-1])


def analyze(
    bars: List[Dict[str, Any]],
    params: Optional[Dict[str, Any]] = None,
) -> TheoryAnnotation:
    params = dict(params or {})
    zigzag_pct = float(params.get("zigzag_pct",
                                       getattr(TUNABLES, "theory_zigzag_pct", 3.0)))
    lookback = int(params.get("lookback", 200))

    ann = TheoryAnnotation(
        theory="andrews_pitchfork",
        params={"zigzag_pct": zigzag_pct, "lookback": lookback},
        citation=(
            "Alan H. Andrews, 'Median Line Study' (Andrews Foundation, "
            "1960s); Mikula, 'The Best Trendline Methods of Alan Andrews' "
            "(Mikula Forecasting, 2003)."
        ),
    )
    if len(bars) < 30:
        ann.notes.append("Not enough bars for Andrews Pitchfork.")
        return ann

    win = bars[-lookback:] if len(bars) > lookback else bars[:]
    offset = max(0, len(bars) - lookback)
    pivots = detect_pivots(win, threshold_pct=zigzag_pct)
    if len(pivots) < 3:
        ann.notes.append("Could not find three pivots to build a pitchfork.")
        return ann
    # Use the three MOST-RECENT pivots A → B → C.
    A, B, C = pivots[-3], pivots[-2], pivots[-1]
    A_i = A["i"] + offset; B_i = B["i"] + offset; C_i = C["i"] + offset

    # Project forward (length of last segment ×2 or remaining bars).
    last_i = len(bars) - 1
    project_to_i = min(last_i + max(20, (C_i - A_i)), last_i + 60)

    # Median line: from A through midpoint(B, C).
    midBC_i = (B_i + C_i) / 2.0
    midBC_price = (B["price"] + C["price"]) / 2.0
    # Slope per bar in price space along the median.
    denom = (midBC_i - A_i) if midBC_i != A_i else 1.0
    slope = (midBC_price - A["price"]) / denom

    def _project_price(start_i, start_price, dest_i):
        return start_price + slope * (dest_i - start_i)

    end_ts = _bar_index_to_ts(bars, project_to_i)
    median_end = _project_price(A_i, A["price"], project_to_i)
    upper_end = _project_price(B_i, B["price"], project_to_i)
    lower_end = _project_price(C_i, C["price"], project_to_i)

    ann.lines.append(Line(
        kind="ray",
        start={"ts": A["ts"], "price": float(A["price"])},
        end={"ts": end_ts, "price": float(median_end)},
        color="#ffd166", width=2, style="solid",
        label="Median line",
        meta={"kind": "pitchfork_median"},
    ))
    ann.lines.append(Line(
        kind="ray",
        start={"ts": B["ts"], "price": float(B["price"])},
        end={"ts": end_ts, "price": float(upper_end)},
        color="#36c26b", width=1, style="solid",
        label="Upper parallel",
        meta={"kind": "pitchfork_upper"},
    ))
    ann.lines.append(Line(
        kind="ray",
        start={"ts": C["ts"], "price": float(C["price"])},
        end={"ts": end_ts, "price": float(lower_end)},
        color="#ff5a5f", width=1, style="solid",
        label="Lower parallel",
        meta={"kind": "pitchfork_lower"},
    ))
    # Trigger line (B–C).
    ann.lines.append(Line(
        kind="trendline",
        start={"ts": B["ts"], "price": float(B["price"])},
        end={"ts": C["ts"], "price": float(C["price"])},
        color="#9aa4b2", width=1, style="dashed",
        label="Trigger (BC)",
        meta={"kind": "pitchfork_trigger"},
    ))

    # Pivot markers.
    for pivot, name in ((A, "A"), (B, "B"), (C, "C")):
        ann.markers.append(Marker(
            ts=pivot["ts"], price=float(pivot["price"]),
            label=name, color="#ffd166", shape="circle",
        ))

    # Latest projection on the parallels.
    last_ts = bar_ts(bars[-1])
    last_close = bar_close(bars[-1])
    median_now = _project_price(A_i, A["price"], last_i)
    upper_now = _project_price(B_i, B["price"], last_i)
    lower_now = _project_price(C_i, C["price"], last_i)

    sigs: List[Signal] = []
    band_width = abs(upper_now - lower_now)
    if band_width > 0:
        if last_close <= lower_now + band_width * 0.05:
            sigs.append(Signal(
                action="BUY",
                ts=last_ts, price=last_close, confidence=0.65,
                reasoning=(
                    f"Spot ({last_close:.2f}) is at the Andrews lower "
                    f"parallel ({lower_now:.2f}) — classic 80% bounce zone."
                ),
                target_price=float(median_now),
                stop_loss=float(lower_now - band_width * 0.10),
                instrument="stock",
                theory_anchor={"side": "lower"},
            ))
        elif last_close >= upper_now - band_width * 0.05:
            sigs.append(Signal(
                action="SELL",
                ts=last_ts, price=last_close, confidence=0.65,
                reasoning=(
                    f"Spot ({last_close:.2f}) is at the Andrews upper "
                    f"parallel ({upper_now:.2f}) — classic rejection zone."
                ),
                target_price=float(median_now),
                stop_loss=float(upper_now + band_width * 0.10),
                instrument="stock",
                theory_anchor={"side": "upper"},
            ))

    ann.signals = sigs
    ann.confidence = 0.72
    ann.primer = {
        "what_it_measures": (
            "Andrews Pitchfork projects a median line through three "
            "consecutive pivots (A→B→C) plus two parallels through B "
            "and C. Andrews' empirical claim: price returns to the "
            "median 80% of the time, and the parallels bound the move."
        ),
        "how_to_read": (
            "Inside the fork = price oscillates between the parallels. "
            "Bounce off the lower parallel = BUY toward the median; "
            "rejection at upper = SELL toward the median. A close that "
            "PERSISTS outside the fork (3+ bars) invalidates the setup "
            "— Andrews called this 'Action/Reaction failure'."
        ),
        "key_levels_now": (
            f"Median {median_now:.2f}  ·  Upper {upper_now:.2f}  ·  "
            f"Lower {lower_now:.2f}"
        ),
    }
    return ann


__all__ = ["analyze"]
