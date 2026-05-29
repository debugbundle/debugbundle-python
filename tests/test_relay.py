from __future__ import annotations

import json
from pathlib import Path

from debugbundle.relay import BrowserRelayAcceptedBatch, BrowserRelayHandler

_RELAY_COMPLIANCE_FIXTURES = json.loads(
    Path(__file__).with_name("fixtures").joinpath("relay-compliance.json").read_text(encoding="utf-8")
)


def _relay_compliance_fixture(case_id: str) -> dict:
    for fixture in _RELAY_COMPLIANCE_FIXTURES["cases"]:
        if fixture["id"] == case_id:
            return fixture
    raise AssertionError(f"Missing relay compliance fixture: {case_id}")


def _valid_event(event_type: str = "frontend_exception") -> dict:
    return {
        "schema_version": "2026-03-01",
        "event_id": "evt-1",
        "event_type": event_type,
        "occurred_at": "2024-01-01T00:00:00Z",
        "sdk_name": "@debugbundle/sdk-browser",
        "sdk_version": "0.1.0",
        "service": {"name": "web-app", "environment": "production"},
        "payload": {"message": "test"},
    }


def _make_request(
    body: object | str | None = None,
    method: str = "POST",
    origin: str = "https://example.com",
    host: str = "example.com",
    content_type: str = "application/json",
    ip_address: str | None = "1.2.3.4",
) -> dict:
    if body is None:
        body = {"batch": [_valid_event()]}
    raw_body = json.dumps(body) if not isinstance(body, str) else body
    return {
        "method": method,
        "headers": {
            "Origin": origin,
            "Host": host,
            "Content-Type": content_type,
        },
        "body": raw_body,
        "ip_address": ip_address,
    }


def _make_request_from_fixture(request: dict) -> dict:
    body = request["bodyText"] if "bodyText" in request else json.dumps(request.get("bodyJson", {"batch": []}))
    return {
        "method": request.get("method", "POST"),
        "headers": request.get("headers", {}),
        "body": body,
        "ip_address": request.get("ipAddress"),
    }


def test_accepts_valid_batch() -> None:
    handler = BrowserRelayHandler(allowed_origins=["https://example.com"])
    response = handler.handle(_make_request())
    assert response.status == 202
    assert response.body is not None
    assert response.body["accepted"] == 1
    assert response.body["rejected"] == 0


def test_rejects_non_post() -> None:
    handler = BrowserRelayHandler(allowed_origins=["https://example.com"])
    response = handler.handle(_make_request(method="GET"))
    assert response.status == 405


def test_answers_allowed_cross_origin_preflight() -> None:
    handler = BrowserRelayHandler(allowed_origins=["https://web.example.com"])
    response = handler.handle(
        _make_request(
            body="",
            method="OPTIONS",
            origin="https://web.example.com",
            host="api.example.com",
        )
    )
    assert response.status == 204
    assert response.headers["access-control-allow-origin"] == "https://web.example.com"
    assert response.headers["access-control-allow-methods"] == "POST, OPTIONS"


def test_adds_cors_headers_to_accepted_cross_origin_posts() -> None:
    handler = BrowserRelayHandler(allowed_origins=["https://web.example.com"])
    response = handler.handle(_make_request(origin="https://web.example.com", host="api.example.com"))
    assert response.status == 202
    assert response.headers["access-control-allow-origin"] == "https://web.example.com"
    assert response.headers["vary"] == "Origin"


def test_rejects_invalid_origin() -> None:
    handler = BrowserRelayHandler(allowed_origins=["https://allowed.com"])
    response = handler.handle(_make_request(origin="https://attacker.com"))
    assert response.status == 403


def test_allows_same_origin_when_no_allowed_origins() -> None:
    handler = BrowserRelayHandler()
    response = handler.handle(_make_request(origin="https://example.com", host="example.com"))
    assert response.status == 202


def test_rejects_cross_origin_when_no_allowed_origins() -> None:
    handler = BrowserRelayHandler()
    response = handler.handle(_make_request(origin="https://other.com", host="example.com"))
    assert response.status == 403


