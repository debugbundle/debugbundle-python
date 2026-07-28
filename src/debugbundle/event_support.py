from __future__ import annotations

import os
import platform
import socket
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from importlib import metadata
from typing import Any, cast

from .redaction import redact_value

try:
    import resource
except ImportError:  # pragma: no cover - resource is unavailable on some platforms.
    resource = None  # type: ignore[assignment]

DEFAULT_LOG_LEVEL = "warning"
PROCESS_START_MONOTONIC = time.monotonic()
LEVEL_RANKS = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}
BALANCED_IMMEDIATE_REQUEST_STATUSES = {408, 423, 424, 425, 429}
INVESTIGATIVE_IMMEDIATE_REQUEST_STATUSES = BALANCED_IMMEDIATE_REQUEST_STATUSES | {409}


def normalize_level(level: str) -> str:
    normalized = level.lower().strip()
    return normalized if normalized in LEVEL_RANKS else DEFAULT_LOG_LEVEL


def level_enabled(candidate: str, threshold: str) -> bool:
    return LEVEL_RANKS[candidate] >= LEVEL_RANKS[threshold]


def redact_mapping(value: object, redact_fields: set[str]) -> Any:
    if isinstance(value, Mapping):
        return redact_value(value, redact_fields)
    return value


def runtime_process_facts() -> dict[str, object]:
    return {
        "version": platform.python_version(),
        "platform": sys.platform,
        "arch": platform.machine() or None,
        "pid": os.getpid(),
        "cwd": _safe_cwd(),
        "uptime_sec": round(max(0.0, time.monotonic() - PROCESS_START_MONOTONIC), 3),
        "hostname": _safe_hostname(),
        "thread_id": threading.get_ident(),
        "memory": _memory_facts(),
    }


def _safe_cwd() -> str | None:
    try:
        return os.getcwd()
    except OSError:
        return None


def _safe_hostname() -> str | None:
    try:
        return socket.gethostname()
    except OSError:
        return None


def _memory_facts() -> dict[str, object]:
    memory: dict[str, object] = {
        "rss": None,
        "heap_total": None,
        "heap_used": None,
        "external": None,
        "peak": None,
    }
    if resource is None:
        return memory

    usage = resource.getrusage(resource.RUSAGE_SELF)
    # ru_maxrss is KiB on Linux and bytes on macOS/BSD.
    memory["peak"] = usage.ru_maxrss if sys.platform == "darwin" else usage.ru_maxrss * 1024
    return memory


def backend_exception_request_payload(candidate: object | None) -> dict[str, object]:
    mapping = dict_from_object(candidate)
    payload: dict[str, object] = {
        "method": str(mapping.get("method") or "UNKNOWN"),
        "path": str(mapping.get("path") or "/"),
        "query": dict_from_object(mapping.get("query")),
        "headers": dict_from_object(mapping.get("headers")),
    }
    if "body" in mapping:
        payload["body"] = mapping.get("body")
    return payload


def backend_exception_response_payload(candidate: object | None) -> dict[str, object]:
    mapping = dict_from_object(candidate)
    payload: dict[str, object] = {
        "status_code": coerce_int(mapping.get("status_code") or mapping.get("response_status"), 0),
    }
    if "headers" in mapping:
        payload["headers"] = dict_from_object(mapping.get("headers"))
    if "body" in mapping:
        payload["body"] = mapping.get("body")
    return payload


