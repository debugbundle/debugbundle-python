from __future__ import annotations

import asyncio
import logging
import sys
import threading
from types import SimpleNamespace

from debugbundle.core import DebugBundleSdk
from debugbundle.logger_integrations import (
    _attach_loguru,
    _attach_structlog,
    _emit_diagnostic,
    _normalize_level,
    _StructlogLoggerProxy,
)


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, request: dict[str, object]) -> object:
        self.calls.append(request)
        return type("Response", (), {"status_code": 202, "retry_after_ms": None})()


def test_capture_exceptions_hooks_sys_excepthook(monkeypatch) -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")
    original = sys.excepthook

    try:
        sdk.capture_exceptions()
        error = RuntimeError("boom")
        sys.excepthook(type(error), error, error.__traceback__)
        sdk.flush()
    finally:
        monkeypatch.setattr(sys, "excepthook", original)

    assert transport.calls[0]["events"][0]["event_type"] == "backend_exception"


def test_capture_logging_registers_handler_and_respects_log_level() -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")
    sdk.capture_logging()

    logger = logging.getLogger("debugbundle-python-test")
    logger.setLevel(logging.INFO)
    logger.info("ignore info")
    logger.warning("keep warning")
    sdk.flush()
    sdk.dispose()

    events = transport.calls[0]["events"]
    assert len(events) == 1
    assert events[0]["event_type"] == "log_event"
    assert events[0]["payload"]["message"] == "keep warning"


def test_capture_async_registers_loop_handler() -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")
    loop = asyncio.new_event_loop()

    try:
        sdk.capture_async(loop=loop)
        handler = loop.get_exception_handler()
        assert handler is not None
        handler(loop, {"exception": RuntimeError("async failure")})
        sdk.flush()
    finally:
        loop.close()

    assert transport.calls[0]["events"][0]["payload"]["message"] == "async failure"


def test_capture_logging_auto_detects_structlog_and_respects_log_level(monkeypatch) -> None:
    class FakeStructLogger:
        def info(self, event: str, **kwargs: object) -> None:
            self.last_info = (event, kwargs)

        def error(self, event: str, **kwargs: object) -> None:
            self.last_error = (event, kwargs)

    fake_structlog = SimpleNamespace()
    fake_structlog.get_logger = lambda *args, **kwargs: FakeStructLogger()
    monkeypatch.setitem(sys.modules, "structlog", fake_structlog)

    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        log_level="warning",
    )
    sdk.capture_logging()

    try:
        logger = fake_structlog.get_logger("debugbundle-python-structlog")
        logger.info("ignore info", request_id="req_1")
        logger.error("keep error", request_id="req_2", tenant="acme")
        events = list(sdk._buffer)
    finally:
        sdk.dispose()

    assert len(events) == 1
    assert events[0]["event_type"] == "log_event"
    assert events[0]["payload"]["message"] == "keep error"
    assert events[0]["payload"]["level"] == "error"
    assert events[0]["payload"]["attributes"] == {"request_id": "req_2", "tenant": "acme"}


def test_capture_logging_auto_detects_loguru_and_restores_sink_on_dispose() -> None:
    loguru_logger = __import__("loguru").logger

    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        log_level="warning",
    )
    sdk.capture_logging()

    loguru_logger.bind(request_id="req_9").info("ignore info")
    loguru_logger.bind(request_id="req_10", tenant="acme").error("keep error")
    sdk.flush()
    sdk.dispose()
    loguru_logger.error("after dispose")
    sdk.flush()

    events = transport.calls[0]["events"]
    assert len(events) == 1
    assert events[0]["payload"]["message"] == "keep error"
    assert events[0]["payload"]["level"] == "error"
    assert events[0]["payload"]["attributes"] == {"request_id": "req_10", "tenant": "acme"}


