from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass
class TransportResponse:
    status_code: int
    retry_after_ms: int | None = None
    body: object | None = None


class Transport(Protocol):
    def __call__(self, request: Mapping[str, object]) -> TransportResponse:
        ...


class HttpTransport:
    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._client = httpx.Client(timeout=5.0)

    def __call__(self, request: Mapping[str, object]) -> TransportResponse:
        headers = {
            "authorization": f"Bearer {request['project_token']}",
            "content-type": "application/json",
        }
        response = self._client.post(
            self._endpoint,
            json={"events": request["events"]},
            headers=headers,
        )
        retry_after_header = response.headers.get("retry-after")
        retry_after_ms = None
        if retry_after_header is not None:
            try:
                retry_after_ms = int(float(retry_after_header) * 1000)
            except ValueError:
                retry_after_ms = None

        try:
            body: object | None = response.json()
        except (ValueError, TypeError):
            body = None
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
