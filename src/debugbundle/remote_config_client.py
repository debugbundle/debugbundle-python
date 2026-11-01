"""Owned SDK boundary extracted without changing capture/delivery semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import MINIMAL_CAPTURE_POLICY, parse_remote_config
from .event_support import sdk_config_endpoint, serialize_error
from .evidence_http import evidence_url

if TYPE_CHECKING:
    from .core import DebugBundleSdk


def refresh_remote_config(sdk: DebugBundleSdk, initial: bool = False) -> None:
    with sdk._lock:
        if not sdk._enabled or sdk._fetch_impl is None:
            return
        request_headers: dict[str, str] = {"authorization": f"Bearer {sdk._project_token}"}
        if sdk._remote_config_etag is not None:
            request_headers["if-none-match"] = sdk._remote_config_etag
        fetch = sdk._fetch_impl
        endpoint = evidence_url(sdk_config_endpoint(sdk._endpoint))
        poll_interval = sdk._configured_probes_poll_interval_ms
        generation = sdk._generation

    try:
        response = fetch(endpoint, {"method": "GET", "headers": request_headers})
        status_code = getattr(response, "status_code", None)
        if status_code == 304:
            snapshot = None
        elif status_code == 200:
            payload = response.json()
            snapshot = parse_remote_config(
                payload,
                poll_interval,
                int(sdk._time_provider() * 1000),
            )
        else:
            raise RuntimeError(f"unexpected config status {status_code}")
        headers = response.headers or {}
        etag = headers.get("etag")
        failure: BaseException | None = None
    except BaseException as error:
        status_code = None
        snapshot = None
        etag = None
        failure = error

    with sdk._lock:
        if generation != sdk._generation or not sdk._enabled:
            return
        if failure is None and status_code == 304:
            sdk._schedule_next_remote_config_refresh()
            return
        if failure is None:
            sdk._evidence.observe(payload, True, snapshot is not None)
            if snapshot is None:
                sdk._emit_diagnostic(
                    "remote_probe_config_invalid",
                    "sdk-python received an invalid remote probe config payload",
                )
                if initial:
                    sdk._capture_policy = MINIMAL_CAPTURE_POLICY
                sdk._schedule_next_remote_config_refresh(use_fallback=True)
                return

            sdk._remote_config_snapshot = snapshot
            sdk._capture_policy = snapshot.capture_policy
            if isinstance(etag, str) and len(etag) > 0:
                sdk._remote_config_etag = etag
            sdk._schedule_next_remote_config_refresh()
        else:
            sdk._emit_diagnostic(
                "remote_probe_config_failed",
                "sdk-python failed to refresh remote probe config",
                metadata={"error": serialize_error(failure) if isinstance(failure, Exception) else "fetch_failed"},
            )
            if initial:
                sdk._capture_policy = MINIMAL_CAPTURE_POLICY
            sdk._schedule_next_remote_config_refresh(use_fallback=True)
