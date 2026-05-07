from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

DUPLICATE_WINDOW_SECONDS = 30.0
LOOP_WINDOW_SECONDS = 2.0
LOOP_THRESHOLD = 10
LOOP_RESET_AFTER_SECONDS = 60.0
LOOP_CHECKPOINT_SECONDS = 30.0
MAX_NORMAL_EVENTS_PER_WINDOW = 3


@dataclass
class SuppressionState:
    window_started_at: float
    emitted_count: int = 0
    pending_suppressed_count: int = 0
    pending_first_seen_at: float | None = None
    pending_last_seen_at: float | None = None
    last_aggregate_emitted_at: float | None = None
    loop_window_started_at: float = 0.0
    loop_hit_count: int = 0
    suppression_mode: bool = False
    last_seen_at: float = 0.0


class EventSuppressionTracker:
    def __init__(self) -> None:
        self._states: dict[str, SuppressionState] = {}

    def should_capture(self, key: str, now: float) -> bool:
        state = self._states.get(key)
        if state is None:
            state = SuppressionState(
                window_started_at=now,
                loop_window_started_at=now,
                last_seen_at=now,
            )
            self._states[key] = state

        if state.suppression_mode and now - state.last_seen_at >= LOOP_RESET_AFTER_SECONDS:
            self._states[key] = SuppressionState(
                window_started_at=now,
                loop_window_started_at=now,
                last_seen_at=now,
            )
            state = self._states[key]

        if now - state.window_started_at >= DUPLICATE_WINDOW_SECONDS:
            state.window_started_at = now
            state.emitted_count = 0

        if now - state.loop_window_started_at >= LOOP_WINDOW_SECONDS:
            state.loop_window_started_at = now
            state.loop_hit_count = 0

        state.loop_hit_count += 1
        state.last_seen_at = now

        if state.loop_hit_count > LOOP_THRESHOLD:
            state.suppression_mode = True

        if state.suppression_mode:
            self._mark_suppressed(state, now)
            return False

        if state.emitted_count < MAX_NORMAL_EVENTS_PER_WINDOW:
            state.emitted_count += 1
            return True

        self._mark_suppressed(state, now)
        return False

    def drain_aggregates(self, now: float) -> list[dict[str, object]]:
        aggregates: list[dict[str, object]] = []

        for key, state in self._states.items():
            if (
                state.pending_suppressed_count == 0
                or state.pending_first_seen_at is None
                or state.pending_last_seen_at is None
            ):
                continue

            if (
                state.suppression_mode
                and state.last_aggregate_emitted_at is not None
                and now - state.last_aggregate_emitted_at < LOOP_CHECKPOINT_SECONDS
            ):
                continue

            aggregates.append(
                {
                    "event_type": "error_suppressed",
                    "payload": {
                        "fingerprint": sha256(key.encode("utf-8")).hexdigest(),
                        "suppressed_count": state.pending_suppressed_count,
                        "first_seen": _to_iso(state.pending_first_seen_at),
                        "last_seen": _to_iso(state.pending_last_seen_at),
                        "window_seconds": int(DUPLICATE_WINDOW_SECONDS),
                    },
                }
            )

            state.pending_suppressed_count = 0
            state.pending_first_seen_at = None
            state.pending_last_seen_at = None
            state.last_aggregate_emitted_at = now

        return aggregates

    @staticmethod
    def _mark_suppressed(state: SuppressionState, now: float) -> None:
        if state.pending_suppressed_count == 0:
            state.pending_first_seen_at = state.window_started_at
        state.pending_suppressed_count += 1
        state.pending_last_seen_at = now


def _to_iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
