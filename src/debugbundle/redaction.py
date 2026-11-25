from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
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
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "private_key",
    "bearer",
    "session_id",
    "passwd",
    "card_number",
    "cvv",
    "cvc",
    "pin",
    "expiry",
    "phone",
    "otp",
    "verification_code",
}
MAX_CUSTOM_FIELDS = 128
MAX_CUSTOM_FIELD_LENGTH = 64
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_HEADER = re.compile(r"\b(Authorization|Proxy-Authorization|Cookie|Set-Cookie)\s*:\s*[^\r\n]*", re.I)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]{6,}", re.I)
_TOKEN = re.compile(r"\bdbundle_(?:proj|mem|probe|agent|invite|org_invite|org_transfer)_[A-Za-z0-9_-]+\b")
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


def ascii_lower(value: str) -> str:
    return value.translate(_ASCII_LOWER)


def coerce_config_fields(raw: object) -> tuple[object, ...]:
    """Snapshot caller field lists without splitting strings or swallowing invalid entries."""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(raw)
    return ("",) if isinstance(raw, str) else (raw,)


def exact_sensitive_key(key: str, exact_keys: set[str]) -> bool:
    """ASCII case-insensitive whole-key match with punctuation significant."""
    if not exact_keys:
        return False
    return ascii_lower(key.strip()) in exact_keys


def _coerce_field_list(fields: object) -> list[object]:
    if fields is None:
        return []
    if isinstance(fields, (list, tuple, set, frozenset)):
        return list(fields)
    raise UnsafeTelemetry("unsafe_input")


def _validate_custom_fields(
    additional_fields: object,
    exact_fields: object,
) -> tuple[list[str], list[str], set[str]]:
    extra_raw = _coerce_field_list(additional_fields)
    exact_raw = _coerce_field_list(exact_fields)
    if len(extra_raw) + len(exact_raw) > MAX_CUSTOM_FIELDS:
        raise UnsafeTelemetry("unsafe_input")
    extra_list: list[str] = []
    for field in extra_raw:
        if not isinstance(field, str) or len(field) > MAX_CUSTOM_FIELD_LENGTH or not field.strip():
            raise UnsafeTelemetry("unsafe_input")
        extra_list.append(field)
    exact_list: list[str] = []
    exact_lookup: set[str] = set()
    for field in exact_raw:
        if not isinstance(field, str) or len(field) > MAX_CUSTOM_FIELD_LENGTH or not field.strip():
            raise UnsafeTelemetry("unsafe_input")
        trimmed = field.strip()
        exact_list.append(trimmed)
        exact_lookup.add(ascii_lower(trimmed))
    return extra_list, exact_list, exact_lookup


def _sensitive_key(key: str, canonical: set[str]) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.strip()).lower()
    segments = [segment for segment in re.split(r"[^a-z0-9]+", normalized) if segment]
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


def _scrub_url(
    raw: str,
    broad_canonical: set[str],
    extra_patterns: tuple[re.Pattern[str], ...],
    exact_patterns: tuple[re.Pattern[str], ...],
    exact_lookup: set[str],
) -> str:
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("invalid_url")
        _ = parsed.port  # Invalid ports make the entire URL unscannable.
        host = parsed.netloc.rsplit("@", 1)[-1]
        authority = f"REDACTED@{host}" if parsed.username or parsed.password else host
        query = urlencode([
            (
                key,
                REDACTED_VALUE
                if _sensitive_key(key, broad_canonical)
                or exact_sensitive_key(key, exact_lookup)
                or _scrub_text(value, broad_canonical, extra_patterns, exact_patterns, exact_lookup, False) != value
                else value,
            )
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if len(key) <= 128
            and _scrub_text(key, broad_canonical, extra_patterns, exact_patterns, exact_lookup, False) == key
        ])
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", query, ""))
    except ValueError:
        return REDACTED_VALUE


def _scrub_text(
    value: str,
    broad_canonical: set[str],
    extra_patterns: tuple[re.Pattern[str], ...],
    exact_patterns: tuple[re.Pattern[str], ...],
    exact_lookup: set[str],
    scan_urls: bool = True,
) -> str:
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
    for pattern in extra_patterns:
        result = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED_VALUE}", result)
    for pattern in exact_patterns:
        result = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED_VALUE}", result)
    result = _CARD.sub(lambda match: REDACTED_VALUE if _luhn(match.group(0)) else match.group(0), result)

    if not scan_urls:
        return result

    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = re.search(r"[).,;]+$", raw)
        trailing = suffix.group(0) if suffix else ""
        return (
            _scrub_url(
                raw[: len(raw) - len(trailing)] if trailing else raw,
                broad_canonical,
                extra_patterns,
                exact_patterns,
                exact_lookup,
            )
            + trailing
        )

    return _URL.sub(replace_url, result)


def sanitize_telemetry(
    value: Any,
    additional_fields: Collection[object] | None = None,
    exact_fields: Collection[object] | None = None,
) -> Any:
    """Mandatory bounded telemetry policy. Failure withholds the input, never returns its original."""
    extra_list, exact_list, exact_lookup = _validate_custom_fields(additional_fields, exact_fields)
    state = {"nodes": 0, "bytes": 0}
    seen: set[int] = set()
    broad_fields = MANDATORY_FIELDS | set(extra_list)
    broad_canonical = {re.sub(r"[^a-z0-9]", "", field.lower()) for field in broad_fields}
    extra_patterns = tuple(
        re.compile(r"\b(" + re.escape(field) + r")\b([\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s&,;]+)", re.I)
        for field in extra_list
    )
    # Non-identifier boundaries keep `tag=secret` redacted while preserving `tag_name=script` and `x-tag-name=script`.
    exact_patterns = tuple(
        re.compile(
            r"(?<![A-Za-z0-9_-])("
            + re.escape(field)
            + r")(?![A-Za-z0-9_-])([\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s&,;]+)",
            re.I | re.ASCII,
        )
        for field in exact_list
    )

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
            result = _scrub_text(candidate, broad_canonical, extra_patterns, exact_patterns, exact_lookup)
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
                    if _scrub_text(key, broad_canonical, extra_patterns, exact_patterns, exact_lookup) != key:
                        continue
                    output[key] = (
                        REDACTED_VALUE
                        if _sensitive_key(key, broad_canonical) or exact_sensitive_key(key, exact_lookup)
                        else visit(nested, depth + 1, structured)
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


def has_safe_event_identity(
    event: Mapping[str, Any],
    extra: Collection[object] | None = None,
    exact: Collection[object] | None = None,
) -> bool:
    try:
        _validate_custom_fields(extra, exact)
    except UnsafeTelemetry:
        return False
    correlation = event.get("correlation") or {}
    if not isinstance(correlation, dict) or len(correlation) > 8:
        return False
    values = [event.get(key) for key in ("schema_version", "sdk_name", "sdk_version")]
    values.extend(correlation.values())
    try:
        return all(
            value is None or (isinstance(value, str) and sanitize_telemetry(value, extra, exact) == value)
            for value in values
        )
    except UnsafeTelemetry:
        return False


def redact_value(
    value: Any,
    redact_fields: set[str],
    exact_fields: Collection[object] | None = None,
) -> Any:
    exact_lookup = (
        {ascii_lower(field.strip()) for field in exact_fields if isinstance(field, str) and field.strip()}
        if exact_fields
        else set()
    )

    def walk(candidate: Any) -> Any:
        if isinstance(candidate, Mapping):
            return {
                key: REDACTED_VALUE
                if isinstance(key, str) and (key.lower() in redact_fields or exact_sensitive_key(key, exact_lookup))
                else walk(nested_value)
                for key, nested_value in candidate.items()
            }
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
            return [walk(item) for item in candidate]
        return candidate

    return walk(value)
