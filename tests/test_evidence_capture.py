from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

from debugbundle.config import MINIMAL_CAPTURE_POLICY
from debugbundle.core import DebugBundleSdk
from debugbundle.evidence_wire import FEATURES, validate_evidence
from debugbundle.transport import TransportResponse

PIN = {"version": 1, "features": list(FEATURES)}


def sdk_with_calls(**options: Any) -> tuple[DebugBundleSdk, list[Any]]:
    calls: list[Any] = []

    def send(request: Any) -> TransportResponse:
        calls.extend(request["events"])
        return TransportResponse(202)

    sdk = DebugBundleSdk(transport=send)
    sdk.init("dbundle_proj_test", batch_size=1000, flush_interval=3600, **options)
    return sdk, calls


def native_error() -> BaseException:
    try:
        try:
            raise ValueError("private cause")
        except ValueError as cause:
            raise RuntimeError("outer") from cause
    except RuntimeError as error:
        return error


def test_unverified_capture_keeps_legacy_shape_and_verified_capture_adds_native_evidence() -> None:
    for pin in (None, PIN):
        sdk, calls = sdk_with_calls(evidence_capabilities=pin)
        try:
            sdk.capture_exception(native_error())
            sdk.flush()
            assert len(calls) == 1
            event = calls[0]
            assert ("exception_evidence" in event["payload"]) == (pin is not None)
            if pin is not None:
                graph = event["payload"]["exception_evidence"]
                assert [node["name"] for node in graph["nodes"]] == ["RuntimeError", "ValueError"]
                assert validate_evidence(event)
                assert "private cause" in event["payload"]["stack"]
        finally:
            sdk.dispose()


def test_operation_scope_is_monotonic_and_hook_can_replace_canonical_identity() -> None:
    replacement_id = str(uuid.uuid4())

    def hook(event: Any) -> Any:
        event["event_id"] = replacement_id
        return event

    sdk, calls = sdk_with_calls(evidence_capabilities=PIN, before_send=hook)
    try:
        with sdk.operation_context({"kind": "job", "name": "invoice.calculate", "limit_ms": 5000}):
            sdk.capture_exception(native_error(), {"failure_ref": str(uuid.uuid4())})
        sdk.flush()
        event = calls[0]
        assert event["event_id"] == replacement_id
        assert event["payload"]["execution"]["kind"] == "job"
        assert event["payload"]["execution"]["limit_ms"] == 5000
        assert event["payload"]["execution"]["elapsed_ms"] >= 0
        assert event["context"]["evidence_reference"]["scope"]["kind"] == "operation"
        assert "failure_ref" not in event["context"]
        assert validate_evidence(event)
        reference = event["context"]["evidence_reference"]
        sdk._capture_recovery_observations = True
        sdk._before_send = None
        sdk.capture_recovery(
            {
                "failure_ref": reference["failure_ref"],
                "evidence_scope": reference["scope"],
                "attempt_id": str(uuid.uuid4()),
                "state": "recovery_observed",
            }
        )
        sdk.flush()
        assert calls[-1]["event_type"] == "diagnostic_context"
        sdk.capture_exception(ValueError("outside"))
        sdk.flush()
        assert "execution" not in calls[-1]["payload"]
    finally:
        sdk.dispose()


def test_detached_context_loses_completed_request_and_reinitialised_context() -> None:
    async def run() -> None:
        sdk, calls = sdk_with_calls(evidence_capabilities=PIN)
        ready = asyncio.Event()

        async def child() -> None:
            await ready.wait()
            sdk.capture_exception(native_error())

        context = sdk._bind_scoped_context({"trace_id": "finished-trace"})
        request = sdk.begin_request({"method": "GET", "path": "/test", "headers": {}})
        task = asyncio.create_task(child())
        sdk.end_request(request)
        sdk._reset_scoped_context(context)
        ready.set()
        await task
        sdk.flush()
        assert calls[0]["correlation"]["trace_id"] is None
        assert "execution" not in calls[0]["payload"]
        old = sdk._bind_scoped_context({"trace_id": "old-project"})
        sdk.init("dbundle_proj_other", evidence_capabilities=PIN, batch_size=1000, flush_interval=3600)
        sdk.capture_exception(ValueError("new project"))
        sdk.flush()
        assert calls[-1]["correlation"]["trace_id"] is None
        sdk._reset_scoped_context(old)
        sdk.dispose()

    asyncio.run(run())


def observation() -> dict[str, Any]:
    return {
        "failure_ref": str(uuid.uuid4()),
        "evidence_scope": {"kind": "operation", "operation_id": str(uuid.uuid4())},
        "attempt_id": str(uuid.uuid4()),
        "state": "attempt_completed",
    }


