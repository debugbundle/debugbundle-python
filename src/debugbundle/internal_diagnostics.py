"""Optional local diagnostics: one callback lane, one pending item, no SDK locks."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from types import CoroutineType

from .redaction import sanitize_telemetry

_ACTIVE: ContextVar[bool] = ContextVar("debugbundle_internal_diagnostic", default=False)


def in_diagnostic() -> bool:
    return _ACTIVE.get()


def error_detail(error: BaseException) -> dict[str, object]:
    # Never invoke application __str__, traceback formatting or custom properties.
    args = BaseException.__dict__["args"].__get__(error)
    message = args[0] if type(args) is tuple and args and type(args[0]) is str else "error_details_withheld"
    return {
        "name": type.__getattribute__(type(error), "__name__")[:128],
        "message": message if len(message) <= 4096 else "error_details_withheld",
    }


class InternalDiagnostics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._callback: Callable[[dict[str, object]], object] | None = None
        self._pending: tuple[int, dict[str, object]] | None = None
        self._generation = 0
        self._running = False
        self._at = 0.0
        self._count = 0

    def configure(self, callback: Callable[[dict[str, object]], object] | None) -> None:
        with self._lock:
            self._callback = callback
            self._pending = None
            self._generation += 1
            # A stalled callback keeps its existing lane across reinitialization.

    def emit(self, diagnostic: dict[str, object], fields: set[str]) -> None:
        if in_diagnostic() or self._callback is None:
            return
        try:
            protected = sanitize_telemetry(diagnostic, fields)
            if type(protected) is not dict:
                return
            if len(json.dumps(protected, ensure_ascii=False).encode("utf-8")) > 4096:
                protected = {"code": protected["code"], "message": protected["message"]}
            if not self._lock.acquire(blocking=False):
                return
            try:
                now = time.monotonic()
                if now - self._at >= 60:
                    self._at, self._count = now, 0
                if self._callback is None or self._count >= 10 or self._pending is not None:
                    return
                self._count += 1
                self._pending = (self._generation, protected)
                if not self._running:
                    self._running = True
                    try:
                        threading.Thread(target=self._run, name="debugbundle-diagnostics", daemon=True).start()
                    except BaseException:
                        self._running = False
                        self._pending = None
                        return
                self._wake.set()
            finally:
                self._lock.release()
        except BaseException:
            return

    def _run(self) -> None:
        while True:
            self._wake.wait(1)
            with self._lock:
                pending, self._pending = self._pending, None
                self._wake.clear()
                if pending is None:
                    self._running = False
                    return
                callback = self._callback if pending[0] == self._generation else None
            if callback is not None:
                token = _ACTIVE.set(True)
                try:
                    result = callback(pending[1])
                    # The API remains synchronous; contain an accidental native
                    # coroutine without executing it or leaking an unawaited warning.
                    if isinstance(result, CoroutineType):
                        result.close()
                except BaseException:
                    pass
                finally:
                    _ACTIVE.reset(token)
