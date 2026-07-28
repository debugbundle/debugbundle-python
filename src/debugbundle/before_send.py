from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime

BeforeSendHook = Callable[[dict[str, object]], dict[str, object] | None]

_EVENT_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "backend_exception": ("name", "message", "stack", "handled", "request", "response", "runtime"),
    "request_event": ("method", "path", "query", "headers", "response_status", "duration_ms"),
    "log_event": ("level", "message", "attributes"),
    "frontend_breadcrumb": ("breadcrumb_type", "data"),
    "frontend_exception": ("name", "message", "stack"),
    "deploy_metadata": ("commit_sha", "version", "branch", "environment", "deployed_at"),
    "error_suppressed": (
        "fingerprint",
        "suppressed_count",
        "window_seconds",
        "first_seen",
        "last_seen",
    ),
    "probe_event": ("label", "data", "activation_id", "probe_label_pattern"),
}
_EVENT_PAYLOAD_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "backend_exception": frozenset((*_EVENT_PAYLOAD_FIELDS["backend_exception"], "probe_data")),
    "request_event": frozenset(
        (
            *_EVENT_PAYLOAD_FIELDS["request_event"],
            "body",
            "route_template",
            "response_headers",
            "response_body",
            "device",
        )
    ),
    "log_event": frozenset((*_EVENT_PAYLOAD_FIELDS["log_event"], "device")),
    "frontend_breadcrumb": frozenset((*_EVENT_PAYLOAD_FIELDS["frontend_breadcrumb"], "route", "device")),
    "frontend_exception": frozenset(
        (
            *_EVENT_PAYLOAD_FIELDS["frontend_exception"],
            "route",
            "browser",
            "breadcrumbs",
            "device",
            "browser_event",
            "rejection_reason",
            "dom_context",
            "probe_data",
        )
    ),
    "deploy_metadata": frozenset(_EVENT_PAYLOAD_FIELDS["deploy_metadata"]),
    "error_suppressed": frozenset((*_EVENT_PAYLOAD_FIELDS["error_suppressed"], "device")),
    "probe_event": frozenset((*_EVENT_PAYLOAD_FIELDS["probe_event"], "device")),
}
_ROOT_FIELDS = frozenset(
    (
        "schema_version",
        "event_id",
        "event_type",
        "project_token",
        "project_id",
        "sdk_name",
        "sdk_version",
        "service",
        "occurred_at",
        "correlation",
        "context",
        "payload",
    )
)


def apply_before_send(
    event: dict[str, object],
    hook: BeforeSendHook | None,
    emit_diagnostic: Callable[[str, str], None],
) -> dict[str, object] | None:
    if hook is None:
        return event

    try:
        result = hook(copy.deepcopy(event))
    except Exception:
        emit_diagnostic("before_send_failed", "Python before_send hook failed")
        return event

    if result is None:
        return None
    if not _is_valid_event(result):
        emit_diagnostic("before_send_invalid_event", "Python before_send returned an invalid event")
        return event
    return result


def _is_valid_event(event: object) -> bool:
    if not isinstance(event, dict):
        return False
    if not set(event).issubset(_ROOT_FIELDS):
        return False
    event_type = event.get("event_type")
    if not isinstance(event_type, str) or event_type not in _EVENT_PAYLOAD_FIELDS:
        return False
    if not all(
        isinstance(event.get(field), str) and event[field]
        for field in ("schema_version", "sdk_name", "sdk_version", "occurred_at")
    ):
        return False
    if not _timestamp(event["occurred_at"]):
        return False
    try:
        uuid.UUID(str(event.get("event_id")))
    except (ValueError, TypeError, AttributeError):
        return False
    service = event.get("service")
    if (
        not isinstance(service, dict)
        or not isinstance(service.get("name"), str)
        or not service["name"]
        or not isinstance(service.get("environment"), str)
        or not service["environment"]
    ):
        return False
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    if not set(payload).issubset(_EVENT_PAYLOAD_ALLOWED_FIELDS[event_type]):
        return False
    if not all(field in payload for field in _EVENT_PAYLOAD_FIELDS[event_type]):
        return False
    return _has_valid_payload_shape(event_type, payload)


def _has_valid_payload_shape(event_type: str, payload: Mapping[object, object]) -> bool:
    if event_type == "backend_exception":
        return (
            _has_nonempty_strings(payload, "name", "message", "stack")
            and isinstance(payload["handled"], bool)
            and all(isinstance(payload[field], dict) for field in ("request", "response", "runtime"))
            and _optional_dict(payload, "probe_data")
        )
    if event_type == "request_event":
        return (
            _has_nonempty_strings(payload, "method", "path")
            and all(isinstance(payload[field], dict) for field in ("query", "headers"))
            and _nonnegative_number(payload["response_status"])
            and _nonnegative_number(payload["duration_ms"])
            and _optional_dict(payload, "response_headers")
        )
    if event_type == "log_event":
        return _has_nonempty_strings(payload, "level", "message") and isinstance(payload["attributes"], dict)
    if event_type == "frontend_breadcrumb":
        return _has_nonempty_strings(payload, "breadcrumb_type") and isinstance(payload["data"], dict)
    if event_type == "frontend_exception":
        return (
            _has_nonempty_strings(payload, "name", "message", "stack")
            and (payload.get("breadcrumbs") is None or isinstance(payload["breadcrumbs"], list))
            and _optional_dict(payload, "probe_data")
        )
    if event_type == "deploy_metadata":
        return _has_nonempty_strings(payload, "commit_sha", "version", "branch", "environment") and _timestamp(
            payload["deployed_at"]
        )
    if event_type == "error_suppressed":
        return (
            _has_nonempty_strings(payload, "fingerprint")
            and _nonnegative_integer(payload["suppressed_count"])
            and _positive_integer(payload["window_seconds"])
            and _timestamp(payload["first_seen"])
            and _timestamp(payload["last_seen"])
        )
    if event_type == "probe_event":
        activation_id = payload["activation_id"]
        return (
            _has_nonempty_strings(payload, "label", "probe_label_pattern")
            and isinstance(payload["data"], dict)
            and (activation_id is None or _uuid(activation_id))
        )
    return False


def _has_nonempty_strings(payload: Mapping[object, object], *fields: str) -> bool:
    return all(isinstance(payload.get(field), str) and bool(str(payload[field]).strip()) for field in fields)


def _optional_dict(payload: Mapping[object, object], field: str) -> bool:
    return field not in payload or isinstance(payload[field], dict)


def _nonnegative_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _uuid(value: object) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return isinstance(value, str)


def _timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
