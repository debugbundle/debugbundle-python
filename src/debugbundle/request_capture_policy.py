"""Metadata-only request admission before request payload construction."""

from __future__ import annotations

from collections.abc import Mapping

from .config import CapturePolicy
from .event_support import is_immediate_request_incident_status


def should_capture_request_event(
    policy: CapturePolicy,
    request: Mapping[str, object] | None,
    response: Mapping[str, object] | None,
) -> bool:
    status_code = None
    if response is not None:
        candidate = response.get("status_code") or response.get("response_status")
        if isinstance(candidate, int):
            status_code = candidate
    request_path = None
    http_method = None
    if request is not None:
        path_candidate = request.get("path") or request.get("url")
        method_candidate = request.get("method")
        request_path = path_candidate if isinstance(path_candidate, str) else None
        http_method = method_candidate if isinstance(method_candidate, str) else None
    if is_immediate_request_incident_status(
        status_code,
        policy.preset,
        policy.immediate_client_error_statuses,
        request_path,
        http_method,
        policy.immediate_client_error_path_rules,
    ):
        return True
    if policy.capture_request_events == "off":
        return False
    if policy.capture_request_events == "all":
        return True
    if response is None or status_code is None:
        return policy.capture_request_events == "filtered"
    if policy.capture_request_events == "failures_only":
        return status_code >= 500
    return False
