# Trading Bot

Autonomous local trading bot for Robinhood with a FastAPI backend and React dashboard.
Trades stocks and options, adapts to technicals, news, fundamentals, and sentiment, and
exposes a real-time UI for risk controls and strategy selection.

> **Warning** — Robinhood has no official API. `robin_stocks` is an unofficial library and
> automated trading may violate Robinhood's terms of service. Use at your own risk. Default
> mode is `PAPER_MODE=true`; switching to live trading is a deliberate, explicit step.

## Setup

```bash
cp .env.example .env
# fill in credentials and API keys in .env
./run.sh
```

The script installs Python and Node dependencies, builds the React UI, runs the test
suite, and (if all tests pass) launches the API on http://localhost:8000.

## Switching to live trading

1. Confirm you understand the risk and have a small experimental amount of capital.
2. Edit `.env` and set `PAPER_MODE=false`.
3. Restart the server.
4. Watch the dashboard closely on the first session — the daily-loss circuit breaker is
   your safety net, not your strategy.

## Project layout

```
backend/   FastAPI app, bot engine, strategies, signals, risk, executor
frontend/  React + Vite dashboard
tests/     pytest unit / integration / e2e suite (mocked Robinhood)
```

## Components

- **Strategies**: momentum, mean reversion, news-driven, options flow, adaptive selector,
  and a free-text custom rule parser.
- **Signals**: technical (RSI, MACD, Bollinger, MAs, volume), news (NewsAPI + VADER),
  fundamentals (P/E, EPS, revenue via yfinance), sentiment aggregation.
- **Risk**: per-trade max size, daily-loss circuit breaker, stop loss / take profit,
  buying-power and max-position caps, intraday EOD auto-close.
- **Scheduler**: pre-market scan, 5-minute intraday loop, EOD summary, weekend/holiday
  skip.
- **Dashboard**: live P&L, risk controls, strategy and signal toggles, custom-rule editor,
  WebSocket activity log.

## Running tests

```bash
pytest tests/ --cov=backend --cov-report=term-missing
```

The suite mocks Robinhood, yfinance, and NewsAPI — it never hits the network.

## Environment variables

See `.env.example` for the full list. Most important:

| Variable | Purpose |
| --- | --- |
| `ROBINHOOD_USERNAME` / `_PASSWORD` | Robinhood login |
| `ROBINHOOD_MFA_SECRET` | Optional TOTP base32 secret for automated MFA |
| `NEWS_API_KEY` | NewsAPI key for news sentiment |
| `PAPER_MODE` | `true` to log-only, `false` to send live orders |
| `DEFAULT_TICKERS` | Comma-separated tickers the bot scans |
| `DB_PATH` | SQLite file path |
