from __future__ import annotations

import logging

import pytest
import structlog

from debugbundle.capture_hooks import DebugBundleLogHandler
from debugbundle.core import DebugBundleSdk
from debugbundle.transport import TransportResponse

_ORIGINAL_GET_LOGGER = structlog.get_logger


@pytest.mark.parametrize("drop_first", [False, True])
def test_repeated_native_records_and_capture_after_rejected_hook(drop_first):
    events = []
    calls = []

    def hook(event):
        calls.append(event)
        return None if len(calls) == 1 else event

    sdk = DebugBundleSdk(transport=lambda request: (events.extend(request["events"]), TransportResponse(202))[1])
    sdk.init("synthetic-token", batch_size=1000, flush_interval=3600, before_send=hook if drop_first else None)
    error = RuntimeError("reused object")
    try:
        with sdk.operation_context({"kind": "request"}):
            sdk.capture_exception(error, context={"logger_name": "synthetic"})
            sdk.flush()
            sdk.capture_exception(error, context={} if drop_first else {"logger_name": "synthetic"})
            sdk.flush()
        assert len(events) == (1 if drop_first else 2)
    finally:
        sdk.dispose()


def test_stdlib_exception_preserves_traceback_and_cause_without_frame_logs():
    events = []

    def send(request):
        events.extend(request["events"])
        return TransportResponse(202)

    sdk = DebugBundleSdk(transport=send)
    sdk.init("synthetic-token", batch_size=1000, flush_interval=3600)
    logger = logging.Logger("synthetic", level=logging.ERROR)
    logger.addHandler(DebugBundleLogHandler(sdk))
    try:
        try:
            try:
                raise ValueError("synthetic cause")
            except ValueError as cause:
                raise RuntimeError("synthetic outer") from cause
        except RuntimeError:
            logger.exception("operation failed")
        logger.error("ordinary error")
        sdk.flush()
        assert [event["event_type"] for event in events] == ["backend_exception", "log_event"]
        assert "synthetic cause" in events[0]["payload"]["stack"]
        assert "test_stdlib_exception" in events[0]["payload"]["stack"]
    finally:
        sdk.dispose()


def test_logger_and_explicit_capture_share_identity_only_inside_owned_operation():
    events = []
    sdk = DebugBundleSdk(transport=lambda request: (events.extend(request["events"]), TransportResponse(202))[1])
    sdk.init("synthetic-token", batch_size=1000, flush_interval=3600)
    logger = logging.Logger("synthetic", level=logging.ERROR)
    logger.addHandler(DebugBundleLogHandler(sdk))
    error = RuntimeError("same object")
    try:
        for _ in range(2):
            with sdk.operation_context({"kind": "request"}):
                logger.error("failed", exc_info=(RuntimeError, error, None))
                sdk.capture_exception(error)
        sdk.flush()
        assert len(events) == 2
    finally:
        sdk.dispose()


def test_loguru_and_structlog_preserve_native_cause_after_formatters(monkeypatch):
    import structlog
    from loguru import logger

    events = []
    sdk = DebugBundleSdk(transport=lambda request: (events.extend(request["events"]), TransportResponse(202))[1])
    sdk.init("synthetic-token", batch_size=1000, flush_interval=3600)
    previous = structlog.get_config()
    monkeypatch.setattr(structlog, "get_logger", _ORIGINAL_GET_LOGGER)
    structlog.configure(
        processors=[structlog.processors.format_exc_info, structlog.processors.JSONRenderer()],
        logger_factory=structlog.ReturnLoggerFactory(),
    )
    sdk.capture_logging(logger=logging.Logger("synthetic-integrations"))
    try:
        try:
            raise ValueError("native cause")
        except ValueError:
            logger.exception("loguru failure")
            structlog.get_logger().exception("structlog failure")
        sdk.flush()
        assert len(events) == 2
        assert all(event["event_type"] == "backend_exception" for event in events)
        assert all("native cause" in event["payload"]["stack"] for event in events)
    finally:
        structlog.configure(**previous)
        sdk.dispose()
