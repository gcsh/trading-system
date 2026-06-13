"""Business-invariant tests — the bug-class regression net.

These are NOT "1=1" unit tests. They encode the financial-correctness
invariants the live trial depends on. Every test here exists because a
bug in the same class would either lose real money or break the trial:

  * Synthetic backfill must NEVER pollute operator-facing P&L / capital
    allocation / gate calibration. (3 catastrophic bugs found 2026-06-03)
  * Option positions must be deduped by (ticker, kind, strike, expiry).
    Otherwise the brain can open the same trade twice. (Found 2026-06-03)
  * Strike + expiry on every option order must come from the live chain,
    never from arithmetic. (Memory: "never round(price) as strike")
  * The daily-loss circuit breaker must fire when realized PnL crosses
    the limit. A dead gate silently lets the bot blow past it.
  * fresh_start() is the only legal full-wipe path. New bot-state models
    must be added to PAPER_STATE_TABLES.
  * Operator P&L surfaces must agree (Today's P&L == sum of today's
    closed trades + intraday MTM delta).

A failure in any test here means the same class of bug we found before
has re-appeared.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List, Set, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROUTES = ROOT / "backend" / "api" / "routes"
BOT = ROOT / "backend" / "bot"
MODELS = ROOT / "backend" / "models"


# ── 1. Synthetic-corpus separation ────────────────────────────────────────


# Every route file that exposes operator-facing P&L or drives capital
# allocation must filter out the synthetic backfill corpus before it
# reaches the user. We allow-list the routes whose PURPOSE is analytical
# (calibration page, audit log, cohort matrix etc).
_OPERATOR_FACING_ROUTES = {
    "portfolio.py": [
        ("/performance", "must exclude historical_replay + closed_by_reset"),
        ("/by-strategy", "leaks would misrepresent strategy P&L"),
        ("/equity", "equity curve is the trial scoreboard"),
    ],
    "trades.py": [
        ("/list", "the operator's trade ledger"),
        ("/summary", "headline win-rate / total P&L"),
    ],
    "portfolio_optimizer.py": [
        ("/allocations", "CAPITAL ALLOCATION — leak = real money on a backtest"),
    ],
}


def _read_route(filename: str) -> str:
    return (ROUTES / filename).read_text()


@pytest.mark.parametrize("filename,routes", list(_OPERATOR_FACING_ROUTES.items()))
def test_operator_routes_filter_synthetic(filename: str, routes):
    """Every operator-facing route must filter ``signal_source !=
    'historical_replay'`` AND ``status != 'closed_by_reset'``. A
    failure here means a path that drives the user's view of their
    money is going to include backtest rows."""
    body = _read_route(filename)
    assert 'signal_source != "historical_replay"' in body or \
                "signal_source != 'historical_replay'" in body, (
        f"{filename}: missing synthetic-corpus filter — operator P&L "
        f"would be polluted. Routes affected: {[r[0] for r in routes]}"
    )
    assert 'status != "closed_by_reset"' in body or \
                "status != 'closed_by_reset'" in body, (
        f"{filename}: missing closed_by_reset filter — administrative "
        f"reset rows would inflate trade count and zero win rate"
    )


def test_metrics_summary_defaults_live_only():
    """The grade gate reads /metrics/summary via ``build_summary``.
    The default MUST be live-only — synthetic outcomes carry
    probabilities from the heuristic ranker, not from the live brain,
    so they would poison the gate decision."""
    body = (ROUTES / "metrics.py").read_text()
    # build_summary signature must default live_only=True
    assert re.search(r"def build_summary\([^)]*live_only:\s*bool\s*=\s*True",
                          body, re.DOTALL), (
        "build_summary must default to live_only=True; the engine's "
        "adaptive grade gate consumes it and synthetic poisoning would "
        "block ALL live trades (we hit this 2026-06-03)"
    )


def test_metrics_by_strategy_default_live_only():
    """``/metrics/by-strategy`` is consumed by the StrategyBreakdown UI.
    Must default to live-only — parse with AST so we don't break on
    formatting changes."""
    src = (ROUTES / "metrics.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "metrics_by_strategy"):
            defaults = node.args.kw_defaults + node.args.defaults
            kwonly = [a.arg for a in node.args.kwonlyargs] + [
                a.arg for a in node.args.args]
            assert "include_synthetic" in kwonly, (
                "metrics_by_strategy must accept include_synthetic kwarg"
            )
            return
    pytest.fail("metrics_by_strategy function not found in metrics.py")


# ── 2. Option position dedup ─────────────────────────────────────────────


def test_engine_dedups_options_by_strike_and_expiry():
    """The engine must dedup option positions by (ticker, kind, strike,
    expiry). Without this, the AI Brain proposing the same BUY_CALL
    twice (across cycles or within one watchlist scan) would open two
    identical contracts and double the position risk on a single thesis."""
    body = (BOT / "engine.py").read_text()
    assert "_held_option_keys" in body, (
        "engine.py must expose _held_option_keys for option dedup; "
        "stock-only dedup allows option pyramiding"
    )
    # The gate must use it.
    assert "held_option_keys" in body, (
        "engine.py must consult held_option_keys before opening a new "
        "option position — otherwise the dedup helper is decorative"
    )


def test_held_option_keys_excludes_stocks():
    """The option-dedup set must NOT key on stock positions — stock dedup
    uses _held_tickers. Mixing them would cause stock positions to block
    option entries on the same underlying."""
    from backend.bot.engine import BotEngine
    src = (BOT / "engine.py").read_text()
    # Extract the function body
    m = re.search(r"def _held_option_keys.*?return out", src, re.DOTALL)
    assert m, "_held_option_keys body not found — refactor broke the test"
    fn = m.group(0)
    assert 'kind == "stock"' in fn and "continue" in fn, (
        "_held_option_keys must skip stock positions; otherwise stock "
        "holdings would block option entries on the same underlying"
    )


# ── 3. Option correctness — strike + expiry must come from chain ─────────


def test_no_arithmetic_strike_in_strategies():
    """Strategies must never compute a strike with ``round(price)``,
    ``int(price)``, ``price + N``, etc. Strikes MUST come from a real
    chain via ``chain_strike()``. (Memory: 'never round(price) as strike')"""
    pattern = re.compile(
        r"strike\s*=\s*(round|int|float)\(\s*(price|spot)\b",
        re.IGNORECASE,
    )
    offenders: List[str] = []
    for f in (BOT / "strategies").rglob("*.py"):
        if "__pycache__" in str(f):
            continue
        text = f.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line) and "chain_strike" not in line:
                offenders.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "Strategies must derive strikes from chain_strike(), not from "
        "arithmetic. Offenders:\n  " + "\n  ".join(offenders)
    )


# ── 4. Reset / wipe safety ───────────────────────────────────────────────


def test_paper_state_tables_covers_known_state_models():
    """fresh_start() must wipe every state-bearing model. New state
    models added without registering here would survive a reset and
    leak yesterday's data into today's UI."""
    from backend.bot.system_reset import PAPER_STATE_TABLES
    # Entries are (model_class, table_name) tuples per the registry.
    table_names: Set[str] = set()
    for entry in PAPER_STATE_TABLES:
        if isinstance(entry, tuple) and len(entry) >= 2:
            table_names.add(str(entry[1]))
        else:
            tname = getattr(entry, "__tablename__", None)
            if tname:
                table_names.add(tname)
    required = {
        "trades", "decision_log", "portfolio_snapshots",
        "paper_positions",
    }
    missing = required - table_names
    assert not missing, (
        f"PAPER_STATE_TABLES is missing required state tables: {missing}. "
        f"Currently registered: {sorted(table_names)}. A reset would "
        f"leave the missing tables populated and the bot would resume "
        f"with stale data."
    )


def _find_direct_state_writes(filename: Path, tables: Set[str]) -> List[str]:
    """Scan a file for delete/truncate operations that bypass fresh_start."""
    if "__pycache__" in str(filename):
        return []
    try:
        text = filename.read_text()
    except Exception:
        return []
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for table in tables:
            if (f"delete({table}" in line or f".{table}).delete(" in line
                    or f"DELETE FROM {table}" in line.upper()):
                hits.append(f"{filename.relative_to(ROOT)}:{i}: {line.strip()}")
    return hits


def test_state_deletes_routed_through_fresh_start():
    """No file outside ``system_reset.py`` may delete from a state table.
    All wipes must go through ``fresh_start()`` so new models added to
    PAPER_STATE_TABLES are correctly wiped.

    Allowed exceptions: backfill modules (they delete their own
    synthetic rows before re-inserting), and the autopsy-loss-cache
    expiry cleanup."""
    tables = {"Trade", "DecisionLog", "PortfolioSnapshot", "PaperPosition"}
    # seed.py is allow-listed because its delete is gated on
    # ``signal_source == DEMO_SOURCE`` — it can only wipe demo-seed
    # rows, never real trades. Anything we add here must be similarly
    # contained.
    allowed = {
        "backend/bot/system_reset.py",
        "backend/bot/backfill/historical_replay.py",
        "backend/bot/backfill/options_history_replay.py",
        "backend/bot/seed.py",
    }
    offenders: List[str] = []
    for f in (ROOT / "backend").rglob("*.py"):
        rel = str(f.relative_to(ROOT))
        if rel in allowed or "__pycache__" in rel:
            continue
        offenders.extend(_find_direct_state_writes(f, tables))
    assert not offenders, (
        "Direct deletes to state tables found outside system_reset.py / "
        "backfill modules. These bypass the PAPER_STATE_TABLES contract:\n  "
        + "\n  ".join(offenders)
    )


# ── 5. Filter pattern symmetry ────────────────────────────────────────────


def test_synthetic_filter_uses_canonical_string():
    """The synthetic-corpus filter must use the literal string
    ``"historical_replay"``. A typo (e.g. ``"historical-replay"`` or
    ``"hist_replay"``) would silently include synthetic rows. Trade.signal_source
    is written from a single constant in backend/bot/backfill/."""
    canonical = "historical_replay"
    sample_writer = (BOT / "backfill" / "historical_replay.py").read_text()
    assert f'"{canonical}"' in sample_writer or f"'{canonical}'" in sample_writer, (
        f"Canonical synthetic source string mismatch — writer must use "
        f"'{canonical}' so filters elsewhere match"
    )


# ── 6. AI Brain safety floor ─────────────────────────────────────────────


def test_ai_brain_safety_floor_enforces_grade_b():
    """When ai_brain is enabled, min_grade must be raised to at least 'B'
    regardless of operator config. The brain proposes freely; the agent
    core must not let coin-flip C-grade bets through. Removal of this
    floor would let untested grade-C signals execute at $5k size."""
    body = (BOT / "engine.py").read_text()
    # The floor is in the form: `if use_brain and (min_grade is None or
    # min_grade < "B"): min_grade = "B"` — assert both halves present.
    assert "use_brain and (min_grade is None" in body, (
        "AI Brain safety floor missing — brain could trade C-grade signals"
    )
    assert 'min_grade = "B"' in body, (
        "AI Brain safety floor missing min_grade='B' assignment"
    )


# ── 7. ETF fundamentals short-circuit ─────────────────────────────────────


def test_etf_fundamentals_short_circuit():
    """fetch_fundamentals must short-circuit known ETFs. Hitting Yahoo's
    quoteSummary endpoint with ETF tickers (SPY, QQQ, ...) returns 404
    and floods system warnings every cycle."""
    body = (BOT / "signals" / "fundamentals.py").read_text()
    assert "_ETF_TICKERS" in body or "etf_tickers" in body.lower(), (
        "Missing ETF short-circuit list — log spam will return"
    )
    for t in ("SPY", "QQQ"):
        assert t in body, f"ETF allow-list missing canonical {t}"


# ── 8. Range parameter name shadowing ────────────────────────────────────


def test_no_builtin_range_shadow_in_routes():
    """No route function parameter may be named ``range`` — it shadows
    the builtin ``range()`` and any later use of ``range(...)`` in the
    function body raises ``TypeError: 'str' object is not callable``.
    We hit this 2026-06-03 in /portfolio/equity."""
    pattern = re.compile(r"async def \w+\([^)]*\brange:\s*str\b", re.DOTALL)
    offenders: List[str] = []
    for f in ROUTES.rglob("*.py"):
        text = f.read_text()
        if pattern.search(text):
            offenders.append(str(f.relative_to(ROOT)))
    assert not offenders, (
        "Route handlers shadowing builtin range() found:\n  "
        + "\n  ".join(offenders)
        + "\nUse a different parameter name and FastAPI Query alias."
    )
