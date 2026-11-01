"""Byte accounting for validated evidence, using the accepting JSON number form."""

from __future__ import annotations

import json
import math
from typing import Any

_OVER = 32769


def _number_size(value: int | float) -> int:
    if type(value) is int and abs(value) <= 9007199254740991:
        return len(str(value))
    number = float(value)
    if not math.isfinite(number):
        return _OVER
    if number == 0:
        return 1
    negative = number < 0
    mantissa, _, exponent = repr(abs(number)).lower().partition("e")
    before = len(mantissa.partition(".")[0])
    raw = mantissa.replace(".", "")
    leading = len(raw) - len(raw.lstrip("0"))
    digits = raw.lstrip("0").rstrip("0")
    position = before + int(exponent or "0") - leading
    power = position - 1
    # ECMAScript uses decimal notation for [1e-6, 1e21), independent
    # of the producer language's exponent padding and float formatting.
    if -6 <= power <= 20:
        length = 2 - position + len(digits) if position <= 0 else max(position, len(digits))
        if 0 < position < len(digits):
            length += 1
    else:
        length = len(digits) + (1 if len(digits) > 1 else 0) + 2 + len(str(abs(power)))
    return length + int(negative)


def canonical_size(value: Any) -> int:
    remaining = 4096

    def count(item: Any, depth: int) -> int:
        nonlocal remaining
        remaining -= 1
        if depth > 16 or remaining < 0:
            return _OVER
        if item is None:
            return 4
        if type(item) is bool:
            return 4 if item else 5
        if type(item) is str:
            return len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if type(item) is int or type(item) is float:
            return _number_size(item)
        if type(item) is list and len(item) <= 256:
            return 2 + max(0, len(item) - 1) + sum(count(child, depth + 1) for child in item)
        if type(item) is dict and len(item) <= 256 and all(type(key) is str for key in item):
            return (
                2
                + max(0, len(item) - 1)
                + sum(count(key, depth + 1) + 1 + count(child, depth + 1) for key, child in item.items())
            )
        return _OVER

    try:
        return count(value, 0)
    except (ValueError, OverflowError, UnicodeError):
        return _OVER
