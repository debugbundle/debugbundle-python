from __future__ import annotations

import json
import threading
from dataclasses import replace

from debugbundle.config import MINIMAL_CAPTURE_POLICY
from debugbundle.core import MAX_PENDING_BYTES, DebugBundleSdk


def test_valid_over_budget_replacement_never_restores_pre_hook_private_fields() -> None:
    sent: list[dict[str, object]] = []

    def hook(event: dict[str, object]) -> dict[str, object]:
        event["payload"]["message"] = "app redacted"
        event["context"] = {f"detail_{index}": "x" * 4096 for index in range(20)}
        return event

    sdk = DebugBundleSdk(transport=lambda request: sent.extend(request["events"]) or 202)
    sdk.init(project_token="dbundle_proj_test", batch_size=1000, flush_interval=3600, before_send=hook)
    try:
        for index in range(150):
            sdk.capture_log(f"private tenant detail {index}", level="error")
        sdk.flush()
        assert sent
        assert all(event["payload"]["message"] == "app redacted" for event in sent)
        assert len(json.dumps(sent).encode()) <= MAX_PENDING_BYTES
    finally:
        sdk.dispose()


def test_replacement_rechecks_log_request_and_probe_policy() -> None:
    sent: list[dict[str, object]] = []

    def hook(event: dict[str, object]) -> dict[str, object]:
        if event["event_type"] == "log_event":
            event["payload"]["level"] = "info"
        elif event["event_type"] == "request_event":
            event["payload"]["response_status"] = 200
        else:
            event["event_type"] = "probe_event"
            event["payload"] = {"label": "test", "data": {},
                                "activation_id": "36beb3e1-bf61-4c3c-94ba-42102a9c26c0",
                                "probe_label_pattern": "test"}
        return event

    sdk = DebugBundleSdk(transport=lambda request: sent.extend(request["events"]) or 202)
    sdk.init(project_token="dbundle_proj_test", batch_size=1000, flush_interval=3600, before_send=hook)
    sdk._capture_policy = MINIMAL_CAPTURE_POLICY
    try:
        sdk.capture_log("admitted error", level="error")
        sdk.capture_request({"method": "GET", "path": "/failed"}, {"status_code": 500})
        sdk.capture_exception(RuntimeError("failure"))
        sdk.flush()
        assert sent == []
    finally:
        sdk.dispose()


def test_hook_keeps_admitted_info_and_promoted_failed_requests() -> None:
    sent: list[dict[str, object]] = []
    sdk = DebugBundleSdk(transport=lambda request: sent.extend(request["events"]) or 202)
    sdk.init(project_token="dbundle_proj_test", log_level="info", flush_interval=3600,
             before_send=lambda event: event)
    sdk._capture_policy = replace(MINIMAL_CAPTURE_POLICY, capture_logs="info", capture_request_events="off")
    try:
        sdk.capture_log("intentional info", level="info")
        sdk.capture_request({"method": "GET", "path": "/failed"}, {"status_code": 500})
        sdk.flush()
        assert {event["event_type"] for event in sent} == {"log_event", "request_event"}
    finally:
        sdk.dispose()


def test_hook_identity_changes_keep_each_retained_event_charged_on_retry() -> None:
    entered = threading.Event()
    release = threading.Event()
    sent: list[dict[str, object]] = []

    def transport(request: dict[str, object]) -> int:
        sent.extend(request["events"])
        entered.set()
        release.wait(5)
        return 500

    def hook(event: dict[str, object]) -> dict[str, object]:
        event["event_id"] = "36beb3e1-bf61-4c3c-94ba-42102a9c26c0"
        event["context"] = {"detail": "x" * 4096}
        return event

    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", batch_size=1000, flush_interval=3600, before_send=hook)
    try:
        for index in range(10):
            sdk.capture_log(f"event {index}", level="error")
        sender = threading.Thread(target=sdk.flush)
        sender.start()
        assert entered.wait(5)
        assert len(sent) == 10
        assert len(sdk._buffer_sizes) == len(sdk._buffer)
        assert sdk._buffer_bytes == sum(sdk._event_bytes(event) for event in sdk._buffer)
        release.set()
        sender.join(5)
        assert len(sdk._buffer_sizes) == len(sdk._buffer)
        assert sdk._buffer_bytes == sum(sdk._event_bytes(event) for event in sdk._buffer)
    finally:
        release.set()
        sdk.dispose()
