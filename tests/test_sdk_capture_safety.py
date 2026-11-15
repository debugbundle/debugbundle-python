from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from helpers.sdk import FakeResponse, FakeTransport

from debugbundle.config import BALANCED_CAPTURE_POLICY, MINIMAL_CAPTURE_POLICY
from debugbundle.core import DebugBundleSdk


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
        event = next(
            event for call in transport.calls for event in call["events"] if event["event_type"] == "backend_exception"
        )
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
        assert any(
            event["event_type"] == "backend_exception" and event["payload"]["message"] == "separate incident"
            for call in transport.calls
            for event in call["events"]
        )
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
        assert (
            sum(
                event["payload"]["attributes"]["suppressed_count"]
                for event in sent
                if event["event_type"] == "log_event"
                and event["payload"]["attributes"].get("reason") == "queue_pressure"
            )
            == 1
        )
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
        assert all(
            event["payload"].get("message") != "old failure" for call in transport.calls for event in call["events"]
        )
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
            sdk.init,
            project_token="dbundle_proj_test",
            fetch_impl=held_fetch,
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
            event["event_type"] == "request_event" and event["payload"]["response_status"] == 500
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
    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=429, retry_after_ms=300_000),
            FakeResponse(status_code=202),
            FakeResponse(status_code=202),
        ]
    )
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
        if any(
            event["event_type"] == "log_event" and event["payload"]["attributes"].get("reason") == "queue_pressure"
            for event in events
        ):
            reported.set()
        return FakeResponse(status_code=202)

    sdk = DebugBundleSdk(transport=transport)
    # Admission setup must not race a wall-clock timer on slow coverage runners.
    # The assertion below still requires a real automatic pressure-only flush.
    sdk.init(project_token="dbundle_proj_test", batch_size=20_000, flush_interval=3600.0)
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
                assert any(event.get("event_type") == "backend_exception" for event in sdk._buffer)
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
