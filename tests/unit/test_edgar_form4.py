"""MITS Phase 11.E — Form 4 parser + persistence tests.

Coverage:
  1. XML parser produces one InsiderTrade per ``<nonDerivativeTransaction>``
     block with the right transaction code, shares, price, and role
     flags.
  2. write_insider_trades is idempotent on the unique-constraint key.
  3. insider_signal aggregators compute net + role-weighted notionals
     across a 30-day window.
"""
from __future__ import annotations

from datetime import date

import pytest


SAMPLE_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>Apple Inc.</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001214156</rptOwnerCik>
      <rptOwnerName>Cook Timothy D</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>1</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-08-15</value></transactionDate>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>225.50</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-08-15</value></transactionDate>
      <transactionCoding>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>226.00</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def test_parse_form4_xml() -> None:
    from backend.bot.data.edgar_form4 import _parse_form4_xml
    rows = _parse_form4_xml(
        SAMPLE_FORM4_XML.encode("utf-8"),
        ticker="AAPL", cik="0000320193",
        accession_number="0000000000-00-000000",
        filing_date=date(2024, 8, 16),
        source_url="https://example.com",
    )
    assert len(rows) == 2
    buys = [r for r in rows if r.transaction_code == "P"]
    sells = [r for r in rows if r.transaction_code == "S"]
    assert len(buys) == 1 and len(sells) == 1
    assert buys[0].insider_name == "Cook Timothy D"
    assert buys[0].insider_role == "Chief Executive Officer"
    assert buys[0].is_officer is True
    assert buys[0].is_director is True
    assert buys[0].shares == 1000.0
    assert buys[0].price == 225.50
    assert buys[0].total_value == 225500.0


def test_write_insider_trades_idempotent(temp_db) -> None:
    from backend.bot.data.edgar_form4 import (
        Form4Transaction, write_insider_trades,
    )
    rows = [
        Form4Transaction(
            ticker="AAPL", cik="0000320193",
            accession_number="0001214156-24-000123",
            filing_date=date(2024, 8, 16),
            transaction_date=date(2024, 8, 15),
            insider_name="Cook Timothy D",
            insider_role="Chief Executive Officer",
            transaction_code="P",
            shares=1000.0, price=225.5, total_value=225500.0,
            is_director=True, is_officer=True, is_10pct_owner=False,
            source_url="https://example.com",
        ),
    ]
    n1 = write_insider_trades(rows)
    n2 = write_insider_trades(rows)
    assert n1 == 1
    assert n2 == 0


def test_insider_signal_aggregators(temp_db) -> None:
    from datetime import datetime
    from backend.bot.data.edgar_form4 import (
        Form4Transaction, write_insider_trades,
    )
    from backend.bot.features.insider_signal import (
        insider_summary, net_insider_purchase_30d,
        role_weighted_net_purchase,
    )
    today = date.today()
    recent = today  # within 30d
    rows = [
        Form4Transaction(
            ticker="AAPL", cik="0000320193",
            accession_number="0001-24-1",
            filing_date=recent, transaction_date=recent,
            insider_name="CEO PERSON",
            insider_role="Chief Executive Officer",
            transaction_code="P",
            shares=1000.0, price=200.0, total_value=200000.0,
            is_director=False, is_officer=True, is_10pct_owner=False,
            source_url="x"),
        Form4Transaction(
            ticker="AAPL", cik="0000320193",
            accession_number="0001-24-2",
            filing_date=recent, transaction_date=recent,
            insider_name="DIRECTOR PERSON",
            insider_role="Director",
            transaction_code="P",
            shares=500.0, price=200.0, total_value=100000.0,
            is_director=True, is_officer=False, is_10pct_owner=False,
            source_url="x"),
        Form4Transaction(
            ticker="AAPL", cik="0000320193",
            accession_number="0001-24-3",
            filing_date=recent, transaction_date=recent,
            insider_name="SELLER",
            insider_role="VP Engineering",
            transaction_code="S",
            shares=200.0, price=200.0, total_value=40000.0,
            is_director=False, is_officer=True, is_10pct_owner=False,
            source_url="x"),
    ]
    assert write_insider_trades(rows) == 3
    # Net = 200000 + 100000 - 40000 = 260000
    net = net_insider_purchase_30d("AAPL")
    assert abs(net - 260000.0) < 0.01
    # Role-weighted = 200000*3 (CEO) + 100000*1 (Director) - 40000*1.5 (Officer)
    #               = 600000 + 100000 - 60000 = 640000
    weighted = role_weighted_net_purchase("AAPL")
    assert abs(weighted - 640000.0) < 0.01
    summary = insider_summary("AAPL")
    assert summary.total_buys == 2
    assert summary.total_sells == 1
    assert summary.cluster_count_buyers == 2


