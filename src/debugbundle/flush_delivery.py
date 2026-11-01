"""Reserved bounded delivery, canonical ACK handling and retry ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .acknowledgement import decide_acknowledgement
from .delivery_hooks import finalize_batch
from .evidence_delivery import downgrade_rejections
from .flush_aggregates import prepare_flush_aggregates
from .ingestion_wire import prefix
from .process_support import ensure_current_process
from .queue_admission import high_priority_event
from .transport import HttpTransport, bounded_retry_after_ms, coerce_transport_response

if TYPE_CHECKING:
    from .core import DebugBundleSdk


def flush_sdk(sdk: DebugBundleSdk, max_events: int, max_bytes: int) -> None:
    ensure_current_process(sdk)
    with sdk._lock:
        if not sdk._enabled or sdk._transport is None or sdk._sending:
            return
        if sdk._timer is not None:
            sdk._timer.cancel()
            sdk._timer = None
        now = sdk._time_provider()
        if now < sdk._retry_after:
            sdk._schedule_flush_locked(delay=sdk._retry_after - now)
            return
        sdk._sending = True
        generation = sdk._generation
        suppression = sdk._suppression.drain_aggregates(now)
        pressure_count = (
            sdk._pressure_drops
            if sdk._pressure_drops and now - sdk._last_pressure_report_at >= 60.0 and len(sdk._buffer) < max_events
            else 0
        )

    try:
        aggregates, pressure = prepare_flush_aggregates(sdk, suppression, pressure_count)
    except BaseException:
        aggregates, pressure = [], None

    with sdk._lock:
        if generation != sdk._generation:
            sdk._sending = False
            sdk._inflight_count = 0
            if sdk._buffer:
                sdk._schedule_flush_locked()
            return
        for aggregate, aggregate_bytes in aggregates:
            sdk._offer_protected_event(aggregate, aggregate_bytes)
        if (
            pressure is not None
            and len(sdk._buffer) < max_events
            and sdk._buffer_bytes + pressure[1] <= max_bytes
            and sdk._offer_protected_event(*pressure)
        ):
            sdk._pressure_drops = max(0, sdk._pressure_drops - pressure_count)
            sdk._last_pressure_report_at = now
        if not sdk._buffer:
            sdk._sending = False
            sdk._schedule_pressure_report_locked()
            return
        batch = list(sdk._buffer)
        sdk._inflight_count = len(batch)
        sdk._pending_low_priority = 0
        transport = sdk._transport
        request = {
            "project_token": sdk._project_token,
            "events": batch,
        }

    finalized = finalize_batch(sdk, batch, generation, max_bytes)
    # Native serialization stays outside the capture lock, like hook/privacy
    # work. The committed queue still owns every finalized event meanwhile.
    wire_batch = prefix(finalized) if finalized is not None and isinstance(transport, HttpTransport) else finalized

    with sdk._lock:
        if generation != sdk._generation or finalized is None:
            sdk._sending = False
            sdk._inflight_count = 0
            if sdk._buffer:
                sdk._schedule_flush_locked()
            return
        batch = finalized
        if isinstance(transport, HttpTransport):
            assert wire_batch is not None
            batch = wire_batch
            # Finalized unsent tails stay charged and must not execute an
            # application hook again merely because the wire batch was split.
            sdk._hook_bypass.update(id(event) for event in finalized[len(batch) :])
        sdk._inflight_count = len(batch)
        if not batch:
            sdk._sending = False
            if sdk._buffer:
                sdk._schedule_flush_locked()
            return
        request["events"] = batch

    try:
        response = coerce_transport_response(transport(request))
    except BaseException:
        response = None

    with sdk._lock:
        sdk._sending = False
        sdk._inflight_count = 0
        if generation != sdk._generation:
            if sdk._buffer:
                sdk._schedule_flush_locked()
            return
        try:
            if response is None:
                sdk._consecutive_failures += 1
                sdk._schedule_flush_locked()
                return

            if 200 <= response.status_code < 300:
                acknowledgement = decide_acknowledgement(
                    response.body, len(batch), isinstance(transport, HttpTransport)
                )
                lost = sdk._evidence.observe(
                    response.body, isinstance(transport, HttpTransport), acknowledgement.kind == "acknowledged"
                )
                if acknowledgement.kind == "protocol_failure":
                    sdk._consecutive_failures += 1
                    retry_after_ms = bounded_retry_after_ms(response.retry_after_ms)
                    sdk._retry_after = sdk._time_provider() + (retry_after_ms / 1000)
                    sdk._emit_diagnostic(
                        "ingestion_acknowledgement_invalid",
                        "sdk-python retained a batch after an invalid ingestion acknowledgement",
                        metadata={"reason": acknowledgement.reason or "invalid"},
                    )
                    sdk._schedule_flush_locked(delay=retry_after_ms / 1000)
                    return
                if acknowledgement.kind == "legacy":
                    sdk._buffer = sdk._buffer[len(batch) :]
                    sdk._retry_after = 0.0
                    sdk._last_event_at = sdk._time_provider() * 1000
                    sdk._consecutive_failures = 0
                    return

                trailing_events = sdk._buffer[len(batch) :]
                downgraded = downgrade_rejections(sdk, batch, acknowledgement.terminal_errors, lost)
                sdk._buffer = (
                    [batch[index] for index in acknowledgement.retryable_indices if 0 <= index < len(batch)]
                    + downgraded
                    + trailing_events
                )
                if acknowledgement.terminal_errors:
                    sdk._emit_diagnostic(
                        "ingestion_events_rejected",
                        "sdk-python removed terminally rejected ingestion events",
                        metadata={
                            "rejected_count": len(acknowledgement.terminal_errors),
                            "reasons": sorted({reason for _, reason in acknowledgement.terminal_errors}),
                        },
                    )
                if acknowledgement.accepted > 0:
                    sdk._last_event_at = sdk._time_provider() * 1000
                if acknowledgement.retryable_indices or downgraded:
                    sdk._consecutive_failures += 1
                    retry_after_ms = bounded_retry_after_ms(response.retry_after_ms)
                    sdk._retry_after = sdk._time_provider() + (retry_after_ms / 1000)
                    sdk._schedule_flush_locked(delay=retry_after_ms / 1000)
                    return
                sdk._retry_after = 0.0
                sdk._consecutive_failures = 0 if acknowledgement.accepted > 0 else 3
                return

            sdk._consecutive_failures += 1
            if response.status_code == 429 or (response.status_code >= 500 and response.retry_after_ms is not None):
                retry_after_ms = bounded_retry_after_ms(response.retry_after_ms)
                sdk._retry_after = sdk._time_provider() + (retry_after_ms / 1000)
                sdk._schedule_flush_locked(delay=retry_after_ms / 1000)
                return
            if 400 <= response.status_code < 500:
                sdk._buffer = sdk._buffer[len(batch) :]
                sdk._retry_after = 0.0
                return
            sdk._schedule_flush_locked()
        finally:
            sdk._hook_bypass.intersection_update(id(event) for event in sdk._buffer)
            sdk._diagnostics.retain(
                {str(event["event_id"]) for event in sdk._buffer if event["event_type"] == "diagnostic_context"}
            )
            sdk._buffer_sizes = {id(event): sdk._buffer_sizes[id(event)] for event in sdk._buffer}
            sdk._buffer_bytes = sum(sdk._buffer_sizes.values())
            sdk._pending_low_priority = sum(not high_priority_event(event) for event in sdk._buffer)
            if sdk._buffer and sdk._timer is None and sdk._retry_after <= sdk._time_provider():
                sdk._schedule_flush_locked(
                    delay=0.0
                    if isinstance(transport, HttpTransport)
                    and response is not None
                    and 200 <= response.status_code < 300
                    and sdk._consecutive_failures == 0
                    else None
                )
            elif not sdk._buffer and sdk._timer is None:
                sdk._schedule_pressure_report_locked()
