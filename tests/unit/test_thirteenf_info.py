"""MITS Phase 15 follow-up #1 — 13f.info fallback parser tests + roster
shape guard.

These tests are network-free: they exercise the pure parser/regex paths
against canned fixtures. The live backfill is validated separately
against EC2 (see the task report)."""
from __future__ import annotations

from datetime import date

import pytest


def test_parse_holdings_json_canonical_shape() -> None:
    """13f.info ships rows as a compact array; we map them into
    FundHoldingRow with value scaled from thousands to full dollars."""
    from backend.bot.data.thirteenf_info import parse_holdings_json
    payload = {
        "data": [
            # [sym, name, class, cusip, value_000, pct, shares, principal, opt]
            ["AAPL", "APPLE INC", "COM", "037833100",
             61_961_735, 22.6, 227_917_808, None, None],
            ["AXP", "AMERICAN EXPRESS CO", "COM", "025816109",
             45_859_204, 17.4, 151_610_700, None, None],
        ],
    }
    rows = parse_holdings_json(
        payload,
        fund_cik="0001067983",
        fund_name="Berkshire Hathaway Inc",
        accession="000119312526054580",
        filing_date=date(2026, 2, 17),
        quarter_end_date=date(2025, 12, 31),
        source_url="https://13f.info/13f/000119312526054580-berkshire",
    )
    assert len(rows) == 2
    aapl = next(r for r in rows if r.cusip == "037833100")
    # value_000 * 1000 = full USD
    assert aapl.value_usd == 61_961_735 * 1000.0
    assert aapl.shares == 227_917_808.0
    assert aapl.pct_of_portfolio == pytest.approx(22.6)
    assert aapl.issuer_name == "APPLE INC"


def test_parse_holdings_json_keeps_option_rows_for_parity() -> None:
    """13F-HR Information Table reports both equity AND option positions
    as line items — the SEC parser includes both, so we match that
    behavior here. Downstream consumers that want equity-only can
    filter on ``issuer_name`` or other fields."""
    from backend.bot.data.thirteenf_info import parse_holdings_json
    payload = {
        "data": [
            ["AAPL", "APPLE INC", "COM", "037833100",
             1000, 50.0, 5000, None, None],
            ["AAPL", "APPLE INC", "COM", "037833100",
             1000, 50.0, 5000, None, "CALL"],
        ],
    }
    rows = parse_holdings_json(
        payload,
        fund_cik="0001237565",
        fund_name="Test Fund",
        accession="9999",
        filing_date=date(2026, 2, 1),
        quarter_end_date=date(2025, 12, 31),
        source_url="https://example.invalid",
    )
    assert len(rows) == 2
    assert all(r.cusip == "037833100" for r in rows)
    # Pct comes straight from 13f.info — sums to 100% across both rows.
    assert sum(r.pct_of_portfolio or 0 for r in rows) == pytest.approx(100.0)


def test_parse_holdings_json_recomputes_missing_pct() -> None:
    """When 13f.info ships pct=null we recompute from value totals so
    every row downstream has a usable pct_of_portfolio."""
    from backend.bot.data.thirteenf_info import parse_holdings_json
    payload = {
        "data": [
            ["AAA", "ALPHA CORP", "COM", "111111111",
             100, None, 1, None, None],
            ["BBB", "BETA INC", "COM", "222222222",
             300, None, 1, None, None],
        ],
    }
    rows = parse_holdings_json(
        payload, fund_cik="0001",
        fund_name="x", accession="x",
        filing_date=date(2026, 1, 1),
        quarter_end_date=date(2025, 12, 31),
        source_url="https://example.invalid",
    )
    pcts = {r.cusip: r.pct_of_portfolio for r in rows}
    assert pcts["111111111"] == pytest.approx(25.0)
    assert pcts["222222222"] == pytest.approx(75.0)


def test_index_row_regex_parses_filings_table() -> None:
    """Sanity-check the regex against the live HTML row shape we
    captured on 2026-06-11."""
    from backend.bot.data.thirteenf_info import _INDEX_ROW_RE
    fragment = (
        '<td class="px-3 py-2 text-center" data-order="2026-03-31">\n'
        '            <a href="/13f/000119312526226661-berkshire-hathaway-inc-q1-2026">Q1 2026</a>\n'
        '          </td>'
    )
    matches = list(_INDEX_ROW_RE.finditer(fragment))
    assert len(matches) == 1
    m = matches[0]
    assert m.group(1) == "2026-03-31"
    assert m.group(2) == "000119312526226661"
    assert m.group(3) == "berkshire-hathaway-inc-q1-2026"


def test_date_filed_regex_extracts_us_date() -> None:
    from backend.bot.data.thirteenf_info import _DATE_FILED_RE, _parse_us_date
    fragment = (
        '<dt class="text-sm font-medium text-gray-500">\n'
        '        Date filed\n'
        '      </dt>\n'
        '      <dd class="mt-1 text-sm text-gray-900 sm:mt-0 sm:col-span-2">\n'
        '        5/15/2026\n'
        '      </dd>'
    )
    m = _DATE_FILED_RE.search(fragment)
    assert m is not None
    assert _parse_us_date(m.group(1)) == date(2026, 5, 15)


def test_manager_slug_is_url_safe() -> None:
    from backend.bot.data.thirteenf_info import _manager_slug
    assert _manager_slug("Berkshire Hathaway Inc") == "berkshire-hathaway-inc"
    assert _manager_slug("AT&T Inc.") == "at-t-inc"
    assert _manager_slug("  ") == ""


def test_roster_has_minimum_size_and_required_funds() -> None:
    """The 13F backfill targets at least 30 distinct fund CIKs (operator
    gate A). Keep that invariant in the roster so a future accidental
    truncation here trips the test."""
    from backend.bot.data.watched_funds import (
        load_watched_funds,
        watched_fund_ciks,
    )
    ciks = watched_fund_ciks()
    assert len(ciks) >= 30, (
        f"roster shrank to {len(ciks)} CIKs; smart-money depends on >=30"
    )
    # The operator-table notable smart-money filers — must stay present.
    required = {
        "0001067983",  # Berkshire
        "0001037389",  # Renaissance
        "0001167557",  # AQR (post 2026-06-11 rename from Adage)
        "0001179392",  # Two Sigma
        "0001350694",  # Bridgewater
        "0001423053",  # Citadel
        "0001009207",  # D.E. Shaw
        "0001273087",  # Millennium
        "0001603466",  # Point72
        "0001167483",  # Tiger Global
        "0001697748",  # ARK
        "0001336528",  # Pershing Square
        "0001029160",  # Soros
    }
    missing = required - set(ciks)
    assert not missing, f"required notable filers missing: {sorted(missing)}"

    # Check the post-2026-06-11 mislabel renames stuck.
    by_cik = {f.cik: f.name for f in load_watched_funds()}
    assert by_cik["0001167557"].startswith("AQR"), (
        f"0001167557 must be AQR (not Adage); got {by_cik['0001167557']}")
    assert "Coatue" in by_cik.get("0001135730", ""), (
        f"0001135730 must be Coatue; got {by_cik.get('0001135730')}")
