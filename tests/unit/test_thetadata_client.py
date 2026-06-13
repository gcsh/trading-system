"""ThetaData v3 client + options.py provider-chain integration."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import backend.bot.data.options as opts
from backend.bot.data.thetadata import OptionQuote, ThetaDataClient


# ── shared fake-session helper ─────────────────────────────────────────


def _make_session(responses):
    """Build a fake requests.Session returning canned (status, body) tuples
    keyed by URL path. ``responses`` is a list of (path, status, json_body).
    The matcher looks for the path as a substring of the request URL."""

    def get(url, params=None, timeout=None):
        for path, status, body in responses:
            if path in url:
                resp = MagicMock()
                resp.status_code = status
                resp.json.return_value = body
                resp.text = "" if not body else str(body)[:200]
                return resp
        # default: 404
        resp = MagicMock()
        resp.status_code = 404
        resp.text = "not stubbed"
        return resp

    sess = MagicMock()
    sess.get.side_effect = get
    return sess


# ── client tests ────────────────────────────────────────────────────────


def test_list_expirations_parses_iso_dates():
    sess = _make_session([
        ("/v3/option/list/expirations", 200, {
            "response": [
                {"symbol": "AAPL", "expiration": "2026-06-19"},
                {"symbol": "AAPL", "expiration": "2026-07-17"},
                {"symbol": "AAPL", "expiration": "2026-08-21"},
            ]
        })
    ])
    c = ThetaDataClient(session=sess)
    exps = c.list_expirations("AAPL")
    assert exps == [date(2026, 6, 19), date(2026, 7, 17), date(2026, 8, 21)]


def test_list_expirations_caches_subsequent_calls():
    """The 1h cache must mean the second call doesn't re-fetch — same
    test sees a single HTTP hit even when called twice."""
    sess = _make_session([
        ("/v3/option/list/expirations", 200, {
            "response": [
                {"symbol": "AAPL", "expiration": "2026-06-19"},
                {"symbol": "AAPL", "expiration": "2026-07-17"},
            ]
        })
    ])
    c = ThetaDataClient(session=sess)
    first = c.list_expirations("AAPL")
    second = c.list_expirations("AAPL")
    third = c.list_expirations("AAPL")
    assert first == second == third == [date(2026, 6, 19), date(2026, 7, 17)]
    # Only one underlying HTTP call despite 3 invocations.
    assert sess.get.call_count == 1


def test_list_expirations_does_not_cache_failures():
    """If the first call returns nothing, the next call should retry —
    don't poison the cache with empty results."""
    sess = MagicMock()
    # First call returns 503, second returns 200.
    resp_fail = MagicMock(status_code=503, text="boom")
    resp_ok = MagicMock(status_code=200, text="ok")
    resp_ok.json.return_value = {"response": [{"expiration": "2026-06-19"}]}
    sess.get.side_effect = [resp_fail, resp_ok]
    c = ThetaDataClient(session=sess)
    first = c.list_expirations("AAPL")
    second = c.list_expirations("AAPL")
    assert first == []
    assert second == [date(2026, 6, 19)]
    assert sess.get.call_count == 2  # the failure was retried


def test_list_expirations_returns_empty_on_failure():
    sess = _make_session([])
    c = ThetaDataClient(session=sess)
    assert c.list_expirations("AAPL") == []


def test_quote_parses_response_shape():
    sess = _make_session([
        ("/v3/option/snapshot/quote", 200, {
            "response": [{
                "contract": {"symbol": "AAPL", "strike": 200.0,
                             "right": "CALL", "expiration": "2026-06-19"},
                "data": [{
                    "bid": 5.40, "ask": 5.55, "bid_size": 12, "ask_size": 8,
                    "timestamp": "2026-06-02T15:59:59.000",
                }],
            }]
        })
    ])
    c = ThetaDataClient(session=sess)
    q = c.quote("AAPL", date(2026, 6, 19), 200.0, "C")
    assert q is not None
    assert q.bid == 5.40 and q.ask == 5.55
    assert q.mid == 5.475
    assert q.right == "CALL"
    # Spread sanity (used by sanity layer in P1.2): (0.15) / 5.475 ≈ 0.0274
    assert q.spread_pct is not None and 0.02 < q.spread_pct < 0.04


def test_quote_returns_none_on_472_no_data():
    sess = _make_session([("/v3/option/snapshot/quote", 472, None)])
    c = ThetaDataClient(session=sess)
    assert c.quote("AAPL", date(2026, 6, 19), 200.0, "C") is None