def test_recovery_requires_explicit_opt_in_policy_and_proof() -> None:
    for flag, pin, minimal in [(False, PIN, False), (True, None, False), (True, PIN, True), (True, PIN, False)]:
        sdk, calls = sdk_with_calls(capture_recovery_observations=flag, evidence_capabilities=pin)
        try:
            if minimal:
                sdk._capture_policy = MINIMAL_CAPTURE_POLICY
            value = observation()
            assert sdk.capture_recovery(value) is None
            sdk.flush()
            assert len(calls) == (1 if flag and pin and not minimal else 0)
            if calls:
                assert calls[0]["payload"]["state"] == "attempt_completed"
                assert validate_evidence(calls[0])
                assert sdk._diagnostics.accounted_bytes == 0 or sdk._diagnostics.accounted_bytes < 1024
        finally:
            sdk.dispose()


def test_recovery_attempt_and_observation_limits_survive_ack_and_hook_identity_changes() -> None:
    next_id = str(uuid.uuid4())

    def hook(event: Any) -> Any:
        event["event_id"] = next_id
        return event

    sdk, calls = sdk_with_calls(capture_recovery_observations=True, evidence_capabilities=PIN, before_send=hook)
    try:
        value = observation()
        sdk.capture_recovery(value)
        sdk.flush()
        assert calls[0]["event_id"] == next_id
        sdk._before_send = None
        for _ in range(25):
            sdk.capture_recovery(value)
            sdk.flush()
        assert len(calls) == 20
        other = observation()
        for _ in range(7):
            other["attempt_id"] = str(uuid.uuid4())
            sdk.capture_recovery(other)
            sdk.flush()
        assert len(calls) == 25
        assert sdk._diagnostics.accounted_bytes < 4096
    finally:
        sdk.dispose()


def test_page_recovery_preserves_zero_session_and_drops_blank_session() -> None:
    sdk, calls = sdk_with_calls(capture_recovery_observations=True, evidence_capabilities=PIN)
    try:
        value = observation()
        value["evidence_scope"] = {"kind": "page", "page_id": str(uuid.uuid4())}
        value["correlation"] = {"session_id": " ", "trace_id": "explicit"}
        sdk.capture_recovery(value)
        value["correlation"]["session_id"] = "0"
        sdk.capture_recovery(value)
        sdk.flush()
        assert len(calls) == 1
        assert calls[0]["correlation"]["session_id"] == "0"
        assert calls[0]["correlation"]["trace_id"] == "explicit"
    finally:
        sdk.dispose()


def test_uncaught_native_capture_bypasses_hook_and_uses_smaller_total_budget() -> None:
    invoked: list[object] = []
    sdk, calls = sdk_with_calls(evidence_capabilities=PIN, before_send=lambda event: invoked.append(event))
    try:
        error = native_error()
        with sdk.operation_context({"kind": "command", "name": "worker.run", "limit_ms": 100}):
            sys.excepthook(type(error), error, error.__traceback__)
        sdk.flush()
        assert invoked == [] and len(calls) == 1
        additions = {key: calls[0]["payload"][key] for key in ("exception_evidence", "execution")}
        assert len(json.dumps(additions, ensure_ascii=False, separators=(",", ":")).encode()) <= 4096
        assert len(additions["exception_evidence"]["nodes"]) <= 2
        assert all(len(node["frames"]) <= 8 for node in additions["exception_evidence"]["nodes"])
        assert calls[0]["payload"]["handled"] is False
        assert sdk._hook_bypass == set()
    finally:
        sdk.dispose()


def test_negotiated_rejection_retries_only_stripped_original_without_reinvoking_hook() -> None:
    requests: list[Any] = []
    now = [1700000000.0]
    hooks: list[Any] = []

    def send(request: Any) -> TransportResponse:
        requests.append(request)
        return TransportResponse(
            202,
            1000,
            {
                "accepted": 0,
                "rejected": 1,
                "errors": [{"index": 0, "reason": "invalid_event"}],
                "evidence_capabilities": {"version": 1, "features": []},
            },
        )

    def hook(event: Any) -> Any:
        hooks.append(event)
        return event

    sdk = DebugBundleSdk(transport=send, time_provider=lambda: now[0])
    sdk.init("dbundle_proj_test", evidence_capabilities=PIN, before_send=hook, batch_size=1000, flush_interval=3600)
    try:
        sdk.capture_exception(native_error())
        sdk.flush()
        assert len(requests) == 1 and len(sdk._buffer) == 1
        now[0] += 2
        sdk.flush()
        assert len(requests) == 2 and len(hooks) == 1
        original, reduced = requests[0]["events"][0], requests[1]["events"][0]
        assert original["event_id"] == reduced["event_id"]
        assert "exception_evidence" in original["payload"]
        assert "exception_evidence" not in reduced["payload"]
        assert original["payload"]["stack"] == reduced["payload"]["stack"]
        assert sdk._buffer == [] and sdk._hook_bypass == set()
    finally:
        sdk.dispose()


def test_malformed_acknowledgement_cannot_grant_evidence_proof() -> None:
    sdk = DebugBundleSdk(
        transport=lambda _: TransportResponse(
            202, None, {"accepted": 99, "rejected": 0, "errors": [], "evidence_capabilities": PIN}
        )
    )
    sdk.init("dbundle_proj_test", batch_size=1000, flush_interval=3600)
    try:
        sdk.capture_exception(native_error())
        sdk.flush()
        assert not sdk._evidence.supports("exception_evidence_v1")
    finally:
        sdk.dispose()
