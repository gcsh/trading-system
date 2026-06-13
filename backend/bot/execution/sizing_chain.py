"""MITS Phase 17.C — Sizing provenance chain.

Records every multiplier in the sizing pipeline as an ordered step on
``event["sizing_chain"]``. At ``_persist_trade`` time the chain is
serialized into ``Trade.sizing_chain_json`` so Phase 18 Learning Layer
can attribute outcomes back to each multiplier in isolation.

Math invariant: each step's ``input * factor == output`` (within float
tolerance). The chain answers "Why this size?" — base risk-manager
quantity at the top, ordered list of multipliers in between, rounded
integer the executor actually used at the bottom.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


def init_sizing_chain(event: Dict[str, Any], base_qty: float) -> None:
    """Seed ``event['sizing_chain']`` with the base quantity from the risk
    manager. Idempotent — opportunistic and statistical paths both call
    init, and a second call must not clobber the first."""
    if "sizing_chain" in event:
        return
    event["sizing_chain"] = {
        "base_qty": float(base_qty),
        "steps": [],
        "final_qty": float(base_qty),
        "rounded_final": None,
        "captured_at": datetime.utcnow().isoformat(),
    }


def record_sizing_step(
    event: Dict[str, Any],
    *,
    name: str,
    input_qty: float,
    factor: float,
    evidence: Optional[Dict[str, Any]] = None,
    source: Optional[str] = None,
) -> float:
    """Append one multiplier step. Returns the output quantity so callers
    can keep chaining. NO-OP when ``event['sizing_chain']`` is not
    initialized — the exit-manager close path runs through executor
    code but doesn't go through the sizing pipeline, and we don't
    want a fake step on those rows."""
    chain = event.get("sizing_chain")
    if chain is None:
        return float(input_qty) * float(factor)
    output = float(input_qty) * float(factor)
    step: Dict[str, Any] = {
        "name": name,
        "input": round(float(input_qty), 4),
        "factor": round(float(factor), 4),
        "output": round(output, 4),
    }
    if evidence is not None:
        step["evidence"] = evidence
    if source is not None:
        step["source"] = source
    chain["steps"].append(step)
    chain["final_qty"] = round(output, 4)
    return output


def finalize_sizing_chain(event: Dict[str, Any], rounded_final: float) -> None:
    """Stamp the final rounded quantity (after the executor's int
    conversion) so the chain records both the float final_qty and the
    integer the order was actually submitted with."""
    chain = event.get("sizing_chain")
    if chain is not None:
        chain["rounded_final"] = float(rounded_final)
