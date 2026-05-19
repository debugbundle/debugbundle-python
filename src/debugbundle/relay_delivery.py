from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .transport import HttpTransport, coerce_transport_response

LOCAL_EVENTS_DIRECTORY_MODE = 0o700
LOCAL_EVENT_FILE_MODE = 0o600
RELAY_SPOOL_DELIVERED_MARKER_SUFFIX = ".delivered"
OPTIONAL_NOFOLLOW_FLAG = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class RelayWriteResult:
    status_code: int
    written_file_path: str | None = None


def resolve_default_local_events_dir(cwd: str | None = None) -> str:
    return os.path.join(cwd or os.getcwd(), ".debugbundle", "local", "events")


def resolve_default_relay_spool_dir(cwd: str | None = None) -> str:
    return os.path.join(cwd or os.getcwd(), ".debugbundle", "local", "browser-relay-spool")


def attach_project_token(events: list[dict[str, Any]], project_token: str) -> list[dict[str, Any]]:
    return [{**event, "project_token": project_token} for event in events]


def mark_spool_file_delivered(written_file_path: str) -> None:
    try:
        with open(f"{written_file_path}{RELAY_SPOOL_DELIVERED_MARKER_SUFFIX}", "w", encoding="utf-8"):
            pass
    except OSError:
        # Durable acceptance already happened at the spool write; marker creation is maintenance metadata only.
        return


class AtomicRelayFileTransport:
    def __init__(self, events_dir: str, service_name: str) -> None:
        self._events_dir = os.path.abspath(os.path.normpath(events_dir))
        self._service_name = _sanitize_service_name(service_name)
        self._sequence = 0
        self._dir_ensured = False
        self._lock = threading.Lock()

    def write(self, events: list[dict[str, Any]]) -> RelayWriteResult:
        if not events:
            return RelayWriteResult(status_code=202)

        try:
            with self._lock:
                if not self._dir_ensured:
                    os.makedirs(self._events_dir, mode=LOCAL_EVENTS_DIRECTORY_MODE, exist_ok=True)
                    self._dir_ensured = True

                timestamp = int(time.time() * 1000)
                self._sequence += 1
                filename = f"{timestamp}-{self._sequence}-{self._service_name}.events.json"
                final_path = os.path.join(self._events_dir, filename)
                tmp_path = f"{final_path}.tmp-{secrets.token_hex(8)}"

                _assert_not_symlink(final_path)
                _write_secure_temp_file(tmp_path, json.dumps(events, separators=(",", ":")))
                os.replace(tmp_path, final_path)
                return RelayWriteResult(status_code=202, written_file_path=final_path)
        except OSError:
            _cleanup_temp_files(self._events_dir)
            return RelayWriteResult(status_code=500)


class RelayForwardTransport:
    def __init__(self, endpoint: str, transport: Callable[[Mapping[str, object]], object] | None = None) -> None:
        self._transport = transport or HttpTransport(endpoint)

    def send(self, project_token: str, events: list[dict[str, Any]]) -> tuple[bool, bool]:
        if not project_token:
            return (False, False)

        try:
            response = coerce_transport_response(
                self._transport(
                    {
                        "project_token": project_token,
                        "events": attach_project_token(events, project_token),
                    }
                )
            )
        except Exception:
            return (True, False)

        return (True, 200 <= response.status_code < 300)


def _sanitize_service_name(service_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", service_name.strip())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized or "service"


def _assert_not_symlink(target_path: str) -> None:
    try:
        if os.path.islink(target_path):
            raise OSError("symlink_path_rejected")
    except OSError:
        raise


def _write_secure_temp_file(tmp_path: str, payload: str) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | OPTIONAL_NOFOLLOW_FLAG
    encoded = payload.encode("utf-8")
    fd = os.open(tmp_path, flags, LOCAL_EVENT_FILE_MODE)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def _cleanup_temp_files(events_dir: str) -> None:
    try:
        for entry in os.listdir(events_dir):
            if ".tmp-" not in entry:
                continue
            try:
                os.remove(os.path.join(events_dir, entry))
            except OSError:
                continue
    except OSError:
        return