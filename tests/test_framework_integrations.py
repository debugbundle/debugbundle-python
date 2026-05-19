from __future__ import annotations

import logging
from pathlib import Path

import pytest

from debugbundle.core import DebugBundleSdk
from debugbundle.integrations import (
    create_django_relay_view,
    create_fastapi_relay_handler,
    create_flask_relay_handler,
)
from debugbundle.integrations.django import DebugBundleDjangoMiddleware
from debugbundle.integrations.fastapi import instrument_fastapi
from debugbundle.integrations.flask import instrument_flask
from debugbundle.relay import BrowserRelayAcceptedBatch


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, request: dict[str, object]) -> object:
        self.calls.append(request)
        return type("Response", (), {"status_code": 202, "retry_after_ms": None})()


def _event_types(transport: FakeTransport) -> list[str]:
    event_types: list[str] = []
    for call in transport.calls:
        for event in call["events"]:
            event_types.append(event["event_type"])
    return event_types


def _last_request_event(transport: FakeTransport) -> dict[str, object]:
    request_events = [
        event
        for call in transport.calls
        for event in call["events"]
        if event["event_type"] == "request_event"
    ]
    return request_events[-1]


def _last_event_of_type(transport: FakeTransport, event_type: str) -> dict[str, object]:
    matching_events = [
        event
        for call in transport.calls
        for event in call["events"]
        if event["event_type"] == event_type
    ]
    return matching_events[-1]


def _relay_batch() -> dict[str, object]:
    return {
        "batch": [
            {
                "schema_version": "2026-03-01",
                "event_id": "evt-relay-1",
                "event_type": "frontend_exception",
                "occurred_at": "2024-01-01T00:00:00Z",
                "sdk_name": "@debugbundle/sdk-browser",
                "sdk_version": "0.1.0",
                "service": {"name": "web-app", "environment": "production"},
                "payload": {"message": "relay test"},
            }
        ]
    }


def _ensure_django() -> None:
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=True,
            SECRET_KEY="debugbundle-test",
            ALLOWED_HOSTS=["*"],
            DEFAULT_CHARSET="utf-8",
        )
        import django

        django.setup()


def test_flask_integration_captures_requests_logs_and_errors() -> None:
    from flask import Flask

    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")

    app = Flask(__name__)
    instrument_flask(app, sdk=sdk)

    @app.get("/ok")
    def ok() -> str:
        app.logger.warning("flask warning")
        return "ok"

    @app.get("/boom")
    def boom() -> str:
        app.logger.warning("flask failure warning")
        raise RuntimeError("flask failure")

    client = app.test_client()
    ok_response = client.get("/ok", headers={"X-DebugBundle-Trace-Id": "trace-flask"})
    assert ok_response.status_code == 200

    boom_response = client.get("/boom", headers={"X-DebugBundle-Trace-Id": "trace-flask-error"})
    assert boom_response.status_code == 500

    sdk.flush()

    event_types = _event_types(transport)
    assert "request_event" in event_types
    assert "log_event" in event_types
    assert "backend_exception" in event_types
    request_event = _last_request_event(transport)
    assert request_event["payload"]["headers"]["x-debugbundle-trace-id"] == "trace-flask-error"
    assert request_event["correlation"]["trace_id"] == "trace-flask-error"
    assert _last_event_of_type(transport, "log_event")["correlation"]["trace_id"] == "trace-flask-error"
    assert _last_event_of_type(transport, "backend_exception")["correlation"]["trace_id"] == "trace-flask-error"


def test_fastapi_integration_captures_requests_logs_and_errors() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")

    app = FastAPI()
    instrument_fastapi(app, sdk=sdk)

    @app.get("/ok")
    def ok() -> dict[str, bool]:
        logging.getLogger("fastapi-test").warning("fastapi warning")
        return {"ok": True}

    @app.get("/boom")
    def boom() -> dict[str, bool]:
        logging.getLogger("fastapi-test").warning("fastapi failure warning")
        raise RuntimeError("fastapi failure")

    client = TestClient(app, raise_server_exceptions=False)
    ok_response = client.get("/ok", headers={"X-DebugBundle-Trace-Id": "trace-fastapi"})
    assert ok_response.status_code == 200

    boom_response = client.get("/boom", headers={"X-DebugBundle-Trace-Id": "trace-fastapi-error"})
    assert boom_response.status_code == 500

    sdk.flush()

    event_types = _event_types(transport)
    assert "request_event" in event_types
    assert "log_event" in event_types
    assert "backend_exception" in event_types
    request_event = _last_request_event(transport)
    assert request_event["payload"]["headers"]["x-debugbundle-trace-id"] == "trace-fastapi-error"
    assert request_event["correlation"]["trace_id"] == "trace-fastapi-error"
    assert _last_event_of_type(transport, "log_event")["correlation"]["trace_id"] == "trace-fastapi-error"
    assert _last_event_of_type(transport, "backend_exception")["correlation"]["trace_id"] == "trace-fastapi-error"


