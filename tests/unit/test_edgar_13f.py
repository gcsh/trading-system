"""MITS Phase 11.E — 13F parser + smart_money aggregator tests."""
from __future__ import annotations

from datetime import date

import pytest


SAMPLE_INFOTABLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>12345678</value>
    <shrsOrPrnAmt>
      <sshPrnamt>1000000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>1000000</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
  <infoTable>
    <nameOfIssuer>MICROSOFT CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>594918104</cusip>
    <value>9876543</value>
    <shrsOrPrnAmt>
      <sshPrnamt>500000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>500000</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
</informationTable>
"""


def test_parse_information_table() -> None:
    from backend.bot.data.edgar_13f import parse_information_table
    rows = parse_information_table(
        SAMPLE_INFOTABLE_XML.encode("utf-8"),
        fund_cik="0001067983",
        fund_name="Berkshire Hathaway Inc",
        accession="0001-24-001",
        filing_date=date(2024, 11, 14),
        quarter_end_date=date(2024, 9, 30),
        source_url="https://example.com/13f",
    )
    assert len(rows) == 2
    by_cusip = {r.cusip: r for r in rows}
    assert "037833100" in by_cusip
    aapl = by_cusip["037833100"]
    assert aapl.shares == 1000000.0
    # Filing post 2023-01-01 → value not scaled (already dollars).
    assert aapl.value_usd == 12345678.0
    # Total = 22222221; AAPL pct = 12345678/22222221 ~ 55.55
    assert aapl.pct_of_portfolio is not None
    assert abs(aapl.pct_of_portfolio - 55.5556) < 0.01


def test_parse_information_table_pre_2023_scales_value() -> None:
    """For filings before 2023-01-01 the SEC reporting unit was
    'thousands' — our parser multiplies by 1000."""
    from backend.bot.data.edgar_13f import parse_information_table
    rows = parse_information_table(
        SAMPLE_INFOTABLE_XML.encode("utf-8"),
        fund_cik="0001067983",
        fund_name="Berkshire Hathaway Inc",
        accession="0001-22-001",
        filing_date=date(2022, 5, 16),
        quarter_end_date=date(2022, 3, 31),
        source_url="https://example.com/13f",
    )
    assert len(rows) == 2
    aapl = next(r for r in rows if r.cusip == "037833100")
    assert aapl.value_usd == 12345678.0 * 1000


def test_smart_money_summary(temp_db, monkeypatch) -> None:
    from backend.bot.data.edgar_13f import (
        FundHoldingRow, write_fund_holdings,
    )
    from backend.bot.features.smart_money import smart_money_summary

    # Pin the watched-fund CIK list so the test doesn't depend on the
    # production JSON roster.
    import backend.bot.features.smart_money as smod
    monkeypatch.setattr(smod, "_watched_cik_set",
                          lambda: {"0001067983", "0001350694"})

    rows = [
        FundHoldingRow(
            fund_cik="0001067983", fund_name="Berkshire Hathaway Inc",
            quarter_end_date=date(2024, 9, 30),
            cusip="037833100", issuer_name="APPLE INC",
            ticker="AAPL",
            shares=1000000.0, value_usd=200_000_000.0,
            pct_of_portfolio=55.0,
            filing_date=date(2024, 11, 14),
            accession_number="0001-24-001",
            source_url="https://example.com",
        ),
        FundHoldingRow(
            fund_cik="0001350694", fund_name="Bridgewater Associates LP",
            quarter_end_date=date(2024, 9, 30),
            cusip="037833100", issuer_name="APPLE INC",
            ticker="AAPL",
            shares=50000.0, value_usd=10_000_000.0,
            pct_of_portfolio=2.5,
            filing_date=date(2024, 11, 14),
            accession_number="0002-24-001",
            source_url="https://example.com",
        ),
    ]
    assert write_fund_holdings(rows) == 2
    summary = smart_money_summary("AAPL", as_of_quarter_end=date(2024, 9, 30))
    assert summary.funds_holding == 2
    assert summary.top5_avg_pct_portfolio > 0
    assert len(summary.top_holders) == 2


def test_write_fund_holdings_dedupe_within_batch(temp_db) -> None:
    """Two rows with the same (fund_cik, quarter_end_date, cusip) inside
    a single batch are deduplicated, not crashed. This is the regression
    test for the EC2 backfill crash where amendments re-emitting the
    same quarter's positions tanked the entire transaction."""
    from backend.bot.data.edgar_13f import (
        FundHoldingRow, write_fund_holdings,
    )

    rows = [
        FundHoldingRow(
            fund_cik="0001067983", fund_name="Berkshire Hathaway Inc",
            quarter_end_date=date(2024, 9, 30),
            cusip="037833100", issuer_name="APPLE INC",
            ticker="AAPL",
            shares=1000000.0, value_usd=200_000_000.0,
            pct_of_portfolio=55.0,
            filing_date=date(2024, 11, 14),
            accession_number="0001-24-001",
            source_url="https://example.com/v1",
        ),
        FundHoldingRow(
            fund_cik="0001067983", fund_name="Berkshire Hathaway Inc",
            quarter_end_date=date(2024, 9, 30),
            cusip="037833100", issuer_name="APPLE INC",
            ticker="AAPL",
            # Same key — amendment with updated share count. Dedupe
            # keeps the first occurrence; the second is a no-op.
            shares=1000001.0, value_usd=200_001_000.0,
            pct_of_portfolio=55.1,
            filing_date=date(2024, 11, 15),
            accession_number="0001-24-001A",
            source_url="https://example.com/v2",
        ),
    ]
    inserted = write_fund_holdings(rows)
    # Both rows share the same UQ key — exactly one new row lands.
    assert inserted == 1


