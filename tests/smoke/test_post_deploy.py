"""Post-deploy smoke pack — must run under 60 seconds.

QA framework: Smoke Testing Strategy (section 14, 38).

Every assertion here is a contract that must hold after every deploy.
A failure here means rollback.

Run with: pytest -m smoke -v
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


pytestmark = pytest.mark.smoke


# ── service shape ─────────────────────────────────────────────────────────


def test_main_app_imports():
    """The FastAPI app object must be importable without side effects.
    A bad import here = a 500 on every request after the next restart."""
    from backend.main import app
    assert app is not None


def test_router_inclusion_count_above_threshold():
    """We've shipped ~30 routers; if a deploy strips imports we'd
    silently lose endpoints. Floor of 25 protects against accidental
    routing regression."""
    from backend.main import app
    routes = [r for r in app.routes if hasattr(r, "path")]
    assert len(routes) >= 25, (
        f"Only {len(routes)} routes registered — likely a broken import"
    )


def test_iv_regime_route_registered():
    """P2.3 endpoint registration check."""
    from backend.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/iv-regime/universe/all" in paths


def test_cohort_matrix_route_registered():
    """P2.4 endpoint registration check."""
    from backend.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/cohorts/matrix" in paths


# ── config shape ──────────────────────────────────────────────────────────


def test_default_config_paper_mode_safe():
    """DEFAULT_BOT_CONFIG must keep paper_mode True. A deploy that
    flips this to False routes real-money orders through the live
    broker silently."""
    from backend.config import DEFAULT_BOT_CONFIG
    assert DEFAULT_BOT_CONFIG.get("paper_mode") is True


def test_default_min_grade_floor():
    """The AI Brain safety floor is hard-coded to B. The default
    config shouldn't be relaxer than that."""
    from backend.config import DEFAULT_BOT_CONFIG
    min_grade = (DEFAULT_BOT_CONFIG.get("analytics", {}) or {}).get("min_grade")
    assert min_grade is None or min_grade >= "B"


# ── persistence shape ─────────────────────────────────────────────────────


def test_paper_state_tables_present():
    """fresh_start contract — every state-bearing model must be in
    PAPER_STATE_TABLES. Smoke check that the registry imports + has
    the canonical 4."""
    from backend.bot.system_reset import PAPER_STATE_TABLES
    names = set()
    for entry in PAPER_STATE_TABLES:
        if isinstance(entry, tuple) and len(entry) >= 2:
            names.add(str(entry[1]))
        else:
            t = getattr(entry, "__tablename__", None)
            if t:
                names.add(t)
    required = {"trades", "decision_log", "paper_positions",
                   "portfolio_snapshots"}
    missing = required - names
    assert not missing, f"PAPER_STATE_TABLES missing {missing}"


# ── frontend bundle ───────────────────────────────────────────────────────


def test_frontend_dist_built():
    """The deploy bundle must include a built frontend. Without dist/
    nginx serves a 404 to the operator."""
    dist = ROOT / "frontend" / "dist"
    if not dist.exists():
        pytest.skip("frontend not built locally — CI/deploy step builds it")
    index = dist / "index.html"
    assert index.exists()


# ── test-suite health ────────────────────────────────────────────────────


def test_pytest_markers_registered():
    """The 12-layer test framework requires markers. If pytest.ini
    loses them, `pytest -m smoke` silently runs everything."""
    pytest_ini = (ROOT / "pytest.ini").read_text()
    for marker in ("smoke", "unit", "integration", "regression",
                       "invariant", "risk", "learning_safety",
                       "ai_safety", "data_integrity", "security",
                       "performance", "load", "stress", "endurance",
                       "dr"):
        assert f"{marker}:" in pytest_ini, (
            f"pytest marker '{marker}' missing — QA framework gap"
        )
