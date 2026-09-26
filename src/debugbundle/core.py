from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from random import random
from typing import Any, Protocol, cast

from .acknowledgement import decide_acknowledgement
from .before_send import BeforeSendHook, apply_before_send
from .capture_hooks import DebugBundleLogHandler, try_capture_lock
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
from .delivery_hooks import finalize_batch
from .event_support import (
    DEFAULT_LOG_LEVEL,
    LEVEL_RANKS,
    backend_exception_request_payload,
    backend_exception_response_payload,
    correlation_payload,
    event_context,
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
from .exception_snapshot import exception_details
from .flush_aggregates import prepare_flush_aggregates
from .logger_integrations import attach_optional_integrations
from .probe_capture import ProbeEntry, capture_probe
from .process_support import ensure_current_process
from .queue_admission import evict_low_priority, high_priority_event, may_prepare_event
from .redaction import (
    DEFAULT_REDACT_FIELDS,
    UnsafeTelemetry,
    has_safe_event_identity,
    redact_value,
    sanitize_telemetry,
)
from .request_capture_policy import should_capture_request_event
from .suppression import EventSuppressionTracker
from .transport import HttpTransport, Transport, bounded_retry_after_ms, coerce_transport_response
from .trigger_token import resolve_request_trigger_directives

DEFAULT_BATCH_SIZE = 25
DEFAULT_FLUSH_INTERVAL = 5.0
DEFAULT_ENDPOINT = "https://api.debugbundle.com/v1/events"
SCHEMA_VERSION = "2026-03-01"
MAX_PENDING_EVENTS = 1_000
MAX_PENDING_BYTES = 8 * 1024 * 1024


class ConfigFetchResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> object: ...


class DebugBundleSdk:
    def __init__(
        self,
        transport: Transport | None = None,
        time_provider: Callable[[], float] | None = None,
    ) -> None:
        self._transport_override = transport
        self._pid = os.getpid()
        self._time_provider = time_provider or time_now
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._remote_config_timer: threading.Timer | None = None
        self._initial_config_ready = threading.Event()
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
        self._buffer_sizes: dict[int, int] = {}
        self._buffer_bytes = 0
        self._pending_low_priority = 0
        self._sending = False
        self._inflight_count = 0
        self._timer_due_at = 0.0
        self._generation = 0
        self._pressure_drops = 0
        self._last_pressure_report_at = 0.0
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
        ensure_current_process(self)
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
        ensure_current_process(self)
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
        ensure_current_process(self)
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
            self._buffer_sizes = {}
            self._buffer_bytes = 0
            self._pending_low_priority = 0
            self._generation += 1
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
            self._capture_policy = MINIMAL_CAPTURE_POLICY if fetch_impl is not None else BALANCED_CAPTURE_POLICY
            self._transport = self._transport_override
            if self._transport is None and self._enabled:
                self._http_transport = HttpTransport(self._endpoint)
                self._transport = self._http_transport
            self.capture_exceptions()
            if self._enabled and self._fetch_impl is not None:
                self._initial_config_ready.clear()
                self._remote_config_timer = threading.Timer(0.0, self._refresh_initial_remote_config)
                self._remote_config_timer.daemon = True
                self._remote_config_timer.start()
            else:
                self._initial_config_ready.set()

    def _refresh_initial_remote_config(self) -> None:
        try:
            self._refresh_remote_config(initial=True)
        finally:
            self._initial_config_ready.set()

    def capture_exception(self, error: BaseException, context: Mapping[str, object] | None = None) -> None:
        self._capture_exception(error, context=context, handled=True)

    def _capture_exception(
        self,
        error: BaseException,
        context: Mapping[str, object] | None = None,
        handled: bool = True,
    ) -> None:
        ensure_current_process(self)
        if not self._enabled or not may_prepare_event(self, True, MAX_PENDING_EVENTS, MAX_PENDING_BYTES):
            return
        try:
            name, message, stack = exception_details(error)
            redacted_context = redact_mapping(dict(context or {}), self._redact_fields)
            request_payload = backend_exception_request_payload(redacted_context.get("request"))
            response_payload = backend_exception_response_payload(redacted_context.get("response"))

            payload: dict[str, object] = {
                "name": name,
                "message": message,
                "stack": stack,
                "handled": handled,
                "request": request_payload,
                "response": response_payload,
                "runtime": runtime_process_facts(),
            }
            if self._probe_flush_on_error and try_capture_lock(self):
                try:
                    probe_data = self._build_probe_data()
                finally:
                    self._lock.release()
                if probe_data is not None:
                    payload["probe_data"] = probe_data

            event = self._protect_event(self._base_event("backend_exception", payload, context=redacted_context))
            if event is None or not self._passes_sample_rate():
                return
            event_payload = cast(dict[str, object], event["payload"])
            suppression_key = (
                f"{event['event_type']}:{event_payload.get('name', '')}:"
                f"{event_payload.get('message', '')}:{event_payload.get('stack', '')}"
            )
            event_bytes = self._event_bytes(event)
            if not try_capture_lock(self):
                self._pressure_drops += 1
                return
            try:
                if self._enabled and self._suppression.should_capture(suppression_key, self._time_provider()):
                    self._offer_protected_event(event, event_bytes)
            finally:
                self._lock.release()
        except BaseException:
            return

    def capture_error(self, error: BaseException, context: Mapping[str, object] | None = None) -> None:
        self.capture_exception(error, context=context)

    def capture_log(
        self,
        message: str,
        level: str = DEFAULT_LOG_LEVEL,
        context: Mapping[str, object] | None = None,
    ) -> None:
        try:
            normalized_level = normalize_level(level)
            if not self._enabled or self._capture_policy.capture_logs == "off" or not level_enabled(
                normalized_level, self._effective_log_threshold()
            ):
                return
            ensure_current_process(self)
            if not may_prepare_event(
                self, normalized_level in ("error", "critical"), MAX_PENDING_EVENTS, MAX_PENDING_BYTES
            ):
                return
            payload: dict[str, object] = {
                "message": message,
                "level": normalized_level,
                "attributes": {},
            }
            if context:
                payload["attributes"] = redact_mapping(dict(context), self._redact_fields)
            event = self._protect_event(self._base_event("log_event", payload, context=context))
            if (
                event is None
                or not self._passes_sample_rate()
            ):
                return
            event_bytes = self._event_bytes(event)
            if not try_capture_lock(self):
                self._pressure_drops += 1
                return
            try:
                if self._log_level_eligible(normalized_level):
                    self._offer_protected_event(event, event_bytes)
            finally:
                self._lock.release()
        except BaseException:
            return

    def capture_request(
        self,
        request: Mapping[str, object],
        response: Mapping[str, object] | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        try:
            if not self._enabled or not should_capture_request_event(self._capture_policy, request, response):
                return
            ensure_current_process(self)
            status = None if response is None else response.get("status_code") or response.get("response_status")
            if not may_prepare_event(
                self, type(status) is int and status >= 500, MAX_PENDING_EVENTS, MAX_PENDING_BYTES
            ):
                return
            payload = request_event_payload(
                redact_mapping(dict(request), self._redact_fields),
                redact_mapping(dict(response or {}), self._redact_fields),
                redact_mapping(dict(context or {}), self._redact_fields),
            )
            event = self._protect_event(self._base_event("request_event", payload, context=context))
            if event is None or not self._passes_sample_rate():
                return
            event_bytes = self._event_bytes(event)
            if not try_capture_lock(self):
                self._pressure_drops += 1
                return
            try:
                if self._enabled:
                    self._offer_protected_event(event, event_bytes)
            finally:
                self._lock.release()
        except BaseException:
            return

    def capture_message(
        self,
        message: str,
        level: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        self.capture_log(message, level=level or DEFAULT_LOG_LEVEL, context=context)

    def set_context(self, key: str, value: object) -> None:
        try:
            ensure_current_process(self)
            protected = sanitize_telemetry({key: value}, self._redact_fields)
            if not isinstance(protected, dict) or not try_capture_lock(self):
                return
            try:
                next_context = dict(self._context)
                next_context[key] = protected.get(key)
                self._context = next_context
            finally:
                self._lock.release()
        except BaseException:
            return

    def _bind_scoped_context(self, context: Mapping[str, object]) -> Token[dict[str, object] | None]:
        ensure_current_process(self)
        scoped_context = dict(self._scoped_context.get() or {})
        for key, value in context.items():
            if value is None:
                continue
            scoped_context[str(key)] = value
        try:
            protected = sanitize_telemetry(scoped_context, self._redact_fields)
            return self._scoped_context.set(cast(dict[str, object], protected))
        except UnsafeTelemetry:
            return self._scoped_context.set({})

    def _reset_scoped_context(self, token: Token[dict[str, object] | None]) -> None:
        self._scoped_context.reset(token)

    def flush(self) -> None:
        ensure_current_process(self)
        with self._lock:
            if not self._enabled or self._transport is None or self._sending:
                return
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            now = self._time_provider()
            if now < self._retry_after:
                self._schedule_flush_locked(delay=self._retry_after - now)
                return
            self._sending = True
            generation = self._generation
            suppression = self._suppression.drain_aggregates(now)
            pressure_count = (
                self._pressure_drops
                if self._pressure_drops and now - self._last_pressure_report_at >= 60.0
                and len(self._buffer) < MAX_PENDING_EVENTS else 0
            )

        try:
            aggregates, pressure = prepare_flush_aggregates(self, suppression, pressure_count)
        except BaseException:
            aggregates, pressure = [], None

        with self._lock:
            if generation != self._generation:
                self._sending = False
                self._inflight_count = 0
                if self._buffer:
                    self._schedule_flush_locked()
                return
            for aggregate, aggregate_bytes in aggregates:
                self._offer_protected_event(aggregate, aggregate_bytes)
            if (pressure is not None and len(self._buffer) < MAX_PENDING_EVENTS
                    and self._buffer_bytes + pressure[1] <= MAX_PENDING_BYTES
                    and self._offer_protected_event(*pressure)):
                self._pressure_drops = max(0, self._pressure_drops - pressure_count)
                self._last_pressure_report_at = now
            if not self._buffer:
                self._sending = False
                self._schedule_pressure_report_locked()
                return
            batch = list(self._buffer)
            self._inflight_count = len(batch)
            self._pending_low_priority = 0
            transport = self._transport
            request = {
                "project_token": self._project_token,
                "events": batch,
            }

        finalized = finalize_batch(self, batch, generation, MAX_PENDING_BYTES)

        with self._lock:
            if generation != self._generation or finalized is None:
                self._sending = False
                self._inflight_count = 0
                if self._buffer:
                    self._schedule_flush_locked()
                return
            batch = finalized
            self._inflight_count = len(batch)
            if not batch:
                self._sending = False
                if self._buffer:
                    self._schedule_flush_locked()
                return
            request["events"] = batch

        try:
            response = coerce_transport_response(transport(request))
        except BaseException:
            response = None

        with self._lock:
            self._sending = False
            self._inflight_count = 0
            if generation != self._generation:
                if self._buffer:
                    self._schedule_flush_locked()
                return
            try:
                if response is None:
                    self._consecutive_failures += 1
                    self._schedule_flush_locked()
                    return

                if 200 <= response.status_code < 300:
                    acknowledgement = decide_acknowledgement(
                        response.body, len(batch), isinstance(transport, HttpTransport)
                    )
                    if acknowledgement.kind == "protocol_failure":
                        self._consecutive_failures += 1
                        retry_after_ms = bounded_retry_after_ms(response.retry_after_ms)
                        self._retry_after = self._time_provider() + (retry_after_ms / 1000)
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
                        retry_after_ms = bounded_retry_after_ms(response.retry_after_ms)
                        self._retry_after = self._time_provider() + (retry_after_ms / 1000)
                        self._schedule_flush_locked(delay=retry_after_ms / 1000)
                        return
                    self._retry_after = 0.0
                    self._consecutive_failures = 0 if acknowledgement.accepted > 0 else 3
                    return

                self._consecutive_failures += 1
                if response.status_code == 429 or (response.status_code >= 500 and response.retry_after_ms is not None):
                    retry_after_ms = bounded_retry_after_ms(response.retry_after_ms)
                    self._retry_after = self._time_provider() + (retry_after_ms / 1000)
                    self._schedule_flush_locked(delay=retry_after_ms / 1000)
                    return
                if 400 <= response.status_code < 500:
                    self._buffer = self._buffer[len(batch) :]
                    self._retry_after = 0.0
                    return
                self._schedule_flush_locked()
            finally:
                self._buffer_sizes = {id(event): self._buffer_sizes[id(event)]
                                      for event in self._buffer}
                self._buffer_bytes = sum(self._buffer_sizes.values())
                self._pending_low_priority = sum(not high_priority_event(event) for event in self._buffer)
                if self._buffer and self._timer is None and self._retry_after <= self._time_provider():
                    self._schedule_flush_locked()
                elif not self._buffer and self._timer is None:
                    self._schedule_pressure_report_locked()

    def probe(self, label: str, data: object | Callable[[], object], opts: Mapping[str, object] | None = None) -> None:
        capture_probe(self, label, data, opts)

    def capture_exceptions(self) -> None:
        ensure_current_process(self)
        with self._lock:
            if self._original_excepthook is None:
                self._original_excepthook = sys.excepthook

            def handler(exc_type: type[BaseException], error: BaseException, tb: Any) -> None:
                if error.__traceback__ is None:
                    error.__traceback__ = tb
                self._capture_exception(error, handled=False)

            sys.excepthook = handler

    def capture_logging(self, logger: logging.Logger | None = None) -> None:
        ensure_current_process(self)
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
        ensure_current_process(self)
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
        ensure_current_process(self)
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
            fetch = self._fetch_impl
            endpoint = sdk_config_endpoint(self._endpoint)
            poll_interval = self._configured_probes_poll_interval_ms
            generation = self._generation

        try:
            response = fetch(endpoint, {"method": "GET", "headers": request_headers})
            status_code = getattr(response, "status_code", None)
            if status_code == 304:
                snapshot = None
            elif status_code == 200:
                snapshot = parse_remote_config(
                    response.json(),
                    poll_interval,
                    int(self._time_provider() * 1000),
                )
            else:
                raise RuntimeError(f"unexpected config status {status_code}")
            headers = response.headers or {}
            etag = headers.get("etag")
            failure: BaseException | None = None
        except BaseException as error:
            status_code = None
            snapshot = None
            etag = None
            failure = error

        with self._lock:
            if generation != self._generation or not self._enabled:
                return
            if failure is None and status_code == 304:
                self._schedule_next_remote_config_refresh()
                return
            if failure is None:
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
                if isinstance(etag, str) and len(etag) > 0:
                    self._remote_config_etag = etag
                self._schedule_next_remote_config_refresh()
            else:
                self._emit_diagnostic(
                    "remote_probe_config_failed",
                    "sdk-python failed to refresh remote probe config",
                    metadata={"error": serialize_error(failure) if isinstance(failure, Exception) else "fetch_failed"},
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

    def _protect_event(self, event: dict[str, object]) -> dict[str, object] | None:
        try:
            if not has_safe_event_identity(event, self._redact_fields):
                return None
            candidate = dict(event)
            for key in ("payload", "context", "service"):
                if key in candidate:
                    candidate[key] = sanitize_telemetry(candidate[key], self._redact_fields)
            return candidate
        except UnsafeTelemetry:
            return None

    def _enqueue_event(self, event: dict[str, object]) -> bool:
        protected = self._protect_event(event)
        if protected is None:
            return False
        return self._offer_protected_event(protected, self._event_bytes(protected))

    def _offer_protected_event(self, protected: dict[str, object], event_bytes: int) -> bool:
        if event_bytes > MAX_PENDING_BYTES:
            self._pressure_drops += 1
            return False
        high_priority = high_priority_event(protected)
        under_pressure = len(self._buffer) >= MAX_PENDING_EVENTS or self._buffer_bytes + event_bytes > MAX_PENDING_BYTES
        while len(self._buffer) >= MAX_PENDING_EVENTS or self._buffer_bytes + event_bytes > MAX_PENDING_BYTES:
            if not high_priority or not evict_low_priority(self):
                self._pressure_drops += 1
                return False
        insertion = self._inflight_count
        if high_priority and (protected.get("event_type") != "request_event" or under_pressure):
            while insertion < len(self._buffer) and high_priority_event(self._buffer[insertion]):
                insertion += 1
            self._buffer.insert(insertion, protected)
        else:
            self._buffer.append(protected)
            self._pending_low_priority += 1
        self._buffer_bytes += event_bytes
        self._buffer_sizes[id(protected)] = event_bytes
        self._schedule_flush_locked(delay=0.0 if len(self._buffer) >= self._batch_size else None)
        return True

    @staticmethod
    def _event_bytes(event: dict[str, object]) -> int:
        return len(json.dumps(event, separators=(",", ":")).encode("utf-8"))

    def _schedule_flush_locked(self, delay: float | None = None) -> None:
        if self._sending:
            return
        next_delay = self._flush_interval if delay is None else max(delay, 0.0)
        due_at = time.monotonic() + next_delay
        if self._timer is not None and self._timer.is_alive():
            if self._timer_due_at <= due_at:
                return
            self._timer.cancel()
        self._timer = threading.Timer(next_delay, self.flush)
        self._timer.daemon = True
        self._timer_due_at = due_at
        self._timer.start()

    def _schedule_pressure_report_locked(self) -> None:
        if self._pressure_drops == 0:
            return
        delay = max(self._flush_interval, self._last_pressure_report_at + 60.0 - self._time_provider())
        self._schedule_flush_locked(delay=delay)

    def _apply_before_send_event(self, event: dict[str, object]) -> dict[str, object] | None:
        protected = self._protect_event(event)
        if protected is None:
            return None
        result = apply_before_send(
            protected,
            self._before_send,
            lambda code, message: self._emit_diagnostic(code, message),
        )
        return self._protect_event(result) if result is not None else None

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

    def _log_level_eligible(self, level: str) -> bool:
        return (
            self._enabled
            and self._capture_policy.capture_logs != "off"
            and level_enabled(normalize_level(level), self._effective_log_threshold())
        )

    def begin_request(self, request: dict[str, Any]) -> Token[list[RemoteProbeDirective] | None]:
        ensure_current_process(self)
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
            try:
                diagnostic["metadata"] = sanitize_telemetry(metadata, self._redact_fields)
            except UnsafeTelemetry:
                pass
        try:
            self._on_diagnostic(diagnostic)
        except Exception:
            return
