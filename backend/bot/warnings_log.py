"""In-memory ring buffer of WARNING + ERROR log records.

Purpose: the operator should never have to `tail` a log file to know
what's failing. Every WARNING/ERROR the backend emits lands in this
ring buffer and is exposed via ``GET /system/warnings`` so the
Authority Spine can surface them.

Design:
  • Bounded ring (default 200 entries) — newest at the front
  • Stores ``timestamp · level · logger · message · path · line``
  • Captured by attaching a ``RingHandler`` to the root logger
  • Thread-safe via ``threading.Lock`` (FastAPI is async but workers
    use threads via the executor; pytest also tickles this)

This module is import-safe — calling ``install()`` more than once is
idempotent. The handler installs at module import time when called
from ``main.py`` so we catch warnings from the startup phase too.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional


# Default buffer size; override via env var TB_WARNINGS_BUFFER_SIZE.
_BUFFER_SIZE = 200


@dataclass
class WarningRecord:
    timestamp: str           # ISO 8601 UTC
    level: str               # WARNING | ERROR | CRITICAL
    logger: str              # logger.name
    message: str             # rendered message
    path: str                # source file basename
    line: int                # line number
    exc_type: Optional[str]  # exception class name if any
    exc_summary: Optional[str]  # first 280 chars of exception repr

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RingHandler(logging.Handler):
    """Capture WARNING+ records into a bounded thread-safe deque."""

    def __init__(self, maxlen: int = _BUFFER_SIZE) -> None:
        super().__init__(level=logging.WARNING)
        self._buf: Deque[WarningRecord] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        # Quiet a few noisy libraries that emit WARNINGs on routine
        # operation (urllib3 retries, etc.) — keep them in the log but
        # not in the operator-facing surface.
        self._suppress_loggers = {
            "urllib3.connectionpool",
            "urllib3.retry",
            "yfinance",
        }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(tuple(self._suppress_loggers)):
                return
            msg = record.getMessage()
            exc_type = None
            exc_summary = None
            if record.exc_info and record.exc_info[0] is not None:
                exc_type = record.exc_info[0].__name__
                try:
                    exc_summary = repr(record.exc_info[1])[:280]
                except Exception:
                    exc_summary = None
            entry = WarningRecord(
                timestamp=datetime.utcnow().isoformat(),
                level=record.levelname,
                logger=record.name,
                message=msg[:500],
                path=record.pathname.rsplit("/", 1)[-1],
                line=record.lineno,
                exc_type=exc_type,
                exc_summary=exc_summary,
            )
            with self._lock:
                self._buf.append(entry)
        except Exception:
            # Logging handlers MUST NOT raise — would crash the
            # emitting code path. Worst case: we drop the record.
            pass

    def snapshot(self, limit: int = 200,
                      level: Optional[str] = None) -> List[Dict[str, Any]]:
        """Most recent records first (newest → oldest)."""
        with self._lock:
            items = list(self._buf)
        items.reverse()
        if level:
            level = level.upper()
            items = [i for i in items if i.level == level]
        return [i.to_dict() for i in items[:limit]]

    def clear(self) -> int:
        with self._lock:
            n = len(self._buf)
            self._buf.clear()
        return n

    def counts(self) -> Dict[str, int]:
        with self._lock:
            items = list(self._buf)
        out: Dict[str, int] = {"WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        for it in items:
            if it.level in out:
                out[it.level] += 1
        out["total"] = len(items)
        return out


# Module-level singleton — one handler attached to the root logger.
_handler: Optional[RingHandler] = None
_install_lock = threading.Lock()


def install() -> RingHandler:
    """Attach the ring handler to the root logger.

    Idempotent: subsequent calls return the existing handler.
    """
    global _handler
    with _install_lock:
        if _handler is None:
            import os
            try:
                maxlen = int(os.getenv("TB_WARNINGS_BUFFER_SIZE", "") or _BUFFER_SIZE)
            except (TypeError, ValueError):
                maxlen = _BUFFER_SIZE
            _handler = RingHandler(maxlen=maxlen)
            logging.getLogger().addHandler(_handler)
        return _handler


def handler() -> RingHandler:
    """Get the singleton (installing if needed)."""
    return install()
