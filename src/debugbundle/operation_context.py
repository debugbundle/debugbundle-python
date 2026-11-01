"""Monotonic operation ownership; retain only bounded technical metadata."""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass
class Operation:
    revision: int
    scope: dict[str, str]
    kind: str
    name: str | None
    limit: float | int | None
    started: float
    parent: Operation | None
    active: bool = True
    token_id: int = 0


class OperationContexts:
    def __init__(self) -> None:
        self._current: ContextVar[Operation | None] = ContextVar("debugbundle_operation", default=None)
        self._revision = 0

    def clear(self) -> None:
        self._revision += 1

    def current(self) -> Operation | None:
        current = self._current.get()
        ancestor = current
        for _ in range(16):
            if ancestor is None:
                return current
            if not ancestor.active or ancestor.revision != self._revision:
                return None
            ancestor = ancestor.parent
        return None

    def begin(self, options: object) -> Token[Operation | None] | None:
        try:
            values: dict[str, Any] = options if type(options) is dict else {}
            kind = values.get("kind")
            kind = kind if type(kind) is str and kind in ("request", "command", "job", "unknown") else "unknown"
            name = values.get("name")
            name = name if type(name) is str and name.strip() and len(name.encode("utf-8")) <= 128 else None
            limit = values.get("limit_ms")
            limit = (
                limit
                if (type(limit) is int or type(limit) is float)
                and 0 <= limit <= 1.7976931348623157e308
                and math.isfinite(limit)
                else None
            )
            parent = self.current()
            # Count-bound inherited metadata as well as serialized fields.
            depth, ancestor = 0, parent
            while ancestor is not None:
                depth += 1
                ancestor = ancestor.parent
            if depth >= 15:
                return None
            value = Operation(
                self._revision,
                {"kind": "operation", "operation_id": str(uuid.uuid4())},
                kind,
                name,
                limit,
                time.monotonic(),
                parent,
            )
            token = self._current.set(value)
            value.token_id = id(token)
            return token
        except BaseException:
            return None

    def end(self, token: Token[Operation | None] | None) -> None:
        if token is None:
            return
        current = self._current.get()
        value = current
        for _ in range(16):
            if value is None:
                return
            if value.token_id == id(token):
                if value is current:
                    self._current.reset(token)
                value.active = False
                return
            value = value.parent

    def snapshot(self) -> dict[str, object] | None:
        value = self.current()
        if value is None:
            return None
        return {
            "version": 1,
            "kind": value.kind,
            "name": value.name,
            "elapsed_ms": max(0, (time.monotonic() - value.started) * 1000),
            "limit_ms": value.limit,
        }

    @contextmanager
    def context(self, options: object) -> Iterator[None]:
        token = self.begin(options)
        try:
            yield
        finally:
            try:
                self.end(token)
            except BaseException:
                pass
