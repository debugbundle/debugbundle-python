from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from debugbundle.core import DebugBundleSdk


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, request: dict[str, object]) -> object:
        self.calls.append(request)
        return type("Response", (), {"status_code": 202, "retry_after_ms": None})()


class FakeConfigResponse:
    def __init__(self, status_code: int, payload: object, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        return self._payload


class FakeFetch:
    def __init__(self, response: FakeConfigResponse) -> None:
        self._response = response

    def __call__(self, url: str, request: dict[str, object]) -> FakeConfigResponse:
        return self._response


def test_emitted_python_sdk_events_validate_against_vendored_event_envelope_schema() -> None:
    schema = json.loads(
        Path(__file__).with_name("fixtures").joinpath("event-envelope.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)

    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        fetch_impl=FakeFetch(
            FakeConfigResponse(
                200,
                {
                    "probes_enabled": True,
                    "remote_probes_enabled": True,
                    "active_probes": [
                        {
                            "id": "550e8400-e29b-41d4-a716-446655440000",
                            "label_pattern": "checkout.*",
                            "service": "checkout-api",
                            "environment": "production",
                            "expires_at": "2099-01-01T00:00:00.000Z"
                        }
                    ],
                    "poll_interval_ms": 15000,
                    "capture_policy": {
                        "preset": "balanced",
                        "capture_logs": "warning",
                        "capture_request_events": "all",
                        "capture_breadcrumbs": "local_only",
                        "capture_probe_events": "standalone_when_activated"
                    }
                },
            )
        ),
    )

    sdk.probe("checkout.tax", {"rate": 0.2})
    sdk.probe("checkout.deep-tax", {"region": "us-east-1"}, opts={"heavy": True})
    for _ in range(5):
        sdk.capture_exception(
            RuntimeError("checkout failed"),
            context={
                "request": {"method": "POST", "path": "/checkout", "headers": {}, "query": {}},
                "response": {"status_code": 500},
            },
        )
    sdk.capture_message("warning raised", level="warning", context={"tenant": "acme"})
    sdk.capture_request(
        {"method": "GET", "path": "/orders", "headers": {"x-request-id": "req_1"}, "query": {"page": "1"}},
        {"status_code": 503, "duration_ms": 41},
    )
    sdk.flush()

    events = transport.calls[0]["events"]
    event_types = {event["event_type"] for event in events}
    assert {"backend_exception", "error_suppressed", "log_event", "request_event", "probe_event"}.issubset(event_types)

    for event in events:
        validator.validate(event)