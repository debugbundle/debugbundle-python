from __future__ import annotations

from dataclasses import dataclass

from debugbundle.core import DebugBundleSdk


@dataclass
class FakeTransportResponse:
    status_code: int
    retry_after_ms: int | None = None


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, request: dict[str, object]) -> FakeTransportResponse:
        self.calls.append(request)
        return FakeTransportResponse(status_code=202)


class ManualClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeConfigResponse:
    def __init__(self, status_code: int, payload: object | None = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        return self._payload


class FakeFetch:
    def __init__(self, responses: list[FakeConfigResponse] | None = None, error: Exception | None = None) -> None:
        self._responses = responses or []
        self._error = error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, url: str, request: dict[str, object]) -> FakeConfigResponse:
        self.calls.append((url, request))
        if self._error is not None:
            raise self._error
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses.pop(0)


def _events(transport: FakeTransport) -> list[dict[str, object]]:
    return [event for call in transport.calls for event in call["events"]]


def test_remote_config_skips_recurring_polling_when_remote_probes_are_disabled() -> None:
    fetch = FakeFetch(
        responses=[
            FakeConfigResponse(
                200,
                {
                    "probes_enabled": True,
                    "remote_probes_enabled": False,
                    "active_probes": [],
                    "poll_interval_ms": 15000,
                },
            )
        ]
    )

    sdk = DebugBundleSdk()
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        fetch_impl=fetch,
        probes_poll_interval=15000,
    )

    assert len(fetch.calls) == 1
    assert fetch.calls[0][0] == "https://api.debugbundle.com/v1/sdk/config"
    assert fetch.calls[0][1]["method"] == "GET"


def test_remote_config_uses_etag_and_activates_heavy_probes_only_while_directive_is_live() -> None:
    clock = ManualClock()
    fetch = FakeFetch(
        responses=[
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
                            "expires_at": "2023-11-14T22:13:30.000Z",
                        }
                    ],
                    "poll_interval_ms": 15000,
                    "capture_policy": {
                        "preset": "balanced",
                        "capture_logs": "warning",
                        "capture_request_events": "failures_only",
                        "capture_breadcrumbs": "exception_only",
                        "capture_probe_events": "standalone_when_activated",
                    },
                },
                headers={"etag": '"cfg-1"'},
            ),
            FakeConfigResponse(304, None, headers={"etag": '"cfg-1"'}),
        ]
    )
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        fetch_impl=fetch,
        probes_poll_interval=60000,
    )

    invoked = {"count": 0}

    def heavy_probe() -> dict[str, object]:
        invoked["count"] += 1
        return {"tax_rate": 0.2}

    sdk.probe("checkout.tax", heavy_probe, opts={"heavy": True})
    sdk.flush()

    assert invoked["count"] == 1
    first_events = _events(transport)
    assert len(first_events) == 1
    assert first_events[0]["event_type"] == "probe_event"
    assert first_events[0]["payload"]["activation_id"] == "550e8400-e29b-41d4-a716-446655440000"
    assert first_events[0]["payload"]["probe_label_pattern"] == "checkout.*"

    sdk._refresh_remote_config()
    assert len(fetch.calls) == 2
    assert fetch.calls[1][1]["headers"]["if-none-match"] == '"cfg-1"'

    transport.calls.clear()
    clock.advance(11)
    sdk.probe("checkout.tax", heavy_probe, opts={"heavy": True})
    sdk.flush()

    assert invoked["count"] == 1
    assert transport.calls == []


def test_failed_init_config_fetch_falls_back_to_minimal_policy() -> None:
    clock = ManualClock()
    fetch = FakeFetch(error=RuntimeError("config refresh failed"))
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        fetch_impl=fetch,
        probes_poll_interval=25000,
    )

    sdk.capture_message("warning blocked", level="warning")
    sdk.capture_message("error still allowed", level="error")
    sdk.capture_message("info blocked", level="info")
    sdk.capture_request({"method": "GET", "path": "/ok", "headers": {}}, {"status_code": 200})
    sdk.capture_request({"method": "GET", "path": "/boom", "headers": {}}, {"status_code": 503})
    sdk.flush()

    events = _events(transport)
    assert [event["event_type"] for event in events] == ["log_event", "request_event"]
    assert events[0]["payload"]["message"] == "error still allowed"
    assert events[1]["payload"]["response_status"] == 503


def test_capture_policy_filters_logs_and_request_events_from_remote_config() -> None:
    clock = ManualClock()
    fetch = FakeFetch(
        responses=[
            FakeConfigResponse(
                200,
                {
                    "probes_enabled": True,
                    "remote_probes_enabled": True,
                    "active_probes": [],
                    "poll_interval_ms": 15000,
                    "capture_policy": {
                        "preset": "minimal",
                        "capture_logs": "error",
                        "capture_request_events": "failures_only",
                        "capture_breadcrumbs": "local_only",
                        "capture_probe_events": "buffer_only",
                    },
                },
            )
        ]
    )
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        fetch_impl=fetch,
    )

    sdk.capture_message("warning blocked", level="warning")
    sdk.capture_message("error kept", level="error")
    sdk.capture_request({"method": "GET", "path": "/ok", "headers": {}}, {"status_code": 200})
    sdk.capture_request({"method": "GET", "path": "/boom", "headers": {}}, {"status_code": 503})
    sdk.flush()

    events = _events(transport)
    assert [event["event_type"] for event in events] == ["log_event", "request_event"]
    assert events[0]["payload"]["message"] == "error kept"
    assert events[1]["payload"]["response_status"] == 503