def test_write_fund_holdings_idempotent_on_rerun(temp_db) -> None:
    """Re-writing the same batch must not crash and must not double-
    insert. INSERT OR IGNORE makes the second pass a 0-row no-op."""
    from backend.bot.data.edgar_13f import (
        FundHoldingRow, write_fund_holdings,
    )

    rows = [
        FundHoldingRow(
            fund_cik="0001067983", fund_name="Berkshire Hathaway Inc",
            quarter_end_date=date(2024, 6, 30),
            cusip="037833100", issuer_name="APPLE INC", ticker="AAPL",
            shares=1000000.0, value_usd=200_000_000.0,
            pct_of_portfolio=55.0,
            filing_date=date(2024, 8, 14),
            accession_number="0001-24-002",
            source_url="https://example.com",
        ),
    ]
    first = write_fund_holdings(rows)
    second = write_fund_holdings(rows)  # idempotent rerun
    assert first == 1
    assert second == 0  # existing row touched via UPDATE path, no new INSERT


def test_write_fund_holdings_mixed_new_and_dup(temp_db) -> None:
    """A batch with one already-present row + one new row inserts only
    the new row and does not poison the transaction."""
    from backend.bot.data.edgar_13f import (
        FundHoldingRow, write_fund_holdings,
    )

    pre_existing = FundHoldingRow(
        fund_cik="0001067983", fund_name="Berkshire Hathaway Inc",
        quarter_end_date=date(2024, 9, 30),
        cusip="037833100", issuer_name="APPLE INC", ticker="AAPL",
        shares=1000000.0, value_usd=200_000_000.0, pct_of_portfolio=55.0,
        filing_date=date(2024, 11, 14),
        accession_number="0001-24-001",
        source_url="https://example.com",
    )
    write_fund_holdings([pre_existing])

    fresh = FundHoldingRow(
        fund_cik="0001067983", fund_name="Berkshire Hathaway Inc",
        quarter_end_date=date(2024, 9, 30),
        cusip="594918104", issuer_name="MICROSOFT CORP", ticker="MSFT",
        shares=500000.0, value_usd=100_000_000.0, pct_of_portfolio=25.0,
        filing_date=date(2024, 11, 14),
        accession_number="0001-24-001",
        source_url="https://example.com",
    )
    # Re-pass the pre-existing row + a brand-new one. Only the new one
    # increments the insert counter.
    inserted = write_fund_holdings([pre_existing, fresh])
    assert inserted == 1
