from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest
import structlog

from debugbundle.core import DebugBundleSdk
from debugbundle.logger_integrations import _attach_structlog

_ORIGINAL_GET_LOGGER = structlog.get_logger


class Capture:
    def __init__(self):
        self.events = []

    def capture_log(self, message, level="warning", context=None):
        self.events.append((message, level, context))


@pytest.fixture(autouse=True)
def reset_structlog(monkeypatch):
    previous = structlog.get_config()
    monkeypatch.setattr(structlog, "get_logger", _ORIGINAL_GET_LOGGER)
    yield
    structlog.configure(**previous)


@pytest.mark.parametrize("mode", ["level", "processor"])
def test_native_structlog_filters_run_before_capture(mode):
    output = io.StringIO()
    sdk = Capture()

    def drop(_logger, _name, event):
        if event["event"] == "suppressed":
            raise structlog.DropEvent
        return event

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR if mode == "level" else logging.DEBUG),
        logger_factory=structlog.PrintLoggerFactory(file=output),
        processors=([drop] if mode == "processor" else []) + [structlog.processors.JSONRenderer()],
    )
    restore = _attach_structlog(sdk)
    try:
        logger = structlog.get_logger().bind(tenant="example")
        logger.warning("suppressed")
        logger.error("accepted", request_id="req-1")
        assert [event[0] for event in sdk.events] == ["accepted"]
        assert [json.loads(line)["event"] for line in output.getvalue().splitlines()] == ["accepted"]
        assert sdk.events[0][2] == {"tenant": "example", "request_id": "req-1"}
        restore()
        logger.error("after detach")
        assert len(sdk.events) == 1
    finally:
        restore()


def test_structlog_async_and_context_operations_preserve_filters():
    sdk = Capture()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR),
        logger_factory=structlog.ReturnLoggerFactory(),
        processors=[],
    )
    restore = _attach_structlog(sdk)
    try:
        logger = structlog.get_logger().bind(old="gone").unbind("old").bind(retained=True)
        asyncio.run(logger.awarning("suppressed"))
        asyncio.run(logger.aerror("accepted"))
        assert sdk.events == [("accepted", "error", {"retained": True})]
    finally:
        restore()


def test_structlog_capture_failures_do_not_change_native_result_or_repeat_processors():
    sdk = Capture()
    calls = []

    def processor(_logger, _name, event):
        calls.append(event["event"])
        return event

    def fail(*_args, **_kwargs):
        raise RuntimeError("SDK failed")

    sdk.capture_log = fail
    structlog.configure(logger_factory=structlog.ReturnLoggerFactory(), processors=[processor])
    restore = _attach_structlog(sdk)
    try:
        result = structlog.get_logger().error("accepted", value=1)
        assert result == ((), {"value": 1, "event": "accepted"})
        assert calls == ["accepted"]
    finally:
        restore()


def test_structlog_console_renderer_preserves_message_and_context():
    sdk = Capture()
    output = io.StringIO()
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=output),
        processors=[structlog.dev.ConsoleRenderer(colors=False)],
    )
    restore = _attach_structlog(sdk)
    try:
        structlog.get_logger().error("rendered message", order_id="123")
        assert sdk.events == [("rendered message", "error", {"order_id": "123"})]
        assert "rendered message" in output.getvalue()
    finally:
        restore()


def test_structlog_numeric_log_is_filtered():
    sdk = Capture()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.ReturnLoggerFactory(), processors=[],
    )
    restore = _attach_structlog(sdk)
    try:
        logger = structlog.get_logger()
        logger.log(logging.DEBUG, "suppressed")
        logger.log(logging.ERROR, "accepted")
        assert [event[:2] for event in sdk.events] == [("accepted", "error")]
    finally:
        restore()


def test_structlog_stdlib_filters_and_non_propagating_loggers_capture_once():
    sdk = DebugBundleSdk(transport=lambda _request: {"status": 202})
    native = logging.getLogger("debugbundle-stdlib-suppression-test")
    previous = (native.level, native.propagate, native.disabled)
    native.setLevel(logging.ERROR)
    native.propagate = False
    reject = logging.Filter()
    reject.filter = lambda record: record.getMessage() != "drop"
    native.addFilter(reject)
    structlog.configure(
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=lambda: native,
        processors=[structlog.processors.EventRenamer("msg"), lambda _log, _name, event: ((event.pop("msg"),), event)],
    )
    sdk.init(project_token="dbundle_proj_test", service="logger-test", environment="production")
    try:
        sdk.capture_logging()
        logger = structlog.get_logger()
        logger.warning("below level")
        logger.error("drop")
        native.disabled = True
        logger.error("disabled")
        native.disabled = False
        logging.disable(logging.CRITICAL)
        logger.error("globally disabled")
        logging.disable(logging.NOTSET)
        logger.error("accepted")
        assert [event["payload"]["message"] for event in sdk._buffer] == ["accepted"]
    finally:
        logging.disable(logging.NOTSET)
        sdk.dispose()
        native.removeFilter(reject)
        native.setLevel(previous[0])
        native.propagate = previous[1]
        native.disabled = previous[2]


def test_structlog_native_exceptions_are_not_retried():
    sdk = Capture()
    calls = []

    def fail_processor(_logger, _method, _event):
        calls.append(_event)
        raise ValueError("application processor failed")

    structlog.configure(processors=[fail_processor])
    restore = _attach_structlog(sdk)
    try:
        with pytest.raises(ValueError, match="application processor failed"):
            structlog.get_logger().error("do not retry")
        assert sdk.events == []
        assert len(calls) == 1
    finally:
        restore()
