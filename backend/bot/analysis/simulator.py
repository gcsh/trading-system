"""MITS Phase 14.C — pre-decision Simulator Agent.

The Simulator answers a single question before any trade ships: "given
the cohort evidence and historical analogs for this setup, what does the
forward payoff distribution actually look like?" It runs TWO independent
estimators and (when both succeed) ensembles them:

  • Analog roll-forward — pull K historical fingerprints from pgvector
    (``regime_snapshot_v2`` namespace), map each to the realized forward
    return from ``MarketOutcome``, and project onto the candidate's
    payoff function (long stock / short stock / long call / long put).

  • Monte Carlo — parameterize a GBM from the cohort cells' mean +
    dispersion of forward returns; fall back to the ticker's classified
    IV regime when the cohort is too thin to estimate sigma.

The verdict is a ``SimulatorVerdict`` carrying expected payoff, p_win,
p_max_loss, payoff std, 5th-percentile drawdown, conviction score, and
sample size. A reject_reason is populated when ``p_max_loss`` crosses
``TUNABLES.simulator_max_loss_veto`` — the simulator council agent reads
that field to veto the trade.

Caching: process-local dict keyed on (ticker, pattern, regime, vol_state,
five-min-bucket, direction, strike, dte). The bucket length is
``TUNABLES.simulator_cache_bucket_sec`` (default 300). Within a bucket,
the second call returns bit-identical numbers with ``cache_hit=True``.
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.bot.greeks import bs_price
from backend.config import TUNABLES


DIRECTIONS = ("long_stock", "short_stock", "long_call", "long_put")

# Modes carried on the SimulatorVerdict.
MODE_ANALOG = "analog"
MODE_MONTE_CARLO = "monte_carlo"
MODE_ENSEMBLE = "ensemble"

# Floor for the cohort-derived sigma. Below this the cohort is treated as
# degenerate and the IV regime fallback is used instead.
_COHORT_SIGMA_FLOOR_PCT = 0.10            # 0.1 % per-period stdev

# Annualization assumption — 252 trading days. Not in TUNABLES because
# this is a calendar fact, not an operational knob.
_TRADING_DAYS_YEAR = 252.0


@dataclass
class SimulatorVerdict:
    """Result of a single pre-trade simulation."""

    mode: str                              # "analog" | "monte_carlo" | "ensemble"
    expected_payoff: float                 # $ per unit (per share / per contract)
    p_win: float                           # P(final payoff > 0)
    p_max_loss: float                      # P(hitting the max-loss leg)
    payoff_std: float
    max_drawdown_pctile_5: float           # 5th percentile of terminal payoff
    conviction_score: float                # blended p_win + dispersion penalty, [0, 1]
    sample_size: int                       # K analogs / N MC paths
    cache_hit: bool = False
    reject_reason: Optional[str] = None    # set when verdict triggers a veto
    # MITS Phase 16.D — counterfactual scenario decomposition. Populated
    # by ``decompose_scenarios`` over the analog cohort; empty list when
    # the analog path returned no hits (MC-only paths leave it empty).
    # Additive ONLY — pre-16.D fields are bit-identical for the same
    # cache key, so the 14.C back-compat guarantee survives.
    scenarios: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScenarioCluster:
    """One counterfactual bucket of the analog cohort.

    Buckets (per operator spec on realized_return_pct):
        continuation:   r >= +5
        fake_breakout:  -3 < r < +5
        stop_out:       -10 <= r <= -3
        macro_shock:    r < -10
    """

    label: str
    probability: float                     # fraction of analogs in cluster
    expected_payoff: float                 # mean payoff $ in this cluster
    payoff_std: float
    n_analogs: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "probability": round(float(self.probability), 4),
            "expected_payoff": round(float(self.expected_payoff), 4),
            "payoff_std": round(float(self.payoff_std), 4),
            "n_analogs": int(self.n_analogs),
        }


# ── caching ─────────────────────────────────────────────────────────────


_CACHE_LOCK = threading.Lock()
_CACHE: Dict[Tuple[Any, ...], SimulatorVerdict] = {}


def _five_min_bucket() -> int:
    bucket_sec = int(TUNABLES.simulator_cache_bucket_sec)
    if bucket_sec <= 0:
        bucket_sec = 300
    return int(time.time()) // bucket_sec


def _cache_key(*, ticker: str, regime: str, vol_state: str,
               direction: str, strike: Optional[float],
               dte: Optional[int]) -> Tuple[Any, ...]:
    return (ticker.upper(), regime or "",
            vol_state or "", _five_min_bucket(), direction,
            None if strike is None else round(float(strike), 4),
            None if dte is None else int(dte))


def reset_cache() -> None:
    """Test helper — wipe the in-process cache."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ── helpers ─────────────────────────────────────────────────────────────