def test_capture_request_is_thread_safe_under_concurrent_writers() -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        batch_size=500,
        flush_interval=60,
    )

    worker_count = 8
    events_per_worker = 25
    start = threading.Barrier(worker_count)

    def worker(worker_id: int) -> None:
        start.wait()
        for event_index in range(events_per_worker):
            sdk.capture_request(
                {
                    "method": "GET",
                    "path": f"/orders/{worker_id}/{event_index}",
                },
                response={"status_code": 500},
            )

    threads = [threading.Thread(target=worker, args=(worker_id,)) for worker_id in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    sdk.flush()
    sdk.dispose()

    events = transport.calls[0]["events"]
    assert len(events) == worker_count * events_per_worker
    assert len({event["payload"]["path"] for event in events}) == worker_count * events_per_worker


def test_module_level_logger_helpers_cover_optional_branches(monkeypatch) -> None:
    captured_logs: list[tuple[str, str, dict[str, object] | None]] = []
    diagnostics: list[dict[str, object]] = []

    class RecordingSdk:
        def capture_log(self, message: str, level: str = "warning", context: dict[str, object] | None = None) -> None:
            captured_logs.append((message, level, context))

    class FakeStructLogger:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
            self.label = "structlog"

        def bind(self, *args: object, **kwargs: object) -> FakeStructLogger:
            self.calls.append(("bind", args, kwargs))
            return self

        def new(self, *args: object, **kwargs: object) -> FakeStructLogger:
            self.calls.append(("new", args, kwargs))
            return self

        def warn(self, event: object = None, *args: object, **kwargs: object) -> dict[str, object]:
            self.calls.append(("warn", (event, *args), kwargs))
            return {"event": event, "args": args, "kwargs": kwargs}

    class FakeLoguruLogger:
        def __init__(self) -> None:
            self.sink = None
            self.removed: list[int] = []

        def add(self, sink, catch: bool = True) -> int:  # type: ignore[no-untyped-def]
            self.sink = sink
            return 42

        def remove(self, sink_id: int) -> None:
            self.removed.append(sink_id)

    fake_structlog = SimpleNamespace(get_logger=lambda *args, **kwargs: FakeStructLogger())
    fake_loguru_logger = FakeLoguruLogger()
    monkeypatch.setitem(sys.modules, "structlog", fake_structlog)
    monkeypatch.setitem(sys.modules, "loguru", SimpleNamespace(logger=fake_loguru_logger))

    restore_structlog = _attach_structlog(RecordingSdk(), on_diagnostic=diagnostics.append)
    assert restore_structlog is not None
    logger = fake_structlog.get_logger("checkout")
    assert isinstance(logger, _StructlogLoggerProxy)
    assert logger.bind(request_id="req_1")
    assert logger.new(tenant="acme")
    assert logger.label == "structlog"
    result = logger.warn("slow query", "db", duration_ms=123)
    assert result["kwargs"] == {"duration_ms": 123}
    restore_structlog()

    restore_loguru = _attach_loguru(RecordingSdk(), on_diagnostic=diagnostics.append)
    assert restore_loguru is not None
    assert fake_loguru_logger.sink is not None
    fake_loguru_logger.sink(SimpleNamespace(record=None))
    fake_loguru_logger.sink(
        SimpleNamespace(
            record={
                "message": "probe exploded",
                "level": SimpleNamespace(name="EXCEPTION"),
                "extra": {"request_id": "req_2"},
            }
        )
    )
    restore_loguru()

    assert captured_logs == [
        ("slow query", "warning", {"arg_0": "db", "duration_ms": 123}),
        ("probe exploded", "error", {"request_id": "req_2"}),
    ]
    assert fake_loguru_logger.removed == [42]
    assert diagnostics == []


def test_logger_helper_error_paths_and_level_aliases(monkeypatch) -> None:
    diagnostics: list[dict[str, object]] = []

    class WrappedStructlog:
        def __init__(self) -> None:
            def get_logger(*args: object, **kwargs: object) -> object:
                return object()

            setattr(get_logger, "__debugbundle_structlog_wrapper__", True)
            self.get_logger = get_logger

    class BrokenStructlog:
        def __init__(self) -> None:
            object.__setattr__(self, "get_logger", lambda *args, **kwargs: object())

        def __setattr__(self, name: str, value: object) -> None:
            if name == "get_logger":
                raise RuntimeError("cannot patch structlog")
            object.__setattr__(self, name, value)

    class RecordingSdk:
        def capture_log(self, message: str, level: str = "warning", context: dict[str, object] | None = None) -> None:
            raise AssertionError("capture_log should not be called")

    monkeypatch.setitem(sys.modules, "structlog", WrappedStructlog())
    assert _attach_structlog(RecordingSdk(), on_diagnostic=diagnostics.append) is None

    monkeypatch.setitem(sys.modules, "structlog", BrokenStructlog())
    assert _attach_structlog(RecordingSdk(), on_diagnostic=diagnostics.append) is None

    class BrokenLoguruLogger:
        def add(self, sink, catch: bool = True) -> int:  # type: ignore[no-untyped-def]
            raise RuntimeError("cannot attach sink")

    monkeypatch.setitem(sys.modules, "loguru", SimpleNamespace(logger=BrokenLoguruLogger()))
    assert _attach_loguru(RecordingSdk(), on_diagnostic=diagnostics.append) is None

    _emit_diagnostic(None, RuntimeError("ignored"))

    assert _normalize_level("warn") == "warning"
    assert _normalize_level("exception") == "error"
    assert _normalize_level(" INFO ") == "info"
    assert [event["code"] for event in diagnostics] == ["logger_attach_failed", "logger_attach_failed"]
    assert diagnostics[0]["metadata"]["error"]["message"] == "cannot patch structlog"
    assert diagnostics[1]["metadata"]["error"]["message"] == "cannot attach sink"