def request_event_payload(
    request: Mapping[str, object],
    response: Mapping[str, object],
    context: Mapping[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "method": str(request.get("method") or "UNKNOWN"),
        "path": str(request.get("path") or "/"),
        "query": dict_from_object(request.get("query")),
        "headers": dict_from_object(request.get("headers")),
        "response_status": coerce_int(response.get("response_status") or response.get("status_code"), 0),
        "duration_ms": coerce_int(response.get("duration_ms"), 0),
    }
    if "body" in request:
        payload["body"] = request.get("body")
    route_template = context.get("route_template") or response.get("route_template") or request.get("route_template")
    if route_template is not None:
        payload["route_template"] = str(route_template)
    response_headers = response.get("response_headers") or response.get("headers")
    if response_headers:
        payload["response_headers"] = dict_from_object(response_headers)
    if "response_body" in response:
        payload["response_body"] = response.get("response_body")
    elif "body" in response and response.get("body") is not None:
        payload["response_body"] = response.get("body")
    return payload


def coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def dict_from_object(value: object | None) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): cast(object, nested_value) for key, nested_value in value.items()}
    return {}


def correlation_payload(context: Mapping[str, object]) -> dict[str, str | None]:
    return {
        "request_id": _coerce_optional_string(context.get("request_id")),
        "trace_id": _coerce_optional_string(context.get("trace_id")),
        "session_id": _coerce_optional_string(context.get("session_id")),
        "user_id_hash": _coerce_optional_string(context.get("user_id_hash")),
    }


def event_context(context: Mapping[str, object]) -> dict[str, object]:
    return {
        str(key): value
        for key, value in context.items()
        if key not in {"request", "response", "correlation", "request_id", "trace_id", "session_id", "user_id_hash"}
    }


def _coerce_optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def iso_now(time_provider: Callable[[], float]) -> str:
    return datetime.fromtimestamp(time_provider(), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def is_immediate_request_incident_status(
    status_code: int | None,
    preset: str,
    immediate_client_error_statuses: tuple[int, ...],
    request_path: str | None = None,
    http_method: str | None = None,
    immediate_client_error_path_rules: tuple[object, ...] = (),
) -> bool:
    if status_code is None:
        return False
    if status_code >= 500:
        return True
    if status_code in immediate_client_error_statuses:
        return True
    if _matches_immediate_client_error_path_rule(
        status_code,
        request_path,
        http_method,
        immediate_client_error_path_rules,
    ):
        return True
    if preset == "investigative":
        return status_code in INVESTIGATIVE_IMMEDIATE_REQUEST_STATUSES
    if preset == "balanced":
        return status_code in BALANCED_IMMEDIATE_REQUEST_STATUSES
    return False


def _matches_immediate_client_error_path_rule(
    status_code: int,
    request_path: str | None,
    http_method: str | None,
    rules: tuple[object, ...],
) -> bool:
    if status_code < 400 or status_code > 499 or request_path is None:
        return False
    normalized_path = _normalize_request_path(request_path)
    normalized_method = http_method.upper() if isinstance(http_method, str) else None
    for rule in rules:
        rule_status = getattr(rule, "status_code", None)
        path_pattern = getattr(rule, "path_pattern", None)
        methods = getattr(rule, "methods", ())
        if rule_status != status_code or not isinstance(path_pattern, str):
            continue
        if methods and (normalized_method is None or normalized_method not in methods):
            continue
        if path_pattern.endswith("*"):
            if normalized_path.startswith(path_pattern[:-1]):
                return True
        elif normalized_path == path_pattern:
            return True
    return False


def _normalize_request_path(value: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(value)
    if parsed.path:
        return parsed.path
    return value.split("?", 1)[0].split("#", 1)[0] if value.startswith("/") else "/"


def time_now() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


def sdk_version() -> str:
    try:
        return metadata.version("debugbundle-python")
    except metadata.PackageNotFoundError:
        return "1.3.0"


def sdk_config_endpoint(events_endpoint: str) -> str:
    if events_endpoint.endswith("/v1/events"):
        return f"{events_endpoint[: -len('/v1/events')]}/v1/sdk/config"
    return f"{events_endpoint.rstrip('/')}/sdk/config"


def serialize_error(error: Exception) -> dict[str, object]:
    return {
        "name": type(error).__name__,
        "message": str(error),
        "stack": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
    }
