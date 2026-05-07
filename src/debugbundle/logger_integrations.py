from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class LogCaptureApi(Protocol):
    def capture_log(
        self,
        message: str,
        level: str = "warning",
        context: dict[str, object] | None = None,
    ) -> None: ...


def attach_optional_integrations(
    sdk: LogCaptureApi,
    on_diagnostic: Callable[[dict[str, object]], None] | None = None,
) -> list[Callable[[], None]]:
    restorers: list[Callable[[], None]] = []

    structlog_restore = _attach_structlog(sdk, on_diagnostic=on_diagnostic)
    if structlog_restore is not None:
        restorers.append(structlog_restore)

    loguru_restore = _attach_loguru(sdk, on_diagnostic=on_diagnostic)
    if loguru_restore is not None:
        restorers.append(loguru_restore)

    return restorers


def _attach_structlog(
    sdk: LogCaptureApi,
    on_diagnostic: Callable[[dict[str, object]], None] | None = None,
) -> Callable[[], None] | None:
    try:
        import structlog
    except Exception:
        return None

    try:
        original_get_logger = structlog.get_logger
        if getattr(original_get_logger, "__debugbundle_structlog_wrapper__", False):
            return None

        def get_logger(*args: Any, **kwargs: Any) -> Any:
            return _StructlogLoggerProxy(original_get_logger(*args, **kwargs), sdk)

        setattr(get_logger, "__debugbundle_structlog_wrapper__", True)
        structlog.get_logger = get_logger

        def restore() -> None:
            structlog.get_logger = original_get_logger

        return restore
    except Exception as error:
        _emit_diagnostic(on_diagnostic, error)
        return None


def _attach_loguru(
    sdk: LogCaptureApi,
    on_diagnostic: Callable[[dict[str, object]], None] | None = None,
) -> Callable[[], None] | None:
    try:
        from loguru import logger as loguru_logger
    except Exception:
        return None

    try:
        def sink(message: Any) -> None:
            record = getattr(message, "record", None)
            if not isinstance(record, dict):
                return
            context = dict(record.get("extra") or {})
            sdk.capture_log(
                str(record.get("message") or ""),
                level=_normalize_level(str(getattr(record.get("level"), "name", "warning"))),
                context=context or None,
            )

        sink_id = loguru_logger.add(sink, catch=True)

        def restore() -> None:
            loguru_logger.remove(sink_id)

        return restore
    except Exception as error:
        _emit_diagnostic(on_diagnostic, error)
        return None


def _normalize_level(level: str) -> str:
    normalized = level.lower().strip()
    if normalized == "warn":
        return "warning"
    if normalized == "exception":
        return "error"
    return normalized


class _StructlogLoggerProxy:
    def __init__(self, logger: Any, sdk: LogCaptureApi) -> None:
        self._logger = logger
        self._sdk = sdk

    def bind(self, *args: Any, **kwargs: Any) -> _StructlogLoggerProxy:
        return _StructlogLoggerProxy(self._logger.bind(*args, **kwargs), self._sdk)

    def new(self, *args: Any, **kwargs: Any) -> _StructlogLoggerProxy:
        return _StructlogLoggerProxy(self._logger.new(*args, **kwargs), self._sdk)

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._logger, name)
        if name not in {
            "debug",
            "info",
            "warning",
            "warn",
            "error",
            "critical",
            "exception",
        } or not callable(attribute):
            return attribute

        def wrapped(event: Any = None, *args: Any, **kwargs: Any) -> Any:
            context = dict(kwargs)
            for index, value in enumerate(args):
                context[f"arg_{index}"] = value
            self._sdk.capture_log(str(event or ""), level=_normalize_level(name), context=context or None)
            return attribute(event, *args, **kwargs)

        return wrapped


def _emit_diagnostic(
    on_diagnostic: Callable[[dict[str, object]], None] | None,
    error: Exception,
) -> None:
    if on_diagnostic is None:
        return
    on_diagnostic(
        {
            "code": "logger_attach_failed",
            "message": "sdk-python failed to attach a logger integration",
            "metadata": {
                "error": {
                    "name": type(error).__name__,
                    "message": str(error),
                }
            },
        }
    )