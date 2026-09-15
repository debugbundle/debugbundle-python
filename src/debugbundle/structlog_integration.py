from __future__ import annotations

import copy
import inspect
import logging
from contextvars import ContextVar
from typing import Any

_CAPTURING: ContextVar[bool] = ContextVar("debugbundle_structlog_capture", default=False)
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "critical", "fatal", "exception", "log"}
_BIND_METHODS = {"bind", "new", "unbind", "try_unbind"}


def _capture(sdk: Any, active: list[bool], name: str, record: dict[str, Any]) -> None:
    if not active[0] or _CAPTURING.get():
        return
    token = _CAPTURING.set(True)
    try:
        context = dict(record)
        message = str(context.pop("event", ""))
        level = {"warn": "warning", "exception": "error", "fatal": "critical"}.get(name, name)
        sdk.capture_log(message, level=level, context=context or None)
    except Exception:
        # A logger adapter must not throw into the application's logging path.
        pass
    finally:
        _CAPTURING.reset(token)


class _SinkProxy:
    def __init__(self, sink: Any, sdk: Any, active: list[bool], record: dict[str, Any]) -> None:
        self._sink = sink
        self._sdk = sdk
        self._active = active
        self._record = record

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._sink, name)
        if name not in _LOG_METHODS or not callable(method):
            return method

        def emit(*args: Any, **kwargs: Any) -> Any:
            result = method(*args, **kwargs)
            # stdlib records go through the existing SDK logging.Handler, after
            # Logger.disabled, logging.disable(), levels and logger filters.
            if not isinstance(self._sink, (logging.Logger, logging.LoggerAdapter)):
                _capture(self._sdk, self._active, name, self._record)
            return result

        return emit


class StructlogLoggerProxy:
    def __init__(self, logger: Any, sdk: Any, active: list[bool] | None = None) -> None:
        self._logger = logger
        self._sdk = sdk
        self._active = active if active is not None else [True]

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._logger, name)
        if not callable(attribute):
            return attribute
        if name in _BIND_METHODS:
            def bind(*args: Any, **kwargs: Any) -> StructlogLoggerProxy:
                return StructlogLoggerProxy(attribute(*args, **kwargs), self._sdk, self._active)
            return bind
        method_name = name[1:] if name.startswith("a") else name
        if method_name not in _LOG_METHODS:
            return attribute

        def prepare(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            if not self._active[0] or _CAPTURING.get():
                return attribute
            try:
                bound = self._logger.bind() if hasattr(self._logger, "bind") else self._logger
                if not hasattr(bound, "_processors") or not hasattr(bound, "_logger"):
                    return self._legacy_method(attribute, method_name)
                # Keep the application's bound logger and processor list intact.
                # The per-call clone also isolates concurrent/async log records.
                clone = copy.copy(bound)
                self._ensure_stdlib_handler(bound._logger)
                record = dict(bound._context)
                record.update(kwargs)
                if args:
                    record["event"] = args[1] if method_name == "log" and len(args) > 1 else args[0]
                processors = list(bound._processors)
                if processors:
                    last = processors[-1]

                    def observe(logger: Any, level: str, event: Any) -> Any:
                        result = last(logger, level, event)
                        snapshot = result if isinstance(result, dict) else event
                        if isinstance(snapshot, dict):
                            record.clear()
                            record.update(snapshot)
                        return result

                    processors[-1] = observe
                clone._processors = processors
                clone._logger = _SinkProxy(bound._logger, self._sdk, self._active, record)
                return getattr(clone, name)
            except Exception:
                # Preparation can fail for a custom logger; call it exactly once.
                return attribute

        if inspect.iscoroutinefunction(attribute):
            async def async_log(*args: Any, **kwargs: Any) -> Any:
                return await prepare(args, kwargs)(*args, **kwargs)
            return async_log

        def log(*args: Any, **kwargs: Any) -> Any:
            return prepare(args, kwargs)(*args, **kwargs)
        return log

    def _ensure_stdlib_handler(self, sink: Any) -> None:
        logger = sink.logger if isinstance(sink, logging.LoggerAdapter) else sink
        if not isinstance(logger, logging.Logger):
            return
        current: logging.Logger | None = logger
        while current is not None:
            if any(getattr(handler, "_sdk", None) is self._sdk for handler in current.handlers):
                return
            if not current.propagate:
                break
            current = current.parent
        attach = getattr(self._sdk, "capture_logging", None)
        if callable(attach):
            attach(logger=logger)

    def _legacy_method(self, attribute: Any, name: str) -> Any:
        def log(event: Any = None, *args: Any, **kwargs: Any) -> Any:
            result = attribute(event, *args, **kwargs)
            record = {**kwargs, **{f"arg_{i}": value for i, value in enumerate(args)}, "event": event or ""}
            _capture(self._sdk, self._active, name, record)
            return result
        return log
