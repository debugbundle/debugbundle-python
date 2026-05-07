from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from .config import RemoteProbeDirective, _as_non_empty_string, _expires_at_ms

HEADER_NAME = "x-debugbundle-probe-trigger"
QUERY_PARAMETER_NAME = "_debug_probe"
TOKEN_PREFIX = "dbundle_probe_"


def resolve_request_trigger_directives(
    request: dict[str, Any] | None,
    trigger_token_key: str | None,
    now_ms: int,
) -> list[RemoteProbeDirective]:
    if request is None or not trigger_token_key:
        return []

    token = _extract_trigger_token(request)
    if token is None or not token.startswith(TOKEN_PREFIX):
        return []

    encoded = token[len(TOKEN_PREFIX) :]
    separator_index = encoded.find(".")
    if separator_index <= 0 or separator_index == len(encoded) - 1:
        return []

    payload_segment = encoded[:separator_index]
    signature_segment = encoded[separator_index + 1 :]
    if not _has_valid_signature(payload_segment, signature_segment, trigger_token_key):
        return []

    payload = _decode_payload_segment(payload_segment)
    if payload is None or _expires_at_ms(payload["trigger_expires_at"]) <= now_ms:
        return []

    return [
        RemoteProbeDirective(
            id=payload["activation_id"],
            label_pattern=payload["label_pattern"],
            service=payload["service"],
            environment=payload["environment"],
            expires_at=payload["trigger_expires_at"],
        )
    ]


def _extract_trigger_token(request: dict[str, Any]) -> str | None:
    headers = request.get("headers")
    if isinstance(headers, dict):
        header_token = _extract_map_value(headers, HEADER_NAME, case_insensitive=True)
        if header_token is not None:
            return header_token

    query = request.get("query")
    if isinstance(query, dict):
        return _extract_map_value(query, QUERY_PARAMETER_NAME, case_insensitive=False)

    return None


def _decode_payload_segment(payload_segment: str) -> dict[str, str] | None:
    try:
        decoded = _base64url_decode(payload_segment)
        if decoded is None:
            return None
        parsed = json.loads(decoded)
    except Exception:
        return None

    if not isinstance(parsed, dict):
        return None

    activation_id = _as_non_empty_string(parsed.get("activation_id"))
    label_pattern = _as_non_empty_string(parsed.get("label_pattern"))
    service = _as_non_empty_string(parsed.get("service"))
    environment = _as_non_empty_string(parsed.get("environment"))
    expires_at = _as_non_empty_string(parsed.get("trigger_expires_at"))

    if activation_id is None or label_pattern is None or service is None or environment is None or expires_at is None:
        return None

    if _expires_at_ms(expires_at) == 0:
        return None

    return {
        "activation_id": activation_id,
        "label_pattern": label_pattern,
        "service": service,
        "environment": environment,
        "trigger_expires_at": expires_at,
    }


def _has_valid_signature(payload_segment: str, signature_segment: str, trigger_token_key: str) -> bool:
    expected = hmac.new(trigger_token_key.encode("utf-8"), payload_segment.encode("utf-8"), hashlib.sha256).digest()
    actual = _base64url_decode_bytes(signature_segment)
    if actual is None or len(expected) != len(actual):
        return False
    return hmac.compare_digest(expected, actual)


def _extract_map_value(mapping: dict[str, Any], key: str, *, case_insensitive: bool) -> str | None:
    for candidate_key, value in mapping.items():
        if case_insensitive:
            matches = str(candidate_key).lower() == key.lower()
        else:
            matches = str(candidate_key) == key

        if not matches:
            continue

        if isinstance(value, str) and value:
            return value

        if isinstance(value, (list, tuple)):
            for entry in value:
                if isinstance(entry, str) and entry:
                    return entry

    return None


def _base64url_decode(value: str) -> str | None:
    raw = _base64url_decode_bytes(value)
    if raw is None:
        return None
    return raw.decode("utf-8")


def _base64url_decode_bytes(value: str) -> bytes | None:
    try:
        padding = len(value) % 4
        if padding > 0:
            value += "=" * (4 - padding)
        return base64.urlsafe_b64decode(value)
    except Exception:
        return None
