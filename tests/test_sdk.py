from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

import debugbundle
from debugbundle.config import BALANCED_CAPTURE_POLICY, MINIMAL_CAPTURE_POLICY
from debugbundle.core import DebugBundleSdk


@dataclass
class FakeResponse:
    status_code: int
    retry_after_ms: int | None = None
    body: object | None = None


class FakeTransport:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = responses or [FakeResponse(status_code=202)]
        self.calls: list[dict[str, object]] = []

    def __call__(self, request: dict[str, object]) -> FakeResponse:
        self.calls.append(request)
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


class ManualClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_filtered_info_burst_never_reaches_the_hook_or_transport() -> None:
    hook_calls = 0
    transport = FakeTransport()

    def hook(event: dict[str, object]) -> dict[str, object]:
        nonlocal hook_calls
        hook_calls += 1
        return event

    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", log_level="warning", before_send=hook)
    try:
        started = time.perf_counter()
        for index in range(10_000):
            sdk.capture_log(f"filtered INFO {index}", level="info", context={"index": index})
        assert time.perf_counter() - started < 2.0
        assert hook_calls == 0
        assert transport.calls == []
    finally:
        sdk.dispose()


def test_held_sender_never_stalls_application_capture() -> None:
    entered = threading.Event()
    release = threading.Event()

    def held_transport(_request: dict[str, object]) -> FakeResponse:
        entered.set()
        release.wait(timeout=5)
        return FakeResponse(status_code=202)

    sdk = DebugBundleSdk(transport=held_transport)
    sdk.init(project_token="dbundle_proj_test", batch_size=1)
    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            first = callers.submit(sdk.capture_log, "failure", "error")
            assert entered.wait(timeout=2)
            second = callers.submit(sdk.capture_log, "filtered", "info")
            try:
                first.result(timeout=0.25)
                second.result(timeout=0.25)
            finally:
                release.set()
    finally:
        release.set()
        sdk.dispose()


def test_slow_probe_supplier_does_not_hold_the_capture_lock_or_lose_an_exception() -> None:
    entered = threading.Event()
    release = threading.Event()
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", flush_interval=60.0)

    def slow_probe() -> dict[str, object]:
        entered.set()
        release.wait(timeout=2)
        return {"probe": "safe"}

    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            pending_probe = callers.submit(sdk.probe, "checkout.slow", slow_probe)
            assert entered.wait(timeout=2)
            try:
                callers.submit(sdk.capture_exception, RuntimeError("retain incident")).result(timeout=0.25)
            finally:
                release.set()
            pending_probe.result(timeout=2)
        sdk.flush()
        assert any(event["event_type"] == "backend_exception" for call in transport.calls for event in call["events"])
    finally:
        release.set()
        sdk.dispose()


def test_probe_supplier_from_old_sdk_generation_cannot_commit_after_reinit() -> None:
    entered = threading.Event()
    release = threading.Event()
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", flush_interval=60.0)

    def slow_probe() -> dict[str, object]:
        entered.set()
        release.wait(timeout=2)
        return {"secret": "old generation"}

    try:
        with ThreadPoolExecutor(max_workers=1) as callers:
            pending_probe = callers.submit(sdk.probe, "checkout.old", slow_probe)
            assert entered.wait(timeout=2)
            sdk.init(project_token="dbundle_proj_test", flush_interval=60.0)
            release.set()
            pending_probe.result(timeout=2)
        sdk.capture_exception(RuntimeError("new generation"))
        sdk.flush()
        event = next(event for call in transport.calls for event in call["events"]
                     if event["event_type"] == "backend_exception")
        assert "probe_data" not in event["payload"]
    finally:
        release.set()
        sdk.dispose()


def test_flush_size_accounting_does_not_hold_the_capture_lock() -> None:
    entered = threading.Event()
    release = threading.Event()
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", flush_interval=60.0)
    sdk.capture_log("initial warning", level="warning")
    original_event_bytes = sdk._event_bytes

    def slow_event_bytes(event: dict[str, object]) -> int:
        if event["event_type"] == "log_event" and not entered.is_set():
            entered.set()
            release.wait(timeout=2)
        return original_event_bytes(event)

    sdk._event_bytes = slow_event_bytes
    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            flushing = callers.submit(sdk.flush)
            assert entered.wait(timeout=2)
            try:
                callers.submit(sdk.capture_exception, RuntimeError("retain during flush")).result(timeout=0.25)
            finally:
                release.set()
            flushing.result(timeout=2)
        sdk.flush()
        assert any(event["event_type"] == "backend_exception" for call in transport.calls for event in call["events"])
    finally:
        release.set()
        sdk.dispose()


