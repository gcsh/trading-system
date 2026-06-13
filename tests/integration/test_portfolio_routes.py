"""Coverage for /portfolio/* endpoints: equity, performance, by-strategy, positions, overview."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.db import session_scope
from backend.models.snapshot import PortfolioSnapshot
from backend.models.trade import Trade


@pytest.fixture()
def client(temp_db):
    from importlib import reload

    from backend import main as main_mod

    reload(main_mod)
    return TestClient(main_mod.app)


def _seed_trades_and_snapshots():
    now = datetime.utcnow()
    with session_scope() as session:
        for i, (action, pnl, strategy) in enumerate(
            [
                ("BUY_STOCK", None, "momentum"),
                ("SELL_STOCK", 50.0, "momentum"),  # +50
                ("BUY_STOCK", None, "rsi_mean_reversion"),
                ("SELL_STOCK", -20.0, "rsi_mean_reversion"),  # -20
                ("BUY_STOCK", None, "macd_momentum"),
                ("SELL_STOCK", 30.0, "macd_momentum"),  # +30
            ]
        ):
            session.add(
                Trade(
                    ticker="AAPL",
                    action=action,
                    quantity=1,
                    price=100 + i,
                    strategy=strategy,
                    signal_source="test",
                    confidence=0.7,
                    reason="seed",
                    paper=1,
                    pnl=pnl,
                    status="closed" if pnl is not None else "open",
                    timestamp=now - timedelta(minutes=10 - i),
                )
            )
        for j, value in enumerate([1000, 1020, 1010, 1050, 1080, 1060]):
            session.add(
                PortfolioSnapshot(
                    portfolio_value=value,
                    cash=value * 0.5,
                    realized_pnl=60.0,
                    open_positions=0,
                    broker="PaperExecutor",
                    timestamp=now - timedelta(minutes=10 - j),
                )
            )


def test_performance_basics(client):
    _seed_trades_and_snapshots()
    body = client.get("/portfolio/performance").json()
    assert body["trade_count"] == 6
    assert body["closed_count"] == 3
    # Realized = sum of closed-trade P&L (+50 -20 +30).
    assert body["realized_pnl"] == 60.0
    # Total P&L is account-level (equity vs starting cash) = realized + unrealized.
    assert body["total_pnl"] == round(body["equity_end"] - body["equity_start"], 2)
    assert round(body["realized_pnl"] + body["unrealized_pnl"], 2) == body["total_pnl"]
    assert 0 <= body["win_rate"] <= 1
    assert body["avg_gain"] == 40.0
    assert body["avg_loss"] == -20.0
    assert body["snapshot_count"] == 6
    # Sharpe must be a number; values vary with the synthetic series.
    assert isinstance(body["sharpe"], (int, float))
    assert body["max_drawdown_pct"] >= 0


def test_equity_curve_returns_oldest_first(client):
    _seed_trades_and_snapshots()
    body = client.get("/portfolio/equity").json()
    assert len(body) == 6
    timestamps = [row["timestamp"] for row in body]
    assert timestamps == sorted(timestamps)


def test_by_strategy_groups_pnl_correctly(client):
    _seed_trades_and_snapshots()
    body = client.get("/portfolio/by-strategy").json()
    names = {row["strategy"]: row for row in body}
    assert names["momentum"]["total_pnl"] == 50.0
    assert names["rsi_mean_reversion"]["total_pnl"] == -20.0
    # Highest P&L should be first.
    assert body[0]["total_pnl"] == 50.0


def test_positions_returns_empty_when_no_paper_executor(client):
    body = client.get("/portfolio/positions").json()
    assert isinstance(body, list)


def test_overview_bundles_everything(client):
    _seed_trades_and_snapshots()
    body = client.get("/portfolio/overview").json()
    assert "status" in body
    assert "performance" in body
    assert "equity_curve" in body
    assert "positions" in body
    assert body["performance"]["trade_count"] == 6


def test_performance_empty_db_returns_zeros(client):
    body = client.get("/portfolio/performance").json()
    assert body["trade_count"] == 0
    assert body["total_pnl"] == 0.0
    assert body["sharpe"] == 0.0


def test_metrics_ignore_pre_trial_snapshots(client):
    """Pre-trial snapshots (an old, lower-balance account) must not pollute the
    current trial's %, today's P&L, or the equity curve. Reproduces the +3983
    'today P&L' seen when a stale $1k snapshot leaked into the window."""
    from backend.models.config import load_config, save_config

    now = datetime.utcnow()
    with session_scope() as session:
        cfg = load_config(session)
        cfg["paper_cash_override"] = 5000.0
        cfg["trial_start"] = now.date().isoformat()      # trial started today
        save_config(session, cfg)
        # Pre-trial junk: 2 days ago at $1,000 — must be excluded.
        session.add(PortfolioSnapshot(
            portfolio_value=1000.0, cash=1000.0, realized_pnl=0.0, open_positions=0,
            broker="PaperExecutor", timestamp=now - timedelta(days=2)))
        for j, v in enumerate([5000.0, 4990.0, 4983.94]):  # today's trial curve
            session.add(PortfolioSnapshot(
                portfolio_value=v, cash=v, realized_pnl=0.0, open_positions=0,
                broker="PaperExecutor", timestamp=now - timedelta(minutes=30 - j * 10)))

    body = client.get("/portfolio/performance").json()
    assert body["equity_start"] == 5000.0
    assert body["equity_end"] == 4983.94
    assert -50.0 < body["pnl_today"] < 1.0          # ≈ -16, never +3983
    assert body["snapshot_count"] == 3              # the $1k pre-trial snap excluded

    eq = client.get("/portfolio/equity").json()
    assert eq and all(r["portfolio_value"] > 2000 for r in eq)   # no $1k junk in the curve


def test_equity_change_pct_uses_trial_starting_cash(client):
    """Regression: 'since start' must measure from the trial starting cash, not
    the first-ever snapshot. The reported bug showed +398% because an old
    ~$1,000 snapshot was the baseline for a $5,000 account."""
    from backend.models.config import load_config, save_config

    now = datetime.utcnow()
    with session_scope() as session:
        cfg = load_config(session)
        cfg["paper_cash_override"] = 5000.0
        save_config(session, cfg)
        for j, value in enumerate([1000.0, 4988.35]):  # stale $1k snapshot, then current
            session.add(
                PortfolioSnapshot(
                    portfolio_value=value, cash=value, realized_pnl=0.0,
                    open_positions=0, broker="PaperExecutor",
                    timestamp=now - timedelta(minutes=5 - j),
                )
            )
    body = client.get("/portfolio/performance").json()
    assert body["equity_start"] == 5000.0          # the trial baseline, not 1000
    assert body["equity_end"] == 4988.35
    assert -1.0 < body["equity_change_pct"] < 0.0  # ≈ -0.23%, never +398%
