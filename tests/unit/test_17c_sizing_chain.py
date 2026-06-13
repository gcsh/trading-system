"""MITS Phase 17.C — sizing provenance chain unit tests.

Each test isolates one observable in the chain helper:
  1. init is idempotent (second call must not clobber the first)
  2. record_sizing_step preserves the input * factor == output invariant
     across a multi-step chain
  3. record on an uninitialized event is a no-op (math still works)
  4. evidence dict survives onto the recorded step
  5. finalize stamps the rounded integer the executor used
  6. a realistic 3-step chain rounds-trips through final_qty
"""
from __future__ import annotations

from backend.bot.execution.sizing_chain import (
    finalize_sizing_chain,
    init_sizing_chain,
    record_sizing_step,
)


def test_init_sizing_chain_idempotent():
    event: dict = {}
    init_sizing_chain(event, base_qty=100)
    assert event["sizing_chain"]["base_qty"] == 100
    init_sizing_chain(event, base_qty=999)
    assert event["sizing_chain"]["base_qty"] == 100


def test_record_sizing_step_math_invariant():
    event: dict = {}
    init_sizing_chain(event, base_qty=100)
    out1 = record_sizing_step(event, name="step1", input_qty=100, factor=0.80)
    assert out1 == 80
    out2 = record_sizing_step(event, name="step2", input_qty=80, factor=0.70)
    assert out2 == 56
    assert event["sizing_chain"]["final_qty"] == 56
    assert len(event["sizing_chain"]["steps"]) == 2
    for step in event["sizing_chain"]["steps"]:
        assert abs(step["input"] * step["factor"] - step["output"]) < 0.01


def test_record_sizing_step_no_op_when_uninitialized():
    event: dict = {}
    out = record_sizing_step(event, name="step1", input_qty=100, factor=0.5)
    assert out == 50
    assert "sizing_chain" not in event


def test_record_sizing_step_evidence_attached():
    event: dict = {}
    init_sizing_chain(event, base_qty=100)
    record_sizing_step(
        event, name="corr_cap", input_qty=100, factor=0.7,
        evidence={"worst_rho": 0.65, "worst_peer": "AMD"},
    )
    step = event["sizing_chain"]["steps"][0]
    assert step["evidence"]["worst_rho"] == 0.65
    assert step["evidence"]["worst_peer"] == "AMD"


def test_finalize_sizing_chain():
    event: dict = {}
    init_sizing_chain(event, base_qty=100)
    record_sizing_step(event, name="step1", input_qty=100, factor=0.476)
    finalize_sizing_chain(event, rounded_final=48)
    assert event["sizing_chain"]["rounded_final"] == 48
    assert event["sizing_chain"]["final_qty"] == 47.6


def test_full_chain_with_evidence():
    """Realistic multi-step chain."""
    event: dict = {}
    init_sizing_chain(event, base_qty=100)
    record_sizing_step(
        event, name="consensus.size_multiplier",
        input_qty=100, factor=0.80,
    )
    record_sizing_step(
        event, name="correlation_cap.sizing_multiplier",
        input_qty=80, factor=0.70,
        evidence={"worst_rho": 0.65, "worst_peer": "AMD"},
    )
    record_sizing_step(
        event, name="conviction_sizing",
        input_qty=56, factor=0.85,
        evidence={"grade": "B"},
    )
    finalize_sizing_chain(event, rounded_final=48)
    chain = event["sizing_chain"]
    assert len(chain["steps"]) == 3
    assert chain["base_qty"] == 100
    assert chain["final_qty"] == 47.6
    assert chain["rounded_final"] == 48
    assert chain["steps"][0]["input"] == chain["base_qty"]
    assert chain["steps"][-1]["output"] == chain["final_qty"]


def test_finalize_no_op_when_uninitialized():
    """Symmetry with record — finalize on a fresh event is harmless."""
    event: dict = {}
    finalize_sizing_chain(event, rounded_final=10)
    assert "sizing_chain" not in event


def test_chain_captured_at_iso_timestamp():
    event: dict = {}
    init_sizing_chain(event, base_qty=100)
    ts = event["sizing_chain"]["captured_at"]
    # ISO format: YYYY-MM-DDTHH:MM:SS(.ssssss)
    assert "T" in ts
    assert ts.startswith("20")


def test_source_field_optional():
    """``source`` is an optional metadata field — when supplied it lands
    on the step, when omitted it is absent."""
    event: dict = {}
    init_sizing_chain(event, base_qty=100)
    record_sizing_step(
        event, name="meta_ai.risk_modifier",
        input_qty=100, factor=0.70, source="meta_engine",
    )
    record_sizing_step(
        event, name="catalyst.multiplier",
        input_qty=70, factor=0.50,
    )
    assert event["sizing_chain"]["steps"][0]["source"] == "meta_engine"
    assert "source" not in event["sizing_chain"]["steps"][1]
