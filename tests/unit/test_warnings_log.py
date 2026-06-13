"""Locks the warnings ring-buffer behavior.

If this test fails, the operator's "ah, something's wrong" surface
on the Authority Spine is broken — log records no longer flow into
the in-memory buffer, or the snapshot API drifted.
"""
import logging

from backend.bot.warnings_log import RingHandler, handler, install


class TestRingHandler:
    def test_captures_warning_and_error_levels(self):
        h = RingHandler(maxlen=10)
        logger = logging.getLogger("tests.warnings.capture")
        logger.addHandler(h)
        logger.setLevel(logging.DEBUG)
        try:
            logger.debug("debug msg — should NOT be captured")
            logger.info("info msg — should NOT be captured")
            logger.warning("warning msg — captured")
            logger.error("error msg — captured")
            counts = h.counts()
            assert counts["WARNING"] == 1
            assert counts["ERROR"] == 1
            assert counts["total"] == 2
        finally:
            logger.removeHandler(h)

    def test_snapshot_newest_first(self):
        h = RingHandler(maxlen=10)
        logger = logging.getLogger("tests.warnings.order")
        logger.addHandler(h)
        try:
            logger.warning("first")
            logger.warning("second")
            logger.warning("third")
            recs = h.snapshot(limit=5)
            assert [r["message"] for r in recs] == ["third", "second", "first"]
        finally:
            logger.removeHandler(h)

    def test_level_filter(self):
        h = RingHandler(maxlen=10)
        logger = logging.getLogger("tests.warnings.filter")
        logger.addHandler(h)
        try:
            logger.warning("a")
            logger.error("b")
            logger.warning("c")
            assert len(h.snapshot(level="WARNING")) == 2
            assert len(h.snapshot(level="ERROR")) == 1
        finally:
            logger.removeHandler(h)

    def test_clear_returns_count(self):
        h = RingHandler(maxlen=10)
        logger = logging.getLogger("tests.warnings.clear")
        logger.addHandler(h)
        try:
            for _ in range(5):
                logger.warning("noise")
            assert h.clear() == 5
            assert h.counts()["total"] == 0
        finally:
            logger.removeHandler(h)

    def test_exception_info_captured(self):
        h = RingHandler(maxlen=10)
        logger = logging.getLogger("tests.warnings.exc")
        logger.addHandler(h)
        try:
            try:
                raise ValueError("boom")
            except ValueError:
                logger.warning("caught it", exc_info=True)
            rec = h.snapshot(limit=1)[0]
            assert rec["exc_type"] == "ValueError"
            assert "boom" in (rec["exc_summary"] or "")
        finally:
            logger.removeHandler(h)

    def test_ring_buffer_evicts_oldest(self):
        h = RingHandler(maxlen=3)
        logger = logging.getLogger("tests.warnings.ring")
        logger.addHandler(h)
        try:
            for i in range(10):
                logger.warning(f"msg {i}")
            recs = h.snapshot(limit=10)
            assert len(recs) == 3
            assert [r["message"] for r in recs] == ["msg 9", "msg 8", "msg 7"]
        finally:
            logger.removeHandler(h)

    def test_suppress_noisy_loggers(self):
        h = RingHandler(maxlen=10)
        # urllib3 WARNINGs (retries / pool) are routine and would spam
        # the operator surface — confirm they're suppressed.
        urllib_logger = logging.getLogger("urllib3.connectionpool")
        urllib_logger.addHandler(h)
        try:
            urllib_logger.warning("retrying ...")
            assert h.counts()["total"] == 0
        finally:
            urllib_logger.removeHandler(h)


class TestInstallIdempotent:
    def test_install_returns_singleton(self):
        h1 = install()
        h2 = install()
        assert h1 is h2

    def test_handler_returns_same_instance(self):
        h1 = install()
        h2 = handler()
        assert h1 is h2
