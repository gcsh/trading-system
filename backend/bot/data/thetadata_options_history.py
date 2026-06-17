"""MITS Phase 11.B.2 — ThetaData v3 EOD option chain history backfill.

Endpoint reality (probed live against operator's terminal 2026-06-09):

  - ``/v3/option/list/expirations?symbol=AAPL`` — every historical
    expiration, CSV ``symbol,expiration`` (date as ``YYYY-MM-DD``).
  - ``/v3/option/list/strikes?symbol=AAPL&expiration=YYYYMMDD`` —
    strikes for that expiry, CSV ``symbol,strike``. Strikes are
    DECIMAL DOLLARS (e.g. ``130.000``), NOT ``× 1000``.
  - ``/v3/option/history/eod?symbol=AAPL&expiration=YYYYMMDD&strike=DOLLARS&right=C&start_date=YYYYMMDD&end_date=YYYYMMDD``
    — daily EOD bars for one contract. JSON envelope:
    ``{"response": [{"contract": {...}, "data": [{...row...}, ...]}]}``.
    Each row has ``open/high/low/close/volume/count/bid/ask`` + a
    timestamp. ThetaData sends 2 snapshot rows per trading day (one
    18:30 ET, one 20:30 ET on the same ``last_trade`` date) — we
    dedupe on ``last_trade`` date.

  - ``/v3/option/bulk_history/eod`` and several other ``bulk_*``
    shapes return HTTP 404 on the operator's Standard terminal. This
    module ONLY uses per-contract calls. The orchestrator's token
    bucket paces them under the rate ceiling.

Storage model
=============

Silver: :class:`OptionContractBar` (PK ``(ticker, expiration, strike,
right, bar_date)``) — INSERT OR IGNORE on duplicate keys so re-running
a chunk is a no-op.

Bronze: parquet partitioned ``bronze/thetadata_options_eod/dt=<fetch>/
ticker=<T>/expiration=<E>/contracts.parquet``. Includes the raw
ThetaData payload + the (strike, right) tuple so the operator can
re-derive silver rows offline.

Strike windowing
================

Pulling EVERY strike on EVERY expiry over 5y blows past the rate
budget. We anchor to ATM-at-expiry-listing by looking up the closest
``stock_bars`` daily close on the FIRST day the contract was listed
(or, if absent, on the ``start_date`` of the requested window) and
keep ±``TUNABLES.options_eod_atm_strike_window`` strikes on either
side. Default 15 strikes = ~30 contracts per expiry per right = ~12k
calls per ticker for 200 expirations × 2 rights — ~17h at 8 rps
across 40 tickers.

The orchestrator drives the chunking at the (ticker, expiry) grain
— one BackfillProgress row per (ticker, expiry) so a crash mid-
backfill resumes from the next un-finished expiry.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.bot.data.sync_orchestrator import CallbackResult
from backend.bot.data.thetadata_stocks import SubscriptionError
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.option_contract_bar import OptionContractBar
from backend.models.stock_bar import StockBar

logger = logging.getLogger(__name__)


def _base_url() -> str:
    port = int(getattr(TUNABLES, "thetadata_port", 25503))
    return f"http://127.0.0.1:{port}"


def _timeout() -> float:
    return float(getattr(TUNABLES, "thetadata_timeout_sec", 30.0))


def _atm_window_count() -> int:
    """How many strikes on each side of ATM to keep. Config knob so
    the operator can dial coverage vs. cost. Default 15 = ~30
    contracts per expiry per right."""
    return max(
        1,
        int(getattr(TUNABLES, "options_eod_atm_strike_window", 15)),
    )


def _per_contract_concurrency() -> int:
    """Parallelism within a single (ticker, expiry) chunk. The
    SyncOrchestrator token bucket still gates total RPS. Default 2."""
    return max(
        1,
        int(getattr(TUNABLES, "options_eod_per_contract_workers", 2)),
    )


# ── HTTP ──────────────────────────────────────────────────────────────


def _http_get(path: str, params: Dict[str, Any]) -> Tuple[int, str]:
    import requests
    url = f"{_base_url()}{path}"
    resp = requests.get(url, params=params, timeout=_timeout())
    return (resp.status_code, resp.text)


def _parse_csv(body: str) -> List[Dict[str, str]]:
    if not body or not body.strip():
        return []
    reader = csv.DictReader(io.StringIO(body))
    return [r for r in reader]


def _coerce_date_iso(s: Any) -> Optional[date]:
    if s in (None, ""):
        return None
    raw = str(s).strip().strip('"')
    try:
        # ThetaData returns YYYY-MM-DD for list endpoints.
        return datetime.fromisoformat(raw[:10]).date()
    except Exception:
        return None


def _coerce_yyyymmdd(s: Any) -> Optional[date]:
    raw = str(s or "").strip().strip('"')
    if not raw:
        return None
    if len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").date()
        except Exception:
            return None
    return _coerce_date_iso(raw)


def _coerce_float(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_int(v: Any) -> Optional[int]:
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ── public discovery ──────────────────────────────────────────────────


def fetch_expirations(ticker: str) -> List[date]:
    """All historical option expirations for ``ticker``. CSV
    ``symbol,expiration`` — empty list on a no-options ticker or a
    miss."""
    status, body = _http_get("/v3/option/list/expirations", {
        "symbol": ticker.upper(),
    })
    if status == 403:
        raise SubscriptionError(
            f"thetadata expirations 403 for {ticker}: {body[:200]}"
        )
    if status in (404, 472):
        return []
    if status != 200:
        raise RuntimeError(
            f"thetadata list/expirations failed status={status} "
            f"ticker={ticker} body={body[:200]}"
        )
    out: List[date] = []
    for row in _parse_csv(body):
        d = _coerce_date_iso(row.get("expiration"))
        if d:
            out.append(d)
    return sorted(set(out))


def fetch_strikes(ticker: str, expiration: date) -> List[float]:
    """All strikes for one expiry, returned as DECIMAL DOLLARS."""
    status, body = _http_get("/v3/option/list/strikes", {
        "symbol": ticker.upper(),
        "expiration": expiration.strftime("%Y%m%d"),
    })
    if status == 403:
        raise SubscriptionError(
            f"thetadata strikes 403 for {ticker} exp={expiration}: "
            f"{body[:200]}"
        )
    if status in (404, 472):
        return []
    if status != 200:
        raise RuntimeError(
            f"thetadata list/strikes failed status={status} ticker={ticker} "
            f"exp={expiration} body={body[:200]}"
        )
    out: List[float] = []
    for row in _parse_csv(body):
        f = _coerce_float(row.get("strike"))
        if f is not None:
            out.append(f)
    return sorted(set(out))


# ── per-contract EOD ──────────────────────────────────────────────────


def fetch_contract_history(ticker: str, expiration: date,
                           strike: float, right: str,
                           start: date, end: date,
                           ) -> List[Dict[str, Any]]:
    """EOD history for one contract. Returns NORMALIZED row dicts —
    one per trading day (ThetaData sends 2 snapshot rows per day; we
    dedupe to the latest snapshot keyed on ``last_trade`` date).

    ``right`` accepts "C" / "CALL" / "P" / "PUT" — normalized to
    "C"/"P" on the wire.
    """
    r = right.upper()
    wire_right = "C" if r in ("C", "CALL") else "P"
    status, body = _http_get("/v3/option/history/eod", {
        "symbol": ticker.upper(),
        "expiration": expiration.strftime("%Y%m%d"),
        "strike": str(strike),
        "right": wire_right,
        "start_date": start.strftime("%Y%m%d"),
        "end_date": end.strftime("%Y%m%d"),
        "format": "json",
    })
    if status in (404, 472):
        return []
    if status == 403:
        raise SubscriptionError(
            f"thetadata option history 403 ticker={ticker} exp={expiration} "
            f"strike={strike} right={wire_right}: {body[:200]}"
        )
    if status != 200:
        raise RuntimeError(
            f"thetadata option history failed status={status} "
            f"ticker={ticker} exp={expiration} strike={strike} "
            f"right={wire_right} body={body[:200]}"
        )
    try:
        envelope = json.loads(body)
    except Exception:
        return []
    raw_rows: List[Dict[str, Any]] = []
    for blob in envelope.get("response") or []:
        if not isinstance(blob, dict):
            continue
        data = blob.get("data")
        if not isinstance(data, list):
            continue
        for d in data:
            if isinstance(d, dict):
                raw_rows.append(d)
    # Dedupe by last_trade DATE — keep the latest ``created`` snapshot
    # so we get end-of-day quotes, not the mid-afternoon refresh.
    by_date: Dict[date, Tuple[str, Dict[str, Any]]] = {}
    for r_row in raw_rows:
        d_iso = r_row.get("last_trade") or r_row.get("date")
        d = _coerce_date_iso(d_iso) or _coerce_yyyymmdd(d_iso)
        if d is None:
            continue
        created = str(r_row.get("created") or "")
        existing = by_date.get(d)
        if existing is None or created > existing[0]:
            by_date[d] = (created, r_row)
    normalized: List[Dict[str, Any]] = []
    for bar_date, (_, r_row) in sorted(by_date.items()):
        bid = _coerce_float(r_row.get("bid"))
        ask = _coerce_float(r_row.get("ask"))
        close = _coerce_float(r_row.get("close"))
        mid: Optional[float] = None
        if bid is not None and ask is not None and ask >= bid >= 0:
            mid = (bid + ask) / 2.0
        elif close is not None:
            mid = close
        normalized.append({
            "ticker": ticker.upper(),
            "expiration": expiration,
            "strike": float(strike),
            "right": wire_right,
            "bar_date": bar_date,
            "open": _coerce_float(r_row.get("open")),
            "high": _coerce_float(r_row.get("high")),
            "low": _coerce_float(r_row.get("low")),
            "close": close,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "volume": _coerce_int(r_row.get("volume")),
            "trade_count": _coerce_int(r_row.get("count")),
        })
    return normalized


# ── strike windowing ──────────────────────────────────────────────────


def _spot_at(ticker: str, anchor: date) -> Optional[float]:
    """Closest daily close from ``stock_bars`` for ``ticker`` at or
    just before ``anchor``. Used to pick ATM ±N strikes. Returns
    ``None`` if stock bars haven't been backfilled yet — caller falls
    back to "all strikes" in that case.
    """
    try:
        with session_scope() as s:
            row = s.execute(
                select(StockBar.bar_ts, StockBar.close)
                .where(StockBar.ticker == ticker.upper())
                .where(StockBar.interval == "1d")
                .where(StockBar.bar_ts <= datetime.combine(
                    anchor, datetime.max.time()))
                .order_by(StockBar.bar_ts.desc())
                .limit(1)
            ).first()
            if row and row[1] is not None:
                return float(row[1])
    except Exception:
        logger.debug("_spot_at failed for %s %s",
                     ticker, anchor, exc_info=True)
    return None


def _select_strike_window(strikes: List[float], spot: Optional[float],
                          count_each_side: int) -> List[float]:
    if not strikes:
        return []
    if spot is None or count_each_side <= 0:
        return strikes
    if not strikes:
        return []
    # Find the ATM index, then take ±count_each_side.
    sorted_strikes = sorted(strikes)
    # Binary search for nearest.
    lo, hi = 0, len(sorted_strikes) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_strikes[mid] < spot:
            lo = mid + 1
        else:
            hi = mid
    atm_idx = lo
    # Pick the closer of [lo-1, lo].
    if atm_idx > 0:
        if abs(sorted_strikes[atm_idx] - spot) > abs(
                sorted_strikes[atm_idx - 1] - spot):
            atm_idx -= 1
    start = max(0, atm_idx - count_each_side)
    end = min(len(sorted_strikes), atm_idx + count_each_side + 1)
    return sorted_strikes[start:end]


# ── silver writer ─────────────────────────────────────────────────────


def write_silver_option_bars(rows: Iterable[Dict[str, Any]]) -> int:
    """INSERT OR IGNORE bulk write to :class:`OptionContractBar`.

    The UniqueConstraint on (ticker, expiration, strike, right,
    bar_date) silently drops duplicates so a chunk re-run is a no-op.
    Returns the number of NEW rows actually written.
    """
    row_list = [r for r in rows if r and r.get("bar_date") is not None]
    if not row_list:
        return 0
    inserted = 0
    try:
        with session_scope() as s:
            for r in row_list:
                stmt = sqlite_insert(OptionContractBar).values(
                    ticker=r["ticker"],
                    expiration=r["expiration"],
                    strike=float(r["strike"]),
                    right=r["right"],
                    bar_date=r["bar_date"],
                    open=r.get("open"),
                    high=r.get("high"),
                    low=r.get("low"),
                    close=r.get("close"),
                    bid=r.get("bid"),
                    ask=r.get("ask"),
                    mid=r.get("mid"),
                    iv=r.get("iv"),
                    delta=r.get("delta"),
                    gamma=r.get("gamma"),
                    vega=r.get("vega"),
                    theta=r.get("theta"),
                    volume=r.get("volume"),
                    open_interest=r.get("open_interest"),
                    trade_count=r.get("trade_count"),
                    source=r.get("source", "thetadata"),
                ).on_conflict_do_nothing(
                    index_elements=[
                        "ticker", "expiration", "strike",
                        "right", "bar_date",
                    ],
                )
                result = s.execute(stmt)
                inserted += int(result.rowcount or 0)
    except Exception:
        logger.exception("write_silver_option_bars failed")
    return inserted


# ── bronze writer ─────────────────────────────────────────────────────


def write_bronze_option_bars(ticker: str, expiration: date,
                             rows: List[Dict[str, Any]],
                             *, chunk_start: date,
                             chunk_end: date) -> None:
    """Persist raw rows under
    ``bronze/thetadata_options_eod/dt=<fetch_date>/ticker=<T>/expiration=<exp>/...``.
    Fire-and-forget — bronze writes never block the orchestrator."""
    if not rows:
        return
    try:
        from backend.bot.data import lake as _lake
        # Convert dates → ISO strings so the parquet writer doesn't
        # choke on python ``date`` objects.
        payload = []
        for r in rows:
            row = dict(r)
            for k in ("expiration", "bar_date"):
                v = row.get(k)
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()
            payload.append(row)
        _lake.write_bronze(
            source="thetadata",
            dtype="options_eod",
            payload=payload,
            ticker=ticker,
            extra_tags={
                "expiration": expiration.isoformat(),
                "chunk_start": chunk_start.isoformat(),
                "chunk_end": chunk_end.isoformat(),
            },
            request_url="thetadata://v3/option/history/eod",
            source_version=__name__,
        )
    except Exception:
        logger.debug("options bronze write failed for %s %s [%s,%s]",
                     ticker, expiration, chunk_start, chunk_end,
                     exc_info=True)


# ── orchestrator callback ─────────────────────────────────────────────


def _parse_expiry_token(token: str) -> date:
    """Tickers feeding the orchestrator carry the expiry encoded in
    their identifier — e.g. ``AAPL|20210618``. The standard ticker
    callback signature is ``(ticker, chunk_start, chunk_end)`` so we
    smuggle the expiry through the ticker slot. The launcher script
    does the encoding.
    """
    if "|" not in token:
        raise ValueError(
            f"options EOD callback expects 'TICKER|YYYYMMDD' tokens, "
            f"got {token!r}"
        )
    _, exp_raw = token.split("|", 1)
    d = _coerce_yyyymmdd(exp_raw)
    if d is None:
        raise ValueError(
            f"options EOD callback could not parse expiry token "
            f"from {token!r}"
        )
    return d


def options_eod_backfill_callback(token: str, chunk_start: date,
                                  chunk_end: date) -> CallbackResult:
    """One orchestrator chunk = one (ticker, expiry) over
    ``[chunk_start, chunk_end]``. Lists strikes, trims to the ATM
    window via ``stock_bars``, fans out per-contract calls across the
    thread pool, writes silver + bronze, returns rows written.

    The token format is ``"TICKER|YYYYMMDD"`` — see the launcher
    script for the encoder.
    """
    if "|" not in token:
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=0,
            metadata={"reason": "missing_expiry_in_token"},
        )
    ticker_raw, _ = token.split("|", 1)
    ticker = ticker_raw.upper().strip()
    expiration = _parse_expiry_token(token)

    # Skip windows that are clearly outside the contract's lifetime.
    if chunk_start > expiration:
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=0,
            metadata={"reason": "chunk_after_expiry"},
        )

    effective_end = min(chunk_end, expiration)
    # The contract trades roughly the year before expiry; we cap
    # ``effective_start`` to one year before expiration to avoid
    # asking ThetaData for a window the contract didn't exist in.
    one_year_pre = expiration - timedelta(days=420)
    effective_start = max(chunk_start, one_year_pre)
    if effective_start > effective_end:
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=0,
            metadata={"reason": "empty_window_after_clamp"},
        )

    # 1. List strikes for this expiry.
    try:
        strikes = fetch_strikes(ticker, expiration)
    except SubscriptionError:
        raise
    except Exception as exc:
        logger.warning("options_eod: fetch_strikes failed %s %s: %s",
                       ticker, expiration, exc)
        raise
    if not strikes:
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=0,
            metadata={"reason": "no_strikes"},
        )

    # 2. Anchor to ATM via stock_bars (close at effective_start).
    spot = _spot_at(ticker, effective_start) or _spot_at(ticker, expiration)
    selected = _select_strike_window(strikes, spot, _atm_window_count())
    if not selected:
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=0,
            metadata={"reason": "no_strikes_in_window",
                      "spot": spot, "total_strikes": len(strikes)},
        )

    # 3. Fan out per-contract calls (calls + puts).
    work = [(s, r) for s in selected for r in ("C", "P")]
    rows_all: List[Dict[str, Any]] = []
    errors = 0

    def _one(strike: float, right: str) -> List[Dict[str, Any]]:
        return fetch_contract_history(
            ticker, expiration, strike, right,
            effective_start, effective_end,
        )

    pool_size = _per_contract_concurrency()
    if pool_size > 1 and len(work) > 1:
        with ThreadPoolExecutor(max_workers=pool_size,
                                thread_name_prefix="ocb-fetch") as pool:
            futures = [pool.submit(_one, s, r) for s, r in work]
            for f in as_completed(futures):
                try:
                    rows_all.extend(f.result())
                except SubscriptionError:
                    raise
                except Exception:
                    errors += 1
                    logger.debug("per-contract fetch failed",
                                 exc_info=True)
    else:
        for s, r in work:
            try:
                rows_all.extend(_one(s, r))
            except SubscriptionError:
                raise
            except Exception:
                errors += 1
                logger.debug("per-contract fetch failed",
                             exc_info=True)

    written = write_silver_option_bars(rows_all)
    write_bronze_option_bars(
        ticker, expiration, rows_all,
        chunk_start=effective_start, chunk_end=effective_end,
    )

    last_complete = max(
        (r["bar_date"] for r in rows_all if r.get("bar_date")),
        default=effective_end,
    )
    return CallbackResult(
        last_completed_date=last_complete,
        rows_written=written,
        metadata={
            "expiration": expiration.isoformat(),
            "strikes_total": len(strikes),
            "strikes_selected": len(selected),
            "contracts_attempted": len(work),
            "rows_fetched": len(rows_all),
            "errors": errors,
            "spot_anchor": spot,
        },
    )


# ── tracking-list helper ──────────────────────────────────────────────


def list_active_expiration_tokens(ticker: str,
                                  history_start: date,
                                  history_end: date,
                                  ) -> List[str]:
    """Build the orchestrator token list for ``ticker`` — one token
    per expiration that had any trading days inside
    ``[history_start, history_end]``. Tokens are encoded as
    ``"TICKER|YYYYMMDD"`` so the existing
    ``SyncOrchestrator.bulk_backfill(source, ticker, ...)`` signature
    can carry the (ticker, expiry) tuple.
    """
    expirations = fetch_expirations(ticker)
    tokens: List[str] = []
    one_year_pre_end = history_end - timedelta(days=420)
    for exp in expirations:
        # Contract had to overlap the window: lifetime is roughly
        # [exp - 420d, exp] (LEAPs live longer; weeklies less). We
        # keep an expiry if its lifetime overlaps the requested
        # window.
        life_start = exp - timedelta(days=420)
        life_end = exp
        if life_end < history_start:
            continue
        if life_start > history_end:
            continue
        tokens.append(f"{ticker.upper()}|{exp.strftime('%Y%m%d')}")
    return tokens


__all__ = [
    "fetch_expirations",
    "fetch_strikes",
    "fetch_contract_history",
    "write_silver_option_bars",
    "write_bronze_option_bars",
    "options_eod_backfill_callback",
    "list_active_expiration_tokens",
]
