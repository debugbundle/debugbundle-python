from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = REPO_ROOT / "tests" / "fixtures" / "event-envelope.schema.json"
PROJECT_TOKEN = "dbundle_proj_smoke"
SERVER_TRACE_ID = "trace-smoke-server"
SERVER_REQUEST_ID = "req-smoke-server"
RELAY_TRACE_ID = "trace-smoke-relay"
RELAY_REQUEST_ID = "req-smoke-relay"
PUBLISHED_INSTALL_ATTEMPTS = 12
PUBLISHED_INSTALL_RETRY_SECONDS = 10


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: dict[str, Any]


class MockIngestionServer:
    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._build_handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

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
                self.send_response(202)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"accepted": true}')

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

        return Handler


def _run_subprocess(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _install_with_retry(command: list[str], retries: int, retry_delay_seconds: int) -> None:
    attempts = 0

    while True:
        attempts += 1
        try:
            _run_subprocess(command)
            return
        except subprocess.CalledProcessError:
            if attempts >= retries:
                raise

            print(
                "Published package not available yet; retrying in "
                f"{retry_delay_seconds}s (attempt {attempts}/{retries}).",
                file=sys.stderr,
            )
            time.sleep(retry_delay_seconds)


def _bootstrap_clean_install(install_target: str, schema_path: Path, *, published_package: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="debugbundle-python-smoke-") as temp_dir:
        temp_path = Path(temp_dir)
        venv_dir = temp_path / "venv"
        python_bin = venv_dir / "bin" / "python"

        _run_subprocess([sys.executable, "-m", "venv", str(venv_dir)])
        _run_subprocess([str(python_bin), "-m", "pip", "install", "--upgrade", "pip"])
        install_command = [
            str(python_bin),
            "-m",
            "pip",
            "install",
            install_target,
            "django>=5,<6",
            "fastapi>=0.115,<1",
            "flask>=3,<4",
            "jsonschema>=4.23,<5",
        ]
        if published_package:
            _install_with_retry(
                install_command,
                retries=PUBLISHED_INSTALL_ATTEMPTS,
                retry_delay_seconds=PUBLISHED_INSTALL_RETRY_SECONDS,
            )
        else:
            _run_subprocess(install_command)
        _run_subprocess(
            [
                str(python_bin),
                str(Path(__file__).resolve()),
                "--installed",
                "--schema",
                str(schema_path),
            ]
        )


def _find_event(requests: list[RecordedRequest], predicate: Any) -> tuple[RecordedRequest, dict[str, Any]]:
    for request in requests:
        events = request.body.get("events")
        if not isinstance(events, list):
            continue
        for event in events:
            if isinstance(event, dict) and predicate(event):
                return request, event
    raise AssertionError("Expected smoke event was not delivered.")


def _run_installed_smoke(schema_path: Path) -> None:
    from flask import Flask, jsonify
    from jsonschema import Draft202012Validator

    import debugbundle
    from debugbundle import create_flask_relay_handler, instrument_flask

    server = MockIngestionServer()
    server.start()

    debugbundle.init(
        project_token=PROJECT_TOKEN,
        service="checkout-api",
        environment="production",
        endpoint=server.endpoint,
        log_level="warning",
    )

    app = Flask(__name__)
    app.config["TESTING"] = True
    instrument_flask(app)
    create_flask_relay_handler(
        allowed_origins=["https://app.example.com"],
        project_mode="connected",
        project_token=PROJECT_TOKEN,
        endpoint=server.endpoint,
    )(app)

    @app.get("/smoke")
    def smoke() -> Any:
        debugbundle.capture_message(
            "python app-driven smoke message",
            level="error",
            context={"feature": "app-driven-smoke"},
        )
        return jsonify(ok=False, smoke=True), 503

    client = app.test_client()
    try:
        response = client.get(
            "/smoke",
            headers={
                "X-DebugBundle-Trace-Id": SERVER_TRACE_ID,
                "X-Request-Id": SERVER_REQUEST_ID,
            },
        )
        if response.status_code != 503:
            raise AssertionError(f"Unexpected smoke route status: {response.status_code}")

        relay_response = client.post(
            "/debugbundle/browser",
            json={
                "batch": [
                    {
                        "schema_version": "2026-03-01",
                        "event_id": "00000000-0000-4000-8000-000000000321",
                        "event_type": "frontend_exception",
                        "occurred_at": "2026-05-25T00:00:00Z",
                        "sdk_name": "spoofed-browser-sdk",
                        "sdk_version": "0.1.0",
                        "service": {"name": "checkout-web", "environment": "production"},
                        "correlation": {
                            "trace_id": RELAY_TRACE_ID,
                            "request_id": RELAY_REQUEST_ID,
                            "session_id": "sess-smoke",
                            "user_id_hash": "user-smoke",
                        },
                        "project_token": "browser-smuggled-token",
                        "organization_id": "org-smuggled",
                        "payload": {"message": "relay smoke"},
                    }
                ]
            },
            headers={
                "Origin": "https://app.example.com",
                "Authorization": "Bearer browser-smuggled-token",
                "Cookie": "session=browser-smuggled-cookie",
            },
        )
        if relay_response.status_code != 202:
            raise AssertionError(f"Unexpected relay status: {relay_response.status_code}")

        debugbundle.flush()
    finally:
        server.close()

    if len(server.requests) < 2:
        raise AssertionError(f"Expected at least 2 ingestion requests, got {len(server.requests)}")

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    sdk_version = version("debugbundle-python")

    backend_request, log_event = _find_event(
        server.requests,
        lambda event: event.get("event_type") == "log_event"
        and event.get("payload", {}).get("message") == "python app-driven smoke message",
    )
    if backend_request.path != "/v1/events":
        raise AssertionError(f"Unexpected backend request path: {backend_request.path}")
    if backend_request.headers.get("authorization") != f"Bearer {PROJECT_TOKEN}":
        raise AssertionError("Backend capture did not use the server-side project token.")

    request_event = next(
        event
        for event in backend_request.body["events"]
        if isinstance(event, dict) and event.get("event_type") == "request_event"
    )
    for event in (log_event, request_event):
        errors = sorted(validator.iter_errors(event), key=lambda error: list(error.path))
        if errors:
            raise AssertionError(errors[0].message)

    if log_event["sdk_name"] != "debugbundle-python" or log_event["sdk_version"] != sdk_version:
        raise AssertionError("Backend log event metadata did not match the installed package.")
    if log_event["service"]["name"] != "checkout-api" or log_event["service"]["environment"] != "production":
        raise AssertionError("Backend log event service metadata was incorrect.")
    if log_event["correlation"]["trace_id"] != SERVER_TRACE_ID:
        raise AssertionError("Backend log event trace correlation was not preserved.")
    if log_event["correlation"]["request_id"] != SERVER_REQUEST_ID:
        raise AssertionError("Backend log event request correlation was not preserved.")

    if request_event["payload"]["path"] != "/smoke" or request_event["payload"]["response_status"] != 503:
        raise AssertionError("Framework request capture did not record the smoke route response.")
    if request_event["correlation"]["trace_id"] != SERVER_TRACE_ID:
        raise AssertionError("Framework request capture lost the trace identifier.")
    if request_event["correlation"]["request_id"] != SERVER_REQUEST_ID:
        raise AssertionError("Framework request capture lost the request identifier.")

    relay_request, relay_event = _find_event(
        server.requests,
        lambda event: event.get("event_type") == "frontend_exception"
        and event.get("sdk_name") == "@debugbundle/sdk-browser",
    )
    if relay_request.path != "/v1/events":
        raise AssertionError(f"Unexpected relay request path: {relay_request.path}")
    if relay_request.headers.get("authorization") != f"Bearer {PROJECT_TOKEN}":
        raise AssertionError("Relay forwarding did not use the server-side project token.")
    if relay_event.get("project_token") != PROJECT_TOKEN:
        raise AssertionError("Relay forwarding did not replace the browser-supplied project token.")
    if "organization_id" in relay_event:
        raise AssertionError("Relay forwarding preserved a browser-supplied organization identifier.")
    if relay_event["service"]["name"] != "checkout-web" or relay_event["service"]["environment"] != "production":
        raise AssertionError("Relay forwarding did not preserve browser service identity.")
    if relay_event["correlation"]["trace_id"] != RELAY_TRACE_ID:
        raise AssertionError("Relay forwarding lost the browser trace identifier.")
    if relay_event["correlation"]["request_id"] != RELAY_REQUEST_ID:
        raise AssertionError("Relay forwarding lost the browser request identifier.")

    print("Python app-driven smoke passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DebugBundle Python app-driven smoke path.")
    parser.add_argument("--wheel", type=Path, help="Built wheel to install into a clean virtualenv.")
    parser.add_argument("--package", help="Published package spec to install into a clean virtualenv.")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    parser.add_argument("--installed", action="store_true", help="Run the installed-package smoke assertions.")
    args = parser.parse_args()

    if args.installed:
        _run_installed_smoke(args.schema)
        return

    if args.wheel is not None and args.package is not None:
        raise SystemExit("Specify only one of --wheel or --package.")

    if args.wheel is None and args.package is None:
        raise SystemExit("--wheel or --package is required unless --installed is set.")

    if args.wheel is not None:
        wheel_path = args.wheel.resolve()
        if not wheel_path.is_file():
            raise SystemExit(f"Wheel not found: {wheel_path}")
        install_target = str(wheel_path)
        published_package = False
    else:
        install_target = args.package or ""
        published_package = True

    _bootstrap_clean_install(install_target, args.schema.resolve(), published_package=published_package)


if __name__ == "__main__":
    main()