def test_django_middleware_captures_requests_logs_and_errors() -> None:
    _ensure_django()

    from django.http import HttpResponse
    from django.test import RequestFactory

    transport = FakeTransport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", service="checkout-api", environment="production")

    factory = RequestFactory()

    def server_error_response(request: object) -> HttpResponse:
        logging.getLogger("django-test").warning("django server warning")
        return HttpResponse("failed", status=503)

    middleware = DebugBundleDjangoMiddleware(server_error_response, sdk=sdk)
    request = factory.get("/failed", HTTP_X_DEBUGBUNDLE_TRACE_ID="trace-django")
    response = middleware(request)
    assert response.status_code == 503

    raising_middleware = DebugBundleDjangoMiddleware(
        lambda request: (_ for _ in ()).throw(RuntimeError("django failure")),
        sdk=sdk,
    )
    with pytest.raises(RuntimeError):
        raising_middleware(factory.get("/boom", HTTP_X_DEBUGBUNDLE_TRACE_ID="trace-django-error"))

    sdk.flush()

    event_types = _event_types(transport)
    assert "request_event" in event_types
    assert "log_event" in event_types
    assert "backend_exception" in event_types
    request_event = _last_request_event(transport)
    assert request_event["payload"]["headers"]["x-debugbundle-trace-id"] == "trace-django"
    assert request_event["correlation"]["trace_id"] == "trace-django"
    assert _last_event_of_type(transport, "log_event")["correlation"]["trace_id"] == "trace-django"
    assert _last_event_of_type(transport, "backend_exception")["correlation"]["trace_id"] == "trace-django-error"


def test_flask_relay_handler_registers_route_and_accepts_valid_batch() -> None:
    from flask import Flask

    accepted: list[BrowserRelayAcceptedBatch] = []
    app = Flask(__name__)
    create_flask_relay_handler(
        allowed_origins=["https://example.com"],
        on_accept=accepted.append,
    )(app)

    client = app.test_client()
    response = client.post(
        "/debugbundle/browser",
        json=_relay_batch(),
        headers={"Origin": "https://example.com"},
    )

    assert response.status_code == 202
    assert response.json == {"accepted": 1, "errors": [], "rejected": 0}
    assert accepted and len(accepted[0].events) == 1


def test_fastapi_relay_handler_registers_route_and_accepts_valid_batch() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    accepted: list[BrowserRelayAcceptedBatch] = []
    app = FastAPI()
    create_fastapi_relay_handler(
        allowed_origins=["https://example.com"],
        on_accept=accepted.append,
    )(app)

    client = TestClient(app)
    response = client.post(
        "/debugbundle/browser",
        json=_relay_batch(),
        headers={"Origin": "https://example.com"},
    )

    assert response.status_code == 202
    assert response.json() == {"accepted": 1, "errors": [], "rejected": 0}
    assert accepted and len(accepted[0].events) == 1


def test_django_relay_view_accepts_valid_batch() -> None:
    _ensure_django()

    import json

    from django.test import RequestFactory

    accepted: list[BrowserRelayAcceptedBatch] = []
    factory = RequestFactory()
    view = create_django_relay_view(
        allowed_origins=["https://example.com"],
        on_accept=accepted.append,
    )
    request = factory.post(
        "/debugbundle/browser",
        data=json.dumps(_relay_batch()),
        content_type="application/json",
        HTTP_ORIGIN="https://example.com",
    )

    response = view(request)

    assert response.status_code == 202
    assert json.loads(response.content) == {"accepted": 1, "errors": [], "rejected": 0}
    assert accepted and len(accepted[0].events) == 1


def test_flask_relay_handler_writes_local_only_event_file(tmp_path: Path) -> None:
    from flask import Flask

    app = Flask(__name__)
    create_flask_relay_handler(
        allowed_origins=["https://example.com"],
        project_mode="local-only",
        local_events_dir=str(tmp_path / "events"),
    )(app)

    client = app.test_client()
    response = client.post(
        "/debugbundle/browser",
        json=_relay_batch(),
        headers={"Origin": "https://example.com"},
    )

    assert response.status_code == 202
    assert len(list((tmp_path / "events").glob("*.events.json"))) == 1


def test_fastapi_relay_handler_writes_local_only_event_file(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    create_fastapi_relay_handler(
        allowed_origins=["https://example.com"],
        project_mode="local-only",
        local_events_dir=str(tmp_path / "events"),
    )(app)

    client = TestClient(app)
    response = client.post(
        "/debugbundle/browser",
        json=_relay_batch(),
        headers={"Origin": "https://example.com"},
    )

    assert response.status_code == 202
    assert len(list((tmp_path / "events").glob("*.events.json"))) == 1


def test_django_relay_view_writes_local_only_event_file(tmp_path: Path) -> None:
    _ensure_django()

    import json

    from django.test import RequestFactory

    factory = RequestFactory()
    view = create_django_relay_view(
        allowed_origins=["https://example.com"],
        project_mode="local-only",
        local_events_dir=str(tmp_path / "events"),
    )
    request = factory.post(
        "/debugbundle/browser",
        data=json.dumps(_relay_batch()),
        content_type="application/json",
        HTTP_ORIGIN="https://example.com",
    )

    response = view(request)

    assert response.status_code == 202
    assert len(list((tmp_path / "events").glob("*.events.json"))) == 1