def _conviction(p_win: float, expected_payoff: float,
                payoff_std: float) -> float:
    """Blend p_win with a dispersion penalty. Higher when the bet is
    consistently positive; lower when payoff_std swamps expected_payoff."""
    if expected_payoff <= 0:
        return round(max(0.0, min(1.0, p_win * 0.5)), 4)
    if payoff_std <= 0:
        return round(max(0.0, min(1.0, p_win)), 4)
    sharpe_like = expected_payoff / payoff_std
    raw = 0.6 * p_win + 0.4 * (sharpe_like / (1.0 + abs(sharpe_like)))
    return round(max(0.0, min(1.0, raw)), 4)


# MITS Phase 16.D — counterfactual cluster boundaries on
# realized_return_pct. Buckets are EXACT per the operator's spec:
#   continuation:   r >= +5
#   fake_breakout:  -3 < r < +5
#   stop_out:       -10 <= r <= -3
#   macro_shock:    r < -10
_SCENARIO_LABELS = ("continuation", "fake_breakout", "stop_out", "macro_shock")


def _classify_scenario(r: float) -> str:
    if r >= 5.0:
        return "continuation"
    if r > -3.0:
        return "fake_breakout"
    if r >= -10.0:
        return "stop_out"
    return "macro_shock"


def decompose_scenarios(
    analogs: List[Any],
    *, direction: str, spot: float,
    strike: Optional[float] = None, dte: Optional[int] = None,
    iv_for_options: float = 0.3,
) -> List["ScenarioCluster"]:
    """Bucket ``AnalogHit`` rows by realized_return_pct, project per-bucket
    payoff, return one ``ScenarioCluster`` per non-empty bucket.

    Probabilities sum to 1.0 across the returned clusters (modulo
    float rounding). Empty buckets are omitted from the result.
    """
    if not analogs:
        return []

    bucketed: Dict[str, List[float]] = {label: [] for label in _SCENARIO_LABELS}
    for a in analogs:
        try:
            r = float(a.realized_return_pct)
        except (AttributeError, TypeError, ValueError):
            continue
        bucketed[_classify_scenario(r)].append(r)

    total = sum(len(v) for v in bucketed.values())
    if total == 0:
        return []

    clusters: List[ScenarioCluster] = []
    for label in _SCENARIO_LABELS:
        returns = bucketed[label]
        if not returns:
            continue
        payoffs = _project_returns_to_payoff(
            returns, direction=direction, spot=spot, strike=strike,
            dte=dte, iv_for_options=iv_for_options,
        )
        # When the options branch can't price (no strike/dte), payoffs
        # comes back empty; surface n_analogs anyway so probability mass
        # is preserved.
        if payoffs:
            mean_payoff = sum(payoffs) / len(payoffs)
            std_payoff = (
                statistics.pstdev(payoffs) if len(payoffs) >= 2 else 0.0
            )
        else:
            mean_payoff = 0.0
            std_payoff = 0.0
        clusters.append(ScenarioCluster(
            label=label,
            probability=len(returns) / total,
            expected_payoff=mean_payoff,
            payoff_std=std_payoff,
            n_analogs=len(returns),
        ))
    return clusters


