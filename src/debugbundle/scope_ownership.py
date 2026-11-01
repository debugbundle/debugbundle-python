"""Revoke copied ContextVar values when their request/project owner ends."""

from __future__ import annotations

from contextvars import Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .config import RemoteProbeDirective
from .operation_context import Operation

if TYPE_CHECKING:
    from .core import DebugBundleSdk


@dataclass
class Lease:
    generation: int
    parent: Lease | None = None
    active: bool = True
    token_id: int = 0
    operation: Token[Operation | None] | None = None


class ScopedContext(dict[str, object]):
    lease: Lease


class ScopedDirectives(list[RemoteProbeDirective]):
    lease: Lease


def alive(value: object, generation: int) -> bool:
    if type(value) is not ScopedContext and type(value) is not ScopedDirectives:
        return False
    lease: Lease | None = value.lease
    for _ in range(16):
        if lease is None:
            return True
        if not lease.active or lease.generation != generation:
            return False
        lease = lease.parent
    return False


def context_value(sdk: DebugBundleSdk) -> dict[str, object]:
    value = sdk._scoped_context.get()
    return value if alive(value, sdk._generation) and value is not None else {}


def bind_context(sdk: DebugBundleSdk, protected: dict[str, object]) -> Token[dict[str, object] | None]:
    parent = sdk._scoped_context.get()
    value = ScopedContext(protected)
    value.lease = Lease(
        sdk._generation, parent.lease if type(parent) is ScopedContext and alive(parent, sdk._generation) else None
    )
    token = sdk._scoped_context.set(value)
    value.lease.token_id = id(token)
    return token


def bind_directives(
    sdk: DebugBundleSdk, directives: list[RemoteProbeDirective]
) -> Token[list[RemoteProbeDirective] | None]:
    parent = sdk._request_trigger_directives.get()
    value = ScopedDirectives(directives)
    value.lease = Lease(
        sdk._generation,
        parent.lease if type(parent) is ScopedDirectives and alive(parent, sdk._generation) else None,
        operation=sdk._operations.begin({"kind": "request"}),
    )
    token = sdk._request_trigger_directives.set(value)
    value.lease.token_id = id(token)
    return token


def release(
    sdk: DebugBundleSdk,
    token: Token[dict[str, object] | None] | Token[list[RemoteProbeDirective] | None],
    *,
    directives: bool = False,
) -> None:
    variable = sdk._request_trigger_directives if directives else sdk._scoped_context
    value = variable.get()
    lease = value.lease if type(value) is ScopedContext or type(value) is ScopedDirectives else None
    current_lease = lease
    for _ in range(16):
        if lease is None:
            return
        if lease.token_id == id(token):
            if lease is current_lease:
                # Preserve native Token context checks; old completion never clears
                # the currently active newer owner.
                cast(Any, variable).reset(token)
            lease.active = False
            if directives:
                sdk._operations.end(lease.operation)
            return
        lease = lease.parent