def test_suppression_aggregate_privacy_does_not_hold_the_capture_lock() -> None:
    entered = threading.Event()
    release = threading.Event()
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", flush_interval=60.0)
    for _ in range(5):
        sdk.capture_exception(RuntimeError("repeated failure"))
    original_protect_event = sdk._protect_event

    def slow_aggregate_protection(event: dict[str, object]) -> dict[str, object] | None:
        if event.get("event_type") == "error_suppressed" and not entered.is_set():
            entered.set()
            release.wait(timeout=2)
        return original_protect_event(event)

    sdk._protect_event = slow_aggregate_protection
    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            flushing = callers.submit(sdk.flush)
            assert entered.wait(timeout=2)
            try:
                callers.submit(sdk.capture_exception, RuntimeError("separate incident")).result(timeout=0.25)
            finally:
                release.set()
            flushing.result(timeout=2)
        sdk.flush()
        assert any(event["event_type"] == "backend_exception" and
                   event["payload"]["message"] == "separate incident"
                   for call in transport.calls for event in call["events"])
    finally:
        release.set()
        sdk.dispose()


def test_pressure_summary_privacy_does_not_hold_the_capture_lock_or_lose_new_drops() -> None:
    entered = threading.Event()
    release = threading.Event()
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", flush_interval=60.0)
    sdk.capture_log("initial warning", level="warning")
    sdk._pressure_drops = 1
    original_protect_event = sdk._protect_event

    def slow_pressure_protection(event: dict[str, object]) -> dict[str, object] | None:
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("attributes", {}).get("reason") == "queue_pressure":
            entered.set()
            release.wait(timeout=2)
        return original_protect_event(event)

    sdk._protect_event = slow_pressure_protection
    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            flushing = callers.submit(sdk.flush)
            assert entered.wait(timeout=2)
            try:
                callers.submit(sdk.capture_exception, RuntimeError("concurrent incident")).result(timeout=0.25)
                with sdk._lock:
                    sdk._pressure_drops += 1
            finally:
                release.set()
            flushing.result(timeout=2)
        sent = [event for call in transport.calls for event in call["events"]]
        assert any(event["event_type"] == "backend_exception" for event in sent)
        assert sdk._pressure_drops == 1
        assert sum(event["payload"]["attributes"]["suppressed_count"] for event in sent
                   if event["event_type"] == "log_event" and
                   event["payload"]["attributes"].get("reason") == "queue_pressure") == 1
    finally:
        release.set()
        sdk.dispose()


def test_old_generation_summary_cannot_commit_after_reinitialization() -> None:
    entered = threading.Event()
    release = threading.Event()
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_old", flush_interval=60.0)
    for _ in range(5):
        sdk.capture_exception(RuntimeError("old failure"))
    original_protect_event = sdk._protect_event

    def slow_aggregate_protection(event: dict[str, object]) -> dict[str, object] | None:
        if event.get("event_type") == "error_suppressed":
            entered.set()
            release.wait(timeout=2)
        return original_protect_event(event)

    sdk._protect_event = slow_aggregate_protection
    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            flushing = callers.submit(sdk.flush)
            assert entered.wait(timeout=2)
            sdk.init(project_token="dbundle_proj_new", flush_interval=60.0)
            release.set()
            flushing.result(timeout=2)
        sdk.capture_exception(RuntimeError("new failure"))
        sdk.flush()
        assert all(call["project_token"] == "dbundle_proj_new" for call in transport.calls)
        assert all(event["payload"].get("message") != "old failure"
                   for call in transport.calls for event in call["events"])
    finally:
        release.set()
        sdk.dispose()


