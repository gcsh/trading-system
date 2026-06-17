"""ThetaData v3 client — wraps the locally-running ThetaTerminal.

Architecture in one line: ThetaTerminal is a Java daemon (systemd unit
``thetadata.service`` on the EC2) that authenticates with ThetaData's
cloud, caches data, and exposes a localhost REST API on port 25503.
Our code talks to ``http://127.0.0.1:25503/v3/...`` — never the cloud
directly.

Why this file exists:
  - Replaces the yfinance options path that gave us silent stale mids
    (the 2026-06-01 AAPL CALL -$711 was caused by yfinance returning a
    quote that looked fresh but was hours old).
  - Centralizes the v3 endpoint paths and response-shape parsing so the
    sanity layer (P1.2) and the IV percentile builder (P1.3) can sit on
    a single client rather than each re-implementing the protocol.

What ThetaData v3 Standard tier does NOT expose:
  - No ``/v3/option/snapshot/implied_volatility`` or ``/snapshot/greeks``
    endpoints — those appear to be Pro-tier. Standard gives us raw NBBO
    quotes, OI, trades, OHLC, plus historical EOD. IV and greeks we
    compute ourselves from the quote data (which actually fits the
    data-integrity-layer principle: we don't trust vendor-derived
    numbers we could compute and validate locally).

Failure modes are graceful — all public methods return ``None`` or empty
list on any error, log a warning, and let the caller fall back to the
existing yfinance/cboe paths.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── config ──────────────────────────────────────────────────────────────


THETADATA_BASE_URL_DEFAULT = "http://127.0.0.1:25503"
THETADATA_TIMEOUT_DEFAULT = 4.0
CHAIN_CACHE_TTL = 10.0  # seconds — chain_strike is called many times per
                       # bot cycle (each strategy × each ticker); without
                       # caching we'd hammer the terminal redundantly.
# Expirations are static intraday (new ones list at most once per day at
# CBOE's discretion). One-hour TTL is generous and still picks up new
# expirations the day they list. Saves ~500 calls per cycle once the
# strategies are all wired through chain_strike → nearest_expiration.
EXPIRATIONS_CACHE_TTL = 3600.0
# OI is exchange-published once per day; 5 min is generous and avoids
# refetching for every strategy in a single GEX cycle.
OI_CACHE_TTL = 300.0


def _base_url() -> str:
    return os.environ.get("THETADATA_BASE_URL", THETADATA_BASE_URL_DEFAULT).rstrip("/")


# ── shapes ──────────────────────────────────────────────────────────────


@dataclass
class OptionQuote:
    """Single NBBO snapshot for one contract."""
    symbol: str
    expiration: date
    strike: float
    right: str  # "CALL" | "PUT"
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    timestamp: Optional[datetime]  # ET-local per terminal's response

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return 0.0

    @property
    def spread_pct(self) -> Optional[float]:
        if self.mid <= 0:
            return None
        return (self.ask - self.bid) / self.mid


@dataclass
class OptionOpenInterest:
    """One open-interest sample for a single contract."""
    expiration: date
    strike: float
    right: str
    open_interest: int
    timestamp: Optional[datetime]


# ── client ──────────────────────────────────────────────────────────────


class ThetaDataClient:
    """Thin REST wrapper over the local ThetaTerminal."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = THETADATA_TIMEOUT_DEFAULT,
        session: Optional[Any] = None,
    ) -> None:
        self.base_url = (base_url or _base_url()).rstrip("/")
        self.timeout = float(timeout)
        # Inject a session for tests; otherwise lazy-create on first use.
        self._session = session
        # Per-instance chain snapshot cache. Keyed by (ticker, expiration_iso).
        self._chain_cache: Dict[Tuple[str, str], Tuple[float, List["OptionQuote"]]] = {}
        # Per-instance expirations cache. Keyed by ticker.
        self._expirations_cache: Dict[str, Tuple[float, List[date]]] = {}
        # Per-instance open-interest cache. OI moves once a day (post-close
        # update) so a 5-minute TTL is generous and still avoids hammering
        # the terminal every cycle.
        self._oi_cache: Dict[Tuple[str, str], Tuple[float, List["OptionOpenInterest"]]] = {}

    # ── transport ─────────────────────────────────────────────────────

    def _get_session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _get_json(self, path: str, params: Dict[str, Any]) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        try:
            r = self._get_session().get(url, params=params, timeout=self.timeout)
        except Exception as exc:
            logger.debug("thetadata GET failed %s: %s", path, exc)
            return None
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        # 472 = "no data" per ThetaData convention. Not an error.
        if r.status_code in (404, 472):
            return None
        if r.status_code == 403:
            # Tier-gated endpoint — log once-ish and return None.
            logger.warning(
                "thetadata 403 (subscription tier) %s: %s",
                path, r.text[:200],
            )
            return None
        if r.status_code == 410:
            logger.error("thetadata 410 (API version) %s — code is using a deprecated path", path)
            return None
        logger.warning("thetadata GET %s -> %s", path, r.status_code)
        return None

    # ── public ────────────────────────────────────────────────────────

    def available(self) -> bool:
        """Cheap reachability check. ``True`` when terminal is up + serving."""
        try:
            r = self._get_session().get(
                f"{self.base_url}/v3/option/list/expirations",
                params={"symbol": "AAPL", "format": "json"},
                timeout=min(2.0, self.timeout),
            )
            return r.status_code == 200
        except Exception:
            return False

    def list_expirations(self, symbol: str) -> List[date]:
        """All listed expirations for a symbol, sorted ascending.

        Cached per-symbol for ``EXPIRATIONS_CACHE_TTL`` (default 1h).
        Listed expirations don't change intraday so this is a near-free
        save: a single bot cycle calls ``nearest_expiration`` many times
        across strategies × tickers, and without the cache each one fired
        a full ~37 KB JSON fetch."""
        key = symbol.upper()
        now = time.monotonic()
        hit = self._expirations_cache.get(key)
        if hit and (now - hit[0]) < EXPIRATIONS_CACHE_TTL:
            return hit[1]
        payload = self._get_json(
            "/v3/option/list/expirations",
            {"symbol": key, "format": "json"},
        )
        if not payload:
            # Don't cache failure — try again next call.
            return []
        rows = payload.get("response") or []
        out: List[date] = []
        for row in rows:
            try:
                out.append(datetime.strptime(row["expiration"], "%Y-%m-%d").date())
            except Exception:
                continue
        result = sorted(set(out))
        self._expirations_cache[key] = (now, result)
        return result

    def list_strikes(self, symbol: str, expiration: date) -> List[float]:
        """All listed strikes for one expiration."""
        payload = self._get_json(
            "/v3/option/list/strikes",
            {
                "symbol": symbol.upper(),
                "expiration": expiration.isoformat(),
                "format": "json",
            },
        )
        if not payload:
            return []
        rows = payload.get("response") or []
        out: List[float] = []
        for row in rows:
            try:
                out.append(float(row["strike"]))
            except Exception:
                continue
        return sorted(set(out))

    def quote(
        self,
        symbol: str,
        expiration: date,
        strike: float,
        right: str,
    ) -> Optional[OptionQuote]:
        """Latest NBBO snapshot for one contract."""
        right_char = "C" if right.upper().startswith("C") else "P"
        payload = self._get_json(
            "/v3/option/snapshot/quote",
            {
                "symbol": symbol.upper(),
                "expiration": expiration.isoformat(),
                "strike": f"{float(strike):.3f}",
                "right": right_char,
                "format": "json",
            },
        )
        if not payload:
            return None
        rows = payload.get("response") or []
        if not rows:
            return None
        first = rows[0]
        contract = first.get("contract") or {}
        data = (first.get("data") or [{}])[0]
        if not data:
            return None
        return OptionQuote(
            symbol=str(contract.get("symbol") or symbol).upper(),
            expiration=expiration,
            strike=float(contract.get("strike") or strike),
            right=str(contract.get("right") or right).upper(),
            bid=float(data.get("bid") or 0.0),
            ask=float(data.get("ask") or 0.0),
            bid_size=int(data.get("bid_size") or 0),
            ask_size=int(data.get("ask_size") or 0),
            timestamp=_parse_timestamp(data.get("timestamp")),
        )

    def chain_snapshot(
        self,
        symbol: str,
        expiration: date,
    ) -> List[OptionQuote]:
        """Full chain (every strike, both rights) for one expiration in one call.

        Cached for ``CHAIN_CACHE_TTL`` seconds per (symbol, expiration). The
        bot cycle is 30s so a 10s TTL means we miss at most twice per cycle
        for any given (ticker, expiry) pair, while still reflecting fresh
        intraday quotes within a few seconds.
        """
        cache_key = (symbol.upper(), expiration.isoformat())
        now = time.monotonic()
        hit = self._chain_cache.get(cache_key)
        if hit and (now - hit[0]) < CHAIN_CACHE_TTL:
            return hit[1]
        payload = self._get_json(
            "/v3/option/snapshot/quote",
            {
                "symbol": symbol.upper(),
                "expiration": expiration.isoformat(),
                "strike": "*",
                "right": "both",
                "format": "json",
            },
        )
        if not payload:
            return []
        rows = payload.get("response") or []
        quotes: List[OptionQuote] = []
        for row in rows:
            contract = row.get("contract") or {}
            data = (row.get("data") or [{}])[0]
            if not data:
                continue
            try:
                quotes.append(OptionQuote(
                    symbol=str(contract.get("symbol") or symbol).upper(),
                    expiration=expiration,
                    strike=float(contract.get("strike") or 0.0),
                    right=str(contract.get("right") or "").upper(),
                    bid=float(data.get("bid") or 0.0),
                    ask=float(data.get("ask") or 0.0),
                    bid_size=int(data.get("bid_size") or 0),
                    ask_size=int(data.get("ask_size") or 0),
                    timestamp=_parse_timestamp(data.get("timestamp")),
                ))
            except Exception:
                continue
        # Cache even an empty list — that's still "answer for now."
        self._chain_cache[cache_key] = (now, quotes)
        # MITS Phase 8.2 — capture chain snapshot to bronze.
        try:
            from backend.bot.data import lake as _lake
            _lake.write_bronze(
                "thetadata", "chain",
                [
                    {
                        "ticker": q.symbol,
                        "expiry": q.expiration.isoformat(),
                        "strike": q.strike,
                        "right": q.right,
                        "bid": q.bid,
                        "ask": q.ask,
                        "bid_size": q.bid_size,
                        "ask_size": q.ask_size,
                        "ts": (q.timestamp.isoformat() if q.timestamp else ""),
                    }
                    for q in quotes
                ],
                ticker=symbol,
                extra_tags={"expiration": expiration.isoformat()},
                request_url="thetadata://v3/option/snapshot/quote",
                source_version=__name__,
            )
        except Exception:
            pass
        return quotes

    def chain_open_interest(
        self,
        symbol: str,
        expiration: date,
    ) -> List["OptionOpenInterest"]:
        """Batch open-interest for every contract at one expiration.

        OI is updated once per day after the OCC posts the prior-session
        figures, so a 5-minute TTL is generous; the GEX path looks up
        ``call_oi + put_oi`` per strike many times per cycle and we don't
        want to refetch every call.

        Returns an empty list on terminal failure or empty response — the
        caller's gamma×OI multiplication degrades to zero, which is what
        we want.
        """
        cache_key = (symbol.upper(), expiration.isoformat())
        now = time.monotonic()
        hit = self._oi_cache.get(cache_key)
        if hit and (now - hit[0]) < OI_CACHE_TTL:
            return hit[1]
        payload = self._get_json(
            "/v3/option/snapshot/open_interest",
            {
                "symbol": symbol.upper(),
                "expiration": expiration.isoformat(),
                "strike": "*",
                "right": "both",
                "format": "json",
            },
        )
        if not payload:
            self._oi_cache[cache_key] = (now, [])
            return []
        rows = payload.get("response") or []
        out: List["OptionOpenInterest"] = []
        for row in rows:
            contract = row.get("contract") or {}
            data = (row.get("data") or [{}])[0]
            if not data:
                continue
            try:
                oi_raw = data.get("open_interest")
                if oi_raw is None:
                    continue
                out.append(OptionOpenInterest(
                    expiration=expiration,
                    strike=float(contract.get("strike") or 0.0),
                    right=str(contract.get("right") or "").upper(),
                    open_interest=int(float(oi_raw)),
                    timestamp=_parse_timestamp(data.get("timestamp")),
                ))
            except Exception:
                continue
        self._oi_cache[cache_key] = (now, out)
        return out

    # ── selection helpers ─────────────────────────────────────────────

    def nearest_expiration(
        self,
        symbol: str,
        target_dte: int = 30,
        today: Optional[date] = None,
        min_dte: int = 1,
    ) -> Optional[date]:
        """Pick the listed expiration closest to ``target_dte`` calendar days out.

        Skips expirations strictly before ``min_dte`` so we don't pick same-day
        0DTE accidentally when a 30d target was asked for.
        """
        today = today or date.today()
        candidates = [
            e for e in self.list_expirations(symbol)
            if (e - today).days >= min_dte
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda e: abs((e - today).days - target_dte))

    def atm_strike(
        self,
        symbol: str,
        expiration: date,
        spot: float,
    ) -> Optional[float]:
        """Listed strike closest to spot."""
        strikes = self.list_strikes(symbol, expiration)
        if not strikes or spot <= 0:
            return None
        return min(strikes, key=lambda s: abs(s - spot))


