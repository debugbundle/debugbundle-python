from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from debugbundle.evidence_wire import FEATURES
from debugbundle.exception_evidence import snapshot_exception
from debugbundle.relay import BrowserRelayHandler
from debugbundle.transport import TransportResponse

PIN = {"version": 1, "features": list(FEATURES)}


def event() -> dict[str, Any]:
    return {
        "schema_version": "2026-03-01",
        "event_id": str(uuid.uuid4()),
        "event_type": "frontend_exception",
        "occurred_at": "2026-10-26T10:00:00Z",
        "sdk_name": "untrusted-browser",
        "sdk_version": "candidate",
        "project_token": "browser-owned-secret",
        "service": {"name": "web", "environment": "test"},
        "context": {
            "evidence_reference": {
                "version": 1,
                "failure_ref": str(uuid.uuid4()),
                "scope": {"kind": "operation", "operation_id": str(uuid.uuid4())},
            }
        },
        "payload": {
            "name": "Error",
            "message": "original",
            "stack": "Error: original",
            "browser": {"name": "unknown", "version": "unknown"},
            "exception_evidence": snapshot_exception(ValueError("cause")),
        },
    }


def request(events: list[Any], opted: bool = True) -> dict[str, Any]:
    return {
        "method": "POST",
        "url": "/debugbundle/browser?debugbundle_evidence=1" if opted else "/debugbundle/browser",
        "headers": {"origin": "https://example.com", "host": "example.com", "content-type": "application/json"},
        "body": json.dumps({"batch": events}),
    }


def test_relay_preserves_verified_dtos_and_strips_unknown_path_without_exposing_browser_credentials() -> None:
    for pin in (None, PIN):
        accepted: list[Any] = []
        handler = BrowserRelayHandler(evidence_capabilities=pin, on_accept=accepted.append)
        response = handler.handle(request([event()]))
        assert response.status == 202
        assert response.body["evidence_capabilities"]["features"] == (list(FEATURES) if pin else [])
        delivered = accepted[0].events[0]
        assert delivered["sdk_name"] == "@debugbundle/sdk-browser"
        assert "project_token" not in delivered
        assert ("exception_evidence" in delivered["payload"]) == (pin is not None)
        assert ("evidence_reference" in delivered["context"]) == (pin is not None)
        assert "evidence_capabilities" not in handler.handle(request([], False)).body


def test_partial_relay_rejection_preserves_legacy_string_errors_and_adds_original_indices() -> None:
    handler = BrowserRelayHandler(evidence_capabilities=PIN)
    invalid = event()
    invalid["payload"]["exception_evidence"]["unknown"] = True
    response = handler.handle(request([event(), invalid, {"event_type": "unsupported"}]))
    assert response.status == 202
    assert response.body["accepted"] == 1 and response.body["rejected"] == 2
    assert all(type(error) is str for error in response.body["errors"])
    assert response.body["evidence_errors"] == [
        {"index": 1, "reason": "invalid_event"},
        {"index": 2, "reason": "invalid_event"},
    ]
    assert handler.handle(request([invalid], False)).status == 400


def test_connected_relay_requires_canonical_ack_before_marking_spool_delivered(tmp_path: Path) -> None:
    def malformed(_: Any) -> TransportResponse:
        return TransportResponse(202, body={"accepted": 99, "rejected": 0, "errors": [], "evidence_capabilities": PIN})

    handler = BrowserRelayHandler(
        project_mode="connected",
        project_token="server-owned",
        endpoint="https://example.invalid/v1/events",
        forward_transport=malformed,
        spool_dir=str(tmp_path),
        evidence_capabilities=PIN,
    )
    assert handler.handle(request([event()])).status == 202  # Durable spool accepts it.
    assert list(tmp_path.glob("*.delivered")) == []
    assert handler.handle(request([])).body["evidence_capabilities"]["features"] == []


def test_connected_relay_downgrades_only_original_failure_once_after_proof_loss() -> None:
    calls: list[Any] = []

    def send(value: Any) -> TransportResponse:
        calls.append(value)
        if len(calls) == 1:
            return TransportResponse(
                202, body={"accepted": 0, "rejected": 1, "errors": [{"index": 0, "reason": "invalid_event"}]}
            )
        return TransportResponse(202, body={"accepted": 1, "rejected": 0, "errors": []})

    handler = BrowserRelayHandler(
        project_mode="connected",
        project_token="server-owned",
        endpoint="https://example.invalid/v1/events",
        forward_transport=send,
        durable_write=False,
        evidence_capabilities=PIN,
    )
    original = event()
    assert handler.handle(request([original])).status == 202
    assert len(calls) == 2
    first, retry = calls[0]["events"][0], calls[1]["events"][0]
    assert first["project_token"] == retry["project_token"] == "server-owned"
    assert retry["event_id"] == original["event_id"]
    assert "exception_evidence" in first["payload"] and "exception_evidence" not in retry["payload"]


