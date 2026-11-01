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
