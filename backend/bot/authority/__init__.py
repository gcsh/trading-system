"""Phase-1 Authority surface.

Rolls up six pillars (DATA / MODEL / COUNCIL / RISK / EXECUTION / LEARNING)
from existing backend modules into a single status document the
Authority Spine consumes. Computes Authority Confidence (CONFIDENT /
WATCHING / RESTRICTED) and Attention as derived states, NOT as
aggregate scores.

Design principles (locked in design discussion):

  • Each pillar's status comes from one or more concrete backend
    signals — never invented numbers. If a signal is unavailable,
    the pillar status is ``unknown``, not fake-healthy.

  • Words reflect decision quality, not infrastructure uptime. Each
    pillar has its own per-tier vocabulary (Reliable/Slipping/Drifting
    for MODEL; Aligned/Divided/Conflicted for COUNCIL; etc).

  • Promotion ≠ Trust. Promotion gates are *not* part of the
    confidence rollup. They live on the Launch Authorization page.

  • Authority Level (operator-set, enforced) ≠ Authority Confidence
    (derived from pillars). Both surface on the Spine; semantics are
    distinct.

  • The aggregate "trust score" is intentionally absent in Phase 1.
    Pillars + confidence + attention give the operator everything they
    need without inventing weights.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── per-pillar vocabularies ────────────────────────────────────────────


# Each pillar uses its own three-tier vocabulary. The KEYS ("ok",
# "mid", "bad") are the underlying contract states; the VALUES are
# what the operator sees.
PILLAR_VOCAB: Dict[str, Dict[str, str]] = {
    "data":      {"ok": "Reliable",   "mid": "Stale",      "bad": "Broken"},
    "model":     {"ok": "Reliable",   "mid": "Slipping",   "bad": "Drifting"},
    "council":   {"ok": "Aligned",    "mid": "Divided",    "bad": "Conflicted"},
    "risk":      {"ok": "Controlled", "mid": "Stretched",  "bad": "Breached"},
    "execution": {"ok": "Ready",      "mid": "Strained",   "bad": "Impaired"},
    "learning":  {"ok": "Active",     "mid": "Quiet",      "bad": "Stalled"},
}

PILLAR_NAMES = tuple(PILLAR_VOCAB.keys())

UNKNOWN_LABEL = "Unknown"


# ── Authority Level (operator-set, enforced) ───────────────────────────


AUTHORITY_LEVELS = ("SHADOW", "PAPER", "GATED", "AUTONOMOUS")


def _current_authority_level() -> str:
    """Resolve the operator-set authority level.

    Today there's no first-class control plane (Phase 2 work) so we
    derive from existing state:
      • ``chairman_authoritative`` env var sets us above PAPER
      • paper_mode in the saved config is the default
      • SHADOW is only set explicitly via env (TB_AUTHORITY=SHADOW)
    """
    explicit = (os.getenv("TB_AUTHORITY") or "").upper().strip()
    if explicit in AUTHORITY_LEVELS:
        return explicit
    try:
        from backend.config import TUNABLES
        if getattr(TUNABLES, "chairman_authoritative", False):
            # Chairman authoritative but no live broker → still PAPER+
            return "GATED"
    except Exception:
        pass
    return "PAPER"


# ── data classes ──────────────────────────────────────────────────────


@dataclass
class PillarStatus:
    name: str                       # one of PILLAR_NAMES
    tier: str                       # "ok" | "mid" | "bad" | "unknown"
    label: str                      # operator-facing word, from PILLAR_VOCAB
    why: str                        # one-line plain English
    signals: Dict[str, Any] = field(default_factory=dict)
    contract: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── per-pillar rollups ────────────────────────────────────────────────


def _label_for(name: str, tier: str) -> str:
    if tier == "unknown":
        return UNKNOWN_LABEL
    return PILLAR_VOCAB.get(name, {}).get(tier, UNKNOWN_LABEL)


def _pillar(name: str, tier: str, why: str, *,
                signals: Optional[Dict[str, Any]] = None,
                contract: Optional[Dict[str, str]] = None) -> PillarStatus:
    return PillarStatus(
        name=name,
        tier=tier,
        label=_label_for(name, tier),
        why=why,
        signals=signals or {},
        contract=contract or {},
    )


def _data_pillar() -> PillarStatus:
    """DATA — Reliable / Stale / Broken.

    Healthy when recent decision snapshots have no source_errors and
    market data is fresh. Looks at source_errors in the last 30
    persisted snapshots as the primary signal.
    """
    contract = {
        "ok":  "< 10% of recent snapshots reported source_errors",
        "mid": "10-30% of recent snapshots reported source_errors",
        "bad": "≥ 30% of recent snapshots reported source_errors",
    }
    err_rate = None
    sample = 0
    try:
        import json as _json
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            rows = (s.query(Trade)
                          .order_by(Trade.id.desc())
                          .limit(30).all())
            if rows:
                sample = len(rows)
                n_err = 0
                for r in rows:
                    if not r.detail_json:
                        continue
                    try:
                        dj = _json.loads(r.detail_json)
                    except Exception:
                        continue
                    if dj.get("source_errors"):
                        n_err += 1
                err_rate = n_err / len(rows)
    except Exception as exc:
        logger.debug("data pillar trade scan failed: %s", exc)

    signals = {"source_error_rate": err_rate, "samples": sample}

    if err_rate is None or sample == 0:
        return _pillar("data", "unknown",
                            "no recent snapshots to evaluate yet",
                            signals=signals, contract=contract)
    if err_rate >= 0.30:
        return _pillar("data", "bad",
                            f"source errors in {err_rate*100:.0f}% of last {sample} snapshots",
                            signals=signals, contract=contract)
    if err_rate >= 0.10:
        return _pillar("data", "mid",
                            f"source errors in {err_rate*100:.0f}% of last {sample} snapshots",
                            signals=signals, contract=contract)
    return _pillar("data", "ok",
                        f"clean — 0 source errors in last {sample} snapshots"
                        if err_rate == 0
                        else f"only {err_rate*100:.0f}% errors in last {sample} snapshots",
                        signals=signals, contract=contract)


def _model_pillar() -> PillarStatus:
    """MODEL — Reliable / Slipping / Drifting / Warming-up.

    Healthy when ECE ≤ 0.05 AND brier ≤ 0.20 AND no strategy halted.
    Slipping in the warning band; Drifting on breach or any halt.

    **Sample-size floor (added 2026-06-02):** ECE and Brier are
    mathematically unstable below ~30 closed trades — one bad prediction
    in a sparse bin can push ECE to 0.8+. Below the floor we return a
    "warming up" verdict instead of falsely claiming the model is
    drifting. The 30 threshold matches ``label_quality``'s own
    "need ≥ 30 for stable metrics" warning.
    """
    contract = {
        "ok":  "ECE ≤ 0.05 AND brier ≤ 0.20 AND no strategy halts (N ≥ 30 closed)",
        "mid": "ECE in (0.05, 0.10] OR brier in (0.20, 0.25] (N ≥ 30 closed)",
        "bad": "ECE > 0.10 OR brier > 0.25 OR ≥ 1 strategy halted (N ≥ 30 closed)",
        "warmup": "fewer than 30 closed trades — ECE/Brier statistically unstable",
    }
    ece = brier = None
    closed_n = 0
    halts: List[Any] = []
    try:
        from backend.api.routes.metrics import build_summary
        summary = build_summary()
        data = summary.get("data") or {}
        ece = data.get("calibration_error")
        brier = data.get("brier")
        label_quality = summary.get("label_quality") or {}
        # `label_quality()` returns the key as ``closed`` (not ``n_closed``).
        closed_n = int(label_quality.get("closed") or 0)
    except Exception as exc:
        logger.debug("model pillar metrics failed: %s", exc)
    try:
        from backend.bot.drift.auto_halt import list_halts
        halts = list_halts() or []
    except Exception as exc:
        logger.debug("model pillar halt list failed: %s", exc)

    signals = {"ece": ece, "brier": brier, "halts": len(halts),
                  "closed_trades": closed_n}

    if ece is None and brier is None and not halts:
        why = (f"warming up · {closed_n}/30 closed trades — "
                  "ECE/Brier need a corpus to score")
        return _pillar("model", "unknown", why,
                            signals=signals, contract=contract)

    # Sample-size floor — below 30 closed trades, ECE/Brier are noise.
    # Halts still take precedence (those are deterministic, not statistical).
    MIN_SAMPLES = 30
    if closed_n < MIN_SAMPLES and not halts:
        why = (f"warming up · {closed_n}/{MIN_SAMPLES} closed trades — "
                  f"ECE/Brier need ≥{MIN_SAMPLES} samples to stabilise")
        return _pillar("model", "unknown", why,
                            signals=signals, contract=contract)

    bad = (
        (ece is not None and ece > 0.10)
        or (brier is not None and brier > 0.25)
        or len(halts) >= 1
    )
    mid = (
        (ece is not None and ece > 0.05)
        or (brier is not None and brier > 0.20)
    )

    if bad:
        # Differentiate baseline miscalibration (high ECE but stable) from
        # genuine drift (ECE worsening across rolling windows). Both are
        # tier=bad, but the operator's response is different: miscalibration
        # → recalibrate strategy confidence formulas; drift → investigate
        # recent regime/data change.
        ece_stability_std = None
        try:
            ece_stability_std = float(
                (summary.get("data") or {}).get("calibration_error_stability_std")
            )
        except (TypeError, ValueError):
            ece_stability_std = None
        is_drifting = (
            ece_stability_std is not None and ece_stability_std > 0.04
        )
        if halts:
            why = f"{len(halts)} strategy halt(s) active"
        elif ece is not None and ece > 0.10:
            if is_drifting:
                why = (f"ECE {ece:.3f} breaches 0.10 band · "
                          f"drifting (std {ece_stability_std:.3f})")
            else:
                why = (f"ECE {ece:.3f} breaches 0.10 band · "
                          f"baseline miscalibration (stable over windows)")
        else:
            why = f"brier {brier:.3f} breaches 0.25 band"
        return _pillar("model", "bad", why, signals=signals, contract=contract)
    if mid:
        why = (f"ECE {ece:.3f}" if (ece is not None and ece > 0.05)
                else f"brier {brier:.3f}")
        why += " in warning band — watching"
        return _pillar("model", "mid", why, signals=signals, contract=contract)
    why_parts = []
    if ece is not None: why_parts.append(f"ECE {ece:.3f}")
    if brier is not None: why_parts.append(f"brier {brier:.3f}")
    why_parts.append("no halts")
    return _pillar("model", "ok", " · ".join(why_parts),
                        signals=signals, contract=contract)


def _council_pillar(recent_consensus: List[Dict[str, Any]]) -> tuple[PillarStatus, float, int]:
    """COUNCIL — Aligned / Divided / Conflicted.

    Reads recent persisted Consensus dicts. Returns the pillar status
    AND the aggregated dissent_share + window size so callers can
    surface dissent as its own Spine field.
    """
    contract = {
        "ok":  "quorum met ≥ 80% AND mean dissent ≤ 25%",
        "mid": "quorum met 50-80% OR mean dissent in (25%, 40%]",
        "bad": "quorum met < 50% OR mean dissent > 40%",
    }
    # Only consider trades where the consensus picked a side — when
    # consensus_stance is itself "abstain", dissent_share is
    # structurally meaningless (Stage-20c semantic fix). Filtering
    # avoids the artificial 100%-dissent reading from old persisted
    # blobs predating that fix.
    decided = [c for c in recent_consensus
                    if c.get("stance") not in (None, "abstain")]
    n = len(decided)
    if n == 0:
        return (
            _pillar("council", "unknown",
                       "no recent decisive consensus to evaluate",
                       contract=contract),
            0.0, 0,
        )
    quorum_met = sum(1 for c in decided if c.get("quorum_met") is not False)
    dissent_shares = []
    for c in decided:
        ch = c.get("chairman_report") or {}
        d = ch.get("dissent") or {}
        if d.get("dissent_share") is not None:
            dissent_shares.append(float(d["dissent_share"]))
    mean_dissent = (sum(dissent_shares) / len(dissent_shares)
                          if dissent_shares else 0.0)
    quorum_rate = quorum_met / n

    signals = {
        "window": n,
        "quorum_met_rate": round(quorum_rate, 3),
        "mean_dissent_share": round(mean_dissent, 3),
    }
    if quorum_rate < 0.50 or mean_dissent > 0.40:
        why = (f"quorum met only {quorum_rate*100:.0f}% of last {n}"
                  if quorum_rate < 0.50
                  else f"mean dissent {mean_dissent*100:.0f}% over last {n}")
        status = _pillar("council", "bad", why, signals=signals, contract=contract)
    elif quorum_rate < 0.80 or mean_dissent > 0.25:
        why = (f"quorum met {quorum_rate*100:.0f}% of last {n}"
                  if quorum_rate < 0.80
                  else f"mean dissent {mean_dissent*100:.0f}% (elevated)")
        status = _pillar("council", "mid", why, signals=signals, contract=contract)
    else:
        why = (f"quorum {quorum_rate*100:.0f}% · "
                  f"dissent {mean_dissent*100:.0f}% over last {n}")
        status = _pillar("council", "ok", why, signals=signals, contract=contract)
    return status, mean_dissent, n


def _risk_pillar() -> PillarStatus:
    """RISK — Controlled / Stretched / Breached."""
    contract = {
        "ok":  "drawdown < 5% AND no concentration flags AND |β| ≤ 1.5",
        "mid": "drawdown 5-8% OR 1 flag OR |β| in (1.5, 1.8]",
        "bad": "drawdown > 8% OR ≥ 2 flags OR |β| > 1.8",
    }
    try:
        from backend.db import session_scope
        from backend.models.trade import Trade
        from backend.bot.portfolio_intel import assess_portfolio
        with session_scope() as s:
            open_trades = (s.query(Trade)
                                 .filter(Trade.status == "open")
                                 .all())
            positions = []
            for t in open_trades:
                positions.append({
                    "ticker": t.ticker,
                    "quantity": t.quantity,
                    "price": t.price,
                    "instrument": t.instrument,
                    "action": t.action,
                })
        risk = assess_portfolio(positions)
        drawdown = float(getattr(risk, "drawdown_pct", 0.0) or 0.0)
        flags = getattr(risk, "concentration_flags", []) or []
        net_beta = float(getattr(risk, "net_beta", 0.0) or 0.0)
    except Exception as exc:
        logger.debug("risk pillar failed: %s", exc)
        return _pillar("risk", "unknown",
                            "portfolio risk unavailable",
                            contract=contract)

    signals = {
        "drawdown_pct": round(drawdown, 4),
        "concentration_flag_count": len(flags),
        "net_beta": round(net_beta, 2),
    }
    if drawdown > 0.08 or len(flags) >= 2 or abs(net_beta) > 1.8:
        if drawdown > 0.08:
            why = f"drawdown {drawdown*100:.1f}% breaches 8% band"
        elif len(flags) >= 2:
            why = f"{len(flags)} concentration flags"
        else:
            why = f"net β {net_beta:+.2f} extreme"
        return _pillar("risk", "bad", why, signals=signals, contract=contract)
    if drawdown > 0.05 or len(flags) >= 1 or abs(net_beta) > 1.5:
        if drawdown > 0.05:
            why = f"drawdown {drawdown*100:.1f}% in warning band"
        elif len(flags) >= 1:
            why = f"1 concentration flag"
        else:
            why = f"net β {net_beta:+.2f} stretched"
        return _pillar("risk", "mid", why, signals=signals, contract=contract)
    return _pillar("risk", "ok",
                        f"drawdown {drawdown*100:.1f}% · β {net_beta:+.2f} · no flags",
                        signals=signals, contract=contract)


def _execution_pillar() -> PillarStatus:
    """EXECUTION — Ready / Strained / Impaired.

    Uses execution_intel.insights() over the recent fills. Strained
    when slippage is elevated; Impaired when fills are failing.
    """
    contract = {
        "ok":  "≥ 90% fill rate AND median slippage ≤ 8 bps",
        "mid": "fill rate 75-90% OR median slippage 8-20 bps",
        "bad": "fill rate < 75% OR median slippage > 20 bps",
    }
    try:
        from backend.bot.execution_intel import insights
        ins = insights(limit=200)
    except Exception as exc:
        logger.debug("execution pillar failed: %s", exc)
        return _pillar("execution", "unknown",
                            "execution telemetry unavailable",
                            contract=contract)

    if not ins or not ins.get("count"):
        return _pillar("execution", "unknown",
                            "no recent fills to evaluate",
                            contract=contract)

    fill_rate = ins.get("fill_rate")
    median_slip_bps = ins.get("median_slippage_bps")
    signals = {"fill_rate": fill_rate, "median_slippage_bps": median_slip_bps,
                  "samples": ins.get("count")}

    bad = (fill_rate is not None and fill_rate < 0.75) or \
              (median_slip_bps is not None and median_slip_bps > 20)
    mid = (fill_rate is not None and fill_rate < 0.90) or \
              (median_slip_bps is not None and median_slip_bps > 8)

    if bad:
        why = (f"fill rate {fill_rate*100:.0f}%"
                  if (fill_rate or 1.0) < 0.75
                  else f"median slip {median_slip_bps:.0f} bps")
        return _pillar("execution", "bad", why, signals=signals, contract=contract)
    if mid:
        why = (f"fill rate {fill_rate*100:.0f}%"
                  if (fill_rate or 1.0) < 0.90
                  else f"median slip {median_slip_bps:.0f} bps")
        return _pillar("execution", "mid", why, signals=signals, contract=contract)
    why = (f"fill rate {fill_rate*100:.0f}%"
              if fill_rate is not None
              else "fills clean")
    if median_slip_bps is not None:
        why += f" · slip {median_slip_bps:.0f} bps"
    return _pillar("execution", "ok", why, signals=signals, contract=contract)


def _learning_pillar() -> PillarStatus:
    """LEARNING — Active / Quiet / Stalled.

    Active when the adaptive layer is producing recent autopsies and
    lessons. Quiet when low activity. Stalled when there's no recent
    learning output despite trades being closed.
    """
    contract = {
        "ok":  "≥ 3 autopsies in last 7d OR ≥ 1 lesson surfaced AND attribution growing",
        "mid": "some recent activity but below thresholds",
        "bad": "no autopsies/lessons in last 14d despite closed trades",
    }
    autopsies = []
    lessons = []
    attribution_size = 0
    try:
        from backend.bot.autopsy import autopsy_recent_losses
        ar = autopsy_recent_losses(limit=100)
        autopsies = (ar or {}).get("autopsies", []) or []
    except Exception as exc:
        logger.debug("learning pillar autopsy failed: %s", exc)
    try:
        from backend.bot.journal import build_lessons
        lessons = build_lessons(limit=2000).lessons if hasattr(build_lessons(limit=2000), "lessons") else []
    except Exception as exc:
        logger.debug("learning pillar journal failed: %s", exc)
        lessons = []
    try:
        from backend.bot.source_attribution import compute_contributions
        rep = compute_contributions(limit=1000, min_trades=10)
        attribution_size = rep.closed_trades or 0
    except Exception as exc:
        logger.debug("learning pillar attribution failed: %s", exc)

    # Count autopsies inside last 7 / 14 days.
    now = datetime.utcnow()
    autopsy_7d = 0
    autopsy_14d = 0
    for a in autopsies:
        try:
            ts = a.get("timestamp") or a.get("closed_at")
            if isinstance(ts, str):
                dt = datetime.fromisoformat(ts.replace("Z", ""))
            else:
                continue
            age = now - dt
            if age <= timedelta(days=7):
                autopsy_7d += 1
            if age <= timedelta(days=14):
                autopsy_14d += 1
        except Exception:
            continue

    n_lessons = len(lessons or [])
    signals = {
        "autopsies_7d": autopsy_7d,
        "autopsies_14d": autopsy_14d,
        "lessons": n_lessons,
        "attribution_trades": attribution_size,
    }
    # Count closed trades in the same window to know if the system
    # SHOULD have produced learning output.
    closed_14d = 0
    try:
        from backend.db import session_scope
        from backend.models.trade import Trade
        cutoff = now - timedelta(days=14)
        with session_scope() as s:
            closed_14d = (s.query(Trade)
                                 .filter(Trade.status == "closed")
                                 .filter(Trade.timestamp >= cutoff)
                                 .count())
    except Exception:
        closed_14d = 0

    # Stalled: real trades but no learning output.
    if closed_14d >= 5 and autopsy_14d == 0 and n_lessons == 0:
        return _pillar("learning", "bad",
                            f"{closed_14d} closed trades in 14d but no autopsies or lessons",
                            signals=signals, contract=contract)
    # Active: recent autopsies OR meaningful lessons + growing attribution corpus.
    if autopsy_7d >= 3 or (n_lessons >= 1 and attribution_size >= 10):
        parts = []
        if autopsy_7d: parts.append(f"{autopsy_7d} autopsies last 7d")
        if n_lessons: parts.append(f"{n_lessons} lessons surfaced")
        if attribution_size: parts.append(f"attribution {attribution_size}t")
        return _pillar("learning", "ok", " · ".join(parts) or "active",
                            signals=signals, contract=contract)
    # Quiet: some output but below thresholds.
    parts = []
    if autopsy_14d: parts.append(f"{autopsy_14d} autopsies last 14d")
    if n_lessons: parts.append(f"{n_lessons} lessons")
    if attribution_size: parts.append(f"attribution {attribution_size}t")
    if not parts: parts.append("waiting for sample size")
    return _pillar("learning", "mid",
                        " · ".join(parts),
                        signals=signals, contract=contract)


# ── confidence + attention derivation ──────────────────────────────────


AUTHORITY_CONFIDENCE = ("CONFIDENT", "WATCHING", "RESTRICTED")


def _compute_confidence(pillars: List[PillarStatus],
                              mean_dissent: float) -> tuple[str, str]:
    """Derive Authority Confidence + reason from pillar tiers + dissent.

    Contract (published — operator can audit):
      • CONFIDENT  — all pillars 'ok' AND mean dissent ≤ 25%
      • WATCHING   — any 1 pillar 'mid' OR dissent in (25%, 40%]
      • RESTRICTED — any pillar 'bad' OR ≥ 2 pillars 'mid' OR dissent > 40%
    Unknown pillars are tolerated up to 2; ≥ 3 unknowns → WATCHING.
    """
    tiers = [p.tier for p in pillars]
    bad = [p for p in pillars if p.tier == "bad"]
    mid = [p for p in pillars if p.tier == "mid"]
    unknown = [p for p in pillars if p.tier == "unknown"]

    if bad or len(mid) >= 2 or mean_dissent > 0.40:
        if bad:
            reason = f"{bad[0].label} {bad[0].name.upper()} — {bad[0].why}"
        elif mean_dissent > 0.40:
            reason = f"Dissent elevated to {mean_dissent*100:.0f}%"
        else:
            reason = (f"{mid[0].label} {mid[0].name.upper()} + "
                          f"{mid[1].label} {mid[1].name.upper()}")
        return "RESTRICTED", reason
    if mid or mean_dissent > 0.25 or len(unknown) >= 3:
        if mid:
            reason = f"{mid[0].label} {mid[0].name.upper()} — {mid[0].why}"
        elif mean_dissent > 0.25:
            reason = f"Dissent at {mean_dissent*100:.0f}%"
        else:
            reason = f"{len(unknown)} pillars not yet evaluable"
        return "WATCHING", reason
    return "CONFIDENT", "All pillars reliable · dissent within band"


def _dissent_label(share: float) -> str:
    if share > 0.40:
        return "High"
    if share > 0.25:
        return "Elevated"
    return "Normal"


def _compute_attention(
    pillars: List[PillarStatus],
    confidence: str,
    mean_dissent: float,
    dissent_window: int,
) -> Dict[str, Any]:
    """Pick the single most important thing the operator should look at.

    Severity matches Confidence:
      • CONFIDENT   → 'low' (no action required)
      • WATCHING    → 'medium' (worst mid-tier pillar or dissent)
      • RESTRICTED  → 'high'   (worst bad-tier pillar or breach)
    """
    bad = [p for p in pillars if p.tier == "bad"]
    mid = [p for p in pillars if p.tier == "mid"]

    if bad:
        p = bad[0]
        return {
            "severity": "high",
            "title": f"{p.label} {p.name.upper()}",
            "detail": p.why,
            "pillar": p.name,
        }
    if mean_dissent > 0.40 and dissent_window:
        return {
            "severity": "high",
            "title": "Dissent elevated",
            "detail": f"Mean dissent {mean_dissent*100:.0f}% over last {dissent_window} decisions",
            "pillar": "council",
        }
    if mid:
        p = mid[0]
        return {
            "severity": "medium",
            "title": f"{p.label} {p.name.upper()}",
            "detail": p.why,
            "pillar": p.name,
        }
    if mean_dissent > 0.25 and dissent_window:
        return {
            "severity": "medium",
            "title": "Dissent rising",
            "detail": f"Mean dissent {mean_dissent*100:.0f}% over last {dissent_window} decisions",
            "pillar": "council",
        }
    return {
        "severity": "low",
        "title": "No action required",
        "detail": "All pillars reliable · system operating within normal band",
        "pillar": None,
    }


# ── main entry point ──────────────────────────────────────────────────


def _load_recent_consensus(limit: int = 50) -> List[Dict[str, Any]]:
    """Pull recent Consensus blobs out of Trade.detail_json."""
    import json as _json
    out: List[Dict[str, Any]] = []
    try:
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            rows = (s.query(Trade)
                          .order_by(Trade.id.desc())
                          .limit(limit).all())
            for r in rows:
                if not r.detail_json:
                    continue
                try:
                    dj = _json.loads(r.detail_json)
                except Exception:
                    continue
                cons = dj.get("consensus")
                if cons:
                    out.append(cons)
    except Exception as exc:
        logger.debug("consensus load failed: %s", exc)
    return out


def _next_cycle_eta_sec() -> Optional[int]:
    """Best-effort ETA to next engine cycle. Reads the configured
    live_interval_sec and assumes the scheduler is running."""
    try:
        from backend.db import session_scope
        from backend.models.config import load_config
        with session_scope() as s:
            cfg = load_config(s)
        return int(cfg.get("live_interval_sec") or 30)
    except Exception:
        return None


def _last_decision_age_sec() -> Optional[int]:
    try:
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            row = (s.query(Trade)
                          .order_by(Trade.timestamp.desc())
                          .first())
            if not row or not row.timestamp:
                return None
            age = datetime.utcnow() - row.timestamp
            return int(age.total_seconds())
    except Exception:
        return None


_STATUS_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None}
_STATUS_CACHE_TTL = 3.0  # seconds — every chip + page polls this; without
                          # caching the 6-pillar compute happens N times
                          # per second across surfaces. Probe (2026-06-02)
                          # showed 808ms/call on t4g.small; cache drops it
                          # to <5ms for cached calls.


def get_authority_status() -> Dict[str, Any]:
    """The Authority Spine payload. Read-only, deterministic on
    backend state, safe to call from any UI surface every few seconds.

    Cached for ``_STATUS_CACHE_TTL`` seconds (per-process). The cache key
    is implicit (single payload); we don't memoize by argument because
    the function takes none. A 3s TTL means each chip's poll within a
    cycle is shared while still reflecting fresh state in the same
    second.
    """
    import time as _time
    now = _time.monotonic()
    cached = _STATUS_CACHE.get("payload")
    if cached is not None and (now - _STATUS_CACHE.get("ts", 0)) < _STATUS_CACHE_TTL:
        return cached
    consensus = _load_recent_consensus(limit=50)
    data = _data_pillar()
    model = _model_pillar()
    council, mean_dissent, dissent_window = _council_pillar(consensus)
    risk = _risk_pillar()
    execution = _execution_pillar()
    learning = _learning_pillar()

    pillars = [data, model, council, risk, execution, learning]
    confidence, confidence_reason = _compute_confidence(pillars, mean_dissent)
    attention = _compute_attention(
        pillars, confidence, mean_dissent, dissent_window,
    )

    pillar_summary = {
        "ok": sum(1 for p in pillars if p.tier == "ok"),
        "mid": sum(1 for p in pillars if p.tier == "mid"),
        "bad": sum(1 for p in pillars if p.tier == "bad"),
        "unknown": sum(1 for p in pillars if p.tier == "unknown"),
    }
    summary = (
        f"{pillar_summary['ok']}/{len(pillars)} "
        f"{'reliable' if pillar_summary['bad']==0 and pillar_summary['mid']==0 else 'tracked'}"
        + (f" · {pillar_summary['mid']} watching" if pillar_summary['mid'] else "")
        + (f" · {pillar_summary['bad']} breached" if pillar_summary['bad'] else "")
        + (f" · {pillar_summary['unknown']} unknown" if pillar_summary['unknown'] else "")
    )

    payload = {
        "authority_level": _current_authority_level(),
        "authority_confidence": confidence,
        "confidence_reason": confidence_reason,
        "dissent": {
            "label": _dissent_label(mean_dissent),
            "share": round(mean_dissent, 3),
            "window": dissent_window,
        },
        "pillars": {p.name: p.to_dict() for p in pillars},
        "pillar_summary": pillar_summary,
        "summary_line": summary,
        "attention": attention,
        "next_cycle_eta_sec": _next_cycle_eta_sec(),
        "last_decision_age_sec": _last_decision_age_sec(),
        "computed_at": datetime.utcnow().isoformat(),
    }
    _STATUS_CACHE["ts"] = now
    _STATUS_CACHE["payload"] = payload
    return payload


def get_pillar_detail(name: str) -> Optional[Dict[str, Any]]:
    """Re-run one pillar and return its full signals + contract."""
    if name not in PILLAR_NAMES:
        return None
    if name == "data":     return _data_pillar().to_dict()
    if name == "model":    return _model_pillar().to_dict()
    if name == "risk":     return _risk_pillar().to_dict()
    if name == "execution": return _execution_pillar().to_dict()
    if name == "learning": return _learning_pillar().to_dict()
    if name == "council":
        status, _, _ = _council_pillar(_load_recent_consensus(50))
        return status.to_dict()
    return None
