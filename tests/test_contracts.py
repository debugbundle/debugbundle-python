from __future__ import annotations

import asyncio
import importlib.resources
import inspect
import logging

import debugbundle
from debugbundle.redaction import DEFAULT_REDACT_FIELDS


def test_module_init_exposes_contract_friendly_signature() -> None:
    parameters = list(inspect.signature(debugbundle.init).parameters)
    assert parameters == [
        "project_token",
        "environment",
        "service",
        "enabled",
        "redact_fields",
        "sample_rate",
        "batch_size",
        "flush_interval",
        "endpoint",
        "log_level",
        "max_probe_labels",
        "max_probe_entries_per_label",
        "probe_flush_on_error",
        "fetch_impl",
        "on_diagnostic",
        "before_send",
        "probes_poll_interval",
    ]


def test_module_probe_and_hook_wrappers_expose_sdk_signatures() -> None:
    assert list(inspect.signature(debugbundle.probe).parameters) == ["label", "data", "opts"]
    assert list(inspect.signature(debugbundle.capture_logging).parameters) == ["logger"]
    assert list(inspect.signature(debugbundle.capture_async).parameters) == ["loop"]


def test_default_redaction_fields_match_contract() -> None:
    assert DEFAULT_REDACT_FIELDS == {
        "password",
        "secret",
        "token",
        "authorization",
        "cookie",
        "ssn",
        "credit_card",
    }


def test_typed_package_marker_exists() -> None:
    assert importlib.resources.files("debugbundle").joinpath("py.typed").is_file()


def test_module_wrappers_delegate_to_singleton_sdk(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    class RecordingSdk:
        def init(self, **kwargs: object) -> None:
            calls.append(("init", (), kwargs))

        def capture_exception(self, error: BaseException, context: dict[str, object] | None = None) -> None:
            calls.append(("capture_exception", (error,), {"context": context}))

        def capture_error(self, error: BaseException, context: dict[str, object] | None = None) -> None:
            calls.append(("capture_error", (error,), {"context": context}))

        def capture_log(self, message: str, level: str = "warning", context: dict[str, object] | None = None) -> None:
            calls.append(("capture_log", (message,), {"level": level, "context": context}))

        def capture_request(
            self,
            request: object,
            response: object | None = None,
            context: object | None = None,
        ) -> None:
            calls.append(("capture_request", (request,), {"response": response, "context": context}))

        def capture_message(
            self,
            message: str,
            level: str | None = None,
            context: dict[str, object] | None = None,
        ) -> None:
            calls.append(("capture_message", (message,), {"level": level, "context": context}))

        def set_context(self, key: str, value: object) -> None:
            calls.append(("set_context", (key, value), {}))

        def flush(self) -> None:
            calls.append(("flush", (), {}))

        def probe(self, label: str, data: object, opts: object | None = None) -> None:
            calls.append(("probe", (label, data), {"opts": opts}))

        def capture_exceptions(self) -> None:
            calls.append(("capture_exceptions", (), {}))

        def capture_logging(self, logger: logging.Logger | None = None) -> None:
            calls.append(("capture_logging", (), {"logger": logger}))

        def capture_async(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
            calls.append(("capture_async", (), {"loop": loop}))

    loop = asyncio.new_event_loop()
    logger = logging.getLogger("debugbundle-contracts")
    error = RuntimeError("boom")
    request = {"method": "GET", "path": "/orders"}
    response = {"status_code": 500}
    context = {"request_id": "req_1"}
    diagnostic_events: list[dict[str, object]] = []

    try:
        monkeypatch.setattr(debugbundle, "_sdk", RecordingSdk())

        debugbundle.init(
            project_token="dbundle_proj_test",
            environment="production",
            service="checkout-api",
            enabled=False,
            redact_fields=["authorization"],
            sample_rate=0.5,
            batch_size=10,
            flush_interval=1.5,
            endpoint="https://example.invalid/v1/events",
            log_level="error",
            max_probe_labels=5,
            max_probe_entries_per_label=2,
            probe_flush_on_error=False,
            fetch_impl=lambda endpoint, params: {"status_code": 304, "json": {}},
            on_diagnostic=diagnostic_events.append,
            probes_poll_interval=15_000,
        )
        debugbundle.capture_exception(error, context=context)
        debugbundle.capture_error(error, context=context)
        debugbundle.capture_log("warning", level="warning", context=context)
        debugbundle.capture_request(request, response=response, context=context)
        debugbundle.capture_message("hello", level="info", context=context)
        debugbundle.set_context("tenant", "acme")
        debugbundle.flush()
        debugbundle.probe("checkout.tax", {"rate": 0.2}, opts={"heavy": True})
        debugbundle.capture_exceptions()
        debugbundle.capture_logging(logger=logger)
        debugbundle.capture_async(loop=loop)
    finally:
        loop.close()

    assert [name for name, _, _ in calls] == [
        "init",
        "capture_exception",
        "capture_error",
        "capture_log",
        "capture_request",
        "capture_message",
        "set_context",
        "flush",
        "probe",
        "capture_exceptions",
        "capture_logging",
        "capture_async",
    ]
    assert calls[0][2]["project_token"] == "dbundle_proj_test"
    assert calls[0][2]["probes_poll_interval"] == 15_000
    assert calls[1][1][0] is error
    assert calls[4][2]["response"] == response
    assert calls[8][2]["opts"] == {"heavy": True}
    assert calls[10][2]["logger"] is logger
    assert calls[11][2]["loop"] is loop
