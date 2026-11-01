"""Closed evidence DTO boundary; never inspect arbitrary application objects."""

from __future__ import annotations

import math
import re
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from .evidence_json import canonical_size as _size

FEATURES = ("exception_evidence_v1", "resource_observations_v1", "recovery_v1", "evidence_provenance_v1")
_UUID = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.I)
_V4 = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$", re.I)
_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$")


def _mapping(value: Any, maximum: int = 256) -> TypeGuard[dict[str, Any]]:
    return type(value) is dict and len(value) <= maximum and all(type(key) is str for key in value)


def _closed(value: Any, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> bool:
    return (
        _mapping(value, len(required) + len(optional))
        and all(key in value for key in required)
        and all(key in required or key in optional for key in value)
    )


def _one_of(value: Any, options: tuple[str | None, ...]) -> bool:
    return (value is None and None in options) or (type(value) is str and value in options)


def _number(value: Any) -> bool:
    return (
        (type(value) is int or type(value) is float) and 0 <= value <= 1.7976931348623157e308 and math.isfinite(value)
    )


def _integer(value: Any, minimum: int, maximum: int | None = None) -> bool:
    return _number(value) and value >= minimum and (maximum is None or value <= maximum) and value == math.floor(value)


def _text(value: Any, maximum: int) -> bool:
    if type(value) is not str or len(value) > maximum:
        return False
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeError:
        return False


def _uuid(value: Any, fresh: bool = False) -> bool:
    return (
        type(value) is str
        and len(value) == 36
        and (bool(_V4.fullmatch(value)) if fresh else bool(_UUID.fullmatch(value)))
    )


def _timestamp(value: Any) -> bool:
    if not _text(value, 32768) or not _TIME.fullmatch(value):
        return False
    # Native fromisoformat acceptance changed in Python 3.14 (24:00 rolls to
    # the following day). Match the accepting UTC calendar on every floor.
    year, month, day = int(value[:4]), int(value[5:7]), int(value[8:10])
    hour, minute, second = int(value[11:13]), int(value[14:16]), int(value[17:19])
    if not 1 <= month <= 12 or hour > 23 or minute > 59 or second > 59:
        return False
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return 1 <= day <= days[month - 1]


def parse_capabilities(value: Any) -> dict[str, Any]:
    empty: dict[str, Any] = {"version": 1, "features": []}
    if not _closed(value, ("version", "features")) or not _integer(value["version"], 1, 1):
        return empty
    features = value["features"]
    if type(features) is not list or len(features) > len(FEATURES):
        return empty
    if any(type(feature) is not str or feature not in FEATURES for feature in features) or len(set(features)) != len(
        features
    ):
        return empty
    return {"version": 1, "features": [feature for feature in FEATURES if feature in features]}


def _scope(value: Any) -> bool:
    if not _mapping(value, 2):
        return False
    field = (
        "page_id"
        if _one_of(value.get("kind"), ("page",))
        else "operation_id"
        if _one_of(value.get("kind"), ("operation",))
        else None
    )
    return field is not None and _closed(value, ("kind", field)) and _uuid(value[field], True)


def reference(value: Any) -> dict[str, Any] | None:
    if (
        _closed(value, ("version", "failure_ref", "scope"))
        and _integer(value["version"], 1, 1)
        and _uuid(value["failure_ref"], True)
        and _scope(value["scope"])
    ):
        return value
    return None


def capture_reference(value: Any, default_scope: Any = None) -> dict[str, Any] | None:
    if not _mapping(value):
        return None
    return reference(
        {"version": 1, "failure_ref": value.get("failure_ref"), "scope": value.get("evidence_scope", default_scope)}
    )


def normalise_recovery(value: Any, default_scope: Any = None) -> dict[str, Any] | None:
    selected = capture_reference(value, default_scope)
    if selected is None:
        return None
    payload = dict(selected, kind="recovery", attempt_id=value.get("attempt_id"), state=value.get("state"))
    for key in ("operation", "status_code", "duration_ms"):
        if key in value:
            payload[key] = value[key]
    return payload if diagnostic(payload) else None


def _url(value: Any) -> bool:
    if (
        not _text(value, 2048)
        or not value
        or value.startswith("//")
        or re.search(r"[\x00-\x20\x7f\\?#]", value)
        or (not value.startswith("/") and not re.match(r"^https?://", value, re.I))
    ):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        return (
            (value.startswith("/") or parsed.hostname is not None)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def resources(value: Any) -> bool:
    if not _closed(value, ("version", "items")) or not _integer(value["version"], 1, 1):
        return False
    items = value["items"]
    if type(items) is not list or not 1 <= len(items) <= 5:
        return False
    for item in items:
        if (
            not _mapping(item, 8)
            or not _timestamp(item.get("observed_at"))
            or (item.get("source_url") is not None and not _url(item["source_url"]))
        ):
            return False
        if _one_of(item.get("kind"), ("timing",)):
            if (
                not _closed(
                    item,
                    (
                        "kind",
                        "observed_at",
                        "source_url",
                        "availability",
                        "response_status",
                        "duration_ms",
                        "transfer_size_bytes",
                    ),
                )
                or not _one_of(
                    item["availability"], ("observed", "withheld", "unsupported", "no_match", "ambiguous", "unknown")
                )
                or (item["response_status"] is not None and not _integer(item["response_status"], 100, 599))
                or (item["duration_ms"] is not None and not _number(item["duration_ms"]))
                or (item["transfer_size_bytes"] is not None and not _integer(item["transfer_size_bytes"], 0))
            ):
                return False
        elif _one_of(item.get("kind"), ("csp",)):
            if (
                not _closed(
                    item,
                    ("kind", "observed_at", "source_url", "effective_directive", "disposition", "document_status_code"),
                )
                or type(item["effective_directive"]) is not str
                or not re.fullmatch(r"[a-z-]{1,64}", item["effective_directive"])
                or not _one_of(item["disposition"], ("report", "enforce"))
                or (item["document_status_code"] is not None and not _integer(item["document_status_code"], 100, 599))
            ):
                return False
        else:
            return False
    return _size(value) <= 32768


def diagnostic(value: Any) -> bool:
    if (
        not _mapping(value, 10)
        or reference({key: value.get(key) for key in ("version", "failure_ref", "scope")}) is None
    ):
        return False
    if _one_of(value.get("kind"), ("recovery",)):
        return (
            _closed(
                value,
                ("version", "kind", "failure_ref", "scope", "attempt_id", "state"),
                ("operation", "status_code", "duration_ms"),
            )
            and _uuid(value["attempt_id"], True)
            and _one_of(
                value["state"],
                ("attempt_started", "attempt_completed", "attempt_failed", "recovery_observed", "failure_persisted"),
            )
            and ("operation" not in value or _text(value["operation"], 64))
            and ("status_code" not in value or _integer(value["status_code"], 100, 599))
            and ("duration_ms" not in value or _number(value["duration_ms"]))
        )
    return (
        _one_of(value.get("kind"), ("resource_observation",))
        and _closed(value, ("version", "kind", "failure_ref", "scope", "source_event_id", "observations"))
        and _uuid(value["source_event_id"])
        and resources(value["observations"])
    )


def execution(value: Any) -> bool:
    return (
        _closed(value, ("version", "kind", "name", "elapsed_ms", "limit_ms"))
        and _integer(value["version"], 1, 1)
        and _one_of(value["kind"], ("request", "command", "job", "unknown"))
        and (value["name"] is None or _text(value["name"], 128))
        and (value["elapsed_ms"] is None or _number(value["elapsed_ms"]))
        and (value["limit_ms"] is None or _number(value["limit_ms"]))
    )


def graph(value: Any) -> bool:
    if (
        not _closed(value, ("version", "runtime", "root_id", "nodes", "truncated", "unavailable_reason"))
        or not _integer(value["version"], 1, 1)
        or not _integer(value["root_id"], 0, 7)
        or not _one_of(
            value["runtime"],
            (
                "node",
                "browser",
                "python",
                "php",
                "jvm",
                "go",
                "ruby",
                "dotnet",
                "android",
                "swift",
                "react_native",
                "unknown",
            ),
        )
        or type(value["truncated"]) is not bool
        or not _one_of(value["unavailable_reason"], (None, "unknown", "unsafe", "unsupported", "budget_exceeded"))
        or type(value["nodes"]) is not list
        or not 1 <= len(value["nodes"]) <= 8
    ):
        return False
    adjacency: dict[int, list[int]] = {}
    for node in value["nodes"]:
        if (
            not _closed(
                node, ("id", "name", "message", "stack", "trace_origin", "parsing_status", "frames", "relationships")
            )
            or not _integer(node["id"], 0, 7)
            or node["id"] in adjacency
            or not _text(node["name"], 256)
            or not _text(node["message"], 2048)
            or (node["stack"] is not None and not _text(node["stack"], 16384))
            or not _one_of(
                node["trace_origin"], ("throw", "native_fatal", "capture", "runtime_reported", "unavailable", "unknown")
            )
            or not _one_of(node["parsing_status"], ("parsed", "partial", "unparsed", "unavailable", "truncated"))
            or type(node["frames"]) is not list
            or len(node["frames"]) > 32
            or type(node["relationships"]) is not list
            or len(node["relationships"]) > 5
        ):
            return False
        for frame in node["frames"]:
            if (
                not _closed(frame, ("function", "file", "line", "column", "classification"))
                or (frame["function"] is not None and not _text(frame["function"], 256))
                or (frame["file"] is not None and not _text(frame["file"], 2048))
                or (frame["line"] is not None and not _integer(frame["line"], 1))
                or (frame["column"] is not None and not _integer(frame["column"], 0))
                or not _one_of(frame["classification"], ("application", "framework", "sdk", "unknown"))
            ):
                return False
        edges: list[int] = []
        seen: set[tuple[str, int]] = set()
        causal = 0
        for edge in node["relationships"]:
            if (
                not _closed(edge, ("kind", "target_id"))
                or not _one_of(edge["kind"], ("cause", "context", "aggregate", "suppressed"))
                or not _integer(edge["target_id"], 0, 7)
                or (edge["kind"], edge["target_id"]) in seen
            ):
                return False
            seen.add((edge["kind"], edge["target_id"]))
            causal += _one_of(edge["kind"], ("cause", "context"))
            edges.append(edge["target_id"])
        if causal > 1:
            return False
        adjacency[node["id"]] = edges
    visited: set[int] = set()

    def visit(node_id: int, depth: int, ancestors: set[int]) -> bool:
        if node_id not in adjacency or node_id in ancestors or depth > 4:
            return False
        visited.add(node_id)
        return all(visit(child, depth + 1, ancestors | {node_id}) for child in adjacency[node_id])

    return visit(value["root_id"], 0, set()) and len(visited) == len(adjacency) and _size(value) <= 32768


def validate_evidence(event: dict[str, Any]) -> bool:
    if not _mapping(event):
        return False
    payload = event.get("payload")
    if not _mapping(payload):
        return False
    if _one_of(event.get("event_type"), ("diagnostic_context",)):
        correlation = event.get("correlation")
        session = correlation.get("session_id") if _mapping(correlation) else None
        return diagnostic(payload) and (
            not _one_of(payload["scope"]["kind"], ("page",)) or (type(session) is str and bool(session.strip()))
        )
    additions: dict[str, Any] = {}
    for field, validator in (
        ("exception_evidence", graph),
        ("execution", execution),
        ("resource_observations", resources),
    ):
        if field in payload:
            allowed = (
                field != "resource_observations" and _one_of(event.get("event_type"), ("backend_exception",))
            ) or (field != "execution" and _one_of(event.get("event_type"), ("frontend_exception",)))
            if not allowed:
                return False
            if not validator(payload[field]):
                return False
            additions[field] = payload[field]
    if not additions:
        return True
    context = event.get("context")
    selected = reference(context.get("evidence_reference")) if _mapping(context) else None
    if selected is not None:
        additions["evidence_reference"] = selected
    return _size(additions) <= 32768


def without_optional(event: dict[str, Any]) -> dict[str, Any] | None:
    if not _mapping(event):
        return None
    payload = event.get("payload")
    if not _one_of(event.get("event_type"), ("backend_exception", "frontend_exception")) or not _mapping(payload):
        return None
    fields = ("exception_evidence", "execution", "resource_observations")
    if not any(field in payload for field in fields):
        return None
    return dict(event, payload={key: value for key, value in payload.items() if key not in fields})


def strip_unsupported(event: dict[str, Any], features: list[str]) -> dict[str, Any] | None:
    if not _mapping(event):
        return None
    payload = event.get("payload")
    if not _mapping(payload):
        return None
    if _one_of(event.get("event_type"), ("diagnostic_context",)):
        feature = "recovery_v1" if _one_of(payload.get("kind"), ("recovery",)) else "resource_observations_v1"
        return event if feature in features else None
    payload = dict(payload)
    if "exception_evidence_v1" not in features:
        payload.pop("exception_evidence", None)
        payload.pop("execution", None)
    if "resource_observations_v1" not in features:
        payload.pop("resource_observations", None)
    candidate = dict(event, payload=payload)
    context = event.get("context")
    if (
        "resource_observations_v1" not in features
        and "recovery_v1" not in features
        and _mapping(context)
        and reference(context.get("evidence_reference")) is not None
    ):
        candidate["context"] = {
            key: value for key, value in context.items() if not _one_of(key, ("evidence_reference",))
        }
    return candidate