def test_relay_bounds_final_server_token_bytes_and_retains_unacknowledged_spool(tmp_path) -> None:
    batches = []

    def send(value):
        batches.append(value["events"])
        assert (
            len(json.dumps({"events": value["events"]}, ensure_ascii=False, separators=(",", ":")).encode()) <= 262144
        )
        count = len(value["events"])
        return TransportResponse(
            202, body={"accepted": count, "rejected": 0, "errors": [], "evidence_capabilities": PIN}
        )

    accepted = []
    handler = BrowserRelayHandler(
        project_mode="connected",
        project_token="x" * 20000,
        endpoint="https://example.invalid/v1/events",
        spool_dir=str(tmp_path),
        forward_transport=send,
        evidence_capabilities=PIN,
        on_accept=accepted.append,
    )
    events = [event() for _ in range(4)]
    for value in events:
        value["context"].update({f"detail_{i}": "x" * 10000 for i in range(5)})
    incoming = request(events)
    assert len(incoming["body"].encode()) <= 262144
    assert handler.handle(incoming).status == 202
    assert len(batches) == 2
    assert [value["event_id"] for batch in batches for value in batch] == [value["event_id"] for value in events]
    assert len(list(tmp_path.glob("*.delivered"))) == 1
    assert all("project_token" not in value for value in accepted[0].events)


def test_relay_legacy_custom_success_keeps_durable_spool_for_canonical_acceptance(tmp_path) -> None:
    handler = BrowserRelayHandler(
        project_mode="connected",
        project_token="server-owned",
        endpoint="https://example.invalid/v1/events",
        spool_dir=str(tmp_path),
        forward_transport=lambda _: TransportResponse(202),
        evidence_capabilities=PIN,
    )
    assert handler.handle(request([event()], False)).status == 202
    assert len(list(tmp_path.glob("*.events.json"))) == 1
    assert not list(tmp_path.glob("*.delivered"))


def test_relay_forwarding_uses_one_deadline_across_chunks(monkeypatch) -> None:
    from debugbundle import relay_delivery

    now = [0.0]
    monkeypatch.setattr(relay_delivery.time, "monotonic", lambda: now[0])
    calls = []

    def send(value):
        calls.append(value)
        now[0] += 6
        return TransportResponse(202, body={"accepted": len(value["events"]), "rejected": 0, "errors": []})

    forwarder = relay_delivery.RelayForwardTransport("https://example.invalid/v1/events", send)
    values = [event(), event()]
    for value in values:
        value["context"] = {"detail": "x" * 150000}
    assert forwarder.send("server-owned", values, True) == (True, False)
    assert len(calls) == 1


def test_unknown_relay_path_can_acquire_verified_support_from_existing_ack_traffic() -> None:
    calls = []

    def send(value):
        calls.append(value)
        return TransportResponse(
            202, body={"accepted": len(value["events"]), "rejected": 0, "errors": [], "evidence_capabilities": PIN}
        )

    handler = BrowserRelayHandler(
        project_mode="connected",
        project_token="server-owned",
        endpoint="https://example.invalid/v1/events",
        forward_transport=send,
        durable_write=False,
    )
    assert handler.handle(request([event()])).body["evidence_capabilities"] == PIN
    assert "exception_evidence" not in calls[0]["events"][0]["payload"]
    assert handler.handle(request([event()])).status == 202
    assert "exception_evidence" in calls[1]["events"][0]["payload"]