def test_rejects_wrong_content_type() -> None:
    handler = BrowserRelayHandler(allowed_origins=["https://example.com"])
    response = handler.handle(_make_request(content_type="text/plain"))
    assert response.status == 400
    assert response.body is not None
    assert "Content-Type" in response.body["errors"][0]


def test_rejects_oversized_body() -> None:
    handler = BrowserRelayHandler(
        allowed_origins=["https://example.com"],
        max_body_bytes=10,
    )
    response = handler.handle(_make_request())
    assert response.status == 413


def test_rate_limits_by_ip() -> None:
    handler = BrowserRelayHandler(
        allowed_origins=["https://example.com"],
        rate_limit_per_minute=2,
    )
    handler.handle(_make_request())
    handler.handle(_make_request())
    response = handler.handle(_make_request())
    assert response.status == 429


def test_rejects_invalid_json() -> None:
    handler = BrowserRelayHandler(allowed_origins=["https://example.com"])
    response = handler.handle(_make_request(body="not json"))
    assert response.status == 400


def test_rejects_non_dict_body() -> None:
    handler = BrowserRelayHandler(allowed_origins=["https://example.com"])
    response = handler.handle(_make_request(body=[1, 2, 3]))
    assert response.status == 400


def test_rejects_missing_batch_key() -> None:
    handler = BrowserRelayHandler(allowed_origins=["https://example.com"])
    response = handler.handle(_make_request(body={"events": []}))
    assert response.status == 400
    assert "batch array" in response.body["errors"][0]


def test_rejects_unsupported_event_type() -> None:
    fixture = _relay_compliance_fixture("mixed-valid-invalid-batch")
    handler = BrowserRelayHandler(allowed_origins=[fixture["request"]["headers"]["origin"]])
    response = handler.handle(_make_request_from_fixture(fixture["request"]))
    assert response.status == fixture["expected"]["status"]
    assert response.body["accepted"] == fixture["expected"]["accepted"]
    assert response.body["rejected"] == fixture["expected"]["rejected"]
    assert response.body["errors"] == fixture["expected"]["errors"]


def test_strips_trust_sensitive_fields() -> None:
    fixture = _relay_compliance_fixture("credential-smuggling-payload")
    accepted: list[BrowserRelayAcceptedBatch] = []
    handler = BrowserRelayHandler(
        allowed_origins=[fixture["request"]["headers"]["origin"]],
        on_accept=lambda batch: accepted.append(batch),
    )
    response = handler.handle(_make_request_from_fixture(fixture["request"]))
    assert response.status == fixture["expected"]["status"]
    assert len(accepted) == 1
    sanitized = accepted[0].events[0]
    assert "authorization" not in accepted[0].headers
    assert "cookie" not in accepted[0].headers
    assert "x-api-key" not in accepted[0].headers
    assert "project_token" not in sanitized
    assert "organization_id" not in sanitized
    assert sanitized == fixture["expectedEventFile"][0]


def test_preserves_correlation_trace_id() -> None:
    accepted: list[BrowserRelayAcceptedBatch] = []
    handler = BrowserRelayHandler(
        allowed_origins=["https://example.com"],
        on_accept=lambda batch: accepted.append(batch),
    )
    evt = _valid_event()
    evt["correlation"] = {"trace_id": "abc-123", "session_id": "sess-should-drop"}
    response = handler.handle(_make_request(body={"batch": [evt]}))
    assert response.status == 202
    sanitized = accepted[0].events[0]
    assert sanitized["correlation"]["trace_id"] == "abc-123"


def test_accepts_all_supported_event_types() -> None:
    handler = BrowserRelayHandler(allowed_origins=["https://example.com"])
    for event_type in ("frontend_exception", "error_suppressed", "frontend_breadcrumb", "request_event", "probe_event"):
        evt = _valid_event(event_type)
        response = handler.handle(
            _make_request(body={"batch": [evt]}, ip_address=f"10.0.0.{hash(event_type) % 254 + 1}")
        )
        assert response.status == 202, f"Failed for event type: {event_type}"


