"""FastAPI example application demonstrating DebugBundle SDK integration.

This module can be used directly or imported in tests to exercise the SDK's
FastAPI integration (middleware + relay).
"""

from __future__ import annotations

from typing import Any

from debugbundle.core import DebugBundleSdk
from debugbundle.integrations.fastapi import instrument_fastapi


class ExampleFastAPIApp:
    def __init__(self, sdk: DebugBundleSdk) -> None:
        self._sdk = sdk
        self._app = self._create_app()

    @property
    def app(self) -> Any:
        return self._app

    def _create_app(self) -> Any:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse

        app = FastAPI()
        instrument_fastapi(app, sdk=self._sdk)

        @app.get("/")
        async def index() -> dict[str, Any]:
            return {"ok": True, "framework": "fastapi"}

        @app.get("/log")
        async def log_endpoint() -> JSONResponse:
            self._sdk.capture_log("fastapi example log", level="error", context={"framework": "fastapi"})
            return JSONResponse(content={"ok": True, "logged": True}, status_code=202)

        @app.get("/exception")
        async def exception_endpoint() -> dict[str, Any]:
            raise RuntimeError("fastapi example failure")

        @app.exception_handler(RuntimeError)
        async def handle_runtime_error(request: Any, exc: RuntimeError) -> JSONResponse:
            return JSONResponse(content={"error": str(exc)}, status_code=500)

        return app

    def reset(self) -> None:
        self._sdk.dispose()
