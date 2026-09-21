from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

DEFAULT_REDACT_FIELDS = {
    "authorization",
    "cookie",
    "credit_card",
    "password",
    "secret",
    "ssn",
    "token",
}

REDACTED_VALUE = "[REDACTED]"
MANDATORY_FIELDS = DEFAULT_REDACT_FIELDS | {
    "api_key", "apikey", "access_token", "refresh_token", "private_key", "bearer", "session_id",
    "passwd", "card_number", "cvv", "cvc", "pin", "expiry", "phone", "otp", "verification_code",
}
_HEADER = re.compile(r"\b(Authorization|Proxy-Authorization|Cookie|Set-Cookie)\s*:\s*[^\r\n]*", re.I)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]{6,}", re.I)
_TOKEN = re.compile(r"\bdbundle_(?:proj|mem|probe|agent)_[A-Za-z0-9_-]+\b")
_PEM = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.I,
)
_PEM_START = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I)
_CARD = re.compile(r"(?<![A-Za-z0-9_-])(?:\d[ -]?){12,18}\d(?![A-Za-z0-9_-])")
_URL = re.compile(r"\bhttps?://[^\s<>\"']+", re.I)
_LABELS = MANDATORY_FIELDS | {"client_secret", "x_api_key", "accessToken", "refreshToken", "privateKey", "clientSecret"}
_ASSIGNMENT = re.compile(
    r"\b(" + "|".join(re.escape(key) for key in sorted(_LABELS, key=len, reverse=True)) +
    r")\b([\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s&,;]+)", re.I,
)


class UnsafeTelemetry(ValueError):
    """A fixed safe failure marker; never includes caller content."""


def _sensitive_key(key: str, fields: set[str]) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.strip()).lower()
    segments = [segment for segment in re.split(r"[^a-z0-9]+", normalized) if segment]
    canonical = {re.sub(r"[^a-z0-9]", "", field.lower()) for field in fields}
    for start in range(len(segments)):
        combined = ""
        for segment in segments[start:]:
            combined += segment
            if combined in canonical:
                return True
    return False


def _luhn(candidate: str) -> bool:
    digits = re.sub(r"[^0-9]", "", candidate)
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _scrub_url(raw: str, fields: set[str]) -> str:
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("invalid_url")
        _ = parsed.port  # Invalid ports make the entire URL unscannable.
        host = parsed.netloc.rsplit("@", 1)[-1]
        authority = f"REDACTED@{host}" if parsed.username or parsed.password else host
        query = urlencode([
            (key, REDACTED_VALUE if _sensitive_key(key, fields)
             or _scrub_text(value, fields, False) != value else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if len(key) <= 128 and _scrub_text(key, fields, False) == key
        ])
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", query, ""))
    except ValueError:
        return REDACTED_VALUE


def _scrub_text(value: str, extra: set[str], scan_urls: bool = True) -> str:
    if _PEM_START.search(value) and not _PEM.search(value):
        return REDACTED_VALUE
    if re.search(r"(?:password|token|secret|authorization|cookie)%3[ad]", value, re.I):
        try:
            value = unquote(value, errors="strict")
        except UnicodeError:
            return REDACTED_VALUE
    result = _PEM.sub(REDACTED_VALUE, value)
    result = _HEADER.sub(lambda match: f"{match.group(1)}: {REDACTED_VALUE}", result)
    result = _BEARER.sub(lambda match: f"{match.group(1)} {REDACTED_VALUE}", result)
    result = _TOKEN.sub(REDACTED_VALUE, result)
    result = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED_VALUE}", result)
    for field in extra:
        named = re.compile(r"\b(" + re.escape(field) + r")\b([\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s&,;]+)", re.I)
        result = named.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED_VALUE}", result)
    result = _CARD.sub(lambda match: REDACTED_VALUE if _luhn(match.group(0)) else match.group(0), result)

    if not scan_urls:
        return result

    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = re.search(r"[).,;]+$", raw)
        trailing = suffix.group(0) if suffix else ""
        return _scrub_url(raw[:len(raw) - len(trailing)] if trailing else raw, MANDATORY_FIELDS | extra) + trailing

    return _URL.sub(replace_url, result)


