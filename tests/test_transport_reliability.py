from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from debugbundle.core import DebugBundleSdk
from debugbundle.transport import HttpTransport, TransportResponse


def test_http_ack_body_is_bounded_and_closed_before_parsing() -> None:
    class Body(httpx.SyncByteStream):
        closed = False
        chunks = 0

        def __iter__(self):
            for _ in range(100):
                self.chunks += 1
                yield b"x" * 16384

        def close(self):
            self.closed = True

    body = Body()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, stream=body)

    transport = HttpTransport("https://example.invalid/events?existing=1")
    transport._client.close()
    transport._client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        result = transport({"project_token": "dbundle_proj_test", "events": []})
        assert result.body is None
        assert body.closed
        assert body.chunks <= 17
        assert requests[0].url.params["existing"] == "1"
        assert requests[0].url.params["debugbundle_evidence"] == "1"
        assert requests[0].headers["accept-encoding"] == "identity"
    finally:
        transport.close()


@pytest.mark.parametrize("hint", [float("nan"), float("inf"), "1e100", True])
def test_invalid_custom_retry_hint_uses_safe_backoff_without_losing_events(hint: object) -> None:
    now = 1_700_000_000.0
    sdk = DebugBundleSdk(
        transport=lambda _: SimpleNamespace(status_code=429, retry_after_ms=hint), time_provider=lambda: now
    )
    sdk.init(project_token="dbundle_proj_test", batch_size=100, flush_interval=3600)
    try:
        sdk.capture_message("retain", level="error")
        sdk.flush()
        assert sdk._retry_after == now + 1
        assert len(sdk._buffer) == 1
        assert sdk.last_event_at is None
    finally:
        sdk.dispose()


@pytest.mark.parametrize(
    "body",
    [
        "",
        '{"accepted":null,"rejected":2,"errors":[{"index":0,"reason":"rate_limited"},{"index":1,"reason":"rate_limited"}]}',
        '{"accepted":2,"rejected":0,"errors":null}',
        '{"accepted":1,"rejected":1,"errors":[{"index":4294967296,"reason":"rate_limited"}]}',
        '{"accepted":1,"rejected":1,"errors":{"one":{"index":1,"reason":"rate_limited"}}}',
        "<html>proxy</html>",
        "{}",
        "[]",
        "null",
        '{"accepted":1,"rejected":0,"errors":[]}',
        '{"accepted":0,"rejected":2,"errors":[{"index":0,"reason":"rate_limited"},{"index":0,"reason":"rate_limited"}]}',
        '{"accepted":1,"rejected":1,"errors":[{"index":2,"reason":"rate_limited"}]}',
    ],
)
def test_builtin_http_retains_unacknowledged_batch_and_recovers(body: str) -> None:
    batches: list[object] = []

    def respond(request: httpx.Request) -> httpx.Response:
        import json

        batches.append(json.loads(request.content)["events"])
        return httpx.Response(
            202,
            text=body if len(batches) == 1 else '{"accepted":2,"rejected":0,"errors":[]}',
            headers={"Retry-After": "300"},
        )

    transport = HttpTransport("https://example.invalid/events")
    transport._client.close()
    transport._client = httpx.Client(transport=httpx.MockTransport(respond))
    now = [1_700_000_000.0]
    sdk = DebugBundleSdk(transport=transport, time_provider=lambda: now[0])
    sdk.init(project_token="dbundle_proj_test", batch_size=100, flush_interval=3600)
    try:
        sdk.capture_message("first", level="error")
        sdk.capture_message("second", level="error")
        sdk.flush()
        assert sdk.last_event_at is None
        assert len(sdk._buffer) == 2
        sdk.flush()
        assert len(batches) == 1
        now[0] += 301
        sdk.flush()
        assert len(batches) == 2 and batches[0] == batches[1]
        assert sdk.last_event_at is not None
    finally:
        sdk.dispose()
        transport.close()


@pytest.mark.parametrize(
    "header,expected",
    [
        ("0.25", 250),
        ("999999", 300_000),
        ("1e300", 300_000),
        ("-1", 0),
        ("NaN", None),
        ("Infinity", None),
        ("nope", None),
        (format_datetime(datetime.now(timezone.utc) + timedelta(days=1), usegmt=True), 300_000),
        ("Sun, 06 Nov 1994 08:49:37 GMT", 0),
    ],
)
def test_http_retry_after_is_bounded(header: str, expected: int | None) -> None:
    transport = HttpTransport("https://example.invalid/events")
    transport._client.close()
    transport._client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": header}))
    )
    try:
        assert transport({"project_token": "dbundle_proj_test", "events": []}).retry_after_ms == expected
    finally:
        transport.close()