# ── data-integrity sanity layer (P1.2) ─────────────────────────────────


@dataclass
class ParitySanity:
    """Verdict on whether an ATM call+put pair satisfies put-call parity.

    Parity: ``C - P = S - K·e^(-rT) - q·K``. A persistent violation means
    one leg's mid is wrong — typical with stale quotes, post-event resets,
    or vendor cleanup gaps. Hard reject when violation exceeds tolerance
    (default $0.50 OR 2% of strike, whichever is greater).
    """
    passed: bool
    deviation: Optional[float]   # signed: (C-P) - (S - Ke^-rT - qK)
    tolerance: float
    flag: Optional[str]          # "parity_violation_$X" when failed


_PARITY_TOL_ABS = 0.50
_PARITY_TOL_PCT = 0.02


def check_parity_sanity(
    call_q: "OptionQuote",
    put_q: "OptionQuote",
    *,
    spot: float,
    expiration: date,
    risk_free_rate: float,
    dividend_yield: float = 0.0,
) -> ParitySanity:
    """Run put-call parity on an ATM call/put pair (same strike, same expiry).

    Returns a verdict. Never raises — degenerate inputs (missing mids,
    negative T) yield ``passed=True`` so we don't gate on inputs we
    can't compute on.
    """
    import math as _math
    if call_q is None or put_q is None:
        return ParitySanity(passed=True, deviation=None, tolerance=0.0, flag=None)
    K = float(call_q.strike or put_q.strike or 0.0)
    if K <= 0 or spot <= 0:
        return ParitySanity(passed=True, deviation=None, tolerance=0.0, flag=None)
    C = call_q.mid
    P = put_q.mid
    if C <= 0 or P <= 0:
        return ParitySanity(passed=True, deviation=None, tolerance=0.0, flag=None)
    today_d = date.today()
    days = (expiration - today_d).days
    if days <= 0:
        return ParitySanity(passed=True, deviation=None, tolerance=0.0, flag=None)
    T = days / 365.0
    discounted_K = K * _math.exp(-float(risk_free_rate) * T)
    div_drag = float(dividend_yield) * K * T  # cont-comp approximation
    rhs = spot - discounted_K - div_drag
    lhs = C - P
    deviation = lhs - rhs
    tolerance = max(_PARITY_TOL_ABS, _PARITY_TOL_PCT * K)
    passed = abs(deviation) <= tolerance
    flag = None if passed else f"parity_violation_${abs(deviation):.2f}_tol${tolerance:.2f}"
    return ParitySanity(passed=passed, deviation=deviation,
                            tolerance=tolerance, flag=flag)


