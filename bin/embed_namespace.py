#!/usr/bin/env python
"""MITS Phase 11.1 — single-namespace embed driver.

A thin wrapper around the existing ``bin/embed_corpus.py`` walkers
that lets the operator run ONE namespace at a time with explicit
batch-size + memory-guard knobs. This is the runner the nightly
``_embed_new_rows_pass`` cron uses; the operator also invokes it
manually after a Phase 11 backfill lands new rows.

Why a separate script? embed_corpus.py loads sentence-transformers
and the FinBERT model into memory; on a t4g.small (3.7 GB total) we
can't afford to walk all 5 namespaces in a single process. By
exec'ing per-namespace we get a clean OS-level memory reset between
runs.

Usage:

    # One namespace, default batch size.
    python bin/embed_namespace.py --namespace news_paragraph

    # All 5 in sequence (forks subprocesses).
    python bin/embed_namespace.py --namespace all

    # Cap memory: bail if usage > 85%, sleep + retry between batches.
    python bin/embed_namespace.py --namespace news_paragraph \\
        --batch-size 500 --pause-between-batches 1.0
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Namespace → embed_corpus.py --kinds value (1:1 today; kept as a map
# so future namespaces can fan out to multiple walkers without changing
# the operator's interface).
NAMESPACE_TO_KIND: Dict[str, str] = {
    "news_paragraph": "news",
    "earnings_call_paragraph": "earnings",
    "insider_form4_narrative": "insider",
    "fund_holding_change": "fund_holdings",
    "regime_snapshot_v2": "regime",
}


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s — %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout)


logger = logging.getLogger("embed_namespace")


def _memory_pressure_ok() -> bool:
    try:
        from backend.bot.data.memory_guard import memory_pressure_ok
        return memory_pressure_ok()
    except Exception:
        try:
            import psutil  # type: ignore
            return psutil.virtual_memory().percent < 85.0
        except Exception:
            return True


def _wait_until_memory_ok(*, max_wait_sec: int = 300,
                              sleep_sec: int = 30) -> bool:
    try:
        from backend.bot.data.memory_guard import wait_until_ok
        return wait_until_ok(max_seconds=max_wait_sec,
                                  sleep_seconds=sleep_sec)
    except Exception:
        waited = 0
        while waited < max_wait_sec:
            if _memory_pressure_ok():
                return True
            logger.warning("memory pressure high — sleeping %ds", sleep_sec)
            time.sleep(sleep_sec)
            waited += sleep_sec
        return _memory_pressure_ok()


def run_one(namespace: str, *, batch_size: int,
              pause_between_batches: float,
              forked: bool = False) -> int:
    """Run the embed walker for a single namespace.

    When ``forked=True``, exec ``embed_corpus.py`` as a subprocess so
    its OOM blast radius is bounded. When ``forked=False``, import +
    call in-process — useful for tests + dry-runs.
    """
    kind = NAMESPACE_TO_KIND.get(namespace)
    if not kind:
        logger.error("unknown namespace: %s — known: %s",
                          namespace, list(NAMESPACE_TO_KIND.keys()))
        return 2

    if not _memory_pressure_ok():
        logger.warning("memory pressure high — waiting up to 5min...")
        if not _wait_until_memory_ok():
            logger.error("memory pressure didn't clear — aborting %s",
                              namespace)
            return 3

    embed_script = _REPO_ROOT / "bin" / "embed_corpus.py"
    if not embed_script.exists():
        logger.error("embed_corpus.py not found at %s", embed_script)
        return 4

    # NOTE — ``embed_corpus.py`` exposes only ``--kinds``, ``--start``,
    # ``--end``, ``-v``. The legacy interface passed ``--batch-size``
    # which would be silently ignored; argparse now treats it as an
    # error. Batch size is taken from ``TUNABLES.embed_batch_size``
    # inside the walker, so we just don't pass it on. We keep
    # ``batch_size`` in this function's signature so existing callers
    # (cron + tests) don't need to change.
    if forked:
        cmd = [
            sys.executable, str(embed_script),
            "--kinds", kind,
        ]
        logger.info(
            "forking embed_corpus for namespace=%s kind=%s "
            "(batch_size=%d via TUNABLES) ...",
            namespace, kind, batch_size,
        )
        proc = subprocess.run(cmd, check=False)
        return int(proc.returncode or 0)

    # In-process path — embed_corpus.main(...).
    from importlib import import_module
    try:
        emb = import_module("bin.embed_corpus")  # type: ignore
    except ModuleNotFoundError:
        # bin/ isn't a package — load via runpy.
        import runpy
        # runpy returns the module globals; we want the `main` function.
        ns = runpy.run_path(str(embed_script), run_name="__embed_namespace__")
        main_fn = ns.get("main")
        if main_fn is None:
            logger.error("embed_corpus.py has no main() — cannot run inline")
            return 4
    else:
        main_fn = getattr(emb, "main", None)
        if main_fn is None:
            logger.error("embed_corpus.main missing")
            return 4
    argv = [
        "--kinds", kind,
    ]
    try:
        rc = int(main_fn(argv) or 0)
    except SystemExit as e:
        rc = int(getattr(e, "code", 0) or 0)
    return rc


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "MITS Phase 11.1 — single-namespace embed runner. Wraps "
            "bin/embed_corpus.py for OS-level memory isolation."
        ),
    )
    parser.add_argument(
        "--namespace", default="all",
        help=("Namespace to embed (default 'all'). One of: "
              + ", ".join(NAMESPACE_TO_KIND.keys()) + ", or 'all'."),
    )
    parser.add_argument(
        "--batch-size", type=int, default=None,
        help=("Batch size passed to embed_corpus.py. Defaults to "
              "TUNABLES.embed_batch_size (1000)."),
    )
    parser.add_argument(
        "--pause-between-batches", type=float, default=None,
        help=("Sleep seconds between embed batches — gives memory + "
              "Postgres pool a chance to recover. Defaults to "
              "TUNABLES.embed_pause_between_batches_sec (0.5)."),
    )
    parser.add_argument(
        "--inline", action="store_true",
        help=("Run embed_corpus.main in-process instead of forking. "
              "Default is fork because the sentence-transformer model "
              "doesn't release GPU/CPU memory cleanly on free."),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="DEBUG logging",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    from backend.config import TUNABLES
    batch_size = args.batch_size or int(
        getattr(TUNABLES, "embed_batch_size", 1000))
    pause = args.pause_between_batches if args.pause_between_batches \
        is not None else float(
            getattr(TUNABLES, "embed_pause_between_batches_sec", 0.5))

    if args.namespace == "all":
        namespaces = list(NAMESPACE_TO_KIND.keys())
    else:
        if args.namespace not in NAMESPACE_TO_KIND:
            logger.error("unknown namespace: %s", args.namespace)
            return 2
        namespaces = [args.namespace]

    grand_rc = 0
    for ns in namespaces:
        if not _memory_pressure_ok():
            logger.warning(
                "memory pressure mid-loop — stopping early at ns=%s", ns)
            grand_rc = 5
            break
        rc = run_one(ns, batch_size=batch_size,
                        pause_between_batches=pause,
                        forked=not args.inline)
        logger.info("namespace=%s exit_rc=%d", ns, rc)
        if rc != 0:
            grand_rc = rc
        # Defensive sleep between namespaces — buys the OS a few
        # seconds to reclaim the freed model memory.
        time.sleep(max(2.0, pause * 4))
    logger.info("embed_namespace DONE — grand_rc=%d", grand_rc)
    return grand_rc


if __name__ == "__main__":
    raise SystemExit(main())
