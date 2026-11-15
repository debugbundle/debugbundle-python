"""Own SDK resources created by the split core capture/safety test modules."""

import sys

import pytest

from debugbundle.core import DebugBundleSdk


@pytest.fixture(autouse=True)
def close_core_test_clients(request, monkeypatch):
    if request.module.__name__ not in {"test_sdk", "test_sdk_capture_safety"}:
        yield
        return
    clients = []
    original_init = DebugBundleSdk.__init__
    previous_hook = sys.excepthook

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        clients.append(self)

    monkeypatch.setattr(DebugBundleSdk, "__init__", initialize)
    try:
        yield
    finally:
        # Timers hold their clients alive; collection alone cannot isolate tests.
        for client in reversed(clients):
            client.dispose()
        sys.excepthook = previous_hook
