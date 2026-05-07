from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

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


def redact_value(value: Any, redact_fields: set[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: REDACTED_VALUE if key.lower() in redact_fields else redact_value(nested_value, redact_fields)
            for key, nested_value in value.items()
        }

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item, redact_fields) for item in value]

    return value