def test_forked_child_discards_inherited_queue_and_locked_parent_state() -> None:
    if not hasattr(os, "fork"):
        return
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", log_level="warning", flush_interval=60)
    sdk.capture_log("parent only", level="warning")
    sdk.set_context("tenant", "parent tenant")
    sdk._capture_policy = replace(MINIMAL_CAPTURE_POLICY, capture_logs="off")
    locked = threading.Event()
    release = threading.Event()

    def hold_parent_lock() -> None:
        with sdk._lock:
            locked.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_parent_lock)
    holder.start()
    assert locked.wait(timeout=2)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            sdk.capture_log("restricted child log", level="error")
            sdk.capture_exception(RuntimeError("child only"))
            sdk.flush()
            events = [event for call in transport.calls for event in call["events"]]
            result = {
                "types": [event["event_type"] for event in events],
                "messages": [event["payload"]["message"] for event in events],
                "context": [event.get("context") for event in events],
            }
            os.write(write_fd, json.dumps(result).encode())
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(write_fd)
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            finished, status = os.waitpid(child, os.WNOHANG)
            if finished:
                assert os.waitstatus_to_exitcode(status) == 0
                result = json.loads(os.read(read_fd, 4096))
                assert result["types"] == ["backend_exception"]
                assert result["messages"] == ["child only"]
                assert not any("parent tenant" in str(value) for value in result["context"])
                break
            time.sleep(0.01)
        else:
            os.kill(child, 9)
            os.waitpid(child, 0)
            raise AssertionError("forked SDK child hung on an inherited lock")
    finally:
        os.close(read_fd)
        release.set()
        holder.join(timeout=2)
        sdk.dispose()


def test_process_change_resets_http_client_and_pending_ownership() -> None:
    sdk = DebugBundleSdk()
    sdk.init(project_token="dbundle_proj_test", flush_interval=60)
    old_transport = sdk._http_transport
    try:
        sdk.capture_exception(RuntimeError("parent only"))
        sdk.set_context("tenant", "parent tenant")
        assert sdk._buffer
        sdk._pid -= 1  # Exercise the process transition in this coverage process.
        assert sdk.status == "healthy"
        assert sdk._buffer == []
        assert sdk._context == {}
        assert sdk._http_transport is not old_transport
        assert sdk._transport is sdk._http_transport
        assert sdk._initial_config_ready.is_set()
    finally:
        sdk.dispose()
        if old_transport is not None:
            old_transport.close()


def test_slow_before_send_hook_runs_after_capture_returns() -> None:
    entered = threading.Event()
    release = threading.Event()

    def hook(event: dict[str, object]) -> dict[str, object]:
        entered.set()
        release.wait(timeout=5)
        return event

    sdk = DebugBundleSdk(transport=FakeTransport())
    sdk.init(project_token="dbundle_proj_test", batch_size=1, before_send=hook)
    try:
        with ThreadPoolExecutor(max_workers=1) as callers:
            capture = callers.submit(sdk.capture_log, "failure", "error")
            assert entered.wait(timeout=2)
            try:
                capture.result(timeout=0.25)
            finally:
                release.set()
    finally:
        release.set()
        sdk.dispose()


def test_contended_internal_lock_never_stalls_a_capture_caller() -> None:
    sdk = DebugBundleSdk(transport=FakeTransport())
    sdk.init(project_token="dbundle_proj_test")
    held = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with sdk._lock:
            held.set()
            release.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as holder:
        future = holder.submit(hold_lock)
        assert held.wait(timeout=2)
        started = time.perf_counter()
        try:
            sdk.capture_exception(RuntimeError("synthetic"))
            assert time.perf_counter() - started < 0.25
        finally:
            release.set()
        future.result(timeout=2)
    sdk.dispose()


def test_initial_remote_config_fetch_never_blocks_initialization() -> None:
    entered = threading.Event()
    release = threading.Event()

    def held_fetch(_url: str, _request: dict[str, object]) -> object:
        entered.set()
        release.wait(timeout=5)
        raise RuntimeError("synthetic config outage")

    sdk = DebugBundleSdk(transport=FakeTransport())
    with ThreadPoolExecutor(max_workers=1) as caller:
        initialized = caller.submit(
            sdk.init, project_token="dbundle_proj_test", fetch_impl=held_fetch,
        )
        assert entered.wait(timeout=2)
        try:
            initialized.result(timeout=0.25)
            started = time.perf_counter()
            sdk.capture_log("host remains responsive", level="error")
            assert time.perf_counter() - started < 0.25
        finally:
            release.set()
    sdk.dispose()


def test_rate_limited_unique_events_have_a_hard_pending_limit() -> None:
    sdk = DebugBundleSdk(transport=lambda _request: FakeResponse(status_code=429, retry_after_ms=300_000))
    sdk.init(project_token="dbundle_proj_test", batch_size=25, log_level="error")
    try:
        for index in range(1_100):
            sdk.capture_log(f"unique error {index}", level="error")
        assert len(sdk._buffer) <= 1_000
    finally:
        sdk.dispose()


