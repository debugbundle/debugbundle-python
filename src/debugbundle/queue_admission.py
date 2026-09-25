"""Constant-time overload admission and pending-event priority helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core import DebugBundleSdk


def high_priority_event(event: dict[str, object]) -> bool:
    if event.get("event_type") == "backend_exception":
        return True
    payload = event.get("payload")
    return isinstance(payload, dict) and (
        payload.get("level") in ("error", "critical")
        or (event.get("event_type") == "request_event" and type(payload.get("response_status")) is int
            and payload["response_status"] >= 500)
    )


def may_prepare_event(sdk: DebugBundleSdk, high_priority: bool, max_events: int, max_bytes: int) -> bool:
    if len(sdk._buffer) < max_events and sdk._buffer_bytes < max_bytes:
        return True
    if high_priority and sdk._pending_low_priority > 0:
        return True
    sdk._pressure_drops = min(2**63 - 1, sdk._pressure_drops + 1)
    return False


def evict_low_priority(sdk: DebugBundleSdk) -> bool:
    for index in range(sdk._inflight_count, len(sdk._buffer)):
        if not high_priority_event(sdk._buffer[index]):
            evicted = sdk._buffer.pop(index)
            sdk._buffer_bytes -= sdk._buffer_sizes.pop(id(evicted))
            sdk._pending_low_priority -= 1
            sdk._pressure_drops = min(2**63 - 1, sdk._pressure_drops + 1)
            return True
    return False
