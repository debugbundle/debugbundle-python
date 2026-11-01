"""Native optional evidence preserves the protected legacy exception event."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from .capture_hooks import try_capture_lock
from .event_support import (
    backend_exception_request_payload,
    backend_exception_response_payload,
    redact_mapping,
    runtime_process_facts,
)
from .exception_evidence import snapshot_exception
from .exception_snapshot import exception_details
from .process_support import ensure_current_process
from .queue_admission import may_prepare_event

if TYPE_CHECKING:
    from .core import DebugBundleSdk


def capture_exception(
    sdk: DebugBundleSdk,
    error: BaseException,
    context: Mapping[str, object] | None,
    handled: bool,
    max_pending_events: int,
    max_pending_bytes: int,
    fatal: bool = False,
) -> None:
    ensure_current_process(sdk)
    if not sdk._enabled or not may_prepare_event(sdk, True, max_pending_events, max_pending_bytes):
        return
    try:
        name, message, stack = exception_details(error)
        redacted_context = redact_mapping(dict(context or {}), sdk._redact_fields)
        request_payload = backend_exception_request_payload(redacted_context.get("request"))
        response_payload = backend_exception_response_payload(redacted_context.get("response"))

        payload: dict[str, object] = {
            "name": name,
            "message": message,
            "stack": stack,
            "handled": handled,
            "request": request_payload,
            "response": response_payload,
            "runtime": runtime_process_facts(),
        }
        if sdk._probe_flush_on_error and try_capture_lock(sdk):
            try:
                probe_data = sdk._build_probe_data()
            finally:
                sdk._lock.release()
            if probe_data is not None:
                payload["probe_data"] = probe_data

        candidate = sdk._base_event("backend_exception", payload, context=redacted_context)
        if sdk._capture_exception_evidence and sdk._evidence.supports("exception_evidence_v1"):
            execution = sdk._operations.snapshot()
            additions: dict[str, object] = {"execution": execution} if execution is not None else {}
            selected_context = candidate.get("context")
            if type(selected_context) is dict and "evidence_reference" in selected_context:
                additions["evidence_reference"] = selected_context["evidence_reference"]
            overhead = len(json.dumps(additions, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 32
            graph = snapshot_exception(error, fatal=fatal, budget_bytes=(4096 if fatal else 32768) - overhead)
            if graph is not None:
                payload["exception_evidence"] = graph
                if execution is not None:
                    payload["execution"] = execution
        event = sdk._protect_event(candidate)
        if event is None or not sdk._passes_sample_rate():
            return
        event_payload = cast(dict[str, object], event["payload"])
        suppression_key = (
            f"{event['event_type']}:{event_payload.get('name', '')}:"
            f"{event_payload.get('message', '')}:{event_payload.get('stack', '')}"
        )
        event_bytes = sdk._event_bytes(event)
        if not try_capture_lock(sdk):
            sdk._pressure_drops += 1
            return
        try:
            if sdk._enabled and sdk._suppression.should_capture(suppression_key, sdk._time_provider()):
                if sdk._offer_protected_event(event, event_bytes) and fatal:
                    sdk._hook_bypass.add(id(event))
        finally:
            sdk._lock.release()
    except BaseException:
        return
