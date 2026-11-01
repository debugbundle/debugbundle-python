"""Explicit application observations share protected queue ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .capture_hooks import try_capture_lock
from .evidence_delivery import allows_recovery
from .evidence_wire import normalise_recovery

if TYPE_CHECKING:
    from .core import DebugBundleSdk


def capture_recovery(sdk: DebugBundleSdk, observation: object) -> None:
    try:
        if not sdk._enabled or not allows_recovery(sdk) or not sdk._passes_sample_rate():
            return
        operation = sdk._operations.current()
        payload = normalise_recovery(observation, operation.scope if operation is not None else None)
        if payload is None:
            return
        correlation = observation.get("correlation") if type(observation) is dict else None
        # Correlation remains metadata and must never invoke application mappings.
        context = (
            {
                key: value
                for key, value in correlation.items()
                if key in ("request_id", "trace_id", "session_id", "user_id_hash")
                and (value is None or type(value) is str)
            }
            if type(correlation) is dict and len(correlation) <= 4 and all(type(key) is str for key in correlation)
            else {}
        )
        event = sdk._protect_event(sdk._base_event("diagnostic_context", payload, context=context))
        if event is None or not try_capture_lock(sdk):
            return
        try:
            sdk._offer_protected_event(event, sdk._event_bytes(event))
        finally:
            sdk._lock.release()
    except BaseException:
        return
