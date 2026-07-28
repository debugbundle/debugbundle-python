from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Mapping

import pytest

from debugbundle.before_send import _is_valid_event, apply_before_send


def event_for(event_type: str, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": "2026-03-01",
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": "2026-07-27T08:00:00Z",
        "sdk_name": "debugbundle-python",
        "sdk_version": "1.3.0",
        "service": {"name": "test-service", "environment": "test"},
        "payload": dict(payload),
    }


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "backend_exception",
            {
                "name": "RuntimeError",
                "message": "boom",
                "stack": "trace",
                "handled": True,
                "request": {},
                "response": {},
                "runtime": {},
            },
        ),
        (
            "request_event",
            {
                "method": "GET",
                "path": "/health",
                "query": {},
                "headers": {},
                "response_status": 200,
                "duration_ms": 1.5,
            },
        ),
        ("log_event", {"level": "error", "message": "boom", "attributes": {}}),
        ("frontend_breadcrumb", {"breadcrumb_type": "navigation", "data": {}}),
        ("frontend_exception", {"name": "TypeError", "message": "boom", "stack": "trace"}),
        (
            "deploy_metadata",
            {
                "commit_sha": "abc123",
                "version": "1.0.0",
                "branch": "main",
                "environment": "production",
                "deployed_at": "2026-07-27T08:00:00Z",
            },
        ),
        (
            "error_suppressed",
            {
                "fingerprint": "error:key",
                "suppressed_count": 2,
                "window_seconds": 60,
                "first_seen": "2026-07-27T08:00:00Z",
                "last_seen": "2026-07-27T08:01:00Z",
            },
        ),
        (
            "probe_event",
            {
                "label": "checkout.tax",
                "data": {"rate": 0.2},
                "activation_id": None,
                "probe_label_pattern": "checkout.*",
            },
        ),
    ],
)
def test_all_canonical_event_shapes_are_valid(event_type: str, payload: Mapping[str, object]) -> None:
    assert _is_valid_event(event_for(event_type, payload))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda event: event.update({"unexpected": True}),
        lambda event: event.update({"event_id": "invalid"}),
        lambda event: event.update({"occurred_at": "not-a-timestamp"}),
        lambda event: event.update({"service": {"name": "", "environment": "test"}}),
        lambda event: event.update({"payload": {"level": "error", "message": "missing attributes"}}),
        lambda event: event["payload"].update({"unexpected": True}),
    ],
)
def test_invalid_event_shapes_are_rejected(mutation: Callable[[dict[str, object]], None]) -> None:
    event = event_for("log_event", {"level": "error", "message": "boom", "attributes": {}})
    mutation(event)
    assert not _is_valid_event(event)


def test_hook_receives_a_clone_and_invalid_results_preserve_the_original() -> None:
    event = event_for("log_event", {"level": "error", "message": "original", "attributes": {}})
    diagnostics: list[str] = []

    def invalid_hook(candidate: dict[str, object]) -> dict[str, object]:
        payload = candidate["payload"]
        assert isinstance(payload, dict)
        payload["message"] = "mutated clone"
        candidate["event_id"] = "invalid"
        return candidate

    result = apply_before_send(event, invalid_hook, lambda code, _message: diagnostics.append(code))

    assert result == event
    assert event["payload"] == {"level": "error", "message": "original", "attributes": {}}
    assert diagnostics == ["before_send_invalid_event"]


def test_hook_drop_and_failure_are_safe() -> None:
    event = event_for("log_event", {"level": "error", "message": "original", "attributes": {}})
    diagnostics: list[str] = []
    assert apply_before_send(event, lambda _candidate: None, lambda *_args: None) is None

    def failing_hook(_candidate: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("hook failed")

    result = apply_before_send(event, failing_hook, lambda code, _message: diagnostics.append(code))
    assert result == copy.deepcopy(event)
    assert diagnostics == ["before_send_failed"]
