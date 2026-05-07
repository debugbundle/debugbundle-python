from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_BODY_BYTES = 262_144
DEFAULT_RATE_LIMIT_PER_MINUTE = 60
BROWSER_SDK_NAME = "@debugbundle/sdk-browser"

ACCEPTED_EVENT_TYPES = frozenset(
    {
        "frontend_exception",
        "error_suppressed",
        "frontend_breadcrumb",
        "probe_event",
    }
)


@dataclass(frozen=True)
class BrowserRelayResponse:
    status: int
    body: dict[str, Any] | None = None


@dataclass(frozen=True)
class BrowserRelayAcceptedBatch:
    events: list[dict[str, Any]]
    headers: dict[str, str]
    ip_address: str | None
    received_at: str


@dataclass
class BrowserRelayHandler:
    allowed_origins: list[str] = field(default_factory=list)
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE
    on_accept: Callable[[BrowserRelayAcceptedBatch], None] | None = None

    def __post_init__(self) -> None:
        self.allowed_origins = [o for o in self.allowed_origins if o]
        self.max_body_bytes = max(1, self.max_body_bytes)
        self.rate_limit_per_minute = max(1, self.rate_limit_per_minute)
        self._rate_limit_state: dict[str, list[int]] = {}

    def handle(self, request: dict[str, Any]) -> BrowserRelayResponse:
        method = str(request.get("method", "POST")).upper()
        if method != "POST":
            return BrowserRelayResponse(405)

        headers = _normalize_headers(request.get("headers") or {})
        if not self._is_origin_allowed(headers):
            return BrowserRelayResponse(403)

        if not _is_supported_content_type(headers.get("content-type")):
            return BrowserRelayResponse(
                400,
                {"accepted": 0, "rejected": 0, "errors": ["Relay requests must use Content-Type: application/json."]},
            )

        body: str = request.get("body", "")
        if len(body.encode("utf-8") if isinstance(body, str) else body) > self.max_body_bytes:
            return BrowserRelayResponse(413)

        ip_address: str | None = request.get("ipAddress") or request.get("ip_address")
        if self._is_rate_limited(ip_address):
            return BrowserRelayResponse(429)

        try:
            decoded = json.loads(body)
        except (json.JSONDecodeError, TypeError, ValueError):
            return BrowserRelayResponse(
                400,
                {"accepted": 0, "rejected": 0, "errors": ["Relay request body must be valid JSON."]},
            )

        if not isinstance(decoded, dict):
            return BrowserRelayResponse(
                400,
                {"accepted": 0, "rejected": 0, "errors": ["Relay request body must be valid JSON."]},
            )

        batch = decoded.get("batch")
        if not isinstance(batch, list):
            return BrowserRelayResponse(
                400,
                {"accepted": 0, "rejected": 0, "errors": ["Relay request body must include a batch array."]},
            )

        accepted_events: list[dict[str, Any]] = []
        errors: list[str] = []

        for index, candidate in enumerate(batch):
            if not isinstance(candidate, dict):
                errors.append(f"batch[{index}]: Relay events must be objects.")
                continue

            event_type = candidate.get("event_type")
            if not isinstance(event_type, str) or event_type not in ACCEPTED_EVENT_TYPES:
                type_label = event_type if isinstance(event_type, str) else "unknown"
                errors.append(f"batch[{index}]: Unsupported browser relay event type {type_label}.")
                continue

            sanitized = _sanitize_event(candidate)
            if sanitized is None:
                errors.append(f"batch[{index}]: Invalid browser relay event payload.")
                continue

            accepted_events.append(sanitized)

        if accepted_events and self.on_accept is not None:
            self.on_accept(
                BrowserRelayAcceptedBatch(
                    events=accepted_events,
                    headers=_strip_sensitive_headers(headers),
                    ip_address=ip_address,
                    received_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
            )

        if errors:
            return BrowserRelayResponse(
                400,
                {"accepted": len(accepted_events), "rejected": len(errors), "errors": errors},
            )

        return BrowserRelayResponse(
            202,
            {"accepted": len(accepted_events), "rejected": 0, "errors": []},
        )

    def _is_origin_allowed(self, headers: dict[str, str]) -> bool:
        origin = _source_origin(headers)
        if origin is None:
            return False

        if self.allowed_origins:
            normalized_origin = _normalize_origin(origin)
            return any(_normalize_origin(candidate) == normalized_origin for candidate in self.allowed_origins)

        host = headers.get("host")
        if not host:
            return False

        try:
            from urllib.parse import urlparse

            origin_host = urlparse(origin).hostname
            return isinstance(origin_host, str) and origin_host.lower() == host.lower().split(":")[0]
        except Exception:
            return False

    def _is_rate_limited(self, ip_address: str | None) -> bool:
        key = ip_address or "unknown"
        now = int(time.time())
        window_start = now - 60
        timestamps = [ts for ts in self._rate_limit_state.get(key, []) if ts > window_start]

        if len(timestamps) >= self.rate_limit_per_minute:
            self._rate_limit_state[key] = timestamps
            return True

        timestamps.append(now)
        self._rate_limit_state[key] = timestamps
        return False


def _normalize_headers(headers: dict[str, Any]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _is_supported_content_type(content_type: str | None) -> bool:
    return isinstance(content_type, str) and "application/json" in content_type.lower()


def _source_origin(headers: dict[str, str]) -> str | None:
    origin = (headers.get("origin") or "").strip()
    if origin:
        return origin

    referer = (headers.get("referer") or "").strip()
    if not referer:
        return None

    try:
        from urllib.parse import urlparse

        parsed = urlparse(referer)
        if parsed.scheme and parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}"
    except Exception:
        pass

    return None


def _normalize_origin(origin: str) -> str:
    return origin.strip().lower().rstrip("/")


def _strip_sensitive_headers(headers: dict[str, str]) -> dict[str, str]:
    sanitized = dict(headers)
    for key in ("authorization", "cookie", "x-api-key"):
        sanitized.pop(key, None)
    return sanitized


def _sanitize_event(event: dict[str, Any]) -> dict[str, Any] | None:
    schema_version = event.get("schema_version")
    event_id = event.get("event_id")
    event_type = event.get("event_type")
    occurred_at = event.get("occurred_at")
    sdk_version = event.get("sdk_version")
    service = event.get("service")
    payload = event.get("payload")

    if (
        not isinstance(schema_version, str)
        or not schema_version
        or not isinstance(event_id, str)
        or not event_id
        or not isinstance(event_type, str)
        or not isinstance(occurred_at, str)
        or not occurred_at
        or not isinstance(sdk_version, str)
        or not sdk_version
        or not isinstance(service, dict)
        or not isinstance(payload, dict)
    ):
        return None

    service_name = service.get("name")
    environment = service.get("environment")
    if not isinstance(service_name, str) or not service_name or not isinstance(environment, str) or not environment:
        return None

    sanitized: dict[str, Any] = {
        "schema_version": schema_version,
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "sdk_name": BROWSER_SDK_NAME,
        "sdk_version": sdk_version,
        "service": {
            "name": service_name,
            "environment": environment,
        },
        "payload": payload,
    }

    correlation = event.get("correlation")
    if isinstance(correlation, dict):
        trace_id = correlation.get("trace_id")
        if isinstance(trace_id, str) or trace_id is None:
            sanitized["correlation"] = {"trace_id": trace_id}

    return sanitized
