from __future__ import annotations

from typing import Any

from .common import correlation_context, request_payload, resolve_sdk, response_payload


def instrument_flask(app: Any, sdk: Any = None) -> Any:
    resolved_sdk = resolve_sdk(sdk)
    resolved_sdk.capture_logging(app.logger)

    from flask import g, request

    @app.before_request
    def debugbundle_before_request() -> None:
        g._debugbundle_started_at = resolved_sdk._time_provider()
        g._debugbundle_context_token = resolved_sdk._bind_scoped_context(
            correlation_context(dict(request.headers.items()))
        )
        g._debugbundle_trigger_token = resolved_sdk.begin_request(
            request_payload(
                method=request.method,
                path=request.path,
                headers=dict(request.headers.items()),
                query=dict(request.args.items()),
            )
        )

    @app.after_request
    def debugbundle_after_request(response: Any) -> Any:
        started_at = getattr(g, "_debugbundle_started_at", resolved_sdk._time_provider())
        resolved_sdk.capture_request(
            request_payload(
                method=request.method,
                path=request.path,
                headers=dict(request.headers.items()),
                query=dict(request.args.items()),
            ),
            response_payload(status_code=response.status_code, started_at=started_at),
        )
        return response

    @app.teardown_request
    def debugbundle_teardown_request(error: BaseException | None) -> None:
        token = getattr(g, "_debugbundle_context_token", None)
        trigger_token = getattr(g, "_debugbundle_trigger_token", None)
        try:
            if error is None:
                return
            resolved_sdk.capture_exception(
                error,
                context={
                    "request": request_payload(
                        method=request.method,
                        path=request.path,
                        headers=dict(request.headers.items()),
                        query=dict(request.args.items()),
                    ),
                },
            )
        finally:
            if trigger_token is not None:
                resolved_sdk.end_request(trigger_token)
            if token is not None:
                resolved_sdk._reset_scoped_context(token)

    return app
