from __future__ import annotations

from typing import Any

from ..relay import BrowserRelayHandler


def create_django_relay_view(
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
    )

    def view(request: Any) -> Any:
        from django.http import JsonResponse  # type: ignore[import-untyped]

        headers: dict[str, str] = {}
        if hasattr(request, "headers"):
            headers = {str(key).lower(): str(value) for key, value in request.headers.items()}

        response = handler.handle(
            {
                "method": request.method,
                "headers": headers,
                "body": request.body.decode("utf-8") if isinstance(request.body, bytes) else str(request.body),
                "ipAddress": _get_client_ip(request),
            }
        )

        if response.body is not None:
            return JsonResponse(response.body, status=response.status, safe=False)

        return JsonResponse({}, status=response.status)

    return view


def _get_client_ip(request: Any) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return str(forwarded).split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")
