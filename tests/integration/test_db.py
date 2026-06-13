from backend.db import session_scope
from backend.models.config import load_config, save_config
from backend.models.trade import Trade


def test_default_config_seeded_on_first_load(temp_db):
    with session_scope() as session:
        cfg = load_config(session)
    assert cfg["strategy"] == "adaptive"
    assert "risk" in cfg


def test_config_persists_across_sessions(temp_db):
    with session_scope() as session:
        cfg = load_config(session)
        cfg["risk"]["max_position_size_usd"] = 555
        save_config(session, cfg)
    with session_scope() as session:
        reloaded = load_config(session)
    assert reloaded["risk"]["max_position_size_usd"] == 555


def test_trade_log_round_trip(temp_db):
    with session_scope() as session:
        session.add(
            Trade(
                ticker="AAPL",
                action="BUY_STOCK",
                quantity=10,
                price=100,
                strategy="momentum",
                signal_source="technical",
                confidence=0.8,
                reason="ok",
                paper=1,
            )
        )
    with session_scope() as session:
        row = session.query(Trade).first()
        assert row.ticker == "AAPL"
        assert row.to_dict()["paper"] is True
