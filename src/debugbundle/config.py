from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_PROBES_POLL_INTERVAL_MS = 60_000


@dataclass(frozen=True)
class CapturePolicy:
    preset: str
    capture_logs: str
    capture_request_events: str
    capture_breadcrumbs: str
    capture_probe_events: str


@dataclass(frozen=True)
class RemoteProbeDirective:
    id: str
    label_pattern: str
    service: str
    environment: str
    expires_at: str


@dataclass(frozen=True)
class RemoteConfigSnapshot:
    probes_enabled: bool
    remote_probes_enabled: bool
    directives: list[RemoteProbeDirective]
    poll_interval_ms: int
    capture_policy: CapturePolicy
    trigger_token_key: str | None = None


BALANCED_CAPTURE_POLICY = CapturePolicy(
    preset="balanced",
    capture_logs="warning",
    capture_request_events="failures_only",
    capture_breadcrumbs="exception_only",
    capture_probe_events="buffer_only",
)

MINIMAL_CAPTURE_POLICY = CapturePolicy(
    preset="minimal",
    capture_logs="error",
    capture_request_events="failures_only",
    capture_breadcrumbs="local_only",
    capture_probe_events="buffer_only",
)


def parse_remote_config(payload: object, fallback_poll_interval_ms: int, now_ms: int) -> RemoteConfigSnapshot | None:
    if not isinstance(payload, dict):
        return None

    probes_enabled = payload.get("probes_enabled") is True
    remote_probes_enabled = payload.get("remote_probes_enabled") is True
    poll_interval_candidate = payload.get("poll_interval_ms")
    poll_interval_ms = (
        int(poll_interval_candidate)
        if isinstance(poll_interval_candidate, (int, float)) and int(poll_interval_candidate) > 0
        else fallback_poll_interval_ms
    )

    directives: list[RemoteProbeDirective] = []
    active_probes = payload.get("active_probes")
    if isinstance(active_probes, list):
        for directive in active_probes:
            parsed = _parse_directive(directive)
            if parsed is not None and _expires_at_ms(parsed.expires_at) > now_ms:
                directives.append(parsed)

    capture_policy = _parse_capture_policy(payload.get("capture_policy"))
    if capture_policy is None:
        return None

    return RemoteConfigSnapshot(
        probes_enabled=probes_enabled,
        remote_probes_enabled=remote_probes_enabled,
        directives=directives,
        poll_interval_ms=poll_interval_ms if remote_probes_enabled else DEFAULT_PROBES_POLL_INTERVAL_MS,
        capture_policy=capture_policy,
        trigger_token_key=_as_non_empty_string(payload.get("trigger_token_key")),
    )


def find_matching_remote_probe_directives(
    directives: list[RemoteProbeDirective],
    label: str,
    service: str,
    environment: str,
    now_ms: int,
) -> list[RemoteProbeDirective]:
    matches: list[RemoteProbeDirective] = []
    for directive in directives:
        if _expires_at_ms(directive.expires_at) <= now_ms:
            continue
        if directive.service != "*" and directive.service != service:
            continue
        if directive.environment != "*" and directive.environment != environment:
            continue
        if _matches_label_pattern(directive.label_pattern, label):
            matches.append(directive)
    return matches


def _parse_capture_policy(payload: object) -> CapturePolicy | None:
    if payload is None:
        return BALANCED_CAPTURE_POLICY
    if not isinstance(payload, dict):
        return None

    preset = _as_non_empty_string(payload.get("preset")) or BALANCED_CAPTURE_POLICY.preset
    capture_logs = _as_non_empty_string(payload.get("capture_logs"))
    capture_request_events = _as_non_empty_string(payload.get("capture_request_events"))
    capture_breadcrumbs = _as_non_empty_string(payload.get("capture_breadcrumbs"))
    capture_probe_events = _as_non_empty_string(payload.get("capture_probe_events"))

    if capture_logs not in {"off", "error", "warning", "info"}:
        return None
    if capture_request_events not in {"off", "failures_only", "filtered", "all"}:
        return None
    if capture_breadcrumbs not in {"local_only", "exception_only", "standalone"}:
        return None
    if capture_probe_events not in {"buffer_only", "standalone_when_activated"}:
        return None

    return CapturePolicy(
        preset=preset,
        capture_logs=capture_logs,
        capture_request_events=capture_request_events,
        capture_breadcrumbs=capture_breadcrumbs,
        capture_probe_events=capture_probe_events,
    )


def _parse_directive(payload: object) -> RemoteProbeDirective | None:
    if not isinstance(payload, dict):
        return None

    directive_id = _as_non_empty_string(payload.get("id"))
    label_pattern = _as_non_empty_string(payload.get("label_pattern"))
    service = _as_non_empty_string(payload.get("service"))
    environment = _as_non_empty_string(payload.get("environment"))
    expires_at = _as_non_empty_string(payload.get("expires_at"))

    if directive_id is None:
        return None
    if label_pattern is None:
        return None
    if service is None:
        return None
    if environment is None:
        return None
    if expires_at is None:
        return None
    if _expires_at_ms(expires_at) == 0:
        return None

    return RemoteProbeDirective(
        id=directive_id,
        label_pattern=label_pattern,
        service=service,
        environment=environment,
        expires_at=expires_at,
    )


def _matches_label_pattern(pattern: str, label: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return label == prefix or label.startswith(f"{prefix}.")
    return pattern == label


def _expires_at_ms(value: str) -> int:
    from datetime import datetime

    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except ValueError:
        return 0


def _as_non_empty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) > 0 else None
