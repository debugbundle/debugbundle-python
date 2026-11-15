import logging
import sys
import threading
import time
from types import SimpleNamespace

from debugbundle.core import DebugBundleSdk


class Transport:
    def __init__(self):
        self.events = []

    def __call__(self, request):
        self.events.extend(request["events"])
        return SimpleNamespace(status_code=202, retry_after_ms=None)


def broken_integrations(monkeypatch):
    class BrokenLogger:
        def add(self, *args, **kwargs):
            raise RuntimeError("token=audit-private-value " + "x" * 65536)

    monkeypatch.setitem(sys.modules, "structlog", None)
    monkeypatch.setitem(sys.modules, "loguru", SimpleNamespace(logger=BrokenLogger()))


def test_diagnostic_failure_does_not_escape_public_logger_setup(monkeypatch):
    broken_integrations(monkeypatch)
    called = threading.Event()

    def fail(_diagnostic):
        called.set()
        raise RuntimeError("synthetic diagnostic callback failure")

    sdk = DebugBundleSdk(transport=Transport())
    sdk.init(project_token="dbundle_proj_test", on_diagnostic=fail)
    try:
        sdk.capture_logging(logging.getLogger("diagnostic-audit-setup"))
        assert called.wait(1)
    finally:
        sdk.dispose()


def test_logger_diagnostics_are_sanitized_and_bounded(monkeypatch):
    broken_integrations(monkeypatch)
    diagnostics = []
    sdk = DebugBundleSdk(transport=Transport())
    sdk.init(project_token="dbundle_proj_test", on_diagnostic=diagnostics.append)
    try:
        sdk.capture_logging(logging.getLogger("diagnostic-audit-privacy"))
        wait_for(lambda: bool(diagnostics))
        message = diagnostics[0]["metadata"]["error"]["message"]
        assert {"raw_secret": "audit-private-value" in message, "over_16k": len(message) > 16384} == {
            "raw_secret": False,
            "over_16k": False,
        }
    finally:
        sdk.dispose()


def test_diagnostic_logging_is_not_recaptured(monkeypatch):
    monkeypatch.setitem(sys.modules, "structlog", None)
    monkeypatch.setitem(sys.modules, "loguru", None)
    transport = Transport()
    logger = logging.getLogger("diagnostic-audit-feedback")
    calls = []

    def diagnostic(value):
        calls.append(value["code"])
        if len(calls) <= 6:  # Test-owned fuse.
            logger.error("internal diagnostic %s", len(calls))

    def failing_hook(_event):
        raise RuntimeError("synthetic hook failure")

    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", flush_interval=60, before_send=failing_hook, on_diagnostic=diagnostic)
    try:
        sdk.capture_logging(logger)
        sdk.capture_log("application error", "error")
        sdk.flush()
        wait_for(lambda: len(calls) >= 1)
        for _ in range(8):
            sdk.flush()
        messages = [e["payload"].get("message") for e in transport.events]
        assert {"diagnostic_calls": len(calls), "messages": messages} == {
            "diagnostic_calls": 1,
            "messages": ["application error"],
        }
    finally:
        sdk.dispose()


def wait_for(predicate):
    deadline = time.monotonic() + 1
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.001)
    assert predicate()


def test_stalled_callback_does_not_block_capture_flush_or_replace_its_worker():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def diagnostic(value):
        calls.append(value)
        entered.set()
        release.wait(3)

    transport = Transport()
    sdk = DebugBundleSdk(transport=transport)
    sdk.init(project_token="dbundle_proj_test", on_diagnostic=diagnostic)
    try:
        sdk._emit_diagnostic("first", "first")
        assert entered.wait(1)
        for _ in range(50):
            sdk._emit_diagnostic("next", "next")
        start = time.monotonic()
        sdk.capture_log("application while diagnostics stalled", "error")
        sdk.flush()
        assert time.monotonic() - start < 0.5
        assert len(transport.events) == 1
        assert len(calls) == 1
        sdk.init(project_token="dbundle_proj_test", on_diagnostic=diagnostic)
        sdk._emit_diagnostic("after_reset", "after_reset")
        assert len(calls) == 1  # Reinitialization cannot replace the stalled lane.
    finally:
        sdk.dispose()
        release.set()


def test_hostile_error_rendering_and_baseexception_callbacks_are_contained(monkeypatch):
    from debugbundle.logger_integrations import _emit_diagnostic

    class HostileError(RuntimeError):
        def __str__(self):
            raise AssertionError("must not render application errors")

    diagnostics = []
    _emit_diagnostic(diagnostics.append, HostileError("token=audit-private-value"))
    assert "audit-private-value" not in str(diagnostics)

    def fail(_value):
        raise SystemExit("synthetic")

    _emit_diagnostic(fail, HostileError())


def test_dispose_cancels_pending_diagnostics_and_releases_the_idle_lane():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def callback(value):
        calls.append(value["code"])
        entered.set()
        release.wait(3)

    sdk = DebugBundleSdk(transport=Transport())
    sdk.init(project_token="dbundle_proj_test", on_diagnostic=callback)
    try:
        sdk._emit_diagnostic("running", "running")
        assert entered.wait(1)
        sdk._emit_diagnostic("queued", "queued")
        sdk.dispose()
        release.set()
        deadline = time.monotonic() + 2
        while sdk._internal_diagnostics._running and time.monotonic() < deadline:
            time.sleep(0.001)
        assert not sdk._internal_diagnostics._running
        assert calls == ["running"]
        sdk.init(project_token="dbundle_proj_test", on_diagnostic=callback)
        sdk._emit_diagnostic("new_lifetime", "new_lifetime")
        wait_for(lambda: calls == ["running", "new_lifetime"])
    finally:
        release.set()
        sdk.dispose()


def test_custom_privacy_and_output_budget_are_enforced():
    diagnostics = []
    sdk = DebugBundleSdk(transport=Transport())
    sdk.init(
        project_token="dbundle_proj_test", on_diagnostic=diagnostics.append, redact_fields=["customer_internal_id"]
    )
    try:
        sdk._emit_diagnostic("custom", "custom", {"customer_internal_id": "audit-private-value"})
        wait_for(lambda: len(diagnostics) == 1)
        assert "audit-private-value" not in str(diagnostics)
        sdk._emit_diagnostic("oversized", "oversized", {"detail": "é" * 20000})
        wait_for(lambda: len(diagnostics) == 2)
        assert len(str(diagnostics[1]).encode()) < 4096
    finally:
        sdk.dispose()


def test_diagnostic_callback_can_update_context_without_recapturing_itself():
    called = threading.Event()
    transport = Transport()
    sdk = DebugBundleSdk(transport=transport)

    def diagnostic(_value):
        sdk.set_context("operator_marker", "diagnostic observed")
        sdk.capture_log("internal diagnostic", "error")
        called.set()

    sdk.init(project_token="dbundle_proj_test", on_diagnostic=diagnostic)
    try:
        sdk._emit_diagnostic("synthetic", "synthetic")
        assert called.wait(1)
        sdk.capture_log("later application event", "error")
        sdk.flush()
        assert len(transport.events) == 1
        assert transport.events[0]["context"]["operator_marker"] == "diagnostic observed"
    finally:
        sdk.dispose()


def test_rate_limit_recovers_and_coroutine_callbacks_do_not_execute(monkeypatch):
    import debugbundle.internal_diagnostics as module

    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    diagnostics = []
    sdk = DebugBundleSdk(transport=Transport())
    sdk.init(project_token="dbundle_proj_test", on_diagnostic=diagnostics.append)
    try:
        for index in range(10):
            sdk._emit_diagnostic("failure", "static failure")
            wait_for(lambda: len(diagnostics) == index + 1)
        sdk._emit_diagnostic("limited", "limited")
        assert len(diagnostics) == 10
        clock[0] += 60
        sdk._emit_diagnostic("resumed", "resumed")
        wait_for(lambda: len(diagnostics) == 11)
    finally:
        sdk.dispose()
    ran = []

    async def callback(_value):
        ran.append(True)

    sdk.init(project_token="dbundle_proj_test", on_diagnostic=callback)
    try:
        sdk._emit_diagnostic("coroutine", "coroutine")
        time.sleep(0.02)
        assert ran == []
    finally:
        sdk.dispose()
