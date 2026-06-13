"""Finnhub client unit tests with a fake httpx client."""
from unittest.mock import MagicMock

from backend.bot.data.finnhub import FinnhubClient, FinnhubQuote


def _client_with(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    http = MagicMock()
    http.get.return_value = response
    return FinnhubClient(api_key="fake", http_client=http), http


def test_unavailable_when_no_key():
    client = FinnhubClient(api_key="")
    assert client.available is False
    assert client.quote("AAPL") is None


def test_quote_parses_response():
    payload = {"c": 150.5, "h": 152, "l": 149, "o": 151, "pc": 149.5, "t": 1_700_000_000}
    client, http = _client_with(payload)
    quote = client.quote("AAPL")
    assert isinstance(quote, FinnhubQuote)
    assert quote.price == 150.5
    assert quote.prev_close == 149.5


def test_cache_hits_avoid_double_call():
    payload = {"c": 1, "h": 1, "l": 1, "o": 1, "pc": 1, "t": 0}
    client, http = _client_with(payload)
    client.quote("AAPL")
    client.quote("AAPL")
    assert http.get.call_count == 1


def test_company_news_returns_list():
    payload = [{"headline": "Some headline", "datetime": 0}]
    client, _ = _client_with(payload)
    assert client.company_news("AAPL")[0]["headline"] == "Some headline"


def test_company_news_handles_non_list_response():
    client, _ = _client_with({"error": "rate limited"})
    assert client.company_news("AAPL") == []


def test_upcoming_earnings_returns_first_event():
    payload = {"earningsCalendar": [{"symbol": "AAPL", "date": "2026-07-01"}]}
    client, _ = _client_with(payload)
    event = client.upcoming_earnings("AAPL")
    assert event["symbol"] == "AAPL"


def test_upcoming_earnings_handles_missing():
    client, _ = _client_with({})
    assert client.upcoming_earnings("AAPL") is None