def test_nearest_expiration_skips_zero_dte():
    today = date(2026, 6, 2)
    sess = _make_session([
        ("/v3/option/list/expirations", 200, {
            "response": [
                {"expiration": today.isoformat()},         # 0DTE — skipped
                {"expiration": "2026-06-19"},              # 17d out
                {"expiration": "2026-07-17"},              # 45d out
            ]
        })
    ])
    c = ThetaDataClient(session=sess)
    # target=30 → 17d is closer than 45d
    assert c.nearest_expiration("AAPL", target_dte=30, today=today) == date(2026, 6, 19)


def test_atm_strike_picks_closest_to_spot():
    sess = _make_session([
        ("/v3/option/list/strikes", 200, {
            "response": [
                {"strike": 195.0}, {"strike": 200.0},
                {"strike": 205.0}, {"strike": 210.0},
            ]
        })
    ])
    c = ThetaDataClient(session=sess)
    assert c.atm_strike("AAPL", date(2026, 6, 19), 203.0) == 205.0


# ── options.py integration tests ───────────────────────────────────────


def test_options_snapshot_prefers_thetadata_when_configured(monkeypatch):
    """When OPTIONS_PROVIDER=thetadata_first, the thetadata path runs first
    and yfinance is not consulted on success."""
    monkeypatch.setenv("OPTIONS_PROVIDER", "thetadata_first")
    monkeypatch.setattr(opts, "_atm_from_thetadata", lambda t, s: {
        "iv_atm": 0.32, "implied_move": 0.045, "dte": 28,
        "expiry": "2026-06-30", "source": "thetadata",
    })

    def yfinance_must_not_be_called(*a, **kw):
        raise AssertionError("yfinance was called even though thetadata succeeded")

    monkeypatch.setattr(opts, "_atm_from_yfinance", yfinance_must_not_be_called)
    monkeypatch.setattr(opts, "_atm_from_cboe", yfinance_must_not_be_called)
    monkeypatch.setattr(opts, "_earnings", lambda t: (10, False))
    opts._CACHE.clear()

    snap = opts.options_snapshot("AAPL", 200.0)
    assert snap["has_options"] is True
    assert snap["options_source"] == "thetadata"
    assert snap["iv_atm"] == 0.32


def test_options_snapshot_falls_back_when_thetadata_returns_none(monkeypatch):
    """When thetadata returns None (terminal down, no quote), the chain
    falls through to the next provider. WARN.4 (2026-06-04) inserted
    Alpaca between thetadata and yfinance; this test now also stubs the
    Alpaca path to None so the chain reaches yfinance the way the test
    intended."""
    monkeypatch.setenv("OPTIONS_PROVIDER", "thetadata_first")
    monkeypatch.setattr(opts, "_atm_from_thetadata", lambda t, s: None)
    # Stub the Alpaca provider too — module may or may not be importable
    # depending on env; patch via the loader hook either way.
    try:
        from backend.bot.data import alpaca_options as alp
        monkeypatch.setattr(alp, "atm_from_alpaca", lambda t, s: None)
    except Exception:
        pass
    monkeypatch.setattr(opts, "_atm_from_yfinance", lambda t, s: {
        "iv_atm": 0.45, "implied_move": 0.06, "dte": 30,
        "expiry": "2026-07-17", "source": "yfinance",
    })
    monkeypatch.setattr(opts, "_atm_from_cboe", lambda t, s: None)
    monkeypatch.setattr(opts, "_earnings", lambda t: (999, False))
    opts._CACHE.clear()
    # WARN.4 added per-ticker yfinance backoff; reset so a prior test in
    # the suite that triggered a 429 doesn't skip this attempt.
    opts._YF_NEXT_ATTEMPT.clear()
    opts._YF_BACKOFF_SECONDS.clear()

    snap = opts.options_snapshot("AAPL", 200.0)
    assert snap["has_options"] is True
    assert snap["options_source"] == "yfinance"


