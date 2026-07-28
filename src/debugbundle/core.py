from __future__ import annotations

import asyncio
import logging
import sys
import threading
import traceback
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from random import random
from typing import Any, Protocol, cast

from .acknowledgement import decide_acknowledgement
from .before_send import BeforeSendHook, apply_before_send
from .config import (
    BALANCED_CAPTURE_POLICY,
    DEFAULT_PROBES_POLL_INTERVAL_MS,
    MINIMAL_CAPTURE_POLICY,
    CapturePolicy,
    RemoteConfigSnapshot,
    RemoteProbeDirective,
    find_matching_remote_probe_directives,
    parse_remote_config,
)
from .event_support import (
    DEFAULT_LOG_LEVEL,
    LEVEL_RANKS,
    backend_exception_request_payload,
    backend_exception_response_payload,
    correlation_payload,
    event_context,
    is_immediate_request_incident_status,
    iso_now,
    level_enabled,
    normalize_level,
    redact_mapping,
    request_event_payload,
    runtime_process_facts,
    sdk_config_endpoint,
    sdk_version,
    serialize_error,
    time_now,
)
from .logger_integrations import attach_optional_integrations
from .redaction import DEFAULT_REDACT_FIELDS, redact_value
from .suppression import EventSuppressionTracker
from .transport import HttpTransport, Transport, coerce_transport_response
from .trigger_token import resolve_request_trigger_directives

DEFAULT_BATCH_SIZE = 25
DEFAULT_FLUSH_INTERVAL = 5.0
DEFAULT_ENDPOINT = "https://api.debugbundle.com/v1/events"
SCHEMA_VERSION = "2026-03-01"


@dataclass
class ProbeEntry:
    label: str
    data: dict[str, object]
    timestamp: str


class ConfigFetchResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> object: ...


class DebugBundleLogHandler(logging.Handler):
    def __init__(self, sdk: DebugBundleSdk) -> None:
        super().__init__()
        self._sdk = sdk

    def emit(self, record: logging.LogRecord) -> None:
        self._sdk.capture_log(
            record.getMessage(),
            level=record.levelname.lower(),
            context={
                "logger_name": record.name,
                "pathname": record.pathname,
                "lineno": record.lineno,
            },
        )


