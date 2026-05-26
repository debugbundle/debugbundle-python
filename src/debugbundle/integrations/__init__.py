import importlib
from typing import Any

_OPTIONAL_EXPORTS = {
    "DebugBundleDjangoMiddleware": (".django", "DebugBundleDjangoMiddleware"),
    "DebugBundleFastAPIMiddleware": (".fastapi", "DebugBundleFastAPIMiddleware"),
    "create_django_relay_view": (".relay_django", "create_django_relay_view"),
    "create_fastapi_relay_handler": (".relay_fastapi", "create_fastapi_relay_handler"),
    "create_flask_relay_handler": (".relay_flask", "create_flask_relay_handler"),
    "instrument_fastapi": (".fastapi", "instrument_fastapi"),
    "instrument_flask": (".flask", "instrument_flask"),
}


def __getattr__(name: str) -> Any:
    if name not in _OPTIONAL_EXPORTS:
        raise AttributeError(f"module 'debugbundle.integrations' has no attribute {name!r}")

    module_name, attribute_name = _OPTIONAL_EXPORTS[name]
    module = importlib.import_module(module_name, __name__)
    return getattr(module, attribute_name)

__all__ = [
    "DebugBundleDjangoMiddleware",
    "DebugBundleFastAPIMiddleware",
    "create_django_relay_view",
    "create_fastapi_relay_handler",
    "create_flask_relay_handler",
    "instrument_fastapi",
    "instrument_flask",
]
