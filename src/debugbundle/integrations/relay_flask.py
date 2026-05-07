from __future__ import annotations

from typing import Any

from ..relay import BrowserRelayHandler


def create_flask_relay_handler(
    *,
    allowed_origins: list[str] | None = None,
    max_body_bytes: int = 262_144,
    rate_limit_per_minute: int = 60,
    on_accept: Any = None,
    route_path: str = "/debugbundle/browser",
) -> Any:
    handler = BrowserRelayHandler(
        allowed_origins=allowed_origins or [],
        max_body_bytes=max_body_bytes,
        rate_limit_per_minute=rate_limit_per_minute,
        on_accept=on_accept,
    )

    def register(app: Any) -> None:
        from flask import Response, request

        @app.route(route_path, methods=["POST"])
        def debugbundle_browser_relay() -> Response:
            response = handler.handle(
                {
                    "method": request.method,
                    "headers": dict(request.headers.items()),
                    "body": request.get_data(as_text=True),
                    "ipAddress": request.remote_addr,
                }
            )

            import json

            body = json.dumps(response.body) if response.body is not None else ""
            return Response(
                body,
                status=response.status,
                content_type="application/json",
            )

    return register