class DebugBundleSdk:
    def __init__(
        self,
        transport: Transport | None = None,
        time_provider: Callable[[], float] | None = None,
    ) -> None:
        self._transport_override = transport
        self._time_provider = time_provider or time_now
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._remote_config_timer: threading.Timer | None = None
        self._transport: Transport | None = None
        self._http_transport: HttpTransport | None = None
        self._enabled = False
        self._project_token = ""
        self._service = "python-service"
        self._environment = "development"
        self._endpoint = DEFAULT_ENDPOINT
        self._batch_size = DEFAULT_BATCH_SIZE
        self._flush_interval = DEFAULT_FLUSH_INTERVAL
        self._log_level = DEFAULT_LOG_LEVEL
        self._sample_rate = 1.0
        self._redact_fields = set(DEFAULT_REDACT_FIELDS)
        self._buffer: list[dict[str, object]] = []
        self._context: dict[str, object] = {}
        self._scoped_context: ContextVar[dict[str, object] | None] = ContextVar(
            "debugbundle_scoped_context",
            default=None,
        )
        self._suppression = EventSuppressionTracker()
        self._retry_after = 0.0
        self._last_event_at: float | None = None
        self._consecutive_failures = 0
        self._max_probe_labels = 50
        self._max_probe_entries_per_label = 10
        self._probe_buffers: dict[str, deque[ProbeEntry]] = {}
        self._logging_bindings: dict[int, tuple[logging.Logger, DebugBundleLogHandler]] = {}
        self._optional_logging_restorers: list[Callable[[], None]] = []
        self._original_excepthook: Any = None
        self._async_handlers: dict[asyncio.AbstractEventLoop, Any] = {}
        self._fetch_impl: Callable[[str, dict[str, object]], ConfigFetchResponse] | None = None
        self._on_diagnostic: Callable[[dict[str, object]], None] | None = None
        self._before_send: BeforeSendHook | None = None
        self._configured_probes_poll_interval_ms = DEFAULT_PROBES_POLL_INTERVAL_MS
        self._remote_config_etag: str | None = None
        self._remote_config_snapshot: RemoteConfigSnapshot | None = None
        self._capture_policy: CapturePolicy = BALANCED_CAPTURE_POLICY
        self._request_trigger_directives: ContextVar[list[RemoteProbeDirective] | None] = ContextVar(
            "debugbundle_request_trigger_directives",
            default=None,
        )

    @property
    def status(self) -> str:
        with self._lock:
            if not self._enabled:
                return "disconnected"
            if self._consecutive_failures >= 3:
                return "disconnected"
            if self._retry_after > 0.0 and self._time_provider() < self._retry_after:
                return "degraded"
            return "healthy"

    @property
    def last_event_at(self) -> float | None:
        with self._lock:
            return self._last_event_at

    def init(
        self,
        project_token: str,
        environment: str | None = None,
        service: str | None = None,
        enabled: bool = True,
        redact_fields: list[str] | None = None,
        sample_rate: float = 1.0,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
        endpoint: str = DEFAULT_ENDPOINT,
        log_level: str = DEFAULT_LOG_LEVEL,
        max_probe_labels: int = 50,
        max_probe_entries_per_label: int = 10,
        probe_flush_on_error: bool = True,
        fetch_impl: Callable[[str, dict[str, object]], ConfigFetchResponse] | None = None,
        on_diagnostic: Callable[[dict[str, object]], None] | None = None,
        before_send: BeforeSendHook | None = None,
        probes_poll_interval: int = DEFAULT_PROBES_POLL_INTERVAL_MS,
    ) -> None:
        with self._lock:
            self.dispose()
            self._project_token = project_token.strip()
            self._service = service or "python-service"
            self._environment = environment or "development"
            self._enabled = enabled and len(self._project_token) > 0
            self._endpoint = endpoint
            self._batch_size = max(1, batch_size)
            self._flush_interval = max(0.1, flush_interval)
            self._log_level = normalize_level(log_level)
            self._sample_rate = min(max(sample_rate, 0.0), 1.0)
            self._redact_fields = set(DEFAULT_REDACT_FIELDS)
            if redact_fields:
                self._redact_fields.update(field.lower() for field in redact_fields)
            self._max_probe_labels = max(1, max_probe_labels)
            self._max_probe_entries_per_label = max(1, max_probe_entries_per_label)
            self._probe_flush_on_error = probe_flush_on_error
            self._buffer = []
            self._context = {}
            self._probe_buffers = {}
            self._suppression = EventSuppressionTracker()
            self._retry_after = 0.0
            self._last_event_at = None
            self._consecutive_failures = 0
            self._fetch_impl = fetch_impl
            self._on_diagnostic = on_diagnostic
            self._before_send = before_send
            self._configured_probes_poll_interval_ms = max(1, int(probes_poll_interval))
            self._remote_config_etag = None
            self._remote_config_snapshot = None
            self._capture_policy = BALANCED_CAPTURE_POLICY
            self._transport = self._transport_override
            if self._transport is None and self._enabled:
                self._http_transport = HttpTransport(self._endpoint)
                self._transport = self._http_transport
            self.capture_exceptions()
            if self._enabled and self._fetch_impl is not None:
                self._refresh_remote_config(initial=True)

    def capture_exception(self, error: BaseException, context: Mapping[str, object] | None = None) -> None:
        self._capture_exception(error, context=context, handled=True)

    def _capture_exception(
        self,
        error: BaseException,
        context: Mapping[str, object] | None = None,
        handled: bool = True,
    ) -> None:
        with self._lock:
            if not self._enabled:
                return

            redacted_context = redact_mapping(dict(context or {}), self._redact_fields)
            request_payload = backend_exception_request_payload(redacted_context.get("request"))
            response_payload = backend_exception_response_payload(redacted_context.get("response"))

            payload: dict[str, object] = {
                "name": type(error).__name__,
                "message": str(error),
                "stack": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
                "handled": handled,
                "request": request_payload,
                "response": response_payload,
                "runtime": runtime_process_facts(),
            }
            if self._probe_flush_on_error:
                probe_data = self._build_probe_data()
                if probe_data is not None:
                    payload["probe_data"] = probe_data

            event = self._apply_before_send_event(
                self._base_event("backend_exception", payload, context=redacted_context)
            )
            if event is None or not self._passes_sample_rate():
                return
            event_payload = cast(dict[str, object], event["payload"])
            suppression_key = (
                f"{event['event_type']}:{event_payload.get('name', '')}:"
                f"{event_payload.get('message', '')}:{event_payload.get('stack', '')}"
            )
            if not self._suppression.should_capture(suppression_key, self._time_provider()):
                return
            self._enqueue_event(event)

    def capture_error(self, error: BaseException, context: Mapping[str, object] | None = None) -> None:
        self.capture_exception(error, context=context)

    def capture_log(
        self,
        message: str,
        level: str = DEFAULT_LOG_LEVEL,
        context: Mapping[str, object] | None = None,
    ) -> None:
        normalized_level = normalize_level(level)
        with self._lock:
            if not self._enabled:
                return
            payload: dict[str, object] = {
                "message": message,
                "level": normalized_level,
                "attributes": {},
            }
            if context:
                payload["attributes"] = redact_mapping(dict(context), self._redact_fields)
            event = self._apply_before_send_event(self._base_event("log_event", payload, context=context))
            if (
                event is None
                or not self._passes_sample_rate()
                or self._capture_policy.capture_logs == "off"
                or not level_enabled(normalized_level, self._effective_log_threshold())
            ):
                return
            self._enqueue_event(event)

    def capture_request(
        self,
        request: Mapping[str, object],
        response: Mapping[str, object] | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        with self._lock:
            if not self._enabled:
                return
            payload = request_event_payload(
                redact_mapping(dict(request), self._redact_fields),
                redact_mapping(dict(response or {}), self._redact_fields),
                redact_mapping(dict(context or {}), self._redact_fields),
            )
            event = self._apply_before_send_event(self._base_event("request_event", payload, context=context))
            if (
                event is None
                or not self._passes_sample_rate()
                or not self._should_capture_request_event(request, response)
            ):
                return
            self._enqueue_event(event)

    def capture_message(
        self,
        message: str,
        level: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        self.capture_log(message, level=level or DEFAULT_LOG_LEVEL, context=context)

    def set_context(self, key: str, value: object) -> None:
        with self._lock:
            self._context[key] = redact_value({key: value}, self._redact_fields)[key]

    def _bind_scoped_context(self, context: Mapping[str, object]) -> Token[dict[str, object] | None]:
        scoped_context = dict(self._scoped_context.get() or {})
        for key, value in context.items():
            if value is None:
                continue
            scoped_context[str(key)] = value
        return self._scoped_context.set(cast(dict[str, object], redact_value(scoped_context, self._redact_fields)))

    def _reset_scoped_context(self, token: Token[dict[str, object] | None]) -> None:
        self._scoped_context.reset(token)

    def flush(self) -> None:
        with self._lock:
            if not self._enabled or self._transport is None:
                return

            self._append_suppression_aggregates()
            if not self._buffer:
                return

            now = self._time_provider()
            if now < self._retry_after:
                return

            batch = [dict(event) for event in self._buffer]
            request = {
                "project_token": self._project_token,
                "events": batch,
            }

            try:
                response = coerce_transport_response(self._transport(request))
            except Exception:
                self._consecutive_failures += 1
                self._schedule_flush_locked()
                return

            if 200 <= response.status_code < 300:
                acknowledgement = decide_acknowledgement(response.body, len(batch))
                if acknowledgement.kind == "protocol_failure":
                    self._consecutive_failures += 1
                    retry_after_ms = response.retry_after_ms if response.retry_after_ms is not None else 1_000
                    self._retry_after = now + (retry_after_ms / 1000)
                    self._emit_diagnostic(
                        "ingestion_acknowledgement_invalid",
                        "sdk-python retained a batch after an invalid ingestion acknowledgement",
                        metadata={"reason": acknowledgement.reason or "invalid"},
                    )
                    self._schedule_flush_locked(delay=retry_after_ms / 1000)
                    return
                if acknowledgement.kind == "legacy":
                    self._buffer = self._buffer[len(batch) :]
                    self._retry_after = 0.0
                    self._last_event_at = self._time_provider() * 1000
                    self._consecutive_failures = 0
                    return

                trailing_events = self._buffer[len(batch) :]
                self._buffer = [
                    batch[index] for index in acknowledgement.retryable_indices if 0 <= index < len(batch)
                ] + trailing_events
                if acknowledgement.terminal_errors:
                    self._emit_diagnostic(
                        "ingestion_events_rejected",
                        "sdk-python removed terminally rejected ingestion events",
                        metadata={
                            "rejected_count": len(acknowledgement.terminal_errors),
                            "reasons": sorted({reason for _, reason in acknowledgement.terminal_errors}),
                        },
                    )
                if acknowledgement.accepted > 0:
                    self._last_event_at = self._time_provider() * 1000
                if acknowledgement.retryable_indices:
                    self._consecutive_failures += 1
                    retry_after_ms = response.retry_after_ms if response.retry_after_ms is not None else 1_000
                    self._retry_after = now + (retry_after_ms / 1000)
                    self._schedule_flush_locked(delay=retry_after_ms / 1000)
                    return
                self._retry_after = 0.0
                self._consecutive_failures = 0 if acknowledgement.accepted > 0 else 3
                return

            self._consecutive_failures += 1
            if response.status_code == 429:
                retry_after_ms = response.retry_after_ms if response.retry_after_ms is not None else 1_000
                self._retry_after = now + (retry_after_ms / 1000)
                self._schedule_flush_locked(delay=retry_after_ms / 1000)
                return

            if 400 <= response.status_code < 500:
                self._buffer = []
                self._retry_after = 0.0
                return

            self._schedule_flush_locked()

    def probe(self, label: str, data: object | Callable[[], object], opts: Mapping[str, object] | None = None) -> None:
        with self._lock:
            if not self._enabled:
                return
            options = dict(opts or {})
            now_ms = int(self._time_provider() * 1000)
            matching_directives = self._find_matching_probe_directives(label, now_ms)
            is_heavy = options.get("heavy") is True
            if is_heavy and not matching_directives:
                return
            if label not in self._probe_buffers and len(self._probe_buffers) >= self._max_probe_labels:
                return

            value = data() if callable(data) else data
            if not isinstance(value, Mapping):
                value = {"value": value}

            redacted_value = redact_mapping(dict(value), self._redact_fields)

            if is_heavy:
                self._emit_probe_events(label, redacted_value, matching_directives)
                return

            entry = ProbeEntry(
                label=label,
                data=redacted_value,
                timestamp=iso_now(self._time_provider),
            )
            bucket = self._probe_buffers.setdefault(label, deque(maxlen=self._max_probe_entries_per_label))
            bucket.append(entry)
            self._emit_probe_events(label, redacted_value, matching_directives)

    def capture_exceptions(self) -> None:
        with self._lock:
            if self._original_excepthook is None:
                self._original_excepthook = sys.excepthook

            def handler(exc_type: type[BaseException], error: BaseException, tb: Any) -> None:
                if error.__traceback__ is None:
                    error.__traceback__ = tb
                self._capture_exception(error, handled=False)

            sys.excepthook = handler

    def capture_logging(self, logger: logging.Logger | None = None) -> None:
        with self._lock:
            target_logger = logger or logging.getLogger()
            logger_id = id(target_logger)
            if logger_id in self._logging_bindings:
                if not self._optional_logging_restorers:
                    self._optional_logging_restorers = attach_optional_integrations(self, self._on_diagnostic)
                return
            handler = DebugBundleLogHandler(self)
            target_logger.addHandler(handler)
            self._logging_bindings[logger_id] = (target_logger, handler)
            if not self._optional_logging_restorers:
                self._optional_logging_restorers = attach_optional_integrations(self, self._on_diagnostic)

    def capture_async(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        with self._lock:
            target_loop = loop or asyncio.get_event_loop()
            if target_loop in self._async_handlers:
                return
            self._async_handlers[target_loop] = target_loop.get_exception_handler()

            def handler(async_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
                error = context.get("exception")
                if isinstance(error, BaseException):
                    self._capture_exception(error, handled=False)
                    return
                message = str(context.get("message") or "asyncio exception")
                self.capture_message(message, level="error")

            target_loop.set_exception_handler(handler)

    def dispose(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._remote_config_timer is not None:
                self._remote_config_timer.cancel()
                self._remote_config_timer = None
            for logger, handler in self._logging_bindings.values():
                logger.removeHandler(handler)
            self._logging_bindings.clear()
            for restore in self._optional_logging_restorers:
                restore()
            self._optional_logging_restorers.clear()
            if self._original_excepthook is not None:
                sys.excepthook = self._original_excepthook
                self._original_excepthook = None
            for loop, handler in list(self._async_handlers.items()):
                loop.set_exception_handler(handler)
            self._async_handlers.clear()
            if self._http_transport is not None:
                self._http_transport.close()
                self._http_transport = None

    def _refresh_remote_config(self, initial: bool = False) -> None:
        with self._lock:
            if not self._enabled or self._fetch_impl is None:
                return

            request_headers: dict[str, str] = {}
            if self._remote_config_etag is not None:
                request_headers["if-none-match"] = self._remote_config_etag

            try:
                response = self._fetch_impl(
                    sdk_config_endpoint(self._endpoint),
                    {
                        "method": "GET",
                        "headers": request_headers,
                    },
                )
                status_code = getattr(response, "status_code", None)
                if status_code == 304:
                    self._schedule_next_remote_config_refresh()
                    return
                if status_code != 200:
                    raise RuntimeError(f"unexpected config status {status_code}")

                payload = response.json()
                snapshot = parse_remote_config(
                    payload,
                    self._configured_probes_poll_interval_ms,
                    int(self._time_provider() * 1000),
                )
                if snapshot is None:
                    self._emit_diagnostic(
                        "remote_probe_config_invalid",
                        "sdk-python received an invalid remote probe config payload",
                    )
                    if initial:
                        self._capture_policy = MINIMAL_CAPTURE_POLICY
                    self._schedule_next_remote_config_refresh(use_fallback=True)
                    return

                self._remote_config_snapshot = snapshot
                self._capture_policy = snapshot.capture_policy
                headers = response.headers or {}
                etag = headers.get("etag")
                if isinstance(etag, str) and len(etag) > 0:
                    self._remote_config_etag = etag
                self._schedule_next_remote_config_refresh()
            except Exception as error:
                self._emit_diagnostic(
                    "remote_probe_config_failed",
                    "sdk-python failed to refresh remote probe config",
                    metadata={"error": serialize_error(error)},
                )
                if initial:
                    self._capture_policy = MINIMAL_CAPTURE_POLICY
                self._schedule_next_remote_config_refresh(use_fallback=True)

    def _base_event(
        self,
        event_type: str,
        payload: dict[str, object],
        context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        merged_context = self._merged_context(context)
        event: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "occurred_at": iso_now(self._time_provider),
            "sdk_name": "debugbundle-python",
            "sdk_version": sdk_version(),
            "service": {
                "name": self._service,
                "runtime": "python",
                "framework": None,
                "environment": self._environment,
            },
            "correlation": correlation_payload(merged_context),
            "payload": payload,
        }
        envelope_context = event_context(merged_context)
        if envelope_context:
            event["context"] = envelope_context
        return event

    def _merged_context(self, context: Mapping[str, object] | None = None) -> dict[str, object]:
        merged = dict(self._context)
        scoped_context = self._scoped_context.get()
        if scoped_context is not None:
            merged.update(scoped_context)
        if context is not None:
            merged.update({str(key): value for key, value in context.items()})
        return cast(dict[str, object], redact_value(merged, self._redact_fields))

    def _enqueue_event(self, event: dict[str, object]) -> None:
        self._buffer.append(event)
        if len(self._buffer) >= self._batch_size:
            self.flush()
            return
        self._schedule_flush_locked()

    def _schedule_flush_locked(self, delay: float | None = None) -> None:
        if self._timer is not None:
            self._timer.cancel()
        next_delay = self._flush_interval if delay is None else max(delay, 0.0)
        self._timer = threading.Timer(next_delay, self.flush)
        self._timer.daemon = True
        self._timer.start()

    def _append_suppression_aggregates(self) -> None:
        aggregates = self._suppression.drain_aggregates(self._time_provider())
        for aggregate in aggregates:
            event_type = aggregate.get("event_type")
            payload = aggregate.get("payload")
            if not isinstance(event_type, str) or not isinstance(payload, dict):
                continue
            aggregate.update(self._base_event(event_type, cast(dict[str, object], payload)))
            prepared = self._apply_before_send_event(aggregate)
            if prepared is not None:
                self._buffer.append(prepared)

    def _apply_before_send_event(self, event: dict[str, object]) -> dict[str, object] | None:
        return apply_before_send(
            event,
            self._before_send,
            lambda code, message: self._emit_diagnostic(code, message),
        )

    def _build_probe_data(self) -> dict[str, object] | None:
        items: list[dict[str, object]] = []
        for label, bucket in self._probe_buffers.items():
            for entry in bucket:
                items.append(
                    {
                        "label": label,
                        "activation_id": None,
                        "timestamp": entry.timestamp,
                        "data": dict(entry.data),
                    }
                )
        if not items:
            return None
        return {"version": 1, "items": items}

    def _passes_sample_rate(self) -> bool:
        return self._sample_rate >= 1.0 or random() <= self._sample_rate

    def _effective_log_threshold(self) -> str:
        policy_threshold = self._capture_policy.capture_logs
        return self._log_level if LEVEL_RANKS[self._log_level] >= LEVEL_RANKS[policy_threshold] else policy_threshold

    def _should_capture_request_event(
        self,
        request: Mapping[str, object] | None,
        response: Mapping[str, object] | None,
    ) -> bool:
        policy = self._capture_policy.capture_request_events
        status_code = None
        if response is not None:
            candidate = response.get("status_code") or response.get("response_status")
            if isinstance(candidate, int):
                status_code = candidate
        request_path = None
        http_method = None
        if request is not None:
            path_candidate = request.get("path") or request.get("url")
            method_candidate = request.get("method")
            request_path = path_candidate if isinstance(path_candidate, str) else None
            http_method = method_candidate if isinstance(method_candidate, str) else None
        if is_immediate_request_incident_status(
            status_code,
            self._capture_policy.preset,
            self._capture_policy.immediate_client_error_statuses,
            request_path,
            http_method,
            self._capture_policy.immediate_client_error_path_rules,
        ):
            return True
        if policy == "off":
            return False
        if policy == "all":
            return True
        if response is None:
            return policy == "filtered"
        if status_code is None:
            return policy == "filtered"
        if policy == "failures_only":
            return status_code >= 500
        if policy == "filtered":
            return False
        return True

    def _emit_probe_events(self, label: str, data: dict[str, object], directives: list[RemoteProbeDirective]) -> None:
        for directive in directives:
            payload = {
                "label": label,
                "activation_id": getattr(directive, "id"),
                "probe_label_pattern": getattr(directive, "label_pattern"),
                "data": dict(data),
            }
            event = self._apply_before_send_event(self._base_event("probe_event", payload))
            if event is not None and self._capture_policy.capture_probe_events == "standalone_when_activated":
                self._enqueue_event(event)

    def begin_request(self, request: dict[str, Any]) -> Token[list[RemoteProbeDirective] | None]:
        trigger_token_key = (
            self._remote_config_snapshot.trigger_token_key if self._remote_config_snapshot is not None else None
        )
        directives = resolve_request_trigger_directives(
            request,
            trigger_token_key,
            int(self._time_provider() * 1000),
        )
        return self._request_trigger_directives.set(directives)

    def end_request(self, token: Token[list[RemoteProbeDirective] | None]) -> None:
        self._request_trigger_directives.reset(token)

    def _find_matching_probe_directives(self, label: str, now_ms: int) -> list[RemoteProbeDirective]:
        directives: list[RemoteProbeDirective] = []
        trigger_directives = self._request_trigger_directives.get()
        if trigger_directives is not None:
            directives.extend(trigger_directives)
        if self._remote_config_snapshot is not None and self._remote_config_snapshot.remote_probes_enabled:
            directives.extend(self._remote_config_snapshot.directives)
        if not directives:
            return []
        return find_matching_remote_probe_directives(
            directives,
            label,
            self._service,
            self._environment,
            now_ms,
        )

    def _schedule_next_remote_config_refresh(self, use_fallback: bool = False) -> None:
        if self._remote_config_timer is not None:
            self._remote_config_timer.cancel()
            self._remote_config_timer = None
        if self._fetch_impl is None:
            return
        if (
            not use_fallback
            and self._remote_config_snapshot is not None
            and not self._remote_config_snapshot.remote_probes_enabled
        ):
            return
        delay_ms = (
            self._configured_probes_poll_interval_ms
            if use_fallback or self._remote_config_snapshot is None
            else self._remote_config_snapshot.poll_interval_ms
        )
        self._remote_config_timer = threading.Timer(delay_ms / 1000, self._refresh_remote_config)
        self._remote_config_timer.daemon = True
        self._remote_config_timer.start()

    def _emit_diagnostic(self, code: str, message: str, metadata: dict[str, object] | None = None) -> None:
        if self._on_diagnostic is None:
            return
        diagnostic: dict[str, object] = {"code": code, "message": message}
        if metadata is not None:
            diagnostic["metadata"] = metadata
        try:
            self._on_diagnostic(diagnostic)
        except Exception:
            return