@dataclass
class IntradayIVSanity:
    """Verdict on whether ``iv_now`` is a reasonable next step from the
    rolling trailing window of recent ATM IVs for this ticker.

    Returns ``passed=False`` when the z-score against the trailing
    distribution exceeds ``_INTRADAY_Z_THRESHOLD`` — typically caused by
    one bad quote producing a wildly off IV, or a stale-quote slip the
    P1.2 staleness gate didn't catch (e.g. quotes that are fresh by
    timestamp but priced as if a session ago).
    """
    passed: bool
    flag: Optional[str]
    z_score: Optional[float]
    sample_count: int


_INTRADAY_IV_WINDOW = 30
# Z-test only fires after we have a stable trailing distribution. Below
# this floor, a 3-σ jump from 5 samples is noise (stdev is artificially
# tight) — we use an absolute % move gate instead so warm-up doesn't
# spam rejections of perfectly normal IV ticks.
_INTRADAY_IV_MIN_SAMPLES_FOR_Z = 20
_INTRADAY_IV_MIN_SAMPLES = 5
_INTRADAY_IV_Z_THRESHOLD = 5.0
# Absolute-% guard for the warm-up period. A real "bad quote" usually
# moves IV by >50% intra-tick; normal intraday IV drift is single-digit.
_INTRADAY_IV_WARMUP_ABS_PCT = 0.50

