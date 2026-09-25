"""Logger adapter and bounded capture admission for callback-heavy paths."""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from .process_support import ensure_current_process

if TYPE_CHECKING:
    from .core import DebugBundleSdk


def try_capture_lock(sdk: DebugBundleSdk) -> bool:
    ensure_current_process(sdk)
    for _ in range(8):
        if sdk._lock.acquire(blocking=False):
            return True
        time.sleep(0)
    return False


class DebugBundleLogHandler(logging.Handler):
    def __init__(self, sdk: DebugBundleSdk) -> None:
        super().__init__()
        self._sdk = sdk

    def emit(self, record: logging.LogRecord) -> None:
        if not self._sdk._log_level_eligible(record.levelname.lower()):
            return
        self._sdk.capture_log(
            safe_log_message(record),
            level=record.levelname.lower(),
            context={
                "logger_name": record.name,
                "pathname": record.pathname,
                "lineno": record.lineno,
            },
        )


_PLACEHOLDER = re.compile(r"%[sdif]")


def safe_log_message(record: logging.LogRecord) -> str:
    template = record.msg
    if type(template) is not str:
        return "[non-string log message withheld]"
    template = template[:16_384]
    args = record.args
    if not args:
        return template
    if type(args) is not tuple or len(args) > 8:
        return template + " [log arguments withheld]"
    safe_args = [
        value[:1024] if type(value) is str else str(value) if type(value) in (int, float, bool)
        else "[unsupported argument]"
        for value in args
    ]
    if len(_PLACEHOLDER.findall(template)) != len(safe_args):
        return (template + " [args: " + ", ".join(safe_args) + "]")[:16_384]
    iterator = iter(safe_args)
    return _PLACEHOLDER.sub(lambda _match: next(iterator), template)[:16_384]