def test_write_insider_trades_dedupe_within_batch(temp_db) -> None:
    """Two transactions with the same UQ key inside a single batch are
    deduplicated, not crashed. Regression for the EC2 backfill where
    Form 4 amendments re-emit prior lines and tanked the transaction."""
    from backend.bot.data.edgar_form4 import (
        Form4Transaction, write_insider_trades,
    )

    base = Form4Transaction(
        ticker="AAPL", cik="0000320193",
        accession_number="0001-24-99",
        filing_date=date(2024, 8, 15),
        transaction_date=date(2024, 8, 15),
        insider_name="Cook Timothy D",
        insider_role="Chief Executive Officer",
        transaction_code="P",
        shares=1000.0, price=225.5, total_value=225500.0,
        is_director=True, is_officer=True, is_10pct_owner=False,
        source_url="https://example.com",
    )
    # Same key — amendment re-emitting the same line. Counter sees 1.
    assert write_insider_trades([base, base]) == 1


def test_form4_cik_cache_hydration(monkeypatch) -> None:
    """Hydration populates the in-process CIK cache from SEC's JSON
    blob so subsequent resolutions don't fan out into 40 parallel
    fetches. Verifies the cache lookup works for both dot and dash
    class-share variants."""
    import backend.bot.data.edgar_form4 as ef
    fake_payload = (
        b'{"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."},'
        b'"1":{"cik_str":1067983,"ticker":"BRK-B","title":"Berkshire"}}'
    )

    def _fake_http_get(url: str):
        assert "company_tickers.json" in url
        return (200, fake_payload)

    # Reset cache state
    ef._CIK_CACHE.clear()
    ef._CIK_CACHE_HYDRATED = False
    monkeypatch.setattr(ef, "_http_get", _fake_http_get)

    cik = ef._resolve_cik("AAPL")
    assert cik == "0000320193"
    # Class-share dot form falls back to the dash form in the SEC map.
    brk = ef._resolve_cik("BRK.B")
    assert brk == "0001067983"
    # Cleanup so other tests aren't perturbed.
    ef._CIK_CACHE.clear()
    ef._CIK_CACHE_HYDRATED = False


def test_form4_cik_hydration_retries_then_fails_gracefully(monkeypatch) -> None:
    """If SEC ticker map fetch keeps returning 429, hydration retries
    up to the configured budget and then leaves the cache empty without
    raising. The per-call EdgarClient fallback path still has a chance."""
    import backend.bot.data.edgar_form4 as ef

    calls = {"n": 0}

    def _always_429(url: str):
        calls["n"] += 1
        return (429, b"<html>Request Rate Threshold Exceeded</html>")

    ef._CIK_CACHE.clear()
    ef._CIK_CACHE_HYDRATED = False
    monkeypatch.setattr(ef, "_http_get", _always_429)
    # Slash the retry budget to keep the test snappy.
    monkeypatch.setattr(ef.TUNABLES, "sec_ticker_map_retry_attempts", 2)
    monkeypatch.setattr(ef.TUNABLES, "sec_ticker_map_retry_base_sec", 0.01)
    # Stub the EdgarClient fallback so the test isolates hydration.
    import backend.bot.data.edgar as edgar_pkg

    class _NullClient:
        def ticker_to_cik(self, t):
            return None

    monkeypatch.setattr(edgar_pkg, "EdgarClient", lambda: _NullClient())

    cik = ef._resolve_cik("AAPL")
    assert cik is None
    # Two retry attempts hit the 429 path.
    assert calls["n"] == 2
    ef._CIK_CACHE.clear()
    ef._CIK_CACHE_HYDRATED = False