# In-process rolling window per ticker. Reset on bot restart (correct —
# overnight gaps shouldn't bias the intraday check).
from collections import deque as _deque
_INTRADAY_IV_HISTORY: Dict[str, "_deque[Tuple[float, float]]"] = {}


def check_intraday_iv_sanity(ticker: str, iv_now: float, *,
                                  now_ts: Optional[float] = None,
                                  add_to_window: bool = True
                                  ) -> IntradayIVSanity:
    """Compare ``iv_now`` to the trailing distribution of recent ATM IVs
    for ``ticker``. Returns a verdict.

    Warm-up phase: fewer than ``_INTRADAY_IV_MIN_SAMPLES`` samples ⇒
    ``passed=True``, no flag, but we DO add the sample to the window
    (so the corpus builds).

    Failure path: outlier observations are NOT added to the window — we
    don't want one bad sample to poison the trailing stdev and let the
    NEXT bad sample slip through.
    """
    import statistics
    import time as _time
    ts = now_ts if now_ts is not None else _time.time()
    if iv_now is None or iv_now <= 0:
        return IntradayIVSanity(passed=True, flag=None, z_score=None,
                                      sample_count=0)
    key = ticker.upper()
    window = _INTRADAY_IV_HISTORY.setdefault(
        key, _deque(maxlen=_INTRADAY_IV_WINDOW))
    samples = [v for _, v in window]

    if len(samples) < _INTRADAY_IV_MIN_SAMPLES:
        if add_to_window:
            window.append((ts, float(iv_now)))
        return IntradayIVSanity(passed=True, flag=None, z_score=None,
                                      sample_count=len(samples))
    # Warm-up phase (5..20 samples): the trailing stdev is too tight for
    # the z-test to mean anything (random small-sample artifacts read as
    # multi-sigma). Gate by absolute % move instead — only a doubling /
    # halving of IV is suspicious during warm-up.
    last_iv = samples[-1] if samples else float(iv_now)
    if len(samples) < _INTRADAY_IV_MIN_SAMPLES_FOR_Z:
        if last_iv > 0:
            pct_move = abs(float(iv_now) - last_iv) / last_iv
            if pct_move > _INTRADAY_IV_WARMUP_ABS_PCT:
                return IntradayIVSanity(
                    passed=False,
                    flag=f"intraday_iv_jump_pct{pct_move:.0%}",
                    z_score=None, sample_count=len(samples),
                )
        if add_to_window:
            window.append((ts, float(iv_now)))
        return IntradayIVSanity(passed=True, flag=None, z_score=None,
                                      sample_count=len(samples))
    mean = statistics.mean(samples)
    stdev = statistics.stdev(samples) if len(samples) > 1 else 0.0
    if stdev <= 0:
        if add_to_window:
            window.append((ts, float(iv_now)))
        return IntradayIVSanity(passed=True, flag=None, z_score=0.0,
                                      sample_count=len(samples))
    z = abs(float(iv_now) - mean) / stdev
    if z > _INTRADAY_IV_Z_THRESHOLD:
        return IntradayIVSanity(
            passed=False,
            flag=f"intraday_iv_jump_z{z:.1f}",
            z_score=z, sample_count=len(samples),
        )
    if add_to_window:
        window.append((ts, float(iv_now)))
    return IntradayIVSanity(passed=True, flag=None, z_score=z,
                                  sample_count=len(samples))


