from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..relay import BrowserRelayHandler


def create_fastapi_relay_handler(
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
        @app.post(route_path)
        async def debugbundle_browser_relay(request: Request) -> Response:
            body = await request.body()
            headers = {str(key): str(value) for key, value in request.headers.items()}
            ip_address = request.client.host if request.client else None

            response = handler.handle(
                {
                    "method": request.method,
                    "headers": headers,
                    "body": body.decode("utf-8") if isinstance(body, bytes) else str(body),
                    "ipAddress": ip_address,
                }
            )

            if response.body is not None:
                return JSONResponse(content=response.body, status_code=response.status)
            return Response(status_code=response.status)

    return register
