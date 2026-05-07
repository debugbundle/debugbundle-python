from __future__ import annotations

from typing import Any

from .common import correlation_context, request_payload, resolve_sdk, response_payload


class DebugBundleDjangoMiddleware:
    def __init__(self, get_response: Any, sdk: Any = None) -> None:
        self._get_response = get_response
        self._sdk = resolve_sdk(sdk)
        self._sdk.capture_logging()

    def __call__(self, request: Any) -> Any:
        started_at = self._sdk._time_provider()
        context_token = self._sdk._bind_scoped_context(correlation_context(request.headers))
        trigger_token = self._sdk.begin_request(
            request_payload(
                method=request.method,
                path=request.path,
                headers=request.headers,
                query=request.GET,
            )
        )
        try:
            response = self._get_response(request)
        except Exception as error:
            self._sdk.capture_exception(
                error,
                context={
                    "request": request_payload(
                        method=request.method,
                        path=request.path,
                        headers=request.headers,
                        query=request.GET,
                    )
                },
            )
            raise
        else:
            self._sdk.capture_request(
                request_payload(
                    method=request.method,
                    path=request.path,
                    headers=request.headers,
                    query=request.GET,
                ),
                response_payload(status_code=response.status_code, started_at=started_at),
            )
            return response
        finally:
            self._sdk.end_request(trigger_token)
            self._sdk._reset_scoped_context(context_token)