def test_full_queue_rejects_lower_priority_logs_and_requests_before_context_scan() -> None:
    class ObservedContext(Mapping[str, object]):
        reads = 0

        def __getitem__(self, key: str) -> object:
            self.reads += 1
            return "secret"

        def __iter__(self) -> Iterator[str]:
            self.reads += 1
            yield "password"

        def __len__(self) -> int:
            self.reads += 1
            return 1

    sdk = DebugBundleSdk(transport=FakeTransport())
    sdk.init(project_token="dbundle_proj_test", batch_size=1_001, flush_interval=60)
    sdk._capture_policy = replace(BALANCED_CAPTURE_POLICY, capture_request_events="all")
    sdk._retry_after = time.time() + 300
    try:
        for index in range(1_000):
            sdk.capture_log(f"pending warning {index}", level="warning")
        assert len(sdk._buffer) == 1_000
        context = ObservedContext()
        started = time.perf_counter()
        for _ in range(10_000):
            sdk.capture_log("overflow warning", level="warning", context=context)
        sdk.capture_request(
            {"method": "GET", "url": "https://example.invalid/failed"},
            {"status_code": 200},
            context=context,
        )
        assert time.perf_counter() - started < 2.0
        assert context.reads == 0
        sdk.capture_request(
            {"method": "GET", "path": "/failed"},
            {"status_code": 500},
        )
        sdk.capture_exception(RuntimeError("priority exception"))
        assert len(sdk._buffer) == 1_000
        assert any(
            event["event_type"] == "request_event"
            and event["payload"]["response_status"] == 500
            for event in sdk._buffer
        )
        assert any(event["event_type"] == "backend_exception" for event in sdk._buffer)
    finally:
        sdk.dispose()


def test_all_error_full_queue_rejects_more_errors_without_context_scan() -> None:
    class ObservedContext(Mapping[str, object]):
        reads = 0

        def __getitem__(self, key: str) -> object:
            self.reads += 1
            return "secret"

        def __iter__(self) -> Iterator[str]:
            self.reads += 1
            yield "password"

        def __len__(self) -> int:
            self.reads += 1
            return 1

    sdk = DebugBundleSdk(transport=FakeTransport())
    sdk.init(project_token="dbundle_proj_test", batch_size=1_001, flush_interval=60)
    sdk._retry_after = time.time() + 300
    try:
        for index in range(1_000):
            sdk.capture_log(f"pending error {index}", level="error")
        assert len(sdk._buffer) == 1_000
        context = ObservedContext()
        started = time.perf_counter()
        for _ in range(10_000):
            sdk.capture_log("overflow error", level="error", context=context)
            sdk.capture_exception(RuntimeError("overflow exception"), context=context)
        assert time.perf_counter() - started < 2.0
        assert context.reads == 0
        assert len(sdk._buffer) == 1_000
    finally:
        sdk.dispose()


def test_rate_limited_full_queue_keeps_exception_priority_after_retry() -> None:
    transport = FakeTransport(responses=[
        FakeResponse(status_code=429, retry_after_ms=300_000),
        FakeResponse(status_code=202),
        FakeResponse(status_code=202),
    ])
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", batch_size=1_001, flush_interval=60)
    sdk._retry_after = time.time() + 300
    try:
        for index in range(1_000):
            sdk.capture_log(f"pending warning {index}", level="warning")
        assert len(sdk._buffer) == 1_000
        sdk.capture_log("discarded warning", level="warning")
        assert sdk._pressure_drops == 1
        with sdk._lock:
            sdk._retry_after = 0.0
            sdk.flush()
        assert len(transport.calls) == 1
        assert len(sdk._buffer) == 1_000
        assert sdk._pressure_drops == 1
        assert len(sdk._buffer_sizes) == len(sdk._buffer)
        assert sdk._buffer_bytes == sum(sdk._buffer_sizes.values())
        sdk.capture_exception(RuntimeError("must survive retry pressure"))
        assert len(sdk._buffer) == 1_000
        assert any(event["event_type"] == "backend_exception" for event in sdk._buffer)
        assert len(sdk._buffer_sizes) == len(sdk._buffer)
        assert sdk._buffer_bytes == sum(sdk._buffer_sizes.values())
        with sdk._lock:
            sdk._retry_after = 0.0
            sdk.flush()
            sdk.flush()
        assert len(transport.calls) == 3
        final_events = transport.calls[-1]["events"]
        assert isinstance(final_events, list)
        assert len(final_events) == 1
        assert final_events[0]["payload"]["attributes"]["suppressed_count"] == 2
        assert sdk._pressure_drops == 0
    finally:
        sdk.dispose()


def test_full_queue_reports_pressure_after_a_successful_drain_without_new_capture() -> None:
    reported = threading.Event()
    calls: list[dict[str, object]] = []

    def transport(request: dict[str, object]) -> FakeResponse:
        calls.append(request)
        events = request["events"]
        assert isinstance(events, list)
        if any(event["event_type"] == "log_event" and
               event["payload"]["attributes"].get("reason") == "queue_pressure"
               for event in events):
            reported.set()
        return FakeResponse(status_code=202)

    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", batch_size=20_000, flush_interval=60.0)
    try:
        for index in range(1_000):
            sdk.capture_log(f"burst warning {index}", level="warning")
        sdk.capture_log("dropped warning", level="warning")
        assert sdk._pressure_drops == 1
        sdk._flush_interval = 0.1
        sdk.flush()
        assert len(calls) == 1
        assert reported.wait(timeout=1.5)
        assert sdk._pressure_drops == 0
    finally:
        sdk.dispose()


def test_context_privacy_scan_does_not_exclude_an_exception(monkeypatch: object) -> None:
    import debugbundle.core as core

    entered = threading.Event()
    release = threading.Event()
    original = core.sanitize_telemetry

    def slow_context_scan(value: object, fields: set[str]) -> object:
        if value == {"tenant": "acme"}:
            entered.set()
            release.wait(timeout=5)
        return original(value, fields)

    monkeypatch.setattr(core, "sanitize_telemetry", slow_context_scan)  # type: ignore[attr-defined]
    sdk = core.DebugBundleSdk(transport=FakeTransport())
    sdk.init(project_token="dbundle_proj_test")
    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            update = callers.submit(sdk.set_context, "tenant", "acme")
            assert entered.wait(timeout=2)
            capture = callers.submit(sdk.capture_exception, RuntimeError("must survive"))
            try:
                capture.result(timeout=0.25)
                assert any(
                    event.get("event_type") == "backend_exception" for event in sdk._buffer
                )
            finally:
                release.set()
                update.result(timeout=2)
    finally:
        release.set()
        sdk.dispose()


def test_hostile_exception_renderer_isolated_and_bounded_cause_is_preserved() -> None:
    class HostileError(RuntimeError):
        def __str__(self) -> str:
            raise AssertionError("application renderer must not run")

    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test")
    try:
        try:
            try:
                raise ValueError("synthetic cause")
            except ValueError as cause:
                raise HostileError("synthetic root") from cause
        except HostileError as error:
            sdk.capture_exception(error)
        sdk.flush()
        events = transport.calls[0]["events"]
        assert isinstance(events, list)
        payload = events[0]["payload"]
        assert payload["message"] == "synthetic root"
        assert "ValueError: synthetic cause" in payload["stack"]
        assert "HostileError: synthetic root" in payload["stack"]
    finally:
        sdk.dispose()


def test_module_exposes_universal_surface() -> None:
    for name in (
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
    ):
        assert hasattr(debugbundle, name)


def test_invalid_config_degrades_silently() -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)

    sdk.init(project_token="", service="checkout-api", environment="production")
    sdk.capture_exception(RuntimeError("boom"))
    sdk.capture_message("still-running", level="error")

    assert sdk.flush() is None
    assert transport.calls == []


def test_before_send_mutates_after_redaction_and_before_queueing() -> None:
    transport = FakeTransport()
    observed_passwords: list[object] = []

    def before_send(event: dict[str, object]) -> dict[str, object]:
        context = event.get("context")
        assert isinstance(context, dict)
        observed_passwords.append(context["password"])
        payload = event["payload"]
        assert isinstance(payload, dict)
        payload["message"] = "mutated"
        return event

    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", before_send=before_send)
    sdk.capture_message("original", level="error", context={"password": "secret"})
    sdk.flush()

    assert observed_passwords == ["[REDACTED]"]
    events = transport.calls[0]["events"]
    assert isinstance(events, list)
    assert events[0]["payload"]["message"] == "mutated"


def test_hook_cannot_reintroduce_credentials_and_context_is_scrubbed_before_buffering() -> None:
    transport = FakeTransport()
    observed: list[str] = []

    def hook(event: dict[str, object]) -> dict[str, object]:
        observed.append(json.dumps(event))
        payload = event["payload"]
        assert isinstance(payload, dict)
        payload["message"] = "Bearer POST_HOOK_SECRET"
        return event

    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", before_send=hook, redact_fields=[])
    sdk.set_context("password", "PREBUFFER_SECRET")
    assert "PREBUFFER_SECRET" not in json.dumps(sdk._context)
    sdk.capture_message("Authorization: Bearer ORIGINAL_SECRET", level="error")
    sdk.flush()
    assert "ORIGINAL_SECRET" not in observed[0]
    assert "POST_HOOK_SECRET" not in json.dumps(transport.calls)
    assert "PREBUFFER_SECRET" not in json.dumps(transport.calls)


def test_before_send_drop_invalid_failure_and_sampling_are_safe() -> None:
    transport = FakeTransport()
    diagnostics: list[dict[str, object]] = []
    calls: list[str] = []

    def dropping_hook(event: dict[str, object]) -> None:
        calls.append(str(event["event_id"]))
        return None

    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", before_send=dropping_hook)
    sdk.capture_message("drop", level="error")
    sdk.flush()
    assert len(calls) == 1
    assert transport.calls == []

    sdk.init(
        project_token="dbundle_proj_test",
        before_send=lambda _event: {"invalid": True},
        on_diagnostic=diagnostics.append,
    )
    sdk.capture_message("preserve invalid", level="error")
    sdk.flush()
    events = transport.calls[0]["events"]
    assert isinstance(events, list)
    assert events[0]["payload"]["message"] == "preserve invalid"
    assert diagnostics[-1]["code"] == "before_send_invalid_event"

    def failing_hook(_event: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("hook failed")

    sdk.init(project_token="dbundle_proj_test", before_send=failing_hook, on_diagnostic=diagnostics.append)
    sdk.capture_message("preserve failure", level="error")
    sdk.flush()
    events = transport.calls[1]["events"]
    assert isinstance(events, list)
    assert events[0]["payload"]["message"] == "preserve failure"
    assert diagnostics[-1]["code"] == "before_send_failed"

    sampled_calls: list[str] = []
    sdk.init(
        project_token="dbundle_proj_test",
        sample_rate=0,
        before_send=lambda event: sampled_calls.append(str(event["event_id"])) or event,
    )
    sdk.capture_message("sampled out", level="error")
    sdk.flush()
    assert len(sampled_calls) == 0
    assert len(transport.calls) == 2


def test_retains_buffered_events_when_transport_fails() -> None:
    transport = FakeTransport(responses=[FakeResponse(status_code=500), FakeResponse(status_code=202)])
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")

    sdk.capture_exception(RuntimeError("database unavailable"))

    sdk.flush()
    sdk.flush()

    assert len(transport.calls) == 2
    second_payload = transport.calls[1]["events"]
    assert isinstance(second_payload, list)
    assert second_payload[0]["payload"]["message"] == "database unavailable"


def test_applies_retry_backoff_after_429_response() -> None:
    clock = ManualClock()
    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=429, retry_after_ms=1000),
            FakeResponse(status_code=202),
        ]
    )
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")

    sdk.capture_message("retry me", level="error")
    sdk.flush()
    sdk.flush()

    assert len(transport.calls) == 1

    clock.advance(1.001)
    sdk.flush()

    assert len(transport.calls) == 2


def test_retries_only_indexed_retryable_ingestion_rejections() -> None:
    clock = ManualClock()
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=202,
                retry_after_ms=1000,
                body={
                    "accepted": 1,
                    "rejected": 1,
                    "errors": [{"index": 1, "reason": "rate_limited"}],
                },
            ),
            FakeResponse(
                status_code=202,
                body={"accepted": 1, "rejected": 0, "errors": []},
            ),
        ]
    )
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")
    sdk.capture_message("accepted", level="error")
    sdk.capture_message("retry", level="error")

    sdk.flush()

    assert sdk.status == "degraded"
    assert sdk.last_event_at is not None
    clock.advance(1.001)
    sdk.flush()
    second_events = transport.calls[1]["events"]
    assert isinstance(second_events, list)
    assert [event["payload"]["message"] for event in second_events] == ["retry"]
    assert sdk.status == "healthy"


def test_all_terminal_rejections_do_not_advance_delivery_state() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=202,
                body={
                    "accepted": 0,
                    "rejected": 1,
                    "errors": [{"index": 0, "reason": "capture_policy_rejected"}],
                },
            )
        ]
    )
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")
    sdk.capture_message("terminal", level="error")

    sdk.flush()
    sdk.flush()

    assert sdk.status == "disconnected"
    assert sdk.last_event_at is None
    assert len(transport.calls) == 1


def test_inconsistent_acknowledgement_retains_the_full_batch() -> None:
    clock = ManualClock()
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=202,
                retry_after_ms=1000,
                body={"accepted": 1, "rejected": 0, "errors": []},
            ),
            FakeResponse(
                status_code=202,
                body={"accepted": 2, "rejected": 0, "errors": []},
            ),
        ]
    )
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")
    sdk.capture_message("first", level="error")
    sdk.capture_message("second", level="error")

    sdk.flush()
    assert sdk.last_event_at is None
    assert sdk.status == "degraded"

    clock.advance(1.001)
    sdk.flush()
    second_events = transport.calls[1]["events"]
    assert isinstance(second_events, list)
    assert [event["payload"]["message"] for event in second_events] == ["first", "second"]


def test_flushes_when_batch_size_is_reached() -> None:
    transport = FakeTransport()
    delivered = threading.Event()

    def send(request: dict[str, object]) -> FakeResponse:
        result = transport(request)
        delivered.set()
        return result

    sdk = DebugBundleSdk(transport=send)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        batch_size=2,
    )

    sdk.capture_message("first", level="warning")
    sdk.capture_message("second", level="warning")

    assert delivered.wait(timeout=2)
    assert len(transport.calls) == 1
    assert len(transport.calls[0]["events"]) == 2


def test_redacts_sensitive_request_fields_before_transport() -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")

    sdk.capture_exception(
        RuntimeError("login failed"),
        context={
            "request": {
                "method": "POST",
                "path": "/login",
                "headers": {"authorization": "Bearer secret-token"},
                "query": {"token": "query-secret"},
                "body": {"password": "super-secret"},
            },
            "response": {"status_code": 401},
        },
    )
    sdk.flush()

    event = transport.calls[0]["events"][0]
    request_payload = event["payload"]["request"]
    assert request_payload["headers"]["authorization"] == "[REDACTED]"
    assert request_payload["query"]["token"] == "[REDACTED]"
    assert request_payload["body"]["password"] == "[REDACTED]"


def test_flushes_always_on_probe_data_and_keeps_heavy_probes_dormant() -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")

    invoked = {"count": 0}

    def heavy_probe() -> dict[str, object]:
        invoked["count"] += 1
        return {"plan": "full scan"}

    sdk.probe("checkout.tax", {"secret": "tax-secret", "rate": 0.2})
    sdk.probe("db.query-plan", heavy_probe, opts={"heavy": True})
    sdk.capture_exception(RuntimeError("checkout failed"))
    sdk.flush()

    assert invoked["count"] == 0
    probe_data = transport.calls[0]["events"][0]["payload"]["probe_data"]
    assert probe_data["items"][0]["label"] == "checkout.tax"
    assert probe_data["items"][0]["data"]["secret"] == "[REDACTED]"


def test_emits_contract_compliant_event_envelopes() -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")

    sdk.capture_message("warning raised", level="warning", context={"tenant": "acme"})
    sdk.capture_request(
        {"method": "GET", "path": "/orders", "headers": {"x-request-id": "req_1"}, "query": {"page": "1"}},
        {"status_code": 503, "duration_ms": 45},
    )
    sdk.capture_exception(
        RuntimeError("checkout failed"),
        context={
            "request": {"method": "POST", "path": "/checkout", "headers": {"authorization": "secret"}, "query": {}},
            "response": {"status_code": 500},
        },
    )
    sdk.flush()

    events = transport.calls[0]["events"]
    for event in events:
        assert event["schema_version"] == "2026-03-01"
        uuid.UUID(event["event_id"])
        assert event["sdk_name"] == "debugbundle-python"
        assert isinstance(event["sdk_version"], str)
        assert event["occurred_at"].endswith("Z")
        assert event["service"] == {
            "name": "checkout-api",
            "runtime": "python",
            "framework": None,
            "environment": "production",
        }
        assert event["correlation"] == {
            "request_id": None,
            "trace_id": None,
            "session_id": None,
            "user_id_hash": None,
        }

    log_event = next(event for event in events if event["event_type"] == "log_event")
    assert log_event["payload"] == {
        "level": "warning",
        "message": "warning raised",
        "attributes": {"tenant": "acme"},
    }

    request_event = next(event for event in events if event["event_type"] == "request_event")
    assert request_event["payload"] == {
        "method": "GET",
        "path": "/orders",
        "query": {"page": "1"},
        "headers": {"x-request-id": "req_1"},
        "response_status": 503,
        "duration_ms": 45,
    }

    exception_event = next(event for event in events if event["event_type"] == "backend_exception")
    assert exception_event["payload"]["name"] == "RuntimeError"
    assert exception_event["payload"]["message"] == "checkout failed"
    assert exception_event["payload"]["handled"] is True
    assert exception_event["payload"]["request"]["path"] == "/checkout"
    assert exception_event["payload"]["response"]["status_code"] == 500
    runtime = exception_event["payload"]["runtime"]
    assert runtime["version"] == platform.python_version()
    assert runtime["platform"] == sys.platform
    assert isinstance(runtime["arch"], str)
    assert isinstance(runtime["pid"], int)
    assert isinstance(runtime["cwd"], str)
    assert isinstance(runtime["uptime_sec"], (int, float))
    assert runtime["uptime_sec"] >= 0
    assert isinstance(runtime["hostname"], str)
    assert isinstance(runtime["thread_id"], int)
    assert isinstance(runtime["memory"], dict)
    assert "environment" not in runtime