def test_options_snapshot_env_yfinance_skips_thetadata(monkeypatch):
    """OPTIONS_PROVIDER=yfinance disables the thetadata path entirely —
    useful for the side-by-side diff log (P1.5) and for tests."""
    monkeypatch.setenv("OPTIONS_PROVIDER", "yfinance")

    def thetadata_must_not_be_called(*a, **kw):
        raise AssertionError("thetadata called when env=yfinance")

    monkeypatch.setattr(opts, "_atm_from_thetadata", thetadata_must_not_be_called)
    monkeypatch.setattr(opts, "_atm_from_yfinance", lambda t, s: {
        "iv_atm": 0.40, "implied_move": 0.05, "dte": 30,
        "expiry": "2026-07-17", "source": "yfinance",
    })
    monkeypatch.setattr(opts, "_atm_from_cboe", lambda t, s: None)
    monkeypatch.setattr(opts, "_earnings", lambda t: (999, False))
    opts._CACHE.clear()
    opts._YF_NEXT_ATTEMPT.clear()
    opts._YF_BACKOFF_SECONDS.clear()

    snap = opts.options_snapshot("AAPL", 200.0)
    assert snap["options_source"] == "yfinance"


def test_chain_strike_picks_liquid_listed_strike(monkeypatch):
    """When chain returns mixed quoted/unquoted strikes, the picker prefers
    the listed strike closest to target that has both bid and ask > 0
    and a spread within bounds."""
    from datetime import date
    from backend.bot.data import options as opts_mod
    from backend.bot.data import thetadata as td

    # Chain: $310 strike with no quote, $315 wide spread, $320 liquid.
    # Target = spot * (1 - 0.05) = 100 * 0.95 = 95 (put). All three strikes
    # are above target, so distance ordering is 310 < 315 < 320. But $310
    # has no quote, $315 spread is 30% (bad), $320 spread is 2% (good).
    chain = [
        OptionQuote(symbol="AAPL", expiration=date(2026, 6, 30), strike=310.0,
                    right="PUT", bid=0.0, ask=0.0, bid_size=0, ask_size=0, timestamp=None),
        OptionQuote(symbol="AAPL", expiration=date(2026, 6, 30), strike=315.0,
                    right="PUT", bid=1.00, ask=1.40, bid_size=5, ask_size=5, timestamp=None),
        OptionQuote(symbol="AAPL", expiration=date(2026, 6, 30), strike=320.0,
                    right="PUT", bid=2.20, ask=2.25, bid_size=50, ask_size=50, timestamp=None),
    ]
    fake_client = SimpleNamespace(
        nearest_expiration=lambda sym, target_dte=30: date(2026, 6, 30),
        chain_snapshot=lambda sym, exp: chain,
    )
    monkeypatch.setattr(td, "get_client", lambda: fake_client)

    # spot=100 puts target at 95. Among liquid strikes, $320 is closest to 95
    # (well, all three are above 95). $315 wide-spread excluded, $310 no-quote
    # excluded, so $320 wins.
    strike = opts_mod.chain_strike("AAPL", spot=100.0, kind="put",
                                       moneyness=-0.05, max_spread_pct=0.05)
    assert strike == 320.0


def test_chain_strike_falls_back_to_snap_when_chain_empty(monkeypatch):
    """If terminal returns no qualifying strikes, fall back to arithmetic
    snap_strike — never raise, never return zero."""
    from datetime import date
    from backend.bot.data import options as opts_mod
    from backend.bot.data import thetadata as td

    fake_client = SimpleNamespace(
        nearest_expiration=lambda sym, target_dte=30: date(2026, 6, 30),
        chain_snapshot=lambda sym, exp: [],  # empty chain
    )
    monkeypatch.setattr(td, "get_client", lambda: fake_client)

    # Should fall back to snap_strike(100, "put", -0.05) = round arithmetic.
    strike = opts_mod.chain_strike("AAPL", spot=100.0, kind="put", moneyness=-0.05)
    # snap_strike with spot 100 and -5% moneyness → target 95, snapped to $1 increment.
    assert strike == 95.0


def test_chain_strike_falls_back_when_thetadata_raises(monkeypatch):
    """Network/connection errors must fall back, not bubble up."""
    from backend.bot.data import options as opts_mod
    from backend.bot.data import thetadata as td

    def boom():
        raise ConnectionError("terminal down")

    monkeypatch.setattr(td, "get_client", boom)
    strike = opts_mod.chain_strike("AAPL", spot=200.0, kind="call", moneyness=0.05)
    assert strike == opts_mod.snap_strike(200.0, "call", 0.05)


