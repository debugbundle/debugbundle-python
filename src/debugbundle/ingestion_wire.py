"""Size the final HTTP serializer; local/custom persistence keeps its own budget."""

from __future__ import annotations

import json
from typing import Any

from .evidence_wire import reference, without_optional

MAX_BODY_BYTES = 256 * 1024
MAX_EVENT_BYTES = MAX_BODY_BYTES - 13


def encode_body(events: object) -> bytes:
    return json.dumps({"events": events}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def event_bytes(event: dict[str, Any]) -> int:
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def fit_event(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if event is None:
        return None
    if event_bytes(event) <= MAX_EVENT_BYTES:
        return event
    reduced = without_optional(event)
    if reduced is not None:
        fields = ("exception_evidence", "execution", "resource_observations")
        marked = dict(
            reduced,
            context={
                **event.get("context", {}),
                "evidence_limits": {
                    "version": 1,
                    **{field: "budget_exceeded" for field in fields if field in event["payload"]},
                },
            },
        )
        if event_bytes(marked) <= MAX_EVENT_BYTES:
            return marked
        if event_bytes(reduced) <= MAX_EVENT_BYTES:
            return reduced
    else:
        reduced = event
    context = reduced.get("context")
    if type(context) is dict and reference(context.get("evidence_reference")) is not None:
        reduced = dict(reduced, context={key: value for key, value in context.items() if key != "evidence_reference"})
        if event_bytes(reduced) <= MAX_EVENT_BYTES:
            return reduced
    return None


def prefix(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    size = 12  # {"events":[]}
    for index, event in enumerate(events):
        length = event_bytes(event)
        cost = length + (1 if index else 0)
        if length > MAX_EVENT_BYTES:
            raise ValueError("event exceeds ingestion limit")
        if size + cost > MAX_BODY_BYTES:
            return events[:index]
        size += cost
    return events


def chunks(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    result = []
    while events:
        selected = prefix(events)
        if not selected:
            raise ValueError("event exceeds ingestion limit")
        result.append(selected)
        events = events[len(selected) :]
    return result