@dataclass
class SmileSanity:
    """Verdict on whether the IV smile around ATM looks sane.

    Approach: sample IV across N strikes near ATM, compute the median,
    flag the chain when any single strike's IV is >``OUTLIER_MULT`` x
    median or <median/``OUTLIER_MULT``. That catches the dominant
    failure mode (one bad mid produces an IV way off the smile curve
    without breaking parity).
    """
    passed: bool
    flag: Optional[str]
    iv_samples: List[Tuple[float, float]]  # [(strike, iv), …]
    median_iv: Optional[float]


_SMILE_SAMPLE_N = 5            # strikes around ATM
_SMILE_OUTLIER_MULT = 3.0


def check_smile_sanity(
    chain_quotes: List["OptionQuote"],
    *,
    spot: float,
    expiration: date,
    risk_free_rate: float,
    kind: str = "call",
) -> SmileSanity:
    """Pull ``_SMILE_SAMPLE_N`` strikes nearest spot from one side of the
    chain, compute IV per strike via BS bisection, check no single
    strike is more than ``OUTLIER_MULT`` x median IV.

    Returns ``passed=True`` when we have fewer than 3 usable samples
    (can't say anything statistically). Soft-fail philosophy: better to
    let through a chain we can't validate than gate trading on a
    statistical check we couldn't run.
    """
    if not chain_quotes or spot <= 0:
        return SmileSanity(passed=True, flag=None, iv_samples=[], median_iv=None)
    today_d = date.today()
    days = (expiration - today_d).days
    if days <= 0:
        return SmileSanity(passed=True, flag=None, iv_samples=[], median_iv=None)
    T = days / 365.0

    target_right = "CALL" if kind.lower().startswith("c") else "PUT"
    candidates = [q for q in chain_quotes if q.right == target_right and q.mid > 0]
    if len(candidates) < 3:
        return SmileSanity(passed=True, flag=None, iv_samples=[], median_iv=None)

    # Sample the N strikes closest to spot.
    candidates.sort(key=lambda q: abs(q.strike - spot))
    sample = candidates[:_SMILE_SAMPLE_N]

    try:
        from backend.bot.greeks import implied_vol
    except Exception:
        return SmileSanity(passed=True, flag=None, iv_samples=[], median_iv=None)

    samples: List[Tuple[float, float]] = []
    for q in sample:
        iv = implied_vol(q.mid, spot, q.strike, T, kind=kind.lower())
        if iv is not None and iv > 0:
            samples.append((q.strike, float(iv)))

    if len(samples) < 3:
        return SmileSanity(passed=True, flag=None, iv_samples=samples, median_iv=None)

    ivs = sorted(iv for _, iv in samples)
    median = ivs[len(ivs) // 2]
    if median <= 0:
        return SmileSanity(passed=True, flag=None, iv_samples=samples, median_iv=None)

    high = median * _SMILE_OUTLIER_MULT
    low = median / _SMILE_OUTLIER_MULT
    bad = [(s, iv) for s, iv in samples if iv > high or iv < low]
    if bad:
        flag = ("smile_outlier_strikes_"
                + ",".join(f"{s:.1f}" for s, _ in bad))
        return SmileSanity(passed=False, flag=flag,
                                iv_samples=samples, median_iv=median)
    return SmileSanity(passed=True, flag=None,
                            iv_samples=samples, median_iv=median)


@dataclass
class QuoteSanity:
    """Verdict on whether a single ``OptionQuote`` is trustworthy.

    Three fields the caller actually cares about:

    ``passed`` — strict pass/fail; ``False`` means do not use this quote
                for trading decisions.
    ``confidence`` — soft signal ``high``/``medium``/``low`` so the
                chairman + risk-sizing can incorporate uncertainty even
                when the strict gate passed.
    ``flags`` — human-readable list of what specifically tripped; goes
                into journal + UI so operator can diagnose recurring
                failure modes.
    """
    passed: bool
    confidence: str  # "high" | "medium" | "low"
    flags: List[str]
    staleness_seconds: Optional[float] = None
    spread_pct: Optional[float] = None


# Stale-quote thresholds. Tunable; defaults picked so that:
#   - during RTH, anything older than 5 min is a red flag (typical NBBO
#     updates several times per second on liquid names),
#   - off-hours, we tolerate "old" quotes that are still from the most
#     recent session — but not multi-day-old data.
STALE_RTH_SEC = 5 * 60          # 5 minutes during RTH
STALE_OFFHOURS_SEC = 18 * 3600  # 18 hours overnight / weekend

# Per-quote spread threshold — same shape as P1.4's chain_strike default.
SPREAD_REJECT_PCT = 0.20  # >20% is a quoted-but-illiquid book
SPREAD_WARN_PCT = 0.10    # 10–20% works but flag medium confidence


def _now_et() -> datetime:
    """Naive ET datetime — matches ThetaData's ET-local timestamp format."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        # Fallback: assume UTC-4 (EDT). Off by an hour in EST winter; the
        # staleness thresholds are wide enough that this doesn't matter.
        return datetime.utcnow() - timedelta(hours=4)


def check_quote_sanity(q: OptionQuote, *,
                            market_open: Optional[bool] = None) -> QuoteSanity:
    """Run staleness + spread + has-quote checks on a single ``OptionQuote``.

    ``market_open`` — if ``None``, queries the calendar module to decide.
    Callers running in market_internals context can pass it explicitly
    to avoid repeated calendar lookups.

    Returns a verdict; never raises.
    """
    flags: List[str] = []
    staleness_sec: Optional[float] = None
    spread_pct = q.spread_pct

    # 1. Has-quote check — both sides quoted at all.
    if q.bid <= 0 or q.ask <= 0:
        flags.append("no_quote")
        return QuoteSanity(
            passed=False, confidence="low", flags=flags,
            staleness_seconds=None, spread_pct=None,
        )

    # 2. Staleness gate. RTH-aware so we don't fail every quote after the close.
    if market_open is None:
        try:
            from backend.bot.calendar import is_us_market_open
            market_open = is_us_market_open()
        except Exception:
            market_open = False
    threshold = STALE_RTH_SEC if market_open else STALE_OFFHOURS_SEC
    if q.timestamp is not None:
        staleness_sec = max(0.0, (_now_et() - q.timestamp).total_seconds())
        if staleness_sec > threshold:
            flags.append(f"stale_{int(staleness_sec)}s")
    else:
        # No timestamp — opaque vintage. Treat as a soft warning, not hard fail.
        flags.append("no_timestamp")

    # 3. Spread sanity.
    if spread_pct is not None:
        if spread_pct > SPREAD_REJECT_PCT:
            flags.append(f"wide_spread_{spread_pct:.2%}")
        elif spread_pct > SPREAD_WARN_PCT:
            flags.append(f"warn_spread_{spread_pct:.2%}")

    # Score the verdict.
    hard_fail = any(f.startswith("stale_") or f.startswith("wide_spread_") for f in flags)
    soft_warn = any(f.startswith("warn_spread_") or f == "no_timestamp" for f in flags)
    if hard_fail:
        verdict = QuoteSanity(passed=False, confidence="low", flags=flags,
                                  staleness_seconds=staleness_sec, spread_pct=spread_pct)
    elif soft_warn:
        verdict = QuoteSanity(passed=True, confidence="medium", flags=flags,
                                  staleness_seconds=staleness_sec, spread_pct=spread_pct)
    else:
        verdict = QuoteSanity(passed=True, confidence="high", flags=flags,
                                  staleness_seconds=staleness_sec, spread_pct=spread_pct)
    return verdict


# ── helpers ─────────────────────────────────────────────────────────────


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        # ThetaData returns ET-local without TZ marker (e.g. "2026-06-02T15:59:53.881").
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


# ── module-level convenience (single shared client) ────────────────────


_CLIENT: Optional[ThetaDataClient] = None


def get_client() -> ThetaDataClient:
    """Process-wide shared client. Cheap to create; this just avoids
    rebuilding the requests.Session on every call."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = ThetaDataClient()
    return _CLIENT


# ── intraday IV (MITS Phase 2 — Standard-tier workaround) ──────────────


# Brenner-Subrahmanyam ATM-straddle → IV inversion constant.
# IV ≈ straddle / (k * S * sqrt(T)), with k = sqrt(2*pi)/2 ≈ 1.2533.
# (Equivalent to the inverse of the more-familiar k = sqrt(2/pi) ≈ 0.7979
# used elsewhere in the codebase — we use the operator-spec'd form here.)
import math as _math
_BRENNER_K = _math.sqrt(2.0 * _math.pi) / 2.0


def _historical_chain_quote_at(client: "ThetaDataClient", *,
                                       ticker: str, expiration: date,
                                       strike: float, right: str,
                                       at_time: datetime) -> Optional[dict]:
    """Fetch the historical NBBO quote nearest ``at_time`` for one option leg.

    Standard tier exposes ``/v3/option/history/quote`` — verified
    available on the live ThetaData Standard subscription. Returns the
    quote row closest in time to ``at_time`` (within the same trading
    day) or None if no quote is available.

    Quote rows come back as a list of ``{bid, ask, bid_size, ask_size,
    timestamp}`` dicts; we pick the one whose timestamp is closest to
    ``at_time``.
    """
    payload = client._get_json(  # noqa: SLF001 — internal call acceptable
        "/v3/option/history/quote",
        {
            "symbol": ticker.upper(),
            "expiration": expiration.isoformat(),
            "strike": f"{float(strike):.3f}",
            "right": "C" if right.upper().startswith("C") else "P",
            "start_date": at_time.date().isoformat(),
            "end_date": at_time.date().isoformat(),
            "format": "json",
        },
    )
    if not payload:
        return None
    rows = payload.get("response") or []
    if not rows:
        return None
    first = rows[0]
    data = first.get("data") or []
    if not data:
        return None

    target_ts = at_time
    best = None
    best_delta = None
    for row in data:
        try:
            row_ts_raw = row.get("timestamp")
            if not row_ts_raw:
                continue
            row_ts = _parse_timestamp(row_ts_raw)
            if row_ts is None:
                continue
            delta = abs((row_ts - target_ts).total_seconds())
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best = row
        except Exception:
            continue
    return best


def compute_intraday_iv_at(
    ticker: str,
    timestamp: datetime,
    *,
    dte_target: int = 30,
    min_dte: int = 7,
    spot: Optional[float] = None,
    client: Optional["ThetaDataClient"] = None,
    use_cache: bool = True,
    persist: bool = True,
) -> Optional[float]:
    """Compute ATM IV at ``timestamp`` via straddle inversion.

    Workaround for ThetaData Standard not exposing a historical intraday
    IV endpoint. Workflow:

      1. Lookup the SQLite cache for (ticker, timestamp) — return cached
         value if available (historical data is immutable).
      2. Pick the listed expiration closest to ``dte_target`` calendar
         days out (relative to ``timestamp.date()``).
      3. Pick the strike closest to ``spot`` (caller-supplied or fetched
         from the underlying's bar at ``timestamp``).
      4. Fetch the historical NBBO quote at ``timestamp`` for both the
         ATM call and ATM put via ``/v3/option/history/quote``.
      5. Compute the straddle = call.mid + put.mid.
      6. Invert via Brenner-Subrahmanyam:
            IV = straddle / (k * S * sqrt(T)),  k = sqrt(2*pi) / 2.
      7. Persist the result (or the failure status) to the cache so
         re-runs of the same replay are free.

    Returns ``None`` (and caches a non-ok status row) when:
      - ThetaData isn't reachable
      - no expirations / strikes available
      - no NBBO quote at the timestamp
      - inverted IV is out-of-band (<=0 or >5.0)

    Caller is expected to fall back to daily-IV carry-forward on None.
    """
    if timestamp is None or ticker is None:
        return None
    ticker = ticker.upper().strip()
    if not ticker:
        return None

    # 1. Cache lookup.
    if use_cache:
        try:
            from backend.db import session_scope
            from backend.models.intraday_iv_cache import IntradayIVCache
            from sqlalchemy import select as _select
            with session_scope() as s:
                row = s.execute(
                    _select(IntradayIVCache)
                    .where(IntradayIVCache.ticker == ticker)
                    .where(IntradayIVCache.timestamp == timestamp)
                ).scalar_one_or_none()
                if row is not None:
                    if row.status == "ok" and row.iv_atm is not None and row.iv_atm > 0:
                        return float(row.iv_atm)
                    # Non-ok cached row: don't retry; caller falls back.
                    return None
        except Exception:
            logger.debug("intraday_iv_cache lookup failed", exc_info=True)

    # 2-5. Compute via ThetaData straddle.
    iv_value: Optional[float] = None
    cache_status = "error"
    straddle_value: Optional[float] = None
    strike_used: Optional[float] = None
    expiry_used: Optional[date] = None
    dte_used: Optional[int] = None
    spot_used: Optional[float] = spot

    try:
        cl = client or get_client()
        target_date = timestamp.date()
        # Pick the expiration nearest dte_target.
        expirations = cl.list_expirations(ticker)
        candidates = [e for e in expirations
                      if (e - target_date).days >= int(min_dte)]
        if not candidates:
            cache_status = "no_quote"
            raise RuntimeError("no_expiration_candidates")
        expiry_used = min(candidates,
                          key=lambda e: abs((e - target_date).days - int(dte_target)))
        dte_used = (expiry_used - target_date).days
        if dte_used <= 0:
            cache_status = "no_quote"
            raise RuntimeError("non_positive_dte")

        # Strike: nearest listed to spot. If caller didn't supply spot,
        # caller is expected to pre-resolve it (intraday loop should
        # always have a bar's close). When absent we attempt a stock-EOD
        # fallback from ThetaData; failing that we abort (no point
        # straddle-pricing a guess).
        if spot_used is None or spot_used <= 0:
            try:
                from backend.bot.data.iv_history import _historical_closes
                closes = _historical_closes(ticker, target_date, target_date)
                spot_used = closes.get(target_date)
            except Exception:
                spot_used = None
        if not spot_used or spot_used <= 0:
            cache_status = "no_quote"
            raise RuntimeError("no_spot")

        strikes = cl.list_strikes(ticker, expiry_used)
        if not strikes:
            cache_status = "no_quote"
            raise RuntimeError("no_strikes")
        strike_used = min(strikes, key=lambda s: abs(s - float(spot_used)))

        # Fetch both legs of the straddle at the target timestamp.
        call_row = _historical_chain_quote_at(
            cl, ticker=ticker, expiration=expiry_used,
            strike=strike_used, right="C", at_time=timestamp,
        )
        put_row = _historical_chain_quote_at(
            cl, ticker=ticker, expiration=expiry_used,
            strike=strike_used, right="P", at_time=timestamp,
        )
        if not call_row or not put_row:
            cache_status = "no_quote"
            raise RuntimeError("no_chain_quote")

        def _mid(row: dict) -> float:
            bid = float(row.get("bid") or 0.0)
            ask = float(row.get("ask") or 0.0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2.0
            return 0.0

        call_mid = _mid(call_row)
        put_mid = _mid(put_row)
        if call_mid <= 0 or put_mid <= 0:
            cache_status = "no_quote"
            raise RuntimeError("zero_mid_leg")

        straddle_value = call_mid + put_mid
        T = max(1, int(dte_used)) / 365.0
        iv_value = straddle_value / (_BRENNER_K * float(spot_used) * _math.sqrt(T))
        if iv_value <= 0 or iv_value > 5.0:
            cache_status = "oob_iv"
            iv_value = None
        else:
            cache_status = "ok"
    except Exception as exc:
        if cache_status == "error":
            logger.debug("compute_intraday_iv_at failed for %s @ %s: %s",
                              ticker, timestamp, exc)

    # 6. Persist (cache failures too — historical data is immutable).
    if persist:
        try:
            from backend.db import session_scope
            from backend.models.intraday_iv_cache import IntradayIVCache
            from sqlalchemy.exc import IntegrityError
            with session_scope() as s:
                row = IntradayIVCache(
                    ticker=ticker,
                    timestamp=timestamp,
                    iv_atm=(round(iv_value, 6) if iv_value is not None else None),
                    straddle=(round(straddle_value, 4)
                                  if straddle_value is not None else None),
                    spot=(round(float(spot_used), 4)
                              if spot_used is not None else None),
                    strike=strike_used,
                    expiry=(expiry_used.isoformat() if expiry_used else None),
                    dte=dte_used,
                    status=cache_status,
                )
                try:
                    s.add(row)
                except IntegrityError:
                    # Lost a race; the other writer's row stands.
                    s.rollback()
        except Exception:
            logger.debug("intraday_iv_cache write failed", exc_info=True)

    return iv_value
