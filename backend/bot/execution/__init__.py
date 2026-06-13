"""MITS Phase 17.B — fill provenance package.

Holds the structured FillSnapshot dataclass + helpers that capture
every observable about the market state at fill time. Persisted as
``Trade.fill_snapshot_json``; consumed by Phase 18 Learning Layer
fill-quality attribution.
"""
