"""Prepare bounded SDK summaries away from the capture admission lock."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .core import DebugBundleSdk


def prepare_flush_aggregates(
    sdk: DebugBundleSdk,
    suppression: list[dict[str, object]],
    pressure_count: int,
) -> tuple[list[tuple[dict[str, object], int]], tuple[dict[str, object], int] | None]:
    prepared: list[tuple[dict[str, object], int]] = []
    for aggregate in suppression:
        event_type = aggregate.get("event_type")
        payload = aggregate.get("payload")
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            continue
        aggregate.update(sdk._base_event(event_type, cast(dict[str, object], payload)))
        protected = sdk._protect_event(aggregate)
        if protected is not None:
            prepared.append((protected, sdk._event_bytes(protected)))

    pressure: tuple[dict[str, object], int] | None = None
    if pressure_count:
        event = sdk._base_event("log_event", {
            "message": f"{pressure_count} SDK events suppressed due to queue pressure",
            "level": "warning",
            "attributes": {"suppressed_count": pressure_count, "reason": "queue_pressure"},
        })
        protected = sdk._protect_event(event)
        if protected is not None:
            pressure = (protected, sdk._event_bytes(protected))
    return prepared, pressure
