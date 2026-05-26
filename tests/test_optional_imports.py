from __future__ import annotations

import builtins
import importlib
import sys


def test_flask_public_exports_do_not_require_other_optional_frameworks(monkeypatch) -> None:
    original_import = builtins.__import__
    blocked_roots = {"django", "fastapi", "starlette"}

    def guarded_import(name: str, globals=None, locals=None, fromlist=(), level: int = 0):  # type: ignore[no-untyped-def]
        root_name = name.lstrip(".").split(".", 1)[0]
        if root_name in blocked_roots:
            raise AssertionError(f"unexpected optional framework import: {name}")
        return original_import(name, globals, locals, fromlist, level)

    for module_name in list(sys.modules):
        if module_name == "debugbundle" or module_name.startswith("debugbundle."):
            sys.modules.pop(module_name, None)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    debugbundle = importlib.import_module("debugbundle")

    assert callable(debugbundle.instrument_flask)
    assert callable(debugbundle.create_flask_relay_handler)