"""Owned JSON and a closed interpreter for the vendored accepting Draft 7 schema.

Only native containers/scalars are inspected. No application serializers, remote
references or optional runtime dependencies participate in hook validation.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .evidence_wire import _timestamp, _uuid

MAX_OWNED_BYTES = 8 * 1024 * 1024
INVALID = object()
EXHAUSTED = object()


class ValidationBudgetExceeded(Exception):
    """Withhold hook output rather than restore content it may have removed."""


def own_json(value: object) -> Any:
    remaining = MAX_OWNED_BYTES
    nodes = 4096

    def copy(item: Any, depth: int) -> Any:
        nonlocal remaining, nodes
        nodes -= 1
        if nodes < 0 or depth > 16 or remaining < 0:
            raise ValidationBudgetExceeded
        kind = type(item)
        if item is None or kind is bool:
            remaining -= 5
            return item
        if kind is str:
            if len(item) > remaining:
                raise ValidationBudgetExceeded
            remaining -= len(item.encode("utf-8")) + 2
            if remaining < 0:
                raise ValidationBudgetExceeded
            return item
        if kind is int or kind is float:
            # The accepting JavaScript number domain is finite IEEE-754.
            if not -1.7976931348623157e308 <= item <= 1.7976931348623157e308 or not math.isfinite(item):
                raise ValueError("non-finite number")
            remaining -= 24
            return item
        if kind is list:
            if len(item) > nodes:
                raise ValidationBudgetExceeded
            remaining -= 2 + len(item)
            return [copy(child, depth + 1) for child in item]
        if kind is dict:
            if len(item) * 2 > nodes:
                raise ValidationBudgetExceeded
            remaining -= 2 + 2 * len(item)
            result = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("non-string key")
                result[copy(key, depth + 1)] = copy(child, depth + 1)
            return result
        raise ValueError("non-native JSON value")

    try:
        return copy(value, 0)
    except ValidationBudgetExceeded:
        return EXHAUSTED
    except (ValueError, UnicodeError, OverflowError):
        return INVALID


class CanonicalEventSchema:
    KEYWORDS = frozenset(
        "$schema $id $comment $ref type anyOf const enum properties required additionalProperties "
        "items minimum maximum exclusiveMinimum minLength maxLength minItems maxItems format pattern".split()
    )
    TYPES = frozenset("null boolean number integer string array object".split())

    def __init__(self, schema: dict[str, Any]) -> None:
        self.root = schema
        self.references: dict[str, Any] = {}
        self.patterns: dict[str, re.Pattern[str]] = {}
        self._compile(schema)

    def _resolve(self, reference: str) -> Any:
        if not reference.startswith("#/"):
            raise ValueError("external schema ref")
        if reference not in self.references:
            node: Any = self.root
            for part in reference[2:].split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                node = node[int(key)] if type(node) is list else node[key]
            self.references[reference] = node
        return self.references[reference]

    def _compile(self, node: Any) -> None:
        if type(node) is bool:
            return
        if not set(node).issubset(self.KEYWORDS):
            raise ValueError("unsupported schema keyword")
        kinds = node.get("type", [])
        if not set([kinds] if type(kinds) is str else kinds).issubset(self.TYPES):
            raise ValueError("unsupported schema type")
        if node.get("format") not in (None, "uuid", "date-time"):
            raise ValueError("unsupported schema format")
        if "pattern" in node:
            self.patterns[node["pattern"]] = re.compile(node["pattern"])
        if "$ref" in node:
            self._resolve(node["$ref"])
        for child in node.get("properties", {}).values():
            self._compile(child)
        for child in node.get("anyOf", []):
            self._compile(child)
        for key in ("items", "additionalProperties"):
            if key in node:
                self._compile(node[key])

    def accepts(self, value: Any) -> bool:
        return self._check(self.root, value, [100_000], 0)

    def _check(self, node: Any, value: Any, budget: list[int], depth: int) -> bool:
        budget[0] -= 1
        if depth > 64 or budget[0] < 0:
            raise ValidationBudgetExceeded
        if type(node) is bool:
            return node
        if "$ref" in node:
            return self._check(self._resolve(node["$ref"]), value, budget, depth + 1)
        if "anyOf" in node and not any(self._check(child, value, budget, depth + 1) for child in node["anyOf"]):
            return False
        if "type" in node:
            kinds = node["type"] if type(node["type"]) is list else [node["type"]]
            if not any(self._type(value, kind) for kind in kinds):
                return False
        if "const" in node and not self._equal(value, node["const"]):
            return False
        if "enum" in node and not any(self._equal(value, choice) for choice in node["enum"]):
            return False
        if type(value) is dict:
            properties = node.get("properties", {})
            return all(key in value for key in node.get("required", [])) and all(
                self._check(properties.get(key, node.get("additionalProperties", True)), child, budget, depth + 1)
                for key, child in value.items()
            )
        if type(value) is list:
            return (
                len(value) >= node.get("minItems", 0)
                and len(value) <= node.get("maxItems", len(value))
                and (
                    "items" not in node or all(self._check(node["items"], child, budget, depth + 1) for child in value)
                )
            )
        if type(value) is str:
            length = len(value.encode("utf-16-le")) // 2  # Zod counts JavaScript UTF-16 code units.
            return (
                length >= node.get("minLength", 0)
                and length <= node.get("maxLength", length)
                and ("pattern" not in node or bool(self.patterns[node["pattern"]].search(value)))
                and (node.get("format") != "uuid" or _uuid(value))
                and (node.get("format") != "date-time" or _timestamp(value))
            )
        if type(value) is int or type(value) is float:
            return (
                ("minimum" not in node or value >= node["minimum"])
                and ("maximum" not in node or value <= node["maximum"])
                and ("exclusiveMinimum" not in node or value > node["exclusiveMinimum"])
            )
        return True

    @staticmethod
    def _equal(value: Any, choice: Any) -> bool:
        return (
            type(value) is type(choice)
            and value == choice
            or (type(value) in (int, float) and type(choice) in (int, float) and value == choice)
        )

    @staticmethod
    def _type(value: Any, kind: str) -> bool:
        if kind == "number" or kind == "integer":
            return type(value) in (int, float) and (kind == "number" or value == math.floor(value))
        return type(value) is {"null": type(None), "boolean": bool, "string": str, "array": list, "object": dict}[kind]


@lru_cache(maxsize=1)
def _engine() -> CanonicalEventSchema:
    return CanonicalEventSchema(json.loads(Path(__file__).with_name("event-envelope.schema.json").read_text()))


def valid_owned_event(value: dict[str, Any]) -> bool:
    try:
        return _engine().accepts(value)
    except ValidationBudgetExceeded:
        raise
    except Exception:
        return False