def test_relay_broad_redact_fields_preserves_protocol_and_html_tag_allowlist() -> None:
    accepted: list[Any] = []
    handler = BrowserRelayHandler(
        evidence_capabilities=PIN,
        redact_fields=["tag", "reference", "reason", "file"],
        on_accept=accepted.append,
    )
    failure_ref = str(uuid.uuid4())
    page_id = str(uuid.uuid4())
    frontend = event()
    frontend["context"] = {
        "evidence_reference": {"version": 1, "failure_ref": failure_ref, "scope": {"kind": "page", "page_id": page_id}},
        "evidence_limits": {"version": 1, "execution": "unsupported"},
        "payout": {
            "tag": "SYNTHETIC_TAG",
            "reference": "SYNTHETIC_REF",
            "reason": "SYNTHETIC_REASON",
            "file": "SYNTHETIC_FILE",
        },
    }
    frontend["payload"]["browser_event"] = {
        "kind": "resource_error",
        "message": "asset failed reason=SYNTHETIC_REASON",
        "file_name": "https://app.example.test/app.js?file=SYNTHETIC_FILE&tag=SYNTHETIC_TAG",
        "line_number": 12,
        "column_number": 4,
        "target": {
            "tag_name": "script",
            "source_url": "https://cdn.example.test/bundle.js?token=SYNTHETIC_TOKEN&tag=SYNTHETIC_TAG",
            "attributes": {"rel": "preload reason=SYNTHETIC_REASON", "async": True},
        },
        "page": {
            "url": "https://app.example.test/checkout?reference=SYNTHETIC_REF",
            "referrer": "https://app.example.test/cart?file=SYNTHETIC_FILE",
            "ready_state": "complete",
            "visibility_state": "visible",
        },
        "opaque": False,
    }
    frontend["payload"]["rejection_reason"] = {
        "kind": "error",
        "name": "TypeError",
        "message": "fetch failed reason=SYNTHETIC_REASON",
        "preview": "tag=SYNTHETIC_TAG",
    }
    frontend["payload"]["resource_observations"] = {
        "version": 1,
        "items": [
            {
                "kind": "timing",
                "observed_at": "2026-10-26T10:00:00Z",
                "source_url": "https://cdn.example.test/tag=SYNTHETIC_TAG/bundle.js",
                "availability": "observed",
                "response_status": 502,
                "duration_ms": 20,
                "transfer_size_bytes": 0,
            }
        ],
    }

    custom_tag_frontend = json.loads(json.dumps(frontend))
    custom_tag_frontend["event_id"] = str(uuid.uuid4())
    custom_tag_frontend["payload"]["browser_event"]["target"]["tag_name"] = "custom-element"

    response = handler.handle(request([frontend, custom_tag_frontend]))
    assert response.status == 202
    assert response.body["accepted"] == 2

    delivered = accepted[0].events[0]
    assert delivered["context"]["evidence_reference"] == {
        "version": 1,
        "failure_ref": failure_ref,
        "scope": {"kind": "page", "page_id": page_id},
    }
    assert delivered["context"]["evidence_limits"] == {"version": 1, "execution": "unsupported"}
    assert delivered["context"]["payout"] == {
        "tag": "[REDACTED]",
        "reference": "[REDACTED]",
        "reason": "[REDACTED]",
        "file": "[REDACTED]",
    }
    assert delivered["payload"]["browser_event"]["target"]["tag_name"] == "script"
    assert delivered["payload"]["browser_event"]["target"]["source_url"] == (
        "https://cdn.example.test/bundle.js?token=%5BREDACTED%5D&tag=%5BREDACTED%5D"
    )
    assert delivered["payload"]["browser_event"]["target"]["attributes"] == {
        "rel": "preload reason=[REDACTED]",
        "async": True,
    }
    assert delivered["payload"]["rejection_reason"] == {
        "kind": "error",
        "name": "TypeError",
        "message": "fetch failed reason=[REDACTED]",
        "preview": "tag=[REDACTED]",
    }
    assert delivered["payload"]["exception_evidence"]["unavailable_reason"] is None
    assert delivered["payload"]["resource_observations"]["items"][0]["source_url"] == (
        "https://cdn.example.test/tag=[REDACTED]"
    )
    assert accepted[0].events[1]["payload"]["browser_event"]["target"]["tag_name"] == "[REDACTED]"

    encoded = json.dumps(accepted[0].events)
    for secret in ("SYNTHETIC_TAG", "SYNTHETIC_REF", "SYNTHETIC_REASON", "SYNTHETIC_FILE", "SYNTHETIC_TOKEN"):
        assert secret not in encoded


def test_relay_redact_exact_fields_and_framework_factories() -> None:
    from debugbundle.integrations.relay_django import create_django_relay_view
    from debugbundle.integrations.relay_fastapi import create_fastapi_relay_handler
    from debugbundle.integrations.relay_flask import create_flask_relay_handler

    accepted: list[Any] = []
    handler = BrowserRelayHandler(
        evidence_capabilities=PIN,
        redact_exact_fields=["tag"],
        on_accept=accepted.append,
    )
    frontend = event()
    frontend["context"] = {
        "tag": "SYNTHETIC_SECRET",
        "TAG": "SYNTHETIC_UPPER",
        "tag_name": "script",
        "payout_tag": "ok",
    }
    frontend["payload"]["message"] = "tag=SYNTHETIC_MSG tag_name=script"
    analytics = {
        "schema_version": "2026-03-01",
        "event_id": str(uuid.uuid4()),
        "event_type": "analytics_event",
        "occurred_at": "2026-10-26T10:00:00Z",
        "sdk_name": "@debugbundle/sdk-browser",
        "sdk_version": "3.0.0",
        "service": {"name": "web", "environment": "production"},
        "correlation": {"session_id": "sess-1"},
        "payload": {
            "kind": "action",
            "custom_dimensions": {"tag": "SYNTHETIC_DIM", "tag_name": "script"},
        },
    }
    response = handler.handle(request([frontend, analytics]))
    assert response.status == 202
    delivered_exc, delivered_analytics = accepted[0].events
    assert delivered_exc["context"] == {
        "tag": "[REDACTED]",
        "TAG": "[REDACTED]",
        "tag_name": "script",
        "payout_tag": "ok",
    }
    assert delivered_exc["payload"]["message"] == "tag=[REDACTED] tag_name=script"
    assert delivered_analytics["payload"]["custom_dimensions"] == {"tag": "[REDACTED]", "tag_name": "script"}

    # Framework relay factories accept redact_fields and redact_exact_fields cleanly.
    assert callable(create_django_relay_view(redact_fields=["reference"], redact_exact_fields=["tag"]))
    assert callable(create_flask_relay_handler(redact_fields=["reference"], redact_exact_fields=["tag"]))
    assert callable(create_fastapi_relay_handler(redact_fields=["reference"], redact_exact_fields=["tag"]))
