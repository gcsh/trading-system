"""Historical-data backfill pipelines (P2 — feed the models).

Modules here populate analytical tables from external history so the
calibration / cohort / IV-regime layers don't have to wait for the bot to
accumulate live trades. Each backfill is idempotent and clearly marks its
synthetic rows so live P&L surfaces can exclude them.
"""