def test_chain_expiry_returns_none_on_failure(monkeypatch):
    """chain_expiry returns None on terminal failure so caller picks
    arithmetic date.today() + N days."""
    from backend.bot.data import options as opts_mod
    from backend.bot.data import thetadata as td

    def boom():
        raise RuntimeError("not reachable")

    monkeypatch.setattr(td, "get_client", boom)
    assert opts_mod.chain_expiry("AAPL", target_dte=30) is None


# ── P1.2 sanity layer tests ─────────────────────────────────────────────


def _quote(*, bid=2.40, ask=2.60, ts_offset_min=1.0, bid_size=10, ask_size=10):
    """Build an OptionQuote with `ts_offset_min` minutes of staleness from
    ET-now (per ThetaData's ET-local timestamps)."""
    from datetime import datetime, timedelta
    from backend.bot.data.thetadata import _now_et
    return OptionQuote(
        symbol="AAPL", expiration=date(2026, 6, 30), strike=200.0, right="CALL",
        bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size,
        timestamp=_now_et() - timedelta(minutes=ts_offset_min),
    )


def test_sanity_passes_fresh_tight_spread():
    from backend.bot.data.thetadata import check_quote_sanity
    v = check_quote_sanity(_quote(bid=2.40, ask=2.50, ts_offset_min=1),
                                market_open=True)
    assert v.passed is True and v.confidence == "high"
    assert v.flags == []


def test_sanity_fails_stale_during_rth():
    """During RTH a 10-minute-old quote is a hard reject (default 5min)."""
    from backend.bot.data.thetadata import check_quote_sanity
    v = check_quote_sanity(_quote(ts_offset_min=10), market_open=True)
    assert v.passed is False
    assert v.confidence == "low"
    assert any(f.startswith("stale_") for f in v.flags)


def test_sanity_accepts_stale_when_market_closed():
    """Off-hours, 10-minute-old quote is fine — typical of post-close
    snapshots. The off-hours threshold is 18h."""
    from backend.bot.data.thetadata import check_quote_sanity
    v = check_quote_sanity(_quote(ts_offset_min=10), market_open=False)
    assert v.passed is True


def test_sanity_rejects_wide_spread():
    """30% spread = collapsed/illiquid book; hard reject."""
    from backend.bot.data.thetadata import check_quote_sanity
    v = check_quote_sanity(_quote(bid=1.00, ask=1.50, ts_offset_min=1),  # spread = ~40%
                                market_open=True)
    assert v.passed is False
    assert any(f.startswith("wide_spread_") for f in v.flags)


def test_sanity_medium_for_warn_spread():
    """12% spread is a soft warn — passes but confidence drops."""
    from backend.bot.data.thetadata import check_quote_sanity
    # bid=2.00, ask=2.30 → mid=2.15, spread = 0.30/2.15 ≈ 13.9%
    v = check_quote_sanity(_quote(bid=2.00, ask=2.30, ts_offset_min=1),
                                market_open=True)
    assert v.passed is True
    assert v.confidence == "medium"
    assert any(f.startswith("warn_spread_") for f in v.flags)


def test_sanity_rejects_no_quote():
    """bid=0 ask=0 → no-quote, hard reject regardless of timestamp."""
    from backend.bot.data.thetadata import check_quote_sanity
    v = check_quote_sanity(_quote(bid=0.0, ask=0.0, ts_offset_min=1),
                                market_open=True)
    assert v.passed is False
    assert "no_quote" in v.flags


def test_atm_from_thetadata_skips_stale_quotes(monkeypatch):
    """The atm path should return None when sanity rejects either leg,
    so the provider chain falls through to yfinance."""
    from datetime import timedelta
    from backend.bot.data import options as opts_mod
    from backend.bot.data import thetadata as td
    from backend.bot.data.thetadata import _now_et

    # Call: fresh quote. Put: 30 min stale during RTH → reject.
    stale_put = OptionQuote(
        symbol="AAPL", expiration=date(2026, 6, 30), strike=100.0, right="PUT",
        bid=2.40, ask=2.60, bid_size=10, ask_size=10,
        timestamp=_now_et() - timedelta(minutes=30),
    )
    fresh_call = OptionQuote(
        symbol="AAPL", expiration=date(2026, 6, 30), strike=100.0, right="CALL",
        bid=2.40, ask=2.60, bid_size=10, ask_size=10,
        timestamp=_now_et() - timedelta(minutes=1),
    )

    fake_client = SimpleNamespace(
        nearest_expiration=lambda sym, target_dte=30: date(2026, 6, 30),
        chain_snapshot=lambda sym, exp: [fresh_call, stale_put],
    )
    monkeypatch.setattr(td, "get_client", lambda: fake_client)
    # Force market_open=True so the stale gate fires.
    monkeypatch.setattr("backend.bot.calendar.is_us_market_open", lambda: True)

    atm = opts_mod._atm_from_thetadata("AAPL", spot=100.0)
    assert atm is None  # sanity rejected → caller falls back


