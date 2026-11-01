"""Finalize one reserved event at a time without retaining uncharged hook output."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .evidence_delivery import allows_recovery, reconcile_diagnostic
from .ingestion_wire import fit_event
from .request_capture_policy import should_capture_request_event
from .transport import HttpTransport

if TYPE_CHECKING:
    from .core import DebugBundleSdk


def replacement_allowed(sdk: DebugBundleSdk, event: dict[str, object]) -> bool:
    payload = cast(dict[str, object], event["payload"])
    if event["event_type"] == "log_event":
        return sdk._log_level_eligible(cast(str, payload["level"]))
    if event["event_type"] == "request_event":
        return should_capture_request_event(sdk._capture_policy, payload, payload)
    if event["event_type"] == "probe_event":
        return sdk._capture_policy.capture_probe_events == "standalone_when_activated"
    if event["event_type"] == "diagnostic_context":
        return payload["kind"] == "recovery" and allows_recovery(sdk)
    return True


def finalize_batch(
    sdk: DebugBundleSdk,
    batch: list[dict[str, object]],
    generation: int,
    max_bytes: int,
) -> list[dict[str, object]] | None:
    retained_index = 0
    for snapshot_index, original in enumerate(batch):
        try:
            replacement = sdk._apply_before_send_event(original)
            if isinstance(sdk._transport, HttpTransport):
                replacement = fit_event(replacement)
            replacement_bytes = sdk._event_bytes(replacement) if replacement is not None else 0
        except BaseException:
            # Hook exceptions/invalid returns already use the protected original
            # inside apply_before_send. Other failures must withhold telemetry.
            replacement, replacement_bytes = None, 0
        with sdk._lock:
            if generation != sdk._generation:
                return None
            original_bytes = sdk._buffer_sizes[id(original)]
            allowed = replacement is not None and replacement_allowed(sdk, replacement)
            fits = sdk._buffer_bytes - original_bytes + replacement_bytes <= max_bytes
            if allowed and fits:
                allowed = reconcile_diagnostic(sdk, original, replacement)
            if allowed and fits:
                assert replacement is not None
                sdk._buffer[retained_index] = replacement
                sdk._buffer_sizes.pop(id(original))
                sdk._buffer_sizes[id(replacement)] = replacement_bytes
                sdk._buffer_bytes += replacement_bytes - original_bytes
                if id(original) in sdk._hook_bypass:
                    sdk._hook_bypass.add(id(replacement))
                sdk._hook_bypass.discard(id(original))
                retained_index += 1
                batch[snapshot_index] = replacement
            else:
                reconcile_diagnostic(sdk, original, None)
                if allowed and not fits:
                    sdk._pressure_drops += 1
                sdk._buffer.pop(retained_index)
                sdk._buffer_sizes.pop(id(original))
                sdk._buffer_bytes -= original_bytes
                sdk._hook_bypass.discard(id(original))
                sdk._inflight_count -= 1
                # Release the snapshot reference before another callback can wait.
                batch[snapshot_index] = {}
    return [event for event in batch if event]
