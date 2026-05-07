from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from debugbundle.core import DebugBundleSdk
from debugbundle.transport import HttpTransport


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: dict[str, object]


@dataclass
class PlannedResponse:
    status_code: int
    body: dict[str, object] | None = None
    headers: dict[str, str] | None = None


class MockIngestionServer:
    def __init__(self, responses: list[PlannedResponse]) -> None:
        self.requests: list[RecordedRequest] = []
        self._responses = deque(responses)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1/events"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                content_length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
                parsed_body = json.loads(raw_body)
                outer.requests.append(
                    RecordedRequest(
                        method="POST",
                        path=self.path,
                        headers={key.lower(): value for key, value in self.headers.items()},
                        body=parsed_body,
                    )
                )

                response = outer._responses[0] if len(outer._responses) == 1 else outer._responses.popleft()
                self.send_response(response.status_code)
                for key, value in (response.headers or {}).items():
                    self.send_header(key, value)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response.body or {}).encode("utf-8"))

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

        return Handler


def test_sdk_flush_posts_real_http_batch_to_mock_ingestion_server() -> None:
    server = MockIngestionServer([PlannedResponse(status_code=202, body={"accepted": True})])
    server.start()

    try:
        sdk = DebugBundleSdk()
        sdk.init(
            project_token="dbundle_proj_test",
            service="checkout-api",
            environment="production",
            endpoint=server.endpoint,
        )

        sdk.capture_message("integration warning", level="warning")
        sdk.capture_request(
            {"method": "GET", "path": "/checkout", "headers": {"x-request-id": "req_http"}, "query": {}},
            {"status_code": 502, "duration_ms": 31},
        )
        sdk.flush()
        sdk.dispose()
    finally:
        server.close()

    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.method == "POST"
    assert request.path == "/v1/events"
    assert request.headers["authorization"] == "Bearer dbundle_proj_test"
    assert request.headers["content-type"] == "application/json"
    events = request.body["events"]
    assert isinstance(events, list)
    assert [event["event_type"] for event in events] == ["log_event", "request_event"]
    assert events[0]["payload"]["message"] == "integration warning"
    assert events[1]["payload"]["path"] == "/checkout"


def test_http_transport_parses_retry_after_from_real_http_response() -> None:
    server = MockIngestionServer(
        [PlannedResponse(status_code=429, body={"error": "rate_limited"}, headers={"Retry-After": "0.25"})]
    )
    server.start()

    try:
        transport = HttpTransport(server.endpoint)
        response = transport(
            {
                "project_token": "dbundle_proj_test",
                "events": [
                    {
                        "schema_version": "2026-03-01",
                        "event_id": "00000000-0000-4000-8000-000000000999",
                        "event_type": "log_event",
                        "sdk_name": "debugbundle-python",
                        "sdk_version": "0.1.0",
                        "sdk_language": "python",
                        "occurred_at": "2026-03-30T00:00:00.000Z",
                        "service": {
                            "name": "checkout-api",
                            "runtime": "python",
                            "framework": None,
                            "environment": "production",
                        },
                        "correlation": {
                            "request_id": None,
                            "trace_id": None,
                            "session_id": None,
                            "user_id_hash": None,
                        },
                        "payload": {
                            "level": "error",
                            "message": "transport integration",
                            "attributes": {},
                        },
                    }
                ],
            }
        )
        transport.close()
    finally:
        server.close()

    assert response.status_code == 429
    assert response.retry_after_ms == 250
    assert len(server.requests) == 1
    assert server.requests[0].headers["authorization"] == "Bearer dbundle_proj_test"