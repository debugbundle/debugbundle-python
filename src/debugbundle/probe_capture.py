"""Probe capture with short admission/commit locks and no application callback under them."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from .capture_hooks import try_capture_lock
from .event_support import iso_now
from .process_support import ensure_current_process
from .redaction import sanitize_telemetry

if TYPE_CHECKING:
    from .core import DebugBundleSdk


@dataclass
class ProbeEntry:
    label: str
    data: dict[str, object]
    timestamp: str


def capture_probe(
    sdk: DebugBundleSdk,
    label: str,
    data: object | Callable[[], object],
    opts: Mapping[str, object] | None,
) -> None:
    try:
        ensure_current_process(sdk)
        options = dict(opts or {})
        now_ms = int(sdk._time_provider() * 1000)
        if not try_capture_lock(sdk):
            sdk._pressure_drops += 1
            return
        try:
            if not sdk._enabled:
                return
            heavy = options.get("heavy") is True
            directives = sdk._find_matching_probe_directives(label, now_ms)
            if heavy and not directives:
                return
            if label not in sdk._probe_buffers and len(sdk._probe_buffers) >= sdk._max_probe_labels:
                return
            redact_fields = set(sdk._redact_fields)
            generation = sdk._generation
        finally:
            sdk._lock.release()

        # A host supplier, mapping iterator, privacy walk, clock, or serializer may
        # be slow or hostile. None may hold the capture lock or block exceptions.
        protected_label = sanitize_telemetry(label, redact_fields)
        value = data() if callable(data) else data
        if not isinstance(value, Mapping):
            value = {"value": value}
        protected_value = sanitize_telemetry(dict(value), redact_fields)
        if not isinstance(protected_label, str) or not isinstance(protected_value, dict):
            return
        label = protected_label
        redacted_value = cast(dict[str, object], protected_value)
        entry = None if heavy else ProbeEntry(label, redacted_value, iso_now(sdk._time_provider))

        prepared: list[tuple[str, dict[str, object], int]] = []
        for directive in directives:
            payload: dict[str, object] = {
                "label": label,
                "activation_id": directive.id,
                "probe_label_pattern": directive.label_pattern,
                "data": dict(redacted_value),
            }
            event = sdk._protect_event(sdk._base_event("probe_event", payload))
            if event is not None:
                prepared.append((directive.id, event, sdk._event_bytes(event)))

        refreshed_now_ms = int(sdk._time_provider() * 1000)
        if not try_capture_lock(sdk):
            sdk._pressure_drops += 1
            return
        try:
            if not sdk._enabled or generation != sdk._generation:
                return
            if label not in sdk._probe_buffers and len(sdk._probe_buffers) >= sdk._max_probe_labels:
                return
            active_ids = {directive.id for directive in sdk._find_matching_probe_directives(label, refreshed_now_ms)}
            if heavy and not active_ids:
                return
            if entry is not None:
                bucket = sdk._probe_buffers.setdefault(label, deque(maxlen=sdk._max_probe_entries_per_label))
                bucket.append(entry)
            if sdk._capture_policy.capture_probe_events == "standalone_when_activated":
                for directive_id, event, event_bytes in prepared:
                    if directive_id in active_ids:
                        sdk._offer_protected_event(event, event_bytes)
        finally:
            sdk._lock.release()
    except BaseException:
        # Probe callbacks and SDK failures must remain observational.
        return