def test_suppresses_duplicate_exceptions_after_the_first_three() -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")

    for _ in range(5):
        sdk.capture_exception(RuntimeError("same failure"))

    sdk.flush()

    events = transport.calls[0]["events"]
    assert [event["event_type"] for event in events].count("backend_exception") == 3
    suppressed = [event for event in events if event["event_type"] == "error_suppressed"]
    assert len(suppressed) == 1
    assert suppressed[0]["payload"]["suppressed_count"] == 2


# ── Health status tests ──


def test_status_disconnected_before_init() -> None:
    sdk = DebugBundleSdk()
    assert sdk.status == "disconnected"
    assert sdk.last_event_at is None


def test_status_healthy_after_init() -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="test", environment="test")
    assert sdk.status == "healthy"
    assert sdk.last_event_at is None


def test_status_healthy_with_last_event_at_after_flush() -> None:
    clock = ManualClock()
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(project_token="dbundle_proj_test", service="test", environment="test")
    sdk.capture_exception(RuntimeError("test"))
    sdk.flush()
    assert sdk.status == "healthy"
    assert sdk.last_event_at == clock.now * 1000


def test_status_degraded_on_429() -> None:
    clock = ManualClock()
    transport = FakeTransport(responses=[FakeResponse(status_code=429, retry_after_ms=5_000)])
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(project_token="dbundle_proj_test", service="test", environment="test")
    sdk.capture_exception(RuntimeError("test"))
    sdk.flush()
    assert sdk.status == "degraded"


def test_status_recovers_to_healthy_after_degraded() -> None:
    clock = ManualClock()
    transport = FakeTransport(responses=[
        FakeResponse(status_code=429, retry_after_ms=1_000),
        FakeResponse(status_code=202),
    ])
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(project_token="dbundle_proj_test", service="test", environment="test")
    sdk.capture_exception(RuntimeError("first"))
    sdk.flush()
    assert sdk.status == "degraded"

    clock.advance(2)
    sdk.flush()
    assert sdk.status == "healthy"


def test_status_disconnected_after_3_failures() -> None:
    transport = FakeTransport(responses=[FakeResponse(status_code=500)])
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="test", environment="test")
    for i in range(3):
        sdk.capture_exception(RuntimeError(f"error-{i}"))
        sdk.flush()
    assert sdk.status == "disconnected"


def test_status_disconnected_after_3_transport_errors() -> None:
    def failing_transport(_: dict[str, object]) -> FakeResponse:
        raise ConnectionError("network down")

    sdk = DebugBundleSdk(transport=failing_transport)
    sdk.init(project_token="dbundle_proj_test", service="test", environment="test")
    for i in range(3):
        sdk.capture_exception(RuntimeError(f"error-{i}"))
        sdk.flush()
    assert sdk.status == "disconnected"


def test_status_resets_on_reinit() -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="test", environment="test")
    sdk.capture_exception(RuntimeError("test"))
    sdk.flush()
    assert sdk.last_event_at is not None

    sdk.init(project_token="dbundle_proj_test", service="test", environment="test")
    assert sdk.status == "healthy"
    assert sdk.last_event_at is None


def test_consecutive_failures_reset_on_success() -> None:
    call_count = 0

    def transport(request: dict[str, object]) -> FakeResponse:
        nonlocal call_count
        call_count += 1
        return FakeResponse(status_code=500) if call_count <= 2 else FakeResponse(status_code=202)

    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="test", environment="test")
    sdk.capture_exception(RuntimeError("fail-1"))
    sdk.flush()
    sdk.capture_exception(RuntimeError("fail-2"))
    sdk.flush()
    sdk.capture_exception(RuntimeError("success"))
    sdk.flush()
    assert sdk.status == "healthy"
    assert sdk.last_event_at is not None