def test_accepts_browser_request_event_payloads() -> None:
    accepted: list[BrowserRelayAcceptedBatch] = []
    handler = BrowserRelayHandler(
        allowed_origins=["https://example.com"],
        on_accept=lambda batch: accepted.append(batch),
    )
    event = _valid_event("request_event")
    event["payload"] = {
        "method": "POST",
        "path": "/v1/billing/checkout",
        "query": {"plan": "team"},
        "headers": {},
        "response_status": 503,
        "duration_ms": 84,
    }

    response = handler.handle(_make_request(body={"batch": [event]}))

    assert response.status == 202
    assert len(accepted) == 1
    assert accepted[0].events[0]["event_type"] == "request_event"
    assert accepted[0].events[0]["payload"]["response_status"] == 503


def test_rejects_event_with_missing_required_fields() -> None:
    handler = BrowserRelayHandler(allowed_origins=["https://example.com"])
    bad_event = {"event_type": "frontend_exception"}
    response = handler.handle(_make_request(body={"batch": [bad_event]}))
    assert response.status == 400
    assert response.body["rejected"] == 1


def test_on_accept_receives_batch_metadata() -> None:
    accepted: list[BrowserRelayAcceptedBatch] = []
    handler = BrowserRelayHandler(
        allowed_origins=["https://example.com"],
        on_accept=lambda batch: accepted.append(batch),
    )
    handler.handle(_make_request(ip_address="9.8.7.6"))
    assert len(accepted) == 1
    assert accepted[0].ip_address == "9.8.7.6"
    assert accepted[0].received_at
    assert "authorization" not in accepted[0].headers


def test_local_only_mode_writes_relay_event_file(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    handler = BrowserRelayHandler(
        allowed_origins=["https://example.com"],
        project_mode="local-only",
        local_events_dir=str(events_dir),
    )

    response = handler.handle(_make_request())

    assert response.status == 202
    written_files = list(events_dir.glob("*.events.json"))
    assert len(written_files) == 1
    assert json.loads(written_files[0].read_text(encoding="utf-8"))[0]["event_type"] == "frontend_exception"


def test_connected_durable_mode_marks_spool_file_delivered_after_forward_success(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"
    forwarded: list[dict[str, object]] = []

    def forward_transport(request: dict[str, object]) -> object:
        forwarded.append(request)
        return type("Response", (), {"status_code": 202, "retry_after_ms": None})()

    handler = BrowserRelayHandler(
        allowed_origins=["https://example.com"],
        project_mode="connected",
        project_token="dbundle_proj_test",
        endpoint="https://api.debugbundle.com/v1/events",
        spool_dir=str(spool_dir),
        forward_transport=forward_transport,
    )

    response = handler.handle(_make_request())

    assert response.status == 202
    spool_files = list(spool_dir.glob("*.events.json"))
    assert len(spool_files) == 1
    assert spool_files[0].with_name(f"{spool_files[0].name}.delivered").exists()
    assert forwarded
    forwarded_event = forwarded[0]["events"][0]
    assert forwarded_event["project_token"] == "dbundle_proj_test"


def test_connected_durable_mode_retains_spool_file_when_forwarding_fails(tmp_path: Path) -> None:
    spool_dir = tmp_path / "spool"

    def forward_transport(_: dict[str, object]) -> object:
        return type("Response", (), {"status_code": 500, "retry_after_ms": None})()

    handler = BrowserRelayHandler(
        allowed_origins=["https://example.com"],
        project_mode="connected",
        project_token="dbundle_proj_test",
        endpoint="https://api.debugbundle.com/v1/events",
        spool_dir=str(spool_dir),
        forward_transport=forward_transport,
    )

    response = handler.handle(_make_request())

    assert response.status == 202
    spool_files = list(spool_dir.glob("*.events.json"))
    assert len(spool_files) == 1
    assert not spool_files[0].with_name(f"{spool_files[0].name}.delivered").exists()


def test_connected_low_latency_mode_returns_500_when_forwarding_fails() -> None:
    def forward_transport(_: dict[str, object]) -> object:
        return type("Response", (), {"status_code": 500, "retry_after_ms": None})()

    handler = BrowserRelayHandler(
        allowed_origins=["https://example.com"],
        project_mode="connected",
        project_token="dbundle_proj_test",
        endpoint="https://api.debugbundle.com/v1/events",
        durable_write=False,
        forward_transport=forward_transport,
    )

    response = handler.handle(_make_request())

    assert response.status == 500