def test_atm_from_thetadata_carries_confidence_high(monkeypatch):
    """Clean quotes → data_confidence=high in the returned atm dict."""
    import math
    from datetime import timedelta
    from backend.bot.data import options as opts_mod
    from backend.bot.data import thetadata as td
    from backend.bot.data.thetadata import _now_et

    ts = _now_et() - timedelta(seconds=10)
    chain = [
        OptionQuote(symbol="AAPL", expiration=date(2026, 7, 2), strike=100.0,
                    right="CALL", bid=2.40, ask=2.50, bid_size=20, ask_size=20,
                    timestamp=ts),
        OptionQuote(symbol="AAPL", expiration=date(2026, 7, 2), strike=100.0,
                    right="PUT", bid=2.40, ask=2.50, bid_size=20, ask_size=20,
                    timestamp=ts),
    ]

    fake_client = SimpleNamespace(
        nearest_expiration=lambda sym, target_dte=30: date(2026, 7, 2),
        chain_snapshot=lambda sym, exp: chain,
    )
    monkeypatch.setattr(td, "get_client", lambda: fake_client)
    monkeypatch.setattr("backend.bot.calendar.is_us_market_open", lambda: True)
    # Don't let live yfinance dividend yield (or a network failure) bias
    # the put-call parity check — pin it for the test.
    monkeypatch.setattr(opts_mod, "_dividend_yield", lambda t: 0.0)

    atm = opts_mod._atm_from_thetadata("AAPL", spot=100.0)
    assert atm is not None
    assert atm["data_confidence"] == "high"
    # First call ever, so the intraday IV self-regression window is warming
    # up — soft "warmup" flag is expected. No hard rejection flags.
    hard_flags = [f for f in atm["sanity_flags"]
                       if not f.startswith("intraday_iv_warmup")]
    assert hard_flags == []


def test_atm_from_thetadata_computes_iv_via_brenner_subrahmanyam(monkeypatch):
    """Verifies the BS straddle inversion: a $5 ATM straddle on a $100 stock
    with 30 DTE should imply ~σ ≈ 5 / (0.7979 × 100 × √(30/365)) ≈ 0.219."""
    import math
    from backend.bot.data import options as opts_mod
    from backend.bot.data import thetadata as td

    bs_chain = [
        OptionQuote(symbol="AAPL", expiration=date(2026, 7, 2), strike=100.0,
                    right="CALL", bid=2.40, ask=2.60, bid_size=10, ask_size=10,
                    timestamp=None),
        OptionQuote(symbol="AAPL", expiration=date(2026, 7, 2), strike=100.0,
                    right="PUT", bid=2.40, ask=2.60, bid_size=10, ask_size=10,
                    timestamp=None),
    ]
    fake_client = SimpleNamespace(
        nearest_expiration=lambda sym, target_dte=30: date(2026, 7, 2),
        chain_snapshot=lambda sym, exp: bs_chain,
    )
    # Pin dividend yield so put-call parity sees a deterministic RHS.
    monkeypatch.setattr(opts_mod, "_dividend_yield", lambda t: 0.0)
    # Patch get_client in the thetadata module so the import inside options.py picks it up.
    monkeypatch.setattr(td, "get_client", lambda: fake_client)

    today = date(2026, 6, 2)
    # Freeze "today" so DTE math is deterministic.
    monkeypatch.setattr(opts_mod, "date", SimpleNamespace(today=lambda: today))

    atm = opts_mod._atm_from_thetadata("AAPL", spot=100.0)
    assert atm is not None
    assert atm["source"] == "thetadata"
    assert atm["dte"] == 30
    # straddle = 2.50 + 2.50 = 5.00 → implied_move = 5/100 = 0.05
    assert atm["implied_move"] == 0.05
    expected_iv = round(5.0 / (math.sqrt(2.0 / math.pi) * 100.0 * math.sqrt(30 / 365.0)), 4)
    assert atm["iv_atm"] == expected_iv
