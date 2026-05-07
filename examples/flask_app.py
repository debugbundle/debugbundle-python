"""Flask example application demonstrating DebugBundle SDK integration.

This module can be used directly or imported in tests to exercise the SDK's
Flask integration (middleware + relay).
"""

from __future__ import annotations

from typing import Any

from debugbundle.core import DebugBundleSdk
from debugbundle.integrations.flask import instrument_flask


class ExampleFlaskApp:
    def __init__(self, sdk: DebugBundleSdk) -> None:
        self._sdk = sdk
        self._app = self._create_app()

    @property
    def app(self) -> Any:
        return self._app

    def _create_app(self) -> Any:
        from flask import Flask, jsonify

        app = Flask(__name__)
        app.config["TESTING"] = True
        instrument_flask(app, sdk=self._sdk)

        @app.route("/")
        def index() -> Any:
            return jsonify(ok=True, framework="flask")

        @app.route("/log")
        def log_endpoint() -> Any:
            self._sdk.capture_log("flask example log", level="error", context={"framework": "flask"})
            return jsonify(ok=True, logged=True), 202

        @app.route("/exception")
        def exception_endpoint() -> Any:
            raise RuntimeError("flask example failure")

        @app.errorhandler(RuntimeError)
        def handle_runtime_error(error: RuntimeError) -> Any:
            return jsonify(error=str(error)), 500

        return app

    def reset(self) -> None:
        self._sdk.dispose()
