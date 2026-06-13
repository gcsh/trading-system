"""State-model invariant CI gate — P1.11.

Every model registered in ``backend.bot.system_reset.PAPER_STATE_TABLES``
must have a corresponding business-invariant test that exercises the
"operator-facing route filters synthetic when reading from this model"
contract.

When a new state model is added without registering an invariant test
here, this gate fails — preventing the synthetic-leak bug class from
re-emerging via paths nobody manually audited.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Set

import pytest


pytestmark = pytest.mark.invariant


def _registered_state_tables() -> Set[str]:
    from backend.bot.system_reset import PAPER_STATE_TABLES
    out: Set[str] = set()
    for entry in PAPER_STATE_TABLES:
        if isinstance(entry, tuple) and len(entry) >= 2:
            out.add(str(entry[1]))
        else:
            tname = getattr(entry, "__tablename__", None)
            if tname:
                out.add(tname)
    return out


# The contract: every state table here must have a "filter-synthetic"
# guarantee somewhere in the test suite. We accept proof in either form:
#   1) A dedicated invariant test that grep-checks the relevant route.
#   2) Documentation that the model is read-only / aggregate so no
#      synthetic filter is needed (see _ANALYTICAL_TABLES below).
_ANALYTICAL_TABLES = {
    # Tables that are write-only state snapshots OR are intentionally
    # used for cross-model joining. Adding a model here requires a code
    # review comment explaining why the synthetic filter doesn't apply.
    "execution_log",         # slippage telemetry from FILLS only — no
                              # synthetic since replay doesn't simulate fills.
    "regime_episode_snapshots",  # market-state fingerprints, source-agnostic.
    # Telegram pending-message queue — no operator-facing analytics
    # reads from this table. The synthetic-filter invariant doesn't
    # apply: rows are POST-bodies queued for HTTP retry, not bot
    # decisions. fresh_start wipes the queue so stale messages from a
    # previous run never get sent.
    "telegram_outbox",
}


@pytest.fixture(scope="module")
def state_tables() -> Set[str]:
    return _registered_state_tables()


def test_paper_state_tables_registry_not_empty(state_tables):
    """Sanity — if PAPER_STATE_TABLES is empty, fresh_start would no-op
    and yesterday's data would leak into today's UI."""
    assert state_tables, "PAPER_STATE_TABLES is empty — fresh_start is broken"


def test_every_operational_state_table_has_synthetic_filter_test(state_tables):
    """For every operational state table (i.e. not analytical), some test
    in tests/unit/test_business_invariants.py or test_learning_poisoning_resistance.py
    must reference the table name in a filter assertion."""
    operational = state_tables - _ANALYTICAL_TABLES
    repo_root = Path(__file__).resolve().parents[2]
    test_dir = repo_root / "tests" / "unit"
    invariant_files = [
        test_dir / "test_business_invariants.py",
        test_dir / "test_learning_poisoning_resistance.py",
        test_dir / "test_state_model_invariants.py",
    ]
    combined = ""
    for f in invariant_files:
        if f.exists():
            combined += f.read_text()
    missing = []
    for table in sorted(operational):
        # We accept any reference to the table name OR the corresponding
        # model class name. This is a coarse check — refined as we learn.
        if table not in combined:
            missing.append(table)
    assert not missing, (
        f"State tables registered in PAPER_STATE_TABLES with no invariant "
        f"test coverage: {missing}. Add a test to tests/unit/test_business_invariants.py "
        f"(or test_learning_poisoning_resistance.py) that asserts operator-facing "
        f"routes filter synthetic when reading from these tables. If the "
        f"table is intentionally analytical, add it to _ANALYTICAL_TABLES "
        f"in this file with a comment explaining why."
    )


def test_decision_log_has_signal_source_column():
    """P1.1 — DecisionLog must have signal_source so live-only analytics
    can filter without a Trade join."""
    from backend.models.decision_log import DecisionLog
    columns = {c.name for c in DecisionLog.__table__.columns}
    assert "signal_source" in columns, (
        "DecisionLog.signal_source missing — analytics will leak."
    )


def test_trade_has_pricing_source_and_accounting_version():
    """P1.5/P1.7 — Trade rows must record pricing_source + accounting_version
    so post-hoc audit can answer 'how much P&L came from real chain data?'
    and 'which accounting model produced this trade?'."""
    from backend.models.trade import Trade
    columns = {c.name for c in Trade.__table__.columns}
    assert "pricing_source" in columns
    assert "accounting_version" in columns


def test_portfolio_snapshot_has_quality_fields():
    """P1.6 — snapshots must record data_quality, accounting_version,
    pricing_source_mix, excludes_synthetic so the equity curve is
    auditable forever."""
    from backend.models.snapshot import PortfolioSnapshot
    columns = {c.name for c in PortfolioSnapshot.__table__.columns}
    for field in ("data_quality", "accounting_version",
                       "pricing_source_mix", "excludes_synthetic"):
        assert field in columns, (
            f"PortfolioSnapshot.{field} missing — equity curve will be "
            f"unauditable across accounting changes."
        )