def _project_returns_to_payoff(
    return_pcts: List[float],
    *,
    direction: str,
    spot: float,
    strike: Optional[float],
    dte: Optional[int],
    iv_for_options: float,
) -> List[float]:
    """Map a list of forward return percentages (as % numbers, NOT
    decimals — e.g. 2.5 means +2.5%) onto per-unit payoff at horizon.

    Stocks: payoff per share. Options: payoff per contract (× 100).
    """
    payoffs: List[float] = []
    if direction in ("long_stock", "short_stock"):
        sign = 1.0 if direction == "long_stock" else -1.0
        for r in return_pcts:
            payoffs.append(sign * spot * (float(r) / 100.0))
        return payoffs

    # Options branch — need strike, dte, and a vol input.
    if strike is None or dte is None or dte <= 0:
        return payoffs
    K = float(strike)
    T_horizon = max(dte / 365.0, 1.0 / 365.0)
    r_free = float(getattr(TUNABLES, "risk_free_rate", 0.045))
    kind = "call" if direction == "long_call" else "put"
    entry_price = bs_price(spot, K, T_horizon, r_free,
                           max(iv_for_options, 0.01), kind)
    if entry_price <= 0:
        return payoffs
    # At horizon the option is intrinsic (T → 0). One contract = 100 units.
    for r in return_pcts:
        S_T = spot * (1.0 + float(r) / 100.0)
        intrinsic = (max(0.0, S_T - K) if kind == "call"
                     else max(0.0, K - S_T))
        payoffs.append((intrinsic - entry_price) * 100.0)
    return payoffs


def _pctile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = pos - lo
    return float(sorted_vals[lo]) * (1 - frac) + float(sorted_vals[hi]) * frac


def _summarize_payoffs(payoffs: List[float], *, max_loss_per_unit: float,
                       sample_size: int, mode: str) -> SimulatorVerdict:
    """Turn a payoff distribution into a SimulatorVerdict."""
    n = len(payoffs)
    if n == 0:
        return SimulatorVerdict(
            mode=mode, expected_payoff=0.0, p_win=0.0, p_max_loss=1.0,
            payoff_std=0.0, max_drawdown_pctile_5=0.0,
            conviction_score=0.0, sample_size=sample_size,
        )
    expected = sum(payoffs) / n
    payoff_std = statistics.pstdev(payoffs) if n >= 2 else 0.0
    p_win = sum(1 for p in payoffs if p > 0) / n
    # Max-loss event: payoff at-or-below the max-loss floor (negative).
    # For options, max loss = -entry_price * 100 (we approximate as the
    # smallest observed terminal payoff threshold). For stocks we treat
    # "max loss" as a >= 50% adverse move which on a long stock is -0.5*spot.
    p_max_loss = sum(1 for p in payoffs if p <= max_loss_per_unit) / n
    srt = sorted(payoffs)
    dd_5 = _pctile(srt, 0.05)
    return SimulatorVerdict(
        mode=mode,
        expected_payoff=round(expected, 4),
        p_win=round(p_win, 4),
        p_max_loss=round(p_max_loss, 4),
        payoff_std=round(payoff_std, 4),
        max_drawdown_pctile_5=round(dd_5, 4),
        conviction_score=_conviction(p_win, expected, payoff_std),
        sample_size=sample_size,
    )


def _cohort_mu_sigma(cohort_cells: List[Dict[str, Any]]) -> Tuple[float, float, int]:
    """Sample-weighted mean and dispersion of cohort forward returns.

    Returns (mu_pct, sigma_pct, total_n). Mu/sigma are in PERCENT units
    (e.g. mu=1.5 means +1.5% over the cohort horizon). Sigma falls back
    to the spread of cell-level avg_return_pct when each cell only carries
    a mean — empirically the cohort table stores per-cell averages, not
    raw distributions, so dispersion across cells is our best proxy.
    """
    total_n = 0
    weighted_sum = 0.0
    for c in cohort_cells or []:
        n = int(c.get("sample_size") or 0)
        r = c.get("avg_return_pct")
        if r is None or n <= 0:
            continue
        total_n += n
        weighted_sum += float(r) * 100.0 * n   # cells store decimals; → percent
    if total_n == 0:
        return 0.0, 0.0, 0
    mu = weighted_sum / total_n
    # Dispersion across cells, weighted by sqrt(n) so a 1k-sample cell
    # doesn't get drowned out by a 30-sample one but also doesn't bully
    # the variance.
    if len(cohort_cells) < 2:
        return mu, 0.0, total_n
    vals = []
    for c in cohort_cells:
        n = int(c.get("sample_size") or 0)
        r = c.get("avg_return_pct")
        if r is None or n <= 0:
            continue
        vals.append(float(r) * 100.0)
    if len(vals) < 2:
        return mu, 0.0, total_n
    sigma = statistics.pstdev(vals)
    return mu, sigma, total_n