@pytest.mark.parametrize(
    "body,status",
    [
        (None, 429),
        (None, 503),
        ({}, 202),
        ({"accepted": 0, "rejected": 1, "errors": [{"index": 0, "reason": "rate_limited"}]}, 202),
    ],
)
def test_custom_retry_hint_cannot_suppress_delivery_beyond_five_minutes(body: object, status: int) -> None:
    calls: list[object] = []
    now = [1_700_000_000.0]

    def send(request: object) -> TransportResponse:
        calls.append(request)
        now[0] += 10  # The delay starts when the response arrives, not when sending began.
        return TransportResponse(status, 10**100, body)

    # An empty custom ACK is explicitly legacy-compatible; use malformed counts for protocol failure.
    if body == {}:
        body = {"accepted": 2, "rejected": 0, "errors": []}
    sdk = DebugBundleSdk(transport=send, time_provider=lambda: now[0])
    sdk.init(project_token="dbundle_proj_test", batch_size=100, flush_interval=3600)
    try:
        sdk.capture_message("retry", level="error")
        sdk.flush()
        assert sdk._retry_after == now[0] + 300
        now[0] += 299
        sdk.flush()
        assert len(calls) == 1
        now[0] += 2
        sdk.flush()
        assert len(calls) == 2
    finally:
        sdk.dispose()


def test_native_sender_keeps_byte_bounded_ordered_batches_and_owned_hook_tails() -> None:
    import json
    import time

    batches = []
    hook_calls = []

    def respond(request):
        assert len(request.content) <= 256 * 1024
        batch = json.loads(request.content)["events"]
        batches.append(batch)
        return httpx.Response(202, json={"accepted": len(batch), "rejected": 0, "errors": []})

    def hook(event):
        hook_calls.append(event["event_id"])
        event["context"] = {"marker": "app redacted"}
        return event

    transport = HttpTransport("https://example.invalid/events")
    transport._client.close()
    transport._client = httpx.Client(transport=httpx.MockTransport(respond))
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", batch_size=1000, flush_interval=3600, before_send=hook)
    try:
        for index in range(25):
            sdk.capture_log("x" * 16000 + str(index), level="error")
        expected = [event["event_id"] for event in sdk._buffer]
        sdk.flush()
        deadline = time.monotonic() + 3
        while sdk._buffer and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(batches) > 1
        assert [event["event_id"] for batch in batches for event in batch] == expected
        assert hook_calls == expected
        assert sdk._buffer_bytes == 0 and not sdk._buffer_sizes
    finally:
        sdk.dispose()
        transport.close()


def test_native_sender_withholds_unfit_valid_hook_without_restoring_removed_primary() -> None:
    requests = []
    transport = HttpTransport("https://example.invalid/events")
    transport._client.close()
    transport._client = httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request)))

    def hook(event):
        event["payload"]["message"] = "safe"
        event["payload"]["attributes"] = {f"part_{i}": "x" * 16000 for i in range(10)}
        event["context"] = {f"part_{i}": "x" * 16000 for i in range(10)}
        return event

    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", batch_size=1000, flush_interval=3600, before_send=hook)
    try:
        sdk.capture_log("private original", level="error")
        sdk.flush()
        assert not requests
        assert not sdk._buffer and sdk._buffer_bytes == 0
    finally:
        sdk.dispose()
        transport.close()


def test_native_wire_reduces_only_added_evidence_preserving_latest_primary_and_custom_reference() -> None:
    import uuid

    from debugbundle.exception_evidence import snapshot_exception
    from debugbundle.ingestion_wire import MAX_EVENT_BYTES, event_bytes, fit_event

    primary = {"event_type": "backend_exception", "payload": {"message": "safe primary"}, "context": {"detail": ""}}
    for allowance in (16, 512):
        primary["context"]["detail"] = "x" * (
            MAX_EVENT_BYTES - event_bytes({**primary, "context": {"detail": ""}}) - allowance
        )
        graph = snapshot_exception(RuntimeError("cause" * 400))
        enriched = {**primary, "payload": {**primary["payload"], "exception_evidence": graph}}
        assert event_bytes(enriched) > MAX_EVENT_BYTES
        reduced = fit_event(enriched)
        assert reduced is not None and event_bytes(reduced) <= MAX_EVENT_BYTES
        assert reduced["payload"] == primary["payload"]
        assert reduced["context"]["detail"] == primary["context"]["detail"]
        assert ("evidence_limits" in reduced["context"]) == (allowance == 512)

    reference = {
        "version": 1,
        "failure_ref": str(uuid.uuid4()),
        "scope": {"kind": "operation", "operation_id": str(uuid.uuid4())},
    }
    base = {"event_type": "log_event", "context": {"detail": "", "evidence_reference": reference}}
    base["context"]["detail"] = "x" * (MAX_EVENT_BYTES - event_bytes(base) + 1)
    reduced = fit_event(base)
    assert reduced is not None and "evidence_reference" not in reduced["context"]
    base["context"]["evidence_reference"]["version"] = 99
    assert fit_event(base) is None
    assert fit_event(None) is None


def test_native_wire_rejects_unfit_single_events_before_any_request() -> None:
    from debugbundle.ingestion_wire import MAX_BODY_BYTES, chunks, prefix

    with pytest.raises(ValueError, match="exceeds ingestion"):
        chunks([{"context": {"detail": "x" * MAX_BODY_BYTES}}])
    assert prefix([]) == []
    assert chunks([]) == []
