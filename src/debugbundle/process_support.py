"""Discard pre-fork SDK ownership without touching inherited thread locks."""

from __future__ import annotations

import os
import threading
from contextvars import ContextVar
from typing import TYPE_CHECKING

from .suppression import EventSuppressionTracker
from .transport import HttpTransport

if TYPE_CHECKING:
    from .core import DebugBundleSdk


def ensure_current_process(sdk: DebugBundleSdk) -> None:
    pid = os.getpid()
    if sdk._pid == pid:
        return

    # The parent may have forked while another thread owned its lock. Never
    # acquire, cancel, close, or join any inherited thread-owned object here.
    sdk._lock = threading.RLock()
    sdk._timer = None
    sdk._remote_config_timer = None
    sdk._initial_config_ready = threading.Event()
    sdk._initial_config_ready.set()
    sdk._generation += 1
    sdk._sending = False
    sdk._inflight_count = 0
    sdk._timer_due_at = 0.0
    sdk._buffer = []
    sdk._buffer_sizes = {}
    sdk._buffer_bytes = 0
    sdk._pending_low_priority = 0
    sdk._context = {}
    sdk._scoped_context = ContextVar("debugbundle_scoped_context", default=None)
    sdk._request_trigger_directives = ContextVar("debugbundle_request_trigger_directives", default=None)
    sdk._probe_buffers = {}
    sdk._suppression = EventSuppressionTracker()
    sdk._pressure_drops = 0
    sdk._last_pressure_report_at = 0.0
    sdk._retry_after = 0.0
    sdk._last_event_at = None
    sdk._consecutive_failures = 0
    sdk._remote_config_etag = None
    sdk._remote_config_snapshot = None

    # Retain the parent's effective policy until the child explicitly reinitializes;
    # resetting to a default here could widen capture after a restricted policy.
    if sdk._http_transport is not None:
        sdk._http_transport = None
        try:
            sdk._http_transport = HttpTransport(sdk._endpoint)
        except Exception:
            pass
        sdk._transport = sdk._http_transport
    sdk._pid = pid