# ── the agent ───────────────────────────────────────────────────────────


class SimulatorAgent:
    """Run analog roll-forward + Monte Carlo and ensemble the results."""

    def __init__(self) -> None:
        # Stateless aside from the cache; left here so future per-agent
        # caches (eg. cached embeddings) can live alongside the dict.
        # MITS Phase 16.D — _analog_rollforward stashes the raw
        # ``AnalogHit`` list here so ``simulate()`` can run scenario
        # decomposition without re-running pgvector. Per-instance, reset
        # at the top of every ``simulate`` call so a verdict's scenarios
        # always reflect THIS call's analogs.
        self._last_analog_hits: List[Any] = []

    # -- public entry point ---------------------------------------------

    def simulate(self, *,
                 ticker: str,
                 pattern: str,
                 regime: str,
                 vol_state: str,
                 direction: str,
                 spot: float,
                 strike: Optional[float] = None,
                 dte: Optional[int] = None,
                 cohort_cells: Optional[List[Dict[str, Any]]] = None,
                 n_paths: Optional[int] = None,
                 analog_k: Optional[int] = None,
                 ) -> SimulatorVerdict:
        if direction not in DIRECTIONS:
            raise ValueError(
                f"direction must be one of {DIRECTIONS}, got {direction!r}")
        if spot <= 0:
            raise ValueError(f"spot must be positive, got {spot}")
        cohort_cells = cohort_cells or []
        n_paths = int(n_paths or TUNABLES.simulator_mc_paths)
        analog_k = int(analog_k or TUNABLES.simulator_analog_k)

        cache_key = _cache_key(ticker=ticker, regime=regime,
                               vol_state=vol_state, direction=direction,
                               strike=strike, dte=dte)
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
        if cached is not None:
            return SimulatorVerdict(
                mode=cached.mode,
                expected_payoff=cached.expected_payoff,
                p_win=cached.p_win,
                p_max_loss=cached.p_max_loss,
                payoff_std=cached.payoff_std,
                max_drawdown_pctile_5=cached.max_drawdown_pctile_5,
                conviction_score=cached.conviction_score,
                sample_size=cached.sample_size,
                cache_hit=True,
                reject_reason=cached.reject_reason,
                scenarios=[dict(sc) for sc in cached.scenarios],
            )

        # MITS Phase 16.D — clear the analog-hit stash so scenarios on
        # this verdict only reflect THIS call's pgvector pull.
        self._last_analog_hits = []
        analog = self._analog_rollforward(
            ticker=ticker, pattern=pattern, regime=regime,
            vol_state=vol_state, direction=direction, spot=spot,
            strike=strike, dte=dte, cohort_cells=cohort_cells,
            analog_k=analog_k,
        )
        mc = self._monte_carlo(
            ticker=ticker, direction=direction, spot=spot, strike=strike,
            dte=dte, cohort_cells=cohort_cells, n_paths=n_paths,
        )

        if analog.sample_size > 0 and mc.sample_size > 0:
            verdict = self.ensemble(analog, mc)
        elif analog.sample_size > 0:
            verdict = analog
        else:
            verdict = mc

        # Veto gate — mutates the verdict in place. Caller-side rendering
        # picks up reject_reason via to_dict().
        veto_threshold = float(TUNABLES.simulator_max_loss_veto)
        if verdict.p_max_loss > veto_threshold:
            verdict.reject_reason = (
                f"simulator_veto: p_max_loss={verdict.p_max_loss:.2%} "
                f"> threshold={veto_threshold:.2%}"
            )

        # MITS Phase 16.D — scenario decomposition runs over the analog
        # hits stashed by _analog_rollforward. Pure post-processing —
        # all numeric verdict fields (expected_payoff, p_win, ...) are
        # already finalized above, so 14.C back-compat is preserved.
        if self._last_analog_hits:
            iv_for_options = self._resolve_iv(ticker, cohort_cells)
            clusters = decompose_scenarios(
                self._last_analog_hits,
                direction=direction, spot=spot,
                strike=strike, dte=dte, iv_for_options=iv_for_options,
            )
            verdict.scenarios = [sc.to_dict() for sc in clusters]

        with _CACHE_LOCK:
            _CACHE[cache_key] = verdict
        return verdict

    # -- analog roll-forward --------------------------------------------

    def _analog_rollforward(self, *, ticker: str, pattern: str, regime: str,
                            vol_state: str, direction: str, spot: float,
                            strike: Optional[float], dte: Optional[int],
                            cohort_cells: List[Dict[str, Any]],
                            analog_k: int) -> SimulatorVerdict:
        """K-NN over regime_snapshot_v2 → realized forward returns →
        payoff distribution. Falls back to cohort-cell mean returns when
        pgvector is offline or returns no usable analogs."""
        from backend.bot.corpus.analog_retrieval import retrieve_analogs
        from backend.bot.regime.vector import RegimeDimension, RegimeVector

        # SimulatorAgent.simulate() takes (regime, vol_state) as plain
        # strings; retrieve_analogs reads them off a RegimeVector. Build
        # a transient one with only those two dims populated so the query
        # text matches the legacy contract exactly.
        transient_rv = RegimeVector(
            ticker=ticker, as_of=datetime.utcnow(),
            trend=RegimeDimension(
                value=regime or "unknown", freshness_seconds=0.0,
                source="regime", health="green",
            ),
            volatility_state=RegimeDimension(
                value=vol_state or "normal", freshness_seconds=0.0,
                source="regime", health="green",
            ),
            iv_rank=RegimeDimension(
                value=None, freshness_seconds=None,
                source="iv_regime", health="yellow",
            ),
            iv_regime=RegimeDimension(
                value=None, freshness_seconds=None,
                source="iv_regime", health="yellow",
            ),
            intraday_regime=RegimeDimension(
                value="unknown", freshness_seconds=None,
                source="intraday", health="yellow",
            ),
            gamma_state=RegimeDimension(
                value=None, freshness_seconds=None,
                source="gex", health="yellow",
            ),
            macro_regime=RegimeDimension(
                value=None, freshness_seconds=None,
                source="macro", health="yellow",
            ),
            health="yellow",
        )
        horizon = self._dte_to_horizon(dte)
        cluster = retrieve_analogs(
            ticker=ticker, regime_vector=transient_rv,
            pattern=pattern or "na", horizon=horizon, k=int(analog_k),
            sector_fallback=True,
        )
        # MITS Phase 16.D — stash the raw hits so simulate() can run
        # scenario decomposition without re-running pgvector. Caller-side
        # responsibility to clear this on each simulate() invocation.
        self._last_analog_hits = list(cluster.analogs)
        return_pcts: List[float] = [
            a.realized_return_pct for a in cluster.analogs
        ]

        # Cohort-cell fallback. Synthesize "analogs" from the cohort
        # cells when pgvector returned nothing. Each cell carries
        # (avg_return_pct, sample_size); we emit sample_size copies of
        # avg_return_pct so the empirical distribution reflects cell
        # weight. This keeps the ANALOG mode honest even when the vector
        # store is empty.
        if not return_pcts:
            for c in cohort_cells:
                n = int(c.get("sample_size") or 0)
                r = c.get("avg_return_pct")
                if r is None or n <= 0:
                    continue
                # cells store decimals (0.012 = +1.2%) — convert to percent
                pct = float(r) * 100.0
                # Cap injection per cell so a 5k-sample cell doesn't drown
                # the empirical distribution.
                inject = min(n, max(5, analog_k // max(1, len(cohort_cells))))
                return_pcts.extend([pct] * inject)
        if not return_pcts:
            return SimulatorVerdict(
                mode=MODE_ANALOG, expected_payoff=0.0, p_win=0.0,
                p_max_loss=0.0, payoff_std=0.0,
                max_drawdown_pctile_5=0.0, conviction_score=0.0,
                sample_size=0,
            )

        iv_for_options = self._resolve_iv(ticker, cohort_cells)
        payoffs = _project_returns_to_payoff(
            return_pcts, direction=direction, spot=spot, strike=strike,
            dte=dte, iv_for_options=iv_for_options,
        )
        max_loss = self._max_loss_per_unit(direction=direction, spot=spot,
                                           strike=strike, dte=dte,
                                           iv=iv_for_options)
        return _summarize_payoffs(
            payoffs, max_loss_per_unit=max_loss,
            sample_size=len(return_pcts), mode=MODE_ANALOG,
        )

    @staticmethod
    def _dte_to_horizon(dte: Optional[int]) -> str:
        """Bucket DTE into the MarketOutcome.horizon enum."""
        if not dte or dte <= 0:
            return "1d"
        if dte <= 1:
            return "1d"
        if dte <= 5:
            return "5d"
        return "20d"

    # -- monte carlo -----------------------------------------------------

    def _monte_carlo(self, *, ticker: str, direction: str, spot: float,
                     strike: Optional[float], dte: Optional[int],
                     cohort_cells: List[Dict[str, Any]],
                     n_paths: int) -> SimulatorVerdict:
        """GBM walk. mu/sigma sourced from cohort_cells first, then
        IV regime classifier."""
        days = int(dte) if dte and dte > 0 else 1
        mu_cohort_pct, sigma_cohort_pct, total_n = _cohort_mu_sigma(
            cohort_cells)
        # Cohort sigma is across-cell dispersion in percent units over the
        # cohort horizon; treat as daily by dividing by sqrt(days_horizon)
        # — approximate but defensible until we record per-cell stdev.
        cohort_horizon_days = max(days, 1)
        if total_n > 0 and sigma_cohort_pct >= _COHORT_SIGMA_FLOOR_PCT:
            # mu daily ≈ mu_horizon / days (drift assumed linear).
            mu_daily = (mu_cohort_pct / 100.0) / cohort_horizon_days
            sigma_daily = (sigma_cohort_pct / 100.0) / math.sqrt(
                cohort_horizon_days)
        else:
            # IV regime fallback. Annualized IV → daily sigma.
            iv_annual = self._iv_for_ticker(ticker)
            sigma_daily = iv_annual / math.sqrt(_TRADING_DAYS_YEAR)
            # Drift fallback — risk-neutral rate / 252.
            r_free = float(getattr(TUNABLES, "risk_free_rate", 0.045))
            mu_daily = r_free / _TRADING_DAYS_YEAR

        # Pure-python GBM (no numpy needed for the engine path, but we
        # use numpy when available for speed).
        terminal_returns = self._simulate_gbm_returns(
            mu_daily=mu_daily, sigma_daily=sigma_daily,
            days=days, n_paths=n_paths,
        )
        iv_for_options = self._resolve_iv(ticker, cohort_cells)
        payoffs = _project_returns_to_payoff(
            terminal_returns, direction=direction, spot=spot, strike=strike,
            dte=dte, iv_for_options=iv_for_options,
        )
        max_loss = self._max_loss_per_unit(direction=direction, spot=spot,
                                           strike=strike, dte=dte,
                                           iv=iv_for_options)
        return _summarize_payoffs(
            payoffs, max_loss_per_unit=max_loss,
            sample_size=len(terminal_returns), mode=MODE_MONTE_CARLO,
        )

    @staticmethod
    def _simulate_gbm_returns(*, mu_daily: float, sigma_daily: float,
                              days: int, n_paths: int) -> List[float]:
        """Return terminal return percentages from a daily-step GBM.

        Uses numpy when available for vectorization; falls back to the
        random module otherwise. Either way the seed is sourced from
        TUNABLES.simulator_mc_seed when set, so verdicts are reproducible
        bit-for-bit across two calls in the same cache bucket — required
        by Gate A. We honor the deterministic path regardless of numpy.
        """
        seed = int(getattr(TUNABLES, "simulator_mc_seed", 0))
        if days <= 0 or n_paths <= 0:
            return []
        try:
            import numpy as np  # type: ignore
            rng = np.random.default_rng(seed if seed > 0 else 1234)
            shocks = rng.standard_normal((n_paths, days))
            log_returns = (mu_daily - 0.5 * sigma_daily * sigma_daily) \
                + sigma_daily * shocks
            terminal_log = log_returns.sum(axis=1)
            terminal_pct = (np.exp(terminal_log) - 1.0) * 100.0
            return terminal_pct.tolist()
        except Exception:
            import random
            rnd = random.Random(seed if seed > 0 else 1234)
            out: List[float] = []
            drift = mu_daily - 0.5 * sigma_daily * sigma_daily
            for _ in range(n_paths):
                total_log = 0.0
                for _ in range(days):
                    total_log += drift + sigma_daily * rnd.gauss(0.0, 1.0)
                out.append((math.exp(total_log) - 1.0) * 100.0)
            return out

    # -- ensemble --------------------------------------------------------

    def ensemble(self, analog: SimulatorVerdict,
                 mc: SimulatorVerdict) -> SimulatorVerdict:
        """Average analog + MC. Max-loss probability is the conservative
        case (MAX of the two); conviction recomputed from the blended
        stats."""
        expected = round(
            (analog.expected_payoff + mc.expected_payoff) / 2.0, 4)
        p_win = round((analog.p_win + mc.p_win) / 2.0, 4)
        p_max_loss = round(max(analog.p_max_loss, mc.p_max_loss), 4)
        payoff_std = round((analog.payoff_std + mc.payoff_std) / 2.0, 4)
        dd_5 = round(min(analog.max_drawdown_pctile_5,
                         mc.max_drawdown_pctile_5), 4)
        return SimulatorVerdict(
            mode=MODE_ENSEMBLE,
            expected_payoff=expected,
            p_win=p_win,
            p_max_loss=p_max_loss,
            payoff_std=payoff_std,
            max_drawdown_pctile_5=dd_5,
            conviction_score=_conviction(p_win, expected, payoff_std),
            sample_size=analog.sample_size + mc.sample_size,
        )

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _resolve_iv(ticker: str,
                    cohort_cells: List[Dict[str, Any]]) -> float:
        """Pick the IV input for option pricing. Cohort first (when cells
        carry an iv_rank or implied_move), then iv_regime classifier,
        then TUNABLES default."""
        iv_from_cells: List[float] = []
        for c in cohort_cells:
            iv = c.get("iv") or c.get("implied_vol")
            if iv is not None:
                try:
                    iv_from_cells.append(float(iv))
                except (TypeError, ValueError):
                    pass
            ivr = c.get("iv_rank")
            if ivr is not None:
                try:
                    # rank 0-100 → decimal IV ≈ 15%..45% (matches the
                    # heuristic in greeks_from_position)
                    iv_from_cells.append(0.15 + 0.30 * (float(ivr) / 100.0))
                except (TypeError, ValueError):
                    pass
        if iv_from_cells:
            return max(0.05, min(2.5, sum(iv_from_cells)
                                 / len(iv_from_cells)))
        return SimulatorAgent._iv_for_ticker(ticker)

    @staticmethod
    def _iv_for_ticker(ticker: str) -> float:
        """Read IV regime classifier's current_iv; fall back to default."""
        try:
            from backend.bot.iv_regime import classify_ticker
            report = classify_ticker(ticker)
            cur = report.current_iv
            if cur and cur > 0:
                return float(cur)
        except Exception:
            pass
        # Final fallback — TUNABLES.default_iv_rank is a rank (0-100), not
        # a decimal IV. Convert per the same heuristic above.
        rank = float(getattr(TUNABLES, "default_iv_rank", 25.0))
        return 0.15 + 0.30 * (rank / 100.0)

    @staticmethod
    def _max_loss_per_unit(*, direction: str, spot: float,
                           strike: Optional[float], dte: Optional[int],
                           iv: float) -> float:
        """Threshold treated as the "max loss" event. For long options
        the floor is -entry_price * 100 (full premium lost). For stocks
        we treat a -50% / +50% adverse move as the max-loss bucket since
        a paper-traded long stock has no defined cap until the operator
        adds one."""
        if direction == "long_stock":
            return -0.50 * spot
        if direction == "short_stock":
            return -0.50 * spot
        if direction in ("long_call", "long_put") and strike and dte:
            T = max(int(dte) / 365.0, 1.0 / 365.0)
            r_free = float(getattr(TUNABLES, "risk_free_rate", 0.045))
            kind = "call" if direction == "long_call" else "put"
            entry = bs_price(spot, float(strike), T, r_free,
                             max(iv, 0.01), kind)
            return -entry * 100.0
        return -0.50 * spot
