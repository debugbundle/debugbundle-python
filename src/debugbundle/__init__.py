"""DebugBundle Python SDK."""

import asyncio
import importlib
import logging
from collections.abc import Callable, Mapping
from contextvars import Token
from typing import Any

from .config import RemoteProbeDirective
from .core import ConfigFetchResponse, DebugBundleSdk
from .relay import BrowserRelayAcceptedBatch, BrowserRelayHandler, BrowserRelayResponse

_sdk = DebugBundleSdk()


def init(
    project_token: str,
    environment: str | None = None,
    service: str | None = None,
    enabled: bool = True,
    redact_fields: list[str] | None = None,
    sample_rate: float = 1.0,
    batch_size: int = 25,
    flush_interval: float = 5.0,
    endpoint: str = "https://api.debugbundle.com/v1/events",
    log_level: str = "warning",
    max_probe_labels: int = 50,
    max_probe_entries_per_label: int = 10,
    probe_flush_on_error: bool = True,
    fetch_impl: Callable[[str, dict[str, object]], ConfigFetchResponse] | None = None,
    on_diagnostic: Callable[[dict[str, object]], None] | None = None,
    probes_poll_interval: int = 60_000,
) -> None:
    _sdk.init(
        project_token=project_token,
        environment=environment,
        service=service,
        enabled=enabled,
        redact_fields=redact_fields,
        sample_rate=sample_rate,
        batch_size=batch_size,
        flush_interval=flush_interval,
        endpoint=endpoint,
        log_level=log_level,
        max_probe_labels=max_probe_labels,
        max_probe_entries_per_label=max_probe_entries_per_label,
        probe_flush_on_error=probe_flush_on_error,
        fetch_impl=fetch_impl,
        on_diagnostic=on_diagnostic,
        probes_poll_interval=probes_poll_interval,
    )


def capture_exception(error: BaseException, context: dict[str, object] | None = None) -> None:
    _sdk.capture_exception(error, context=context)


def capture_error(error: BaseException, context: dict[str, object] | None = None) -> None:
    _sdk.capture_error(error, context=context)


def capture_log(message: str, level: str = "warning", context: dict[str, object] | None = None) -> None:
    _sdk.capture_log(message, level=level, context=context)


def capture_request(
    request: Mapping[str, object],
    response: Mapping[str, object] | None = None,
    context: Mapping[str, object] | None = None,
) -> None:
    _sdk.capture_request(request, response=response, context=context)


def capture_message(message: str, level: str | None = None, context: dict[str, object] | None = None) -> None:
    _sdk.capture_message(message, level=level, context=context)


def set_context(key: str, value: object) -> None:
    _sdk.set_context(key, value)


def flush() -> None:
    _sdk.flush()


def get_status() -> str:
    return _sdk.status


def get_last_event_at() -> float | None:
    return _sdk.last_event_at


def probe(label: str, data: object | Callable[[], object], opts: Mapping[str, object] | None = None) -> None:
    _sdk.probe(label, data, opts=opts)


def capture_exceptions() -> None:
    _sdk.capture_exceptions()


def capture_logging(logger: logging.Logger | None = None) -> None:
    _sdk.capture_logging(logger=logger)


def capture_async(loop: asyncio.AbstractEventLoop | None = None) -> None:
    _sdk.capture_async(loop=loop)


def begin_request(request: dict) -> object:
    return _sdk.begin_request(request)


def end_request(token: Token[list[RemoteProbeDirective] | None]) -> None:
    _sdk.end_request(token)


_OPTIONAL_EXPORTS = {
    "DebugBundleDjangoMiddleware": (".integrations.django", "DebugBundleDjangoMiddleware"),
    "DebugBundleFastAPIMiddleware": (".integrations.fastapi", "DebugBundleFastAPIMiddleware"),
    "create_django_relay_view": (".integrations.relay_django", "create_django_relay_view"),
    "create_fastapi_relay_handler": (".integrations.relay_fastapi", "create_fastapi_relay_handler"),
    "create_flask_relay_handler": (".integrations.relay_flask", "create_flask_relay_handler"),
    "instrument_fastapi": (".integrations.fastapi", "instrument_fastapi"),
    "instrument_flask": (".integrations.flask", "instrument_flask"),
}


def __getattr__(name: str) -> Any:
    if name not in _OPTIONAL_EXPORTS:
        raise AttributeError(f"module 'debugbundle' has no attribute {name!r}")

    module_name, attribute_name = _OPTIONAL_EXPORTS[name]
    module = importlib.import_module(module_name, __name__)
    return getattr(module, attribute_name)


__all__ = [
    "BrowserRelayAcceptedBatch",
    "BrowserRelayHandler",
    "BrowserRelayResponse",
    "DebugBundleSdk",
    "DebugBundleDjangoMiddleware",
    "DebugBundleFastAPIMiddleware",
    "begin_request",
    "capture_async",
    "capture_error",
    "capture_exception",
    "capture_exceptions",
    "capture_log",
    "capture_logging",
    "capture_message",
    "capture_request",
    "create_django_relay_view",
    "create_fastapi_relay_handler",
    "create_flask_relay_handler",
    "end_request",
    "flush",
    "init",
    "instrument_fastapi",
    "instrument_flask",
    "probe",
    "set_context",
]
