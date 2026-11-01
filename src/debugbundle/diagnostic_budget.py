"""Bound tracker metadata and pending payload ownership to 64 KiB per destination."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass
class Window:
    starts: float
    failure_at: float | None = None
    failure_id: str | None = None
    attempts: set[str] = field(default_factory=set)
    observations: int = 0
    resource: bool = False
    ambiguous: bool = False
    pending: int = 0
    cost: int = 0


@dataclass
class Pending:
    scope: str
    kind: str
    attempt: str | None
    size: int
    cost: int


def _key(payload: dict[str, Any]) -> str:
    return json.dumps([payload["failure_ref"], payload["scope"]], sort_keys=True, separators=(",", ":"))


def _cost(key: str, value: Window) -> int:
    return len(
        json.dumps(
            [
                key,
                value.starts,
                value.failure_at,
                value.failure_id,
                sorted(value.attempts),
                value.observations,
                value.resource,
                value.ambiguous,
                value.pending,
            ],
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _size(value: int) -> bool:
    return type(value) is int and 0 <= value <= 65536


class DiagnosticCaptureBudget:
    def __init__(self) -> None:
        self._windows: dict[str, Window] = {}
        self._pending: dict[str, Pending] = {}
        self._held = 0

    @property
    def accounted_bytes(self) -> int:
        return self._held + sum(window.cost for window in self._windows.values())

    def clear(self) -> None:
        self._windows.clear()
        self._pending.clear()
        self._held = 0

    def _prune(self, now: float) -> None:
        self._windows = {
            key: value for key, value in self._windows.items() if value.pending or now < value.starts + 300_000
        }

    def register_failure(self, payload: dict[str, Any], event_id: str, now: float) -> bool:
        if not math.isfinite(now):
            return False
        self._prune(now)
        key = _key(payload)
        previous = self._windows.get(key)
        if previous is not None and previous.failure_id is not None and previous.failure_id != event_id:
            previous.ambiguous = True
            previous.cost = _cost(key, previous)
            return False
        if previous is None and len(self._windows) >= 50:
            return False
        value = replace(previous) if previous is not None else Window(now)
        value.failure_id = event_id
        value.failure_at = value.failure_at if value.failure_at is not None else now
        value.cost = _cost(key, value)
        if self.accounted_bytes - (previous.cost if previous is not None else 0) + value.cost > 65536:
            return False
        self._windows[key] = value
        return not value.ambiguous

    def reserve(self, event_id: str, payload: dict[str, Any], size: int, now: float) -> bool:
        if not _size(size) or not math.isfinite(now) or event_id in self._pending:
            return False
        self._prune(now)
        key = _key(payload)
        previous = self._windows.get(key)
        if (previous is None and len(self._windows) >= 50) or (previous is not None and previous.ambiguous):
            return False
        value = replace(previous, attempts=set(previous.attempts)) if previous is not None else Window(now)
        if (
            now < value.starts
            or now > (value.failure_at if value.failure_at is not None else value.starts) + 300_000
            or value.observations >= 20
        ):
            return False
        attempt = payload["attempt_id"] if payload["kind"] == "recovery" else None
        if attempt is not None:
            if attempt not in value.attempts and len(value.attempts) >= 5:
                return False
            value.attempts.add(attempt)
        else:
            if (
                value.resource
                or value.failure_id != payload["source_event_id"]
                or value.failure_at is None
                or now > value.failure_at + 30_000
            ):
                return False
            value.resource = True
        value.pending += 1
        value.observations += 1
        value.cost = _cost(key, value)
        cost = size + len(event_id) + len(key) + 64
        if self.accounted_bytes - (previous.cost if previous is not None else 0) + value.cost + cost > 65536:
            return False
        self._windows[key] = value
        self._pending[event_id] = Pending(key, payload["kind"], attempt, size, cost)
        self._held += cost
        return True

    def replace(self, original_id: str, event_id: str, payload: dict[str, Any], size: int, now: float) -> bool:
        entry = self._pending.get(original_id)
        key, attempt = _key(payload), payload.get("attempt_id")
        if entry is not None and entry.scope == key and entry.kind == payload["kind"] and entry.attempt == attempt:
            if event_id != original_id:
                if event_id in self._pending:
                    self.release([original_id])
                    return False
                self._pending[event_id] = self._pending.pop(original_id)
                delta = len(event_id) - len(original_id)
                entry.cost += delta
                self._held += delta
            if not self.adjust(event_id, size) or not self.eligible(event_id, payload):
                self.release([event_id])
                return False
            return True
        self.release([original_id])
        return self.reserve(event_id, payload, size, now)

    def adjust(self, event_id: str, size: int) -> bool:
        entry = self._pending.get(event_id)
        if entry is None or not _size(size) or self.accounted_bytes + size - entry.size > 65536:
            return False
        self._held += size - entry.size
        entry.cost += size - entry.size
        entry.size = size
        return True

    def eligible(self, event_id: str, payload: dict[str, Any]) -> bool:
        entry, value = self._pending.get(event_id), self._windows.get(_key(payload))
        return (
            entry is not None
            and entry.scope == _key(payload)
            and value is not None
            and not value.ambiguous
            and (payload["kind"] == "recovery" or value.failure_id == payload["source_event_id"])
        )

    def release(self, event_ids: list[str]) -> None:
        for event_id in event_ids:
            entry = self._pending.pop(event_id, None)
            if entry is None:
                continue
            self._held -= entry.cost
            value = self._windows.get(entry.scope)
            if value is not None:
                value.pending = max(0, value.pending - 1)
                value.cost = _cost(entry.scope, value)

    def retain(self, event_ids: set[str]) -> None:
        self.release([event_id for event_id in self._pending if event_id not in event_ids])
