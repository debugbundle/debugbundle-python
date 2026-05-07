from __future__ import annotations

import json

from debugbundle.relay import BrowserRelayAcceptedBatch, BrowserRelayHandler


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
    handler = BrowserRelayHandler(allowed_origins=["https://example.com"])
    bad_event = _valid_event()
    bad_event["event_type"] = "backend_exception"
    response = handler.handle(_make_request(body={"batch": [bad_event]}))
    assert response.status == 400
    assert response.body["rejected"] == 1
    assert "backend_exception" in response.body["errors"][0]


def test_strips_trust_sensitive_fields() -> None:
    accepted: list[BrowserRelayAcceptedBatch] = []
    handler = BrowserRelayHandler(
        allowed_origins=["https://example.com"],
        on_accept=lambda batch: accepted.append(batch),
    )
    evt = _valid_event()
    evt["project_token"] = "dbundle_proj_secret"
    evt["organization_id"] = "org_1234"
    evt["sdk_name"] = "tampered_sdk_name"
    response = handler.handle(_make_request(body={"batch": [evt]}))
    assert response.status == 202
    assert len(accepted) == 1
    sanitized = accepted[0].events[0]
    assert "project_token" not in sanitized
    assert "organization_id" not in sanitized
    assert sanitized["sdk_name"] == "@debugbundle/sdk-browser"


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
    for event_type in ("frontend_exception", "error_suppressed", "frontend_breadcrumb", "probe_event"):
        evt = _valid_event(event_type)
        response = handler.handle(
            _make_request(body={"batch": [evt]}, ip_address=f"10.0.0.{hash(event_type) % 254 + 1}")
        )
        assert response.status == 202, f"Failed for event type: {event_type}"


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
