from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

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
                "request": {"method": "GET", "path": "/", "query": {}, "headers": {}},
                "response": {"status_code": 500},
                "runtime": {"version": "3.10"},
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
        ("frontend_breadcrumb", {"breadcrumb_type": "route_change", "data": {}}),
        (
            "frontend_exception",
            {
                "name": "TypeError",
                "message": "boom",
                "stack": "trace",
                "browser": {"name": "unknown", "version": "unknown"},
            },
        ),
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


def test_hook_validation_never_iterates_custom_envelopes_or_formats_custom_identity() -> None:
    calls: list[str] = []

    class Envelope(dict):
        def __iter__(self):
            calls.append("iterate")
            return super().__iter__()

    class Identity:
        def __str__(self):
            calls.append("format")
            return str(uuid.uuid4())

    event = event_for("log_event", {"level": "error", "message": "original", "attributes": {}})
    for candidate in (Envelope(event), {**event, "event_id": Identity()}):
        assert apply_before_send(event, lambda _: candidate, lambda *_args: None) == event
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {
            "name": "Error",
            "message": "safe",
            "stack": "trace",
            "handled": True,
            "request": {},
            "response": {},
            "runtime": {},
        },
        {"level": "error", "message": "safe", "attributes": {}, "device": {"platform": 7}},
    ],
)
def test_hook_rejects_nested_fields_outside_the_accepting_contract(payload) -> None:
    original = event_for("log_event", {"level": "error", "message": "original", "attributes": {}})
    kind = "backend_exception" if "request" in payload else "log_event"
    replacement = event_for(kind, payload)
    assert not _is_valid_event(replacement)
    assert apply_before_send(original, lambda _: replacement, lambda *_: None) == original


def test_hook_withholds_validation_budget_exhaustion_without_restoring_private_original() -> None:
    original = event_for("log_event", {"level": "error", "message": "private original", "attributes": {}})
    replacement = event_for("log_event", {"level": "error", "message": "safe", "attributes": {}})
    replacement["context"] = {"application": "x" * (9 * 1024 * 1024)}
    assert apply_before_send(original, lambda _: replacement, lambda *_: None) is None


def test_hook_owns_nested_json_and_never_executes_application_serializers_or_equality() -> None:
    calls = []

    class Foreign(dict):
        def items(self):
            calls.append("items")
            raise AssertionError("application callback")

        def __deepcopy__(self, memo):
            calls.append("copy")
            raise AssertionError("application callback")

    original = event_for("log_event", {"level": "error", "message": "original", "attributes": {}})
    replacement = event_for("log_event", {"level": "custom", "message": "safe", "attributes": {"nested": []}})
    accepted = apply_before_send(original, lambda _: replacement, lambda *_: None)
    assert accepted == replacement and accepted is not replacement
    replacement["payload"]["attributes"]["nested"].append("later mutation")
    assert accepted["payload"]["attributes"]["nested"] == []
    replacement["context"] = {"foreign": Foreign()}
    assert apply_before_send(original, lambda _: replacement, lambda *_: None) == original
    assert calls == []


@pytest.mark.parametrize(
    "fixture",
    json.loads(Path(__file__).with_name("fixtures").joinpath("evidence-wire-cases.json").read_text())["events"],
    ids=lambda fixture: fixture["id"],
)
def test_complete_boundary_matches_accepting_evidence_corpus(fixture) -> None:
    assert _is_valid_event(fixture["event"]) == fixture["valid"]
