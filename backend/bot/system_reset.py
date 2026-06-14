"""Fresh-start utility — single canonical wipe of paper-state tables.

Use whenever the operator wants to clear paper trading state and start
over (typically: new month, new trial, after a config change you want
to evaluate from zero).

Tables fall into two groups:

  PAPER-STATE TABLES — derived from / referencing the engine's own
  trades and decisions. These MUST be wiped on a fresh start;
  otherwise stats / charts / pillars show the old run mixed with the
  new one.

  EXTERNAL-CACHE TABLES — cached data from outside sources (FRED,
  EDGAR, FINRA, CFTC, market breadth, etc). These should NOT be
  wiped — they're slow to refetch and not tied to the bot's own
  history. Treat as read-only on reset.

If you add a new SQLAlchemy model:
  • holds bot-generated trades / decisions / fills / snapshots
       → add it to ``PAPER_STATE_TABLES`` below.
  • caches external market / regulator data
       → leave it alone; document it in ``EXTERNAL_CACHE_TABLES`` so
         future readers know it was considered and intentionally kept.

This rule is the contract. Failing to follow it is what caused the
2026-05-31 incident where /portfolio still showed historical
portfolio_snapshots rows after the first reset attempt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from backend.db import session_scope
from backend.models.brain_prediction import BrainPrediction
from backend.models.counterfactual_replay import CounterfactualReplay
from backend.models.decision_log import DecisionLog
from backend.models.decision_provenance import DecisionProvenance
from backend.models.eod_prediction_outcome import EodPredictionOutcome
from backend.models.execution_log import ExecutionLog
from backend.models.paper import PaperAccount, PaperPosition
from backend.models.regime_episode import RegimeEpisodeSnapshot
from backend.models.snapshot import PortfolioSnapshot
from backend.models.telegram_outbox import TelegramOutbox
from backend.models.trade import Trade


# ── tables to wipe on fresh-start ──────────────────────────────────────


# (model class, human label) — order matters only for FK constraints
# (none here at the moment, but be careful when adding).
PAPER_STATE_TABLES = [
    # MITS Phase 16.B — decision provenance is bot-generated decision
    # history with FK to trades.id; wipe BEFORE trades to avoid FK
    # constraint failures during the bulk delete.
    # MITS Phase 18.B — counterfactual cache is keyed by
    # provenance_id; wipe BEFORE decision_provenance to keep the
    # cache from holding dangling references.
    (CounterfactualReplay,   "counterfactual_replays"),
    (DecisionProvenance,     "decision_provenance"),
    # Fix N=6 — eod_prediction_outcomes + brain_predictions both
    # FK trades.id. The 2026-06-13 fresh_start crashed with a
    # FOREIGN KEY constraint failure because these were classified
    # as external-cache tables (preserved across resets) while the
    # parent ``trades`` row was being deleted. Wipe them BEFORE
    # ``trades`` so the FK constraint holds during the bulk delete.
    (EodPredictionOutcome,   "eod_prediction_outcomes"),
    (BrainPrediction,        "brain_predictions"),
    (Trade,                  "trades"),
    (DecisionLog,            "decision_log"),
    (ExecutionLog,           "execution_log"),
    (PaperPosition,          "paper_positions"),
    (PortfolioSnapshot,      "portfolio_snapshots"),
    (RegimeEpisodeSnapshot,  "regime_episode_snapshots"),
    # Telegram pending messages — wipe so a reset doesn't push stale
    # alerts about trades from the previous run that no longer exist.
    (TelegramOutbox,         "telegram_outbox"),
]


# Documented intentional keeps — extend when adding new external-cache
# models so future readers can confirm we considered them.
EXTERNAL_CACHE_TABLES = [
    "bot_config",          # user settings — keep
    "breadth_snapshots",   # cached market breadth
    "cot_reports",         # CFTC COT cache
    "earnings_call_intel", # SEC 8-K Ex-99.1 parsed cache
    "edgar_filings",       # EDGAR cache
    "fred_observations",   # FRED macro panel cache
    "gex_regime_history",  # historical GEX snapshots
    "iv_history",          # ATM IV per ticker per date (P1.3) — never wipe
    "seen_flow_alerts",    # flow dedup state — keep
    "short_interest",      # FINRA cache
    "watchlist_items",     # user-curated — keep
    # MITS Phase 0 — historical pattern corpus + derived knowledge graph.
    # Derived from public bar data, not bot decisions; preserve on reset.
    "market_observations",
    "market_outcomes",
    "knowledge_graph",
    "pattern_priors",
    "corpus_status",
    # MITS Phase 1 — nightly snapshots of knowledge_graph for sparklines.
    "knowledge_graph_history",
    # MITS Phase 2 — intraday IV cache (ThetaData straddle inversion).
    "intraday_iv_cache",
    # MITS Phase 3 — operator-set detector toggles + Pine imports —
    # operator-curated config, keep on reset (lives next to watchlist).
    "detector_config",
    # MITS Phase 3 — per-ticker EOD analysis snapshots. Derived from
    # the public corpus + AI, not from bot decisions. Keep on reset so
    # operators can audit yesterday's setups even after a wipe.
    "eod_analysis",
    # MITS Phase 6 — recursive learning loop tables. All derive from
    # corpus posteriors / closed-trade outcomes; the live-outcome
    # learning history MUST survive `fresh_start` so the corpus
    # doesn't replay the same trades into the knowledge graph on
    # restart.
    "ingest_watermarks",
    "detector_suggestions",
    "weekly_retrospectives",
    # MITS Phase 7 — intraday regime transition log. Derived from
    # public market data (SPY / VIX / breadth / put-call / sectors),
    # not bot decisions; preserve on reset so the discretionary
    # layer's autopsy survives trial restarts.
    "intraday_regime_events",
    # MITS Phase 8 — S3 lake sync watermark. Tracks which rows have
    # been uploaded to S3 + which vectors are indexed in pgvector.
    # The underlying lake objects + pgvector entries OUTLIVE a paper
    # reset, so wiping the local watermark would just force an
    # expensive re-upload that produces zero new value.
    "lake_sync_watermark",
    # MITS Phase 9 — Theory Studio operator-edited annotations.
    # Operator-curated drawings (Gann/Fib anchors, hand-tuned pattern
    # lines). Keep on reset alongside watchlist/detector_config.
    "saved_theory_annotations",
    # MITS Phase 9 — Lake health alert ledger. Tied to the shared S3
    # lake + vector store which outlive any paper-trial reset.
    "lake_health_alerts",
    # MITS Phase 11.G — sync orchestration watermark + chunked backfill
    # progress. Wiping these would force a 20y stock + 5y options
    # re-pull on every reset — hours of work, zero new value. Keep.
    "data_watermarks",
    "backfill_progress",
    # MITS Phase 11.B.1 — silver-layer typed stock bar rows. Derived
    # from ThetaData EOD/intraday, not bot decisions. Keep.
    "stock_bars",
    # MITS Phase 11.B.2 — silver-layer EOD option contract bars (the
    # 20M-row chain corpus). Re-pulling 5y of per-contract EOD would
    # take ~17 hours on ThetaData Standard's rate budget. Keep across
    # paper resets — derived from public option data, not bot logic.
    "option_contract_bars",
    # MITS Phase 11.C — Finnhub company news + FinBERT sentiment.
    # Re-fetching 5y × 40 tickers of news would cost hours + burn the
    # 60 req/min budget; keep on reset. Sentiment is computed at
    # ingest by a finance-tuned model, not by bot decisions.
    "news_articles",
    # MITS Phase 11.D — AlphaVantage earnings-call transcript header
    # + per-speaker paragraph rows. AlphaVantage's 25 req/day cap means
    # a full reset-rebuild would take ~32 days; keep these cached
    # across paper trials.
    "earnings_transcripts",
    "transcript_paragraphs",
    # MITS Phase 11.E — Form 4 insider transactions + 13F fund
    # holdings. Derived from EDGAR public filings; preserve across
    # paper resets so the insider + smart-money feature surfaces don't
    # cold-start every month.
    "insider_trades",
    "fund_holdings",
    # MITS Phase 11.J — cross-vendor parity audit ledger. Derived from
    # public bars (yfinance vs ThetaData), survives reset so we keep
    # historical disagreements on file.
    "parity_audit_history",
    # MITS Phase 11.I — per-source health snapshots. Derived from the
    # backfill_progress + data_watermarks ledgers; survives reset.
    "data_source_health",
    # MITS Phase 18.A — Learned Hypothesis Attribution scoreboard.
    # Derived from closed-trade outcomes vs decision_provenance; the
    # learning trajectory MUST survive paper resets so the operator can
    # see "agent X improved over 90 days" across trial restarts. Wiping
    # would also waste the EOD compute that produced the rows.
    "learned_attribution",
    # MITS Phase 18.C — Policy Auto-Tuning advisory recommendations.
    # Derived from closed-trade outcomes; the recommendation history is
    # the operator's record of "what did the advisor suggest last
    # month?" so it MUST survive paper resets. Wiping would erase
    # operator review state (operator_reviewed / operator_approved /
    # applied_at) for prior advisories.
    "policy_tunings",
    # MITS Phase 18.D — Adaptive agent weight history. Append-only
    # ledger of weight-adaptation advisories. Same lifecycle reasoning
    # as policy_tunings — survives fresh_start so the operator review
    # trail persists across paper resets.
    "agent_weight_history",
    # MITS Phase 18.E — Operator approve/rollback audit ledger.
    # Non-repudiation trail of every approve/rollback action the
    # operator took on any learning-table row. Wiping this on a paper
    # reset would erase "who decided what when" — the exact audit
    # trail the hypothesis studio exists to preserve.
    "learning_rollback_log",
    # MITS Phase 18-FU Stream A — Decision Funnel daily rollups.
    # Derived from decision_provenance + policy_rule_evaluations + Trade
    # rows. Operator wants cross-trial funnel snapshots intact so
    # throughput diagnostics persist across paper resets — same
    # lifecycle reasoning as learned_attribution.
    "decision_funnel_daily",
]


@dataclass
class ResetReport:
    cleared: Dict[str, int]
    account_before: Dict[str, float]
    account_after: Dict[str, float]
    starting_cash: float

    def to_dict(self) -> dict:
        return {
            "cleared": self.cleared,
            "account_before": self.account_before,
            "account_after": self.account_after,
            "starting_cash": self.starting_cash,
            "kept_intentionally": EXTERNAL_CACHE_TABLES,
        }


def fresh_start(starting_cash: float = 5000.0) -> ResetReport:
    """Wipe every paper-state table and reset the paper account.

    Idempotent — safe to call repeatedly. Returns a report of what
    was cleared so the operator can audit.
    """
    cleared: Dict[str, int] = {}
    account_before: Dict[str, float] = {}
    account_after: Dict[str, float] = {}

    with session_scope() as s:
        acct = s.query(PaperAccount).first()
        if acct:
            account_before = {
                "starting_cash": float(acct.starting_cash or 0.0),
                "cash": float(acct.cash or 0.0),
                "realized_pnl": float(acct.realized_pnl or 0.0),
            }

        for model, label in PAPER_STATE_TABLES:
            n = s.query(model).count()
            cleared[label] = n
            if n:
                s.query(model).delete()

        if acct is None:
            acct = PaperAccount(
                starting_cash=starting_cash,
                cash=starting_cash,
                realized_pnl=0.0,
                last_portfolio_value=starting_cash,
            )
            s.add(acct)
        else:
            acct.starting_cash = float(starting_cash)
            acct.cash = float(starting_cash)
            acct.realized_pnl = 0.0
            acct.last_portfolio_value = float(starting_cash)

        account_after = {
            "starting_cash": float(acct.starting_cash),
            "cash": float(acct.cash),
            "realized_pnl": float(acct.realized_pnl),
        }
        s.commit()

    # Drift halts are stored in a JSON file, not a SQL table — clear
    # via the existing module API rather than the SQL pathway.
    try:
        from backend.bot.drift.auto_halt import clear_halt, list_halts
        for h in (list_halts() or []):
            name = h.get("strategy") or h.get("name")
            if name:
                clear_halt(name)
    except Exception:
        pass

    return ResetReport(
        cleared=cleared,
        account_before=account_before,
        account_after=account_after,
        starting_cash=float(starting_cash),
    )


def soft_reset(starting_cash: float = 5000.0) -> ResetReport:
    """Reset account + close open positions BUT preserve the historical
    corpus (trades, decision_log, execution_log, portfolio_snapshots,
    regime_episode_snapshots) so agent learning data isn't lost.

    The trade-off vs ``fresh_start``: the trial scoreboard starts clean
    ($5k cash, 0 positions, 0 realized P&L) but the agents keep their
    accumulated context. Orphaned ``open`` trades (whose positions are
    being deleted) get re-labeled ``closed_by_reset`` so the trades log
    stays consistent with ``paper_positions``.

    Used when the operator wants a clean trial-from-here without
    discarding historical decisions / outcomes used for calibration.
    """
    from datetime import datetime
    from backend.models.trade import Trade
    from backend.models.paper import PaperPosition
    cleared: Dict[str, int] = {}
    account_before: Dict[str, float] = {}
    account_after: Dict[str, float] = {}
    reset_at = datetime.utcnow()

    with session_scope() as s:
        acct = s.query(PaperAccount).first()
        if acct:
            account_before = {
                "starting_cash": float(acct.starting_cash or 0.0),
                "cash": float(acct.cash or 0.0),
                "realized_pnl": float(acct.realized_pnl or 0.0),
            }

        # 1. Mark every still-open trade as closed_by_reset so the
        #    trades table reflects what really happened.
        orphan_open = s.query(Trade).filter(Trade.status == "open").all()
        for t in orphan_open:
            t.status = "closed_by_reset"
            if t.pnl is None:
                t.pnl = 0.0
        cleared["trades_marked_closed_by_reset"] = len(orphan_open)

        # 2. Drop the in-flight position state. Trades log stays.
        n_pos = s.query(PaperPosition).count()
        if n_pos:
            s.query(PaperPosition).delete()
        cleared["paper_positions"] = n_pos

        # 3. Reset the paper account scorecard.
        if acct is None:
            acct = PaperAccount(
                starting_cash=starting_cash,
                cash=starting_cash,
                realized_pnl=0.0,
                last_portfolio_value=starting_cash,
            )
            s.add(acct)
        else:
            acct.starting_cash = float(starting_cash)
            acct.cash = float(starting_cash)
            acct.realized_pnl = 0.0
            acct.last_portfolio_value = float(starting_cash)

        account_after = {
            "starting_cash": float(acct.starting_cash),
            "cash": float(acct.cash),
            "realized_pnl": float(acct.realized_pnl),
        }
        s.commit()

    return ResetReport(
        cleared=cleared,
        account_before=account_before,
        account_after=account_after,
        starting_cash=float(starting_cash),
    )


if __name__ == "__main__":  # pragma: no cover — CLI helper
    import argparse, json
    parser = argparse.ArgumentParser()
    parser.add_argument("starting_cash", type=float, nargs="?", default=5000.0)
    parser.add_argument("--soft", action="store_true",
                          help="Preserve trades / decisions / snapshots (agent corpus); "
                               "only reset account + close positions.")
    args = parser.parse_args()
    fn = soft_reset if args.soft else fresh_start
    report = fn(starting_cash=args.starting_cash)
    print(json.dumps(report.to_dict(), indent=2, default=str))


if False:  # legacy CLI replaced by argparse block above on 2026-06-02
    import json, sys
    starting = 5000.0
    if len(sys.argv) > 1:
        starting = float(sys.argv[1])
    report = fresh_start(starting_cash=starting)
    print(json.dumps(report.to_dict(), indent=2))
