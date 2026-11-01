from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from .evidence_http import bounded_response_json, evidence_url
from .ingestion_wire import MAX_BODY_BYTES, encode_body

MAX_RETRY_AFTER_MS = 300_000


def bounded_retry_after_ms(value: object, default: int = 1_000) -> int:
    # Clamp before conversion so arbitrarily large integer hints cannot overflow a float deadline.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    return int(min(MAX_RETRY_AFTER_MS, max(0, value)))


def _parse_retry_after(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return bounded_retry_after_ms(min(300, max(0, seconds)) * 1_000)


@dataclass
class TransportResponse:
    status_code: int
    retry_after_ms: int | None = None
    body: object | None = None


class Transport(Protocol):
    def __call__(self, request: Mapping[str, object]) -> TransportResponse: ...


class HttpTransport:
    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._client = httpx.Client(timeout=5.0)

    def __call__(self, request: Mapping[str, object]) -> TransportResponse:
        return self.send_until(request, time.monotonic() + 5.0)

    def send_until(self, request: Mapping[str, object], deadline: float) -> TransportResponse:
        remaining = min(5.0, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("ingestion deadline exhausted")
        body_bytes = encode_body(request["events"])
        if len(body_bytes) > MAX_BODY_BYTES:
            raise ValueError("ingestion request exceeds byte limit")
        headers = {
            "authorization": f"Bearer {request['project_token']}",
            "content-type": "application/json",
            "accept-encoding": "identity",
        }
        with self._client.stream(
            "POST", evidence_url(self._endpoint), content=body_bytes, headers=headers, timeout=remaining
        ) as response:
            retry_after_ms = _parse_retry_after(response.headers.get("retry-after"))
            body = bounded_response_json(response, deadline)
            return TransportResponse(status_code=response.status_code, retry_after_ms=retry_after_ms, body=body)

    def close(self) -> None:
        self._client.close()


def coerce_transport_response(response: Any) -> TransportResponse:
    if isinstance(response, TransportResponse):
        return response

    status_code = getattr(response, "status_code", None)
    retry_after_ms = getattr(response, "retry_after_ms", None)
    body = getattr(response, "body", None)
    if isinstance(status_code, int):
        return TransportResponse(status_code=status_code, retry_after_ms=retry_after_ms, body=body)

    raise TypeError("Unsupported transport response")
