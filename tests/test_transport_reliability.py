from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from debugbundle.core import DebugBundleSdk
from debugbundle.transport import HttpTransport, TransportResponse


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
