"""Stage-19 — Earnings Call Intelligence + Source Contribution Tracker."""
import json
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.bot.earnings_intel import (
    CallIntel,
    ClaudeExtractor,
    analyze,
    heuristic_extract,
    history_for,
    latest_for,
    reset_extractor,
)
from backend.bot.source_attribution import (
    SOURCES,
    compute_contributions,
    snapshot_sources,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_extractor()
    yield
    reset_extractor()


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


# ── Earnings intel heuristic extractor ──────────────────────────────────


_RAISE_RELEASE = """
NVIDIA Corporation reported quarterly revenue of $30.0 billion, up 122% from
a year ago. Record revenue. Strong demand from data center customers continued
to accelerate. We are raising guidance for the full year above the prior range.
Operating margin expanded to 65%, up from 58% a year ago. Management's tone
was confident: "We see exceptional opportunities ahead." We expect continued
strength next quarter. Going forward, we anticipate sustained customer demand.
"""

_LOWER_RELEASE = """
The company reported quarterly revenue below expectations. Soft demand and
challenging environment in the consumer segment weighed on results. We are
lowering guidance for the full year. Operating margin contracted on margin
pressure from input costs. Management sounded cautious about the next quarter,
citing uncertainty and headwinds. We anticipate softening conditions in the
near term.
"""


class TestHeuristicExtractor:
    def test_raise_release_detects_improved_expanding_confident(self):
        intel = heuristic_extract(_RAISE_RELEASE)
        assert intel.guidance_change == "improved"
        assert intel.margin_trajectory == "expanding"
        assert intel.management_tone == "confident"
        assert len(intel.key_quotes) >= 2
        assert len(intel.forward_looking) >= 1
        assert "guidance improved" in intel.summary

    def test_lower_release_detects_reduced_contracting_cautious(self):
        intel = heuristic_extract(_LOWER_RELEASE)
        assert intel.guidance_change == "reduced"
        assert intel.margin_trajectory == "contracting"
        assert intel.management_tone == "cautious"
        assert "guidance reduced" in intel.summary

    def test_empty_text_returns_defaults(self):
        intel = heuristic_extract("")
        assert intel.guidance_change == "none"
        assert intel.margin_trajectory == "n/a"
        assert intel.management_tone == "neutral"
        assert intel.source == "heuristic"

    def test_neutral_release(self):
        intel = heuristic_extract(
            "The company reported quarterly revenue in line with expectations. "
            "Management did not provide updated guidance. Margins were unchanged."
        )
        assert intel.management_tone == "neutral"
        assert intel.margin_trajectory == "stable"


# ── Claude extractor (mocked) ───────────────────────────────────────────


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = MagicMock(input_tokens=200, output_tokens=300)


class _Messages:
    def __init__(self, text):
        self._text = text

    def create(self, **kw):
        return _Resp(self._text)


class _Client:
    def __init__(self, text):
        self.messages = _Messages(text)


class TestClaudeExtractor:
    def test_falls_through_without_key(self):
        ex = ClaudeExtractor(api_key="")
        intel = ex.extract(_RAISE_RELEASE)
        assert intel.source == "heuristic"

    def test_mocked_claude_returns_structured(self):
        payload = json.dumps({
            "guidance_change": "improved",
            "margin_trajectory": "expanding",
            "management_tone": "confident",
            "key_quotes": ["We see exceptional opportunities ahead.",
                              "Record revenue this quarter."],
            "forward_looking": ["We expect continued strength next quarter."],
            "summary": "Strong beat with raised outlook and expanding margins.",
        })
        ex = ClaudeExtractor(client=_Client(payload))
        intel = ex.extract(_RAISE_RELEASE, ticker="NVDA")
        assert intel.source == "claude"
        assert intel.guidance_change == "improved"
        assert "exceptional" in intel.key_quotes[0]

    def test_bad_json_falls_through_to_heuristic(self):
        ex = ClaudeExtractor(client=_Client("not JSON"))
        intel = ex.extract(_RAISE_RELEASE)
        assert intel.source == "heuristic"


# ── analyze() persists into DB ──────────────────────────────────────────


class TestAnalyzeAndCache:
    def test_persists_and_reads_back(self, temp_db):
        result = analyze(
            ticker="NVDA", accession_number="0001045810-26-000051",
            filed_at=datetime(2026, 5, 20),
            text=_RAISE_RELEASE, prefer_claude=False,
        )
        assert result["guidance_change"] == "improved"
        assert result["ticker"] == "NVDA"

        latest = latest_for("NVDA")
        assert latest is not None
        assert latest["guidance_change"] == "improved"

    def test_idempotent_upsert(self, temp_db):
        analyze(ticker="NVDA", accession_number="acc-1",
                  filed_at=datetime(2026, 5, 20),
                  text=_RAISE_RELEASE, prefer_claude=False)
        analyze(ticker="NVDA", accession_number="acc-1",
                  filed_at=datetime(2026, 5, 20),
                  text=_LOWER_RELEASE, prefer_claude=False)
        # Same accession → second analyze updates in place, not duplicate
        history = history_for("NVDA")
        assert len(history) == 1
        # Second call's content (lower) won
        assert history[0]["guidance_change"] == "reduced"


# ── Source attribution snapshot ────────────────────────────────────────


class TestSnapshotSources:
    def test_returns_one_score_per_source(self):
        ctx = {
            "action": "BUY_CALL",
            "breadth": {"verdict": "healthy_advance"},
            "macro": {"yield_curve_inverted": False,
                         "BAMLH0A0HYM2": {"value": 2.5},
                         "NFCI": {"value": -0.40}},
            "earnings_intel": {"guidance_change": "improved",
                                  "management_tone": "confident",
                                  "margin_trajectory": "expanding"},
            "short_pressure": {"level": "high", "trend": "rising"},
            "cot_snapshot": {"ES": {"noncommercial_net": 10000,
                                        "open_interest": 100000}},
            "insider_activity": {"form4_count": 1},
        }
        scores = snapshot_sources(ctx)
        for name, _ in SOURCES:
            assert name in scores
        # Bullish bias case: breadth, macro, edgar should all be positive.
        assert (scores["breadth"] or 0) > 0
        assert (scores["macro"] or 0) > 0
        assert (scores["edgar"] or 0) > 0
        assert (scores["short_interest"] or 0) > 0

    def test_returns_none_for_missing(self):
        scores = snapshot_sources({"action": "BUY_STOCK"})
        # No data → most sources return None.
        nones = sum(1 for v in scores.values() if v is None)
        assert nones >= 4

    def test_short_pressure_inverts_for_shorts(self):
        ctx = {
            "action": "BUY_PUT",        # short bias
            "short_pressure": {"level": "high", "trend": "rising"},
        }
        scores = snapshot_sources(ctx)
        # Crowded short = penalty
        assert scores["short_interest"] < 0


# ── Source attribution rollup ──────────────────────────────────────────


def _seed_trade_with_scores(*, pnl, scores, ts_offset_min=0):
    from backend.db import session_scope
    from backend.models.trade import Trade
    with session_scope() as s:
        t = Trade(
            ticker="NVDA", action="BUY_CALL", quantity=1, price=215,
            strategy="trend_pullback", signal_source="t",
            confidence=0.7, paper=1, status="closed",
            instrument="option", pnl=pnl,
            detail_json=json.dumps({"source_scores": scores}),
        )
        t.timestamp = datetime.utcnow() + timedelta(minutes=ts_offset_min)
        s.add(t); s.flush()
        return t.id


class TestComputeContributions:
    def test_empty_corpus(self, temp_db):
        report = compute_contributions()
        assert report.closed_trades == 0
        # All sources surface with None correlation
        assert all(s.correlation_with_pnl is None for s in report.sources)

    def test_positive_correlation_surfaces(self, temp_db):
        # 40 trades where higher breadth score → bigger wins, perfectly correlated
        for i in range(40):
            score = -1.0 + i * (2.0 / 39)        # ramp from -1 to +1
            pnl = score * 100                        # perfectly linear
            _seed_trade_with_scores(pnl=pnl,
                                          scores={"breadth": score},
                                          ts_offset_min=i)
        report = compute_contributions(min_trades=30)
        by = {s.source: s for s in report.sources}
        # Breadth should show r ~ +1 and dominate contribution
        assert by["breadth"].correlation_with_pnl is not None
        assert by["breadth"].correlation_with_pnl > 0.9
        # Other sources had no data → contribution 0
        assert by["macro"].correlation_with_pnl is None

    def test_below_min_trades_skips_correlation(self, temp_db):
        for i in range(10):
            _seed_trade_with_scores(pnl=10.0,
                                          scores={"breadth": 0.5},
                                          ts_offset_min=i)
        report = compute_contributions(min_trades=30)
        by = {s.source: s for s in report.sources}
        # 10 < 30 min_trades → no correlation reported
        assert by["breadth"].correlation_with_pnl is None
        assert "need more" in by["breadth"].insight \
            or "insufficient" in by["breadth"].insight


# ── Endpoints (cold start) ─────────────────────────────────────────────


class TestEndpoints:
    def test_earnings_endpoint_empty(self, client):
        body = client.get("/earnings-intel/NVDA").json()
        assert body["ticker"] == "NVDA"
        assert body["intel"] is None

    def test_analyze_endpoint(self, client):
        body = client.post("/earnings-intel/analyze", json={
            "ticker": "NVDA",
            "text": _RAISE_RELEASE,
        }).json()
        assert body["intel"]["guidance_change"] == "improved"

    def test_contributions_endpoint(self, client):
        body = client.get("/source-attribution/contributions").json()
        assert "sources" in body
        names = [s["source"] for s in body["sources"]]
        assert "breadth" in names and "macro" in names
