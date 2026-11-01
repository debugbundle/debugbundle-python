from __future__ import annotations

import json
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .acknowledgement import decide_acknowledgement
from .evidence_delivery import EvidenceDelivery
from .evidence_wire import FEATURES, strip_unsupported
from .ingestion_wire import MAX_EVENT_BYTES, chunks, event_bytes, fit_event
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
        if not stat.S_ISREG(os.lstat(written_file_path).st_mode):
            return
        marker = f"{written_file_path}{RELAY_SPOOL_DELIVERED_MARKER_SUFFIX}"
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | OPTIONAL_NOFOLLOW_FLAG, LOCAL_EVENT_FILE_MODE)
        os.close(fd)
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
    def __init__(
        self,
        endpoint: str,
        transport: Callable[[Mapping[str, object]], object] | None = None,
        evidence: EvidenceDelivery | None = None,
    ) -> None:
        self._transport = transport or HttpTransport(endpoint)
        self._evidence = evidence or EvidenceDelivery()

    @staticmethod
    def prepare_event(event: dict[str, Any], project_token: str) -> dict[str, Any] | None:
        owned = dict(event, project_token=project_token)
        if event.get("event_type") == "analytics_event":
            reduced = owned if event_bytes(owned) <= MAX_EVENT_BYTES else None
        else:
            reduced = fit_event(owned)
        return {key: value for key, value in reduced.items() if key != "project_token"} if reduced else None

    def send(self, project_token: str, events: list[dict[str, Any]], discover: bool = False) -> tuple[bool, bool]:
        configured, success, _complete = self.send_verified(project_token, events, discover)
        return configured, success

    def send_verified(
        self, project_token: str, events: list[dict[str, Any]], discover: bool = False
    ) -> tuple[bool, bool, bool]:
        if not project_token:
            return False, False, False
        deadline = time.monotonic() + 5.0
        try:
            complete = True
            # Start with the neutral intersection, so a previously unknown path
            # can learn proof from this batch's canonical existing ACK traffic.
            features = list(FEATURES)
            for original in chunks(attach_project_token(events, project_token)):
                if time.monotonic() >= deadline:
                    return True, False, False
                batch: list[dict[str, Any]] = []
                for event in original:
                    selected = (
                        strip_unsupported(event, self._evidence.features)
                        if event.get("event_type") != "analytics_event"
                        else event
                    )
                    if selected is None:
                        return True, False, False
                    batch.append(selected)
                success, canonical = self._send_chunk(project_token, batch, discover, deadline)
                features = [feature for feature in features if feature in self._evidence.features]
                self._evidence.features = list(features)
                if not success or time.monotonic() >= deadline:
                    return True, False, False
                complete = complete and canonical
            return True, True, complete
        except Exception:
            return True, False, False

    def _send_chunk(
        self, project_token: str, events: list[dict[str, Any]], discover: bool, deadline: float
    ) -> tuple[bool, bool]:
        try:

            def send(batch: list[dict[str, Any]]):
                if time.monotonic() >= deadline:
                    raise TimeoutError("relay forwarding deadline exhausted")
                request = {"project_token": project_token, "events": batch}
                response = (
                    self._transport.send_until(request, deadline)
                    if isinstance(self._transport, HttpTransport)
                    else self._transport(request)
                )
                return coerce_transport_response(response)

            response = send(events)
            if not 200 <= response.status_code < 300:
                return False, False
            required = discover or isinstance(self._transport, HttpTransport)
            ack = decide_acknowledgement(response.body, len(events), required)
            lost = self._evidence.observe(response.body, required, ack.kind == "acknowledged")
            if ack.kind == "legacy":
                return True, False
            if ack.kind != "acknowledged":
                return False, False
            if ack.accepted == len(events):
                return True, True
            retries = []
            if lost:
                for index, reason in ack.terminal_errors:
                    original = events[index]
                    if reason == "invalid_event" and original.get("event_type") == "frontend_exception":
                        reduced = strip_unsupported(original, self._evidence.features)
                        if reduced is not None and reduced != original:
                            retries.append(reduced)
            if not retries:
                return False, False
            response = send(retries)
            if not 200 <= response.status_code < 300:
                return False, False
            retry_ack = decide_acknowledgement(response.body, len(retries), True)
            self._evidence.observe(response.body, required, retry_ack.kind == "acknowledged")
            complete = retry_ack.kind == "acknowledged" and ack.accepted + retry_ack.accepted == len(events)
            return complete, complete
        except Exception:
            return False, False


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
