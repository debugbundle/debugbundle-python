from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from debugbundle.trigger_token import (
    HEADER_NAME,
    QUERY_PARAMETER_NAME,
    TOKEN_PREFIX,
    resolve_request_trigger_directives,
)


def _build_trigger_token(
    key: str,
    activation_id: str = "act-1",
    label_pattern: str = "checkout.*",
    service: str = "*",
    environment: str = "*",
    expires_at: str | None = None,
) -> str:
    if expires_at is None:
        expires_at = "2099-12-31T23:59:59.000Z"

    payload = {
        "activation_id": activation_id,
        "label_pattern": label_pattern,
        "service": service,
        "environment": environment,
        "trigger_expires_at": expires_at,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).rstrip(b"=").decode()
    signature = hmac.new(key.encode(), payload_b64.encode(), hashlib.sha256).digest()
    signature_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{TOKEN_PREFIX}{payload_b64}.{signature_b64}"


KEY = "test-trigger-key-32chars-minimum"
NOW_MS = int(time.time() * 1000)


def test_extracts_directive_from_header() -> None:
    token = _build_trigger_token(KEY)
    request = {"headers": {HEADER_NAME: token}}
    directives = resolve_request_trigger_directives(request, KEY, NOW_MS)
    assert len(directives) == 1
    assert directives[0].id == "act-1"
    assert directives[0].label_pattern == "checkout.*"
    assert directives[0].service == "*"
    assert directives[0].environment == "*"


def test_extracts_directive_from_query_parameter() -> None:
    token = _build_trigger_token(KEY)
    request = {"query": {QUERY_PARAMETER_NAME: token}}
    directives = resolve_request_trigger_directives(request, KEY, NOW_MS)
    assert len(directives) == 1
    assert directives[0].id == "act-1"


def test_header_takes_priority_over_query() -> None:
    header_token = _build_trigger_token(KEY, activation_id="header-act")
    query_token = _build_trigger_token(KEY, activation_id="query-act")
    request = {
        "headers": {HEADER_NAME: header_token},
        "query": {QUERY_PARAMETER_NAME: query_token},
    }
    directives = resolve_request_trigger_directives(request, KEY, NOW_MS)
    assert len(directives) == 1
    assert directives[0].id == "header-act"


def test_case_insensitive_header_name() -> None:
    token = _build_trigger_token(KEY)
    request = {"headers": {"X-DebugBundle-Probe-Trigger": token}}
    directives = resolve_request_trigger_directives(request, KEY, NOW_MS)
    assert len(directives) == 1


def test_returns_empty_on_invalid_hmac() -> None:
    token = _build_trigger_token(KEY)
    directives = resolve_request_trigger_directives(
        {"headers": {HEADER_NAME: token}},
        "wrong-key-definitely-not-matching",
        NOW_MS,
    )
    assert directives == []


def test_returns_empty_on_expired_token() -> None:
    token = _build_trigger_token(KEY, expires_at="2020-01-01T00:00:00.000Z")
    directives = resolve_request_trigger_directives(
        {"headers": {HEADER_NAME: token}},
        KEY,
        NOW_MS,
    )
    assert directives == []


def test_returns_empty_on_missing_prefix() -> None:
    token = _build_trigger_token(KEY)
    bad_token = token.replace(TOKEN_PREFIX, "badprefix_")
    directives = resolve_request_trigger_directives(
        {"headers": {HEADER_NAME: bad_token}},
        KEY,
        NOW_MS,
    )
    assert directives == []


def test_returns_empty_on_malformed_base64() -> None:
    directives = resolve_request_trigger_directives(
        {"headers": {HEADER_NAME: f"{TOKEN_PREFIX}!!!invalid!!!.alsoinvalid"}},
        KEY,
        NOW_MS,
    )
    assert directives == []


def test_returns_empty_when_no_trigger_token_key() -> None:
    token = _build_trigger_token(KEY)
    directives = resolve_request_trigger_directives(
        {"headers": {HEADER_NAME: token}},
        None,
        NOW_MS,
    )
    assert directives == []


def test_returns_empty_when_request_is_none() -> None:
    directives = resolve_request_trigger_directives(None, KEY, NOW_MS)
    assert directives == []


def test_returns_empty_on_missing_separator() -> None:
    directives = resolve_request_trigger_directives(
        {"headers": {HEADER_NAME: f"{TOKEN_PREFIX}noseparatorhere"}},
        KEY,
        NOW_MS,
    )
    assert directives == []


def test_header_value_from_list() -> None:
    token = _build_trigger_token(KEY)
    request = {"headers": {HEADER_NAME: [token]}}
    directives = resolve_request_trigger_directives(request, KEY, NOW_MS)
    assert len(directives) == 1
    assert directives[0].id == "act-1"


def test_preserves_directive_fields() -> None:
    token = _build_trigger_token(
        KEY,
        activation_id="uuid-123",
        label_pattern="payment.process",
        service="payment-svc",
        environment="staging",
    )
    directives = resolve_request_trigger_directives(
        {"headers": {HEADER_NAME: token}},
        KEY,
        NOW_MS,
    )
    assert len(directives) == 1
    d = directives[0]
    assert d.id == "uuid-123"
    assert d.label_pattern == "payment.process"
    assert d.service == "payment-svc"
    assert d.environment == "staging"
