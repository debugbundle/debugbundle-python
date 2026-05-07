from __future__ import annotations

from typing import Any

from .common import correlation_context, request_payload, resolve_sdk, response_payload


class DebugBundleFastAPIMiddleware:
    def __init__(self, app: Any, sdk: Any) -> None:
        self.app = app
        self.sdk = resolve_sdk(sdk)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        query_string = scope.get("query_string", b"").decode("latin-1")
        query: dict[str, str] = {}
        if query_string:
            for pair in query_string.split("&"):
                if not pair:
                    continue
                key, _, value = pair.partition("=")
                query[key] = value

        started_at = self.sdk._time_provider()
        status_holder: dict[str, int] = {}
        context_token = self.sdk._bind_scoped_context(correlation_context(headers))
        trigger_token = self.sdk.begin_request(
            request_payload(
                method=scope.get("method", "GET"),
                path=scope.get("path", "/"),
                headers=headers,
                query=query,
            )
        )

        async def wrapped_send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status_code"] = int(message.get("status", 200))
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        except Exception as error:
            self.sdk.capture_request(
                request_payload(
                    method=scope.get("method", "GET"),
                    path=scope.get("path", "/"),
                    headers=headers,
                    query=query,
                ),
                response_payload(status_code=500, started_at=started_at),
            )
            self.sdk.capture_exception(
                error,
                context={
                    "request": request_payload(
                        method=scope.get("method", "GET"),
                        path=scope.get("path", "/"),
                        headers=headers,
                        query=query,
                    )
                },
            )
            raise
        finally:
            status_code = status_holder.get("status_code")
            try:
                if status_code is not None:
                    self.sdk.capture_request(
                        request_payload(
                            method=scope.get("method", "GET"),
                            path=scope.get("path", "/"),
                            headers=headers,
                            query=query,
                        ),
                        response_payload(status_code=status_code, started_at=started_at),
                    )
            finally:
                self.sdk.end_request(trigger_token)
                self.sdk._reset_scoped_context(context_token)


def instrument_fastapi(app: Any, sdk: Any = None) -> Any:
    resolved_sdk = resolve_sdk(sdk)
    resolved_sdk.capture_logging()
    app.add_middleware(DebugBundleFastAPIMiddleware, sdk=resolved_sdk)
    return app