def sanitize_telemetry(value: Any, additional_fields: set[str] | None = None) -> Any:
    """Mandatory bounded telemetry policy. Failure withholds the input, never returns its original."""
    extra = additional_fields or set()
    if len(extra) > 128 or any(not isinstance(field, str) or not 0 < len(field) <= 64 for field in extra):
        raise UnsafeTelemetry("unsafe_input")
    state = {"nodes": 0, "bytes": 0}
    seen: set[int] = set()
    fields = MANDATORY_FIELDS | extra

    def visit(candidate: Any, depth: int, structured: bool) -> Any:
        state["nodes"] += 1
        if state["nodes"] > 4096:
            raise UnsafeTelemetry("budget_exceeded")
        if depth > 16:
            return REDACTED_VALUE
        if isinstance(candidate, str):
            if len(candidate) > 16 * 1024:
                return REDACTED_VALUE
            try:
                length = len(candidate.encode("utf-8"))
            except UnicodeError as error:
                raise UnsafeTelemetry("unsafe_input") from error
            if length > 16 * 1024:
                return REDACTED_VALUE
            state["bytes"] += length
            if state["bytes"] > 256 * 1024:
                raise UnsafeTelemetry("budget_exceeded")
            if structured and candidate.startswith(("{", "[")):
                try:
                    nested = json.loads(candidate)
                except (ValueError, TypeError):
                    nested = None
                if isinstance(nested, (dict, list)):
                    return json.dumps(visit(nested, 0, False), separators=(",", ":"), ensure_ascii=False)
            result = _scrub_text(candidate, extra)
            if candidate.startswith(("{", "[")) and result == candidate and re.search(
                r"(?:password|token|secret|authorization|cookie)[\"']?\s*[:=]", candidate, re.I
            ):
                return REDACTED_VALUE
            return result
        if candidate is None or isinstance(candidate, (bool, int, float)):
            return candidate
        identity = id(candidate)
        if identity in seen:
            return "[Circular]"
        seen.add(identity)
        try:
            if isinstance(candidate, (list, tuple)):
                if len(candidate) > 256:
                    return REDACTED_VALUE
                return [visit(item, depth + 1, structured) for item in candidate]
            if isinstance(candidate, Mapping):
                if len(candidate) > 256:
                    return REDACTED_VALUE
                output: dict[str, Any] = {}
                for key, nested in candidate.items():
                    if not isinstance(key, str):
                        raise UnsafeTelemetry("unsafe_input")
                    if len(key) > 128:
                        continue
                    state["bytes"] += len(key.encode("utf-8"))
                    if state["bytes"] > 256 * 1024:
                        raise UnsafeTelemetry("budget_exceeded")
                    if _scrub_text(key, extra) != key:
                        continue
                    output[key] = (
                        REDACTED_VALUE if _sensitive_key(key, fields) else visit(nested, depth + 1, structured)
                    )
                return output
            raise UnsafeTelemetry("unsafe_input")
        finally:
            seen.remove(identity)

    try:
        sanitized = visit(value, 0, True)
        if len(json.dumps(sanitized, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 256 * 1024:
            raise UnsafeTelemetry("budget_exceeded")
        return sanitized
    except UnsafeTelemetry:
        raise
    except (TypeError, ValueError, UnicodeError, RuntimeError, OverflowError) as error:
        raise UnsafeTelemetry("unsafe_input") from error


def has_safe_event_identity(event: Mapping[str, Any], extra: set[str] | None = None) -> bool:
    correlation = event.get("correlation") or {}
    if not isinstance(correlation, dict) or len(correlation) > 8:
        return False
    values = [event.get(key) for key in ("schema_version", "sdk_name", "sdk_version")]
    values.extend(correlation.values())
    return all(value is None or isinstance(value, str)
               and sanitize_telemetry(value, extra) == value for value in values)


def redact_value(value: Any, redact_fields: set[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: REDACTED_VALUE if key.lower() in redact_fields else redact_value(nested_value, redact_fields)
            for key, nested_value in value.items()
        }

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item, redact_fields) for item in value]

    return value
