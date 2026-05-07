from __future__ import annotations

import platform
import uuid
from dataclasses import dataclass

import debugbundle
from debugbundle.core import DebugBundleSdk


@dataclass
class FakeResponse:
    status_code: int
    retry_after_ms: int | None = None


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


def test_flushes_when_batch_size_is_reached() -> None:
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        batch_size=2,
    )

    sdk.capture_message("first", level="warning")
    sdk.capture_message("second", level="warning")

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
    assert exception_event["payload"]["runtime"] == {"version": platform.python_version()}


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