def test_balanced_capture_policy_keeps_immediate_failures_but_not_unconfigured_4xx() -> None:
    clock = ManualClock()
    fetch = FakeFetch(
        responses=[
            FakeConfigResponse(
                200,
                {
                    "probes_enabled": True,
                    "remote_probes_enabled": True,
                    "active_probes": [],
                    "poll_interval_ms": 15000,
                    "capture_policy": {
                        "preset": "balanced",
                        "capture_logs": "warning",
                        "capture_request_events": "failures_only",
                        "capture_breadcrumbs": "exception_only",
                        "capture_probe_events": "buffer_only",
                    },
                },
            )
        ]
    )
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        fetch_impl=fetch,
    )

    sdk.capture_request({"method": "POST", "path": "/checkout", "headers": {}}, {"status_code": 429})
    sdk.capture_request({"method": "POST", "path": "/checkout", "headers": {}}, {"status_code": 404})
    sdk.capture_request({"method": "POST", "path": "/checkout", "headers": {}}, {"status_code": 409})
    sdk.flush()

    events = _events(transport)
    request_statuses = [
        event["payload"]["response_status"]
        for event in events
        if event["event_type"] == "request_event"
    ]
    assert request_statuses == [429]


def test_capture_policy_promotes_configured_client_error_path_rules_when_request_capture_is_off() -> None:
    clock = ManualClock()
    fetch = FakeFetch(
        responses=[
            FakeConfigResponse(
                200,
                {
                    "probes_enabled": True,
                    "remote_probes_enabled": True,
                    "active_probes": [],
                    "poll_interval_ms": 15000,
                    "capture_policy": {
                        "preset": "minimal",
                        "capture_logs": "error",
                        "capture_request_events": "off",
                        "capture_breadcrumbs": "local_only",
                        "capture_probe_events": "buffer_only",
                        "immediate_client_error_path_rules": [
                            {"status_code": 404, "path_pattern": "/checkout/*", "methods": ["POST"]}
                        ],
                    },
                },
            )
        ]
    )
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        fetch_impl=fetch,
    )

    sdk.capture_request({"method": "POST", "path": "/checkout/cart", "headers": {}}, {"status_code": 404})
    sdk.capture_request({"method": "GET", "path": "/checkout/cart", "headers": {}}, {"status_code": 404})
    sdk.capture_request({"method": "POST", "path": "/robots.txt", "headers": {}}, {"status_code": 404})
    sdk.flush()

    events = _events(transport)
    request_events = [event for event in events if event["event_type"] == "request_event"]
    assert len(request_events) == 1
    assert request_events[0]["payload"]["path"] == "/checkout/cart"
    assert request_events[0]["payload"]["response_status"] == 404


def test_investigative_capture_policy_promotes_409_even_when_request_capture_is_off() -> None:
    clock = ManualClock()
    fetch = FakeFetch(
        responses=[
            FakeConfigResponse(
                200,
                {
                    "probes_enabled": True,
                    "remote_probes_enabled": True,
                    "active_probes": [],
                    "poll_interval_ms": 15000,
                    "capture_policy": {
                        "preset": "investigative",
                        "capture_logs": "info",
                        "capture_request_events": "off",
                        "capture_breadcrumbs": "standalone",
                        "capture_probe_events": "standalone_when_activated",
                    },
                },
            )
        ]
    )
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        fetch_impl=fetch,
    )

    sdk.capture_request({"method": "POST", "path": "/checkout", "headers": {}}, {"status_code": 409})
    sdk.capture_request({"method": "POST", "path": "/checkout", "headers": {}}, {"status_code": 404})
    sdk.flush()

    events = _events(transport)
    assert [event["payload"]["response_status"] for event in events if event["event_type"] == "request_event"] == [409]


def test_capture_policy_promotes_configured_client_error_statuses_when_request_capture_is_off() -> None:
    clock = ManualClock()
    fetch = FakeFetch(
        responses=[
            FakeConfigResponse(
                200,
                {
                    "probes_enabled": True,
                    "remote_probes_enabled": True,
                    "active_probes": [],
                    "poll_interval_ms": 15000,
                    "capture_policy": {
                        "preset": "minimal",
                        "capture_logs": "error",
                        "capture_request_events": "off",
                        "capture_breadcrumbs": "local_only",
                        "capture_probe_events": "buffer_only",
                        "immediate_client_error_statuses": [422, 403, 403],
                    },
                },
            )
        ]
    )
    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport, time_provider=clock.time)
    sdk.init(
        project_token="dbundle_proj_test",
        service="checkout-api",
        environment="production",
        fetch_impl=fetch,
    )

    sdk.capture_request({"method": "POST", "path": "/checkout", "headers": {}}, {"status_code": 403})
    sdk.capture_request({"method": "POST", "path": "/checkout", "headers": {}}, {"status_code": 404})
    sdk.capture_request({"method": "POST", "path": "/checkout", "headers": {}}, {"status_code": 422})
    sdk.flush()

    events = _events(transport)
    request_statuses = [
        event["payload"]["response_status"]
        for event in events
        if event["event_type"] == "request_event"
    ]
    assert request_statuses == [403, 422]
