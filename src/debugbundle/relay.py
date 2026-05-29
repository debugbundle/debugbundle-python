from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .relay_delivery import (
    AtomicRelayFileTransport,
    RelayForwardTransport,
    mark_spool_file_delivered,
    resolve_default_local_events_dir,
    resolve_default_relay_spool_dir,
)

DEFAULT_MAX_BODY_BYTES = 262_144
DEFAULT_RATE_LIMIT_PER_MINUTE = 60
BROWSER_SDK_NAME = "@debugbundle/sdk-browser"

ACCEPTED_EVENT_TYPES = frozenset(
    {
        "frontend_exception",
        "error_suppressed",
        "frontend_breadcrumb",
        "request_event",
        "probe_event",
    }
)


@dataclass(frozen=True)
class BrowserRelayResponse:
    status: int
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


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
    project_mode: str | None = None
    project_token: str | None = None
    endpoint: str | None = None
    local_events_dir: str | None = None
    spool_dir: str | None = None
    durable_write: bool = True
    service: str | None = None
    environment: str | None = None
    forward_transport: Callable[[Mapping[str, object]], object] | None = None

    def __post_init__(self) -> None:
        self.allowed_origins = [o for o in self.allowed_origins if o]
        self.max_body_bytes = max(1, self.max_body_bytes)
        self.rate_limit_per_minute = max(1, self.rate_limit_per_minute)
        normalized_project_mode = (self.project_mode or "").strip().lower()
        self.project_mode = normalized_project_mode or None
        if self.project_mode not in {None, "connected", "local-only"}:
            self.project_mode = None
        self._rate_limit_state: dict[str, list[int]] = {}
        self._local_transports: dict[str, AtomicRelayFileTransport] = {}
        self._spool_transports: dict[str, AtomicRelayFileTransport] = {}
        self._forwarder = (
            RelayForwardTransport(self.endpoint, self.forward_transport)
            if self.project_mode == "connected" and self.endpoint is not None
            else None
        )

    def handle(self, request: dict[str, Any]) -> BrowserRelayResponse:
        method = str(request.get("method", "POST")).upper()
        headers = _normalize_headers(request.get("headers") or {})
        source_origin = _source_origin(headers)
        if not self._is_origin_allowed(headers):
            return BrowserRelayResponse(403)

        response_headers = _cors_headers(source_origin) if source_origin else {}

        def with_headers(response: BrowserRelayResponse) -> BrowserRelayResponse:
            return BrowserRelayResponse(response.status, response.body, {**response_headers, **response.headers})

        if method == "OPTIONS":
            return with_headers(BrowserRelayResponse(204))

        if method != "POST":
            return with_headers(BrowserRelayResponse(405))

        if not _is_supported_content_type(headers.get("content-type")):
            return with_headers(BrowserRelayResponse(
                400,
                {"accepted": 0, "rejected": 0, "errors": ["Relay requests must use Content-Type: application/json."]},
            ))

        body: str = request.get("body", "")
        if len(body.encode("utf-8") if isinstance(body, str) else body) > self.max_body_bytes:
            return with_headers(BrowserRelayResponse(413))

        ip_address: str | None = request.get("ipAddress") or request.get("ip_address")
        if self._is_rate_limited(ip_address):
            return with_headers(BrowserRelayResponse(429))

        try:
            decoded = json.loads(body)
        except (json.JSONDecodeError, TypeError, ValueError):
            return with_headers(BrowserRelayResponse(
                400,
                {"accepted": 0, "rejected": 0, "errors": ["Relay request body must be valid JSON."]},
            ))

        if not isinstance(decoded, dict):
            return with_headers(BrowserRelayResponse(
                400,
                {"accepted": 0, "rejected": 0, "errors": ["Relay request body must be valid JSON."]},
            ))

        batch = decoded.get("batch")
        if not isinstance(batch, list):
            return with_headers(BrowserRelayResponse(
                400,
                {"accepted": 0, "rejected": 0, "errors": ["Relay request body must include a batch array."]},
            ))

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

            sanitized = _sanitize_event(candidate, service_override=self.service, environment_override=self.environment)
            if sanitized is None:
                errors.append(f"batch[{index}]: Invalid browser relay event payload.")
                continue

            accepted_events.append(sanitized)

        if accepted_events:
            try:
                if not self._deliver_events(accepted_events):
                    return with_headers(BrowserRelayResponse(500))

                if self.on_accept is not None:
                    self.on_accept(
                        BrowserRelayAcceptedBatch(
                            events=accepted_events,
                            headers=_strip_sensitive_headers(headers),
                            ip_address=ip_address,
                            received_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        )
                    )
            except Exception:
                return with_headers(BrowserRelayResponse(500))

        if errors:
            return with_headers(BrowserRelayResponse(
                400,
                {"accepted": len(accepted_events), "rejected": len(errors), "errors": errors},
            ))

        return with_headers(BrowserRelayResponse(
            202,
            {"accepted": len(accepted_events), "rejected": 0, "errors": []},
        ))

    def _deliver_events(self, accepted_events: list[dict[str, Any]]) -> bool:
        if self.project_mode is None:
            return True

        service_name = self.service or str(accepted_events[0]["service"]["name"])

        if self.project_mode == "local-only":
            local_transport = self._local_transports.get(service_name)
            if local_transport is None:
                local_transport = AtomicRelayFileTransport(
                    self.local_events_dir or resolve_default_local_events_dir(),
                    service_name,
                )
                self._local_transports[service_name] = local_transport

            return local_transport.write(accepted_events).status_code == 202

        if self.project_mode != "connected":
            return True

        if self.durable_write:
            spool_transport = self._spool_transports.get(service_name)
            if spool_transport is None:
                spool_transport = AtomicRelayFileTransport(
                    self.spool_dir or resolve_default_relay_spool_dir(),
                    service_name,
                )
                self._spool_transports[service_name] = spool_transport

            spool_write_result = spool_transport.write(accepted_events)
            if spool_write_result.status_code != 202:
                return False

            configured, succeeded = self._forward_connected_events(accepted_events)
            if succeeded and spool_write_result.written_file_path is not None:
                mark_spool_file_delivered(spool_write_result.written_file_path)

            return True if configured or spool_write_result.written_file_path is not None else False

        configured, succeeded = self._forward_connected_events(accepted_events)
        return configured and succeeded

    def _forward_connected_events(self, accepted_events: list[dict[str, Any]]) -> tuple[bool, bool]:
        if self._forwarder is None or not self.project_token:
            return (False, False)

        return self._forwarder.send(self.project_token, accepted_events)

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


