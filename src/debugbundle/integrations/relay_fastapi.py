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
    project_mode: str | None = None,
    project_token: str | None = None,
    endpoint: str | None = None,
    local_events_dir: str | None = None,
    spool_dir: str | None = None,
    durable_write: bool = True,
    service: str | None = None,
    environment: str | None = None,
    forward_transport: Any = None,
    evidence_capabilities: object = None,
    route_path: str = "/debugbundle/browser",
) -> Any:
    handler = BrowserRelayHandler(
        allowed_origins=allowed_origins or [],
        max_body_bytes=max_body_bytes,
        rate_limit_per_minute=rate_limit_per_minute,
        on_accept=on_accept,
        project_mode=project_mode,
        project_token=project_token,
        endpoint=endpoint,
        local_events_dir=local_events_dir,
        spool_dir=spool_dir,
        durable_write=durable_write,
        service=service,
        environment=environment,
        forward_transport=forward_transport,
        evidence_capabilities=evidence_capabilities,
    )

    def register(app: Any) -> None:
        @app.api_route(route_path, methods=["POST", "OPTIONS"])
        async def debugbundle_browser_relay(request: Request) -> Response:
            body = bytearray()
            async for chunk in request.stream():
                remaining = handler.max_body_bytes + 1 - len(body)
                body.extend(chunk[:remaining])
                if len(body) > handler.max_body_bytes:
                    break
            headers = {str(key): str(value) for key, value in request.headers.items()}
            ip_address = request.client.host if request.client else None

            response = handler.handle(
                {
                    "method": request.method,
                    "url": str(request.url),
                    "headers": headers,
                    "body": bytes(body),
                    "ipAddress": ip_address,
                }
            )

            if response.body is not None:
                return JSONResponse(content=response.body, status_code=response.status, headers=response.headers)
            return Response(status_code=response.status, headers=response.headers)

    return register
