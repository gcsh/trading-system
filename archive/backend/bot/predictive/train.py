"""Runnable trainer — reads the DecisionLog table, fits the predictive model,
saves the artifact. Safe to call repeatedly; idempotent on inputs.

Usage:
    .venv/bin/python -m backend.bot.predictive.train [--limit N] [--path FILE]
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import List

from sqlalchemy import desc, select

from backend.bot.predictive import (
    DEFAULT_MODEL_PATH,
    MIN_TRAINING_ROWS,
    MLProbabilityModel,
    reset_model,
)
from backend.db import session_scope
from backend.models.decision_log import DecisionLog


def _load_rows(limit: int) -> List[dict]:
    """Pull labeled (outcome_pnl IS NOT NULL) decision rows as plain dicts —
    extracted inside the session to avoid DetachedInstanceError."""
    with session_scope() as session:
        rows = session.execute(
            select(DecisionLog)
            .where(DecisionLog.outcome_pnl.is_not(None))
            .order_by(desc(DecisionLog.timestamp))
            .limit(limit)
        ).scalars().all()
        return [r.to_dict() | {"features_json": r.features_json} for r in rows]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Train the predictive probability model")
    parser.add_argument("--limit", type=int, default=5000,
                         help="max rows to pull from decision_log")
    parser.add_argument("--path", type=str, default=DEFAULT_MODEL_PATH,
                         help="output artifact path")
    args = parser.parse_args(argv)

    rows = _load_rows(args.limit)
    logging.info("loaded %d labeled decision rows", len(rows))
    if len(rows) < MIN_TRAINING_ROWS:
        logging.warning("only %d rows — need %d to train. Run more cycles first.",
                         len(rows), MIN_TRAINING_ROWS)
        return 1

    model = MLProbabilityModel(model_path=args.path)
    result = model.train(rows)
    if result is None:
        logging.warning("training skipped (insufficient data / sklearn missing)")
        return 1
    reset_model()  # ensure the next get_model() picks up the fresh artifact
    logging.info("trained ok: %s", result.to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
