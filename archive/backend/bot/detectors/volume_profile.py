"""MITS Phase 0 — volume-profile detectors (HVN / LVN).

We approximate volume profile with a rolling 60-bar volume-by-price
histogram (10 bins). At each bar we ask:
  * Is the current close inside a high-volume-node (HVN) — top-30%-bin?
  * Did it just bounce off / reject from a low-volume-node (LVN)?

Pure rule-of-thumb — the corpus will decide whether the rule has edge.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from backend.bot.detectors.base import (
    Detector, Observation, _bar_timeframe, _classify_regime,
    _classify_vol_state, _lower_columns, _time_bucket,
)


def _build_obs(ticker: str, bars, i: int, pattern: str,
                  features: Dict[str, Any], closes) -> Observation:
    ts = bars.index[i]
    try:
        ts_py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    except Exception:
        ts_py = ts
    return Observation(
        ticker=ticker,
        pattern=pattern,
        timestamp=ts_py,
        timeframe=_bar_timeframe(bars),
        regime=_classify_regime(bars, i),
        vol_state=_classify_vol_state(bars, i),
        time_bucket=_time_bucket(ts_py) if hasattr(ts_py, "hour") else "rth",
        spot=float(closes[i]),
        features=features,
    )


def _volume_profile(closes: List[float], volumes: List[float],
                          start: int, end: int, n_bins: int = 10
                          ) -> Tuple[List[float], List[float]]:
    """Return (bin_edges, bin_volumes) over [start, end)."""
    if start >= end:
        return [], []
    window_closes = closes[start:end]
    window_vols = volumes[start:end]
    lo = min(window_closes)
    hi = max(window_closes)
    if hi <= lo:
        return [], []
    width = (hi - lo) / n_bins
    edges = [lo + i * width for i in range(n_bins + 1)]
    bins = [0.0] * n_bins
    for c, v in zip(window_closes, window_vols):
        idx = int((c - lo) / width)
        if idx >= n_bins:
            idx = n_bins - 1
        bins[idx] += v
    return edges, bins


def _bin_index(price: float, edges: List[float]) -> int:
    """Bin that `price` falls in. -1 if outside the range."""
    if not edges or price < edges[0] or price > edges[-1]:
        return -1
    width = edges[1] - edges[0]
    idx = int((price - edges[0]) / width)
    if idx >= len(edges) - 1:
        idx = len(edges) - 2
    return idx


class HVNAcceptanceDetector(Detector):
    """Current close sits in a high-volume-node — the top-30% bin by
    cumulative volume in the prior 60-bar window. Acceptance signal:
    price is consolidating where heavy volume has already transacted."""

    pattern = "hvn_acceptance"

    def default_params(self) -> Dict[str, Any]:
        return {
            "lookback_bars": 60,
            "n_bins": 10,
            "top_fraction": 0.3,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 65:
            return []
        bars = _lower_columns(bars)
        try:
            closes = bars["close"].astype(float).tolist()
            volumes = (bars["volume"].astype(float).tolist()
                       if "volume" in bars.columns else None)
        except Exception:
            return []
        if not volumes:
            return []
        p = params if params is not None else self.default_params()
        lookback = int(p.get("lookback_bars", 60))
        n_bins = int(p.get("n_bins", 10))
        top_frac = float(p.get("top_fraction", 0.3))
        out: List[Observation] = []
        for i in range(lookback, len(closes)):
            edges, bins = _volume_profile(closes, volumes, i - lookback, i,
                                              n_bins=n_bins)
            if not edges or not bins:
                continue
            sorted_bins = sorted(enumerate(bins), key=lambda kv: kv[1], reverse=True)
            top30_count = max(1, int(top_frac * len(bins)))
            top30_idxs = {idx for idx, _ in sorted_bins[:top30_count]}
            idx = _bin_index(closes[i], edges)
            if idx in top30_idxs:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "bin_idx": idx,
                    "bin_volume": round(bins[idx], 2),
                    "max_bin_volume": round(max(bins), 2),
                }, closes))
        return out


class LVNRejectionDetector(Detector):
    """Bar's range touches a low-volume-node (bottom-30% bin) but the
    close moves away from that bin — interpreted as rejection of the
    low-volume area."""

    pattern = "lvn_rejection"

    def default_params(self) -> Dict[str, Any]:
        return {
            "lookback_bars": 60,
            "n_bins": 10,
            "bottom_fraction": 0.3,
        }

    def detect(self, ticker: str, bars,
                  params: Dict[str, Any] | None = None,
                  **kwargs) -> List[Observation]:
        if bars is None or len(bars) < 65:
            return []
        bars = _lower_columns(bars)
        try:
            highs = bars["high"].astype(float).tolist()
            lows = bars["low"].astype(float).tolist()
            closes = bars["close"].astype(float).tolist()
            volumes = (bars["volume"].astype(float).tolist()
                       if "volume" in bars.columns else None)
        except Exception:
            return []
        if not volumes:
            return []
        p = params if params is not None else self.default_params()
        lookback = int(p.get("lookback_bars", 60))
        n_bins = int(p.get("n_bins", 10))
        bot_frac = float(p.get("bottom_fraction", 0.3))
        out: List[Observation] = []
        for i in range(lookback, len(closes)):
            edges, bins = _volume_profile(closes, volumes, i - lookback, i,
                                              n_bins=n_bins)
            if not edges or not bins:
                continue
            sorted_bins = sorted(enumerate(bins), key=lambda kv: kv[1])
            bot30_count = max(1, int(bot_frac * len(bins)))
            # Capture bins whose volume <= the bot30 threshold so ties
            # don't shut out legitimate low-volume nodes. Otherwise a
            # market with eight equally-empty bins would arbitrarily
            # pick the first three.
            threshold = sorted_bins[bot30_count - 1][1] if sorted_bins else 0.0
            bot30_idxs = {idx for idx, v in enumerate(bins) if v <= threshold}
            touched_lvn = (_bin_index(highs[i], edges) in bot30_idxs
                                  or _bin_index(lows[i], edges) in bot30_idxs)
            close_idx = _bin_index(closes[i], edges)
            if touched_lvn and close_idx not in bot30_idxs:
                out.append(_build_obs(ticker, bars, i, self.pattern, {
                    "close_bin": close_idx,
                    "touched_low_volume_node": True,
                }, closes))
        return out


def build_volume_profile_detectors() -> List[Detector]:
    return [HVNAcceptanceDetector(), LVNRejectionDetector()]
