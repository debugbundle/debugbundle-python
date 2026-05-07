from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from ..core import DebugBundleSdk

TRACE_ID_HEADER = "x-debugbundle-trace-id"
REQUEST_ID_HEADERS = ("x-request-id", "x-correlation-id")


def now_seconds() -> float:
    return time.time()


def duration_ms(started_at: float, finished_at: float | None = None) -> int:
    end = now_seconds() if finished_at is None else finished_at
    return max(0, int((end - started_at) * 1000))


def normalize_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        normalized[str(key).lower()] = str(value)
    return normalized


def correlation_context(headers: Mapping[str, Any]) -> dict[str, str]:
    normalized = normalize_headers(headers)
    context: dict[str, str] = {}
    trace_id = normalized.get(TRACE_ID_HEADER)
    if trace_id:
        context["trace_id"] = trace_id
    for header_name in REQUEST_ID_HEADERS:
        request_id = normalized.get(header_name)
        if request_id:
            context["request_id"] = request_id
            break
    return context


def normalize_query_items(items: Mapping[str, Any] | None = None) -> dict[str, str]:
    if items is None:
        return {}
    return {str(key): str(value) for key, value in items.items()}


def request_payload(
    *,
    method: str,
    path: str,
    headers: Mapping[str, Any],
    query: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    return {
        "method": method,
        "path": path,
        "headers": normalize_headers(headers),
        "query": normalize_query_items(query),
    }


def response_payload(*, status_code: int, started_at: float) -> dict[str, object]:
    return {
        "status_code": status_code,
        "duration_ms": duration_ms(started_at),
    }


def resolve_sdk(sdk: DebugBundleSdk | None) -> DebugBundleSdk:
    if sdk is not None:
        return sdk

    from .. import _sdk

    return _sdk