def _cors_headers(origin: str) -> dict[str, str]:
    return {
        "access-control-allow-origin": origin,
        "access-control-allow-methods": "POST, OPTIONS",
        "access-control-allow-headers": "content-type",
        "access-control-max-age": "600",
        "vary": "Origin",
    }


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


def _sanitize_event(
    event: dict[str, Any],
    service_override: str | None = None,
    environment_override: str | None = None,
) -> dict[str, Any] | None:
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

    normalized_service_name = service_override or service_name
    normalized_environment = environment_override or environment
    if not normalized_service_name or not normalized_environment:
        return None

    sanitized: dict[str, Any] = {
        "schema_version": schema_version,
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "sdk_name": BROWSER_SDK_NAME,
        "sdk_version": sdk_version,
        "service": {
            "name": normalized_service_name,
            "environment": normalized_environment,
        },
        "payload": payload,
    }

    runtime = service.get("runtime")
    if isinstance(runtime, str) or runtime is None:
        sanitized["service"]["runtime"] = runtime

    framework = service.get("framework")
    if isinstance(framework, str) or framework is None:
        sanitized["service"]["framework"] = framework

    correlation = event.get("correlation")
    if isinstance(correlation, dict):
        normalized_correlation: dict[str, Any] = {}
        for key in ("request_id", "trace_id", "session_id", "user_id_hash"):
            value = correlation.get(key)
            if isinstance(value, str) or value is None:
                normalized_correlation[key] = value

        if normalized_correlation:
            sanitized["correlation"] = normalized_correlation

    return sanitized
