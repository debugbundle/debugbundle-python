"""Opaque receipts on native exceptions; no raw exception retention or equality hooks."""

from __future__ import annotations

import time
from typing import Any

_SLOT = "_debugbundle_capture_receipt"
_DICT = BaseException.__dict__["__dict__"]


def admit(sdk: Any, error: BaseException, automatic: bool, *, accepted: bool = False) -> bool:
    try:
        # BaseException is not universally weak-referenceable. Store only an opaque
        # owner, operation ID and time on its native dictionary, bypassing overrides.
        if sdk._before_send is not None:
            return True
        fields = _DICT.__get__(error)
        owner = getattr(sdk, "_exception_identity_owner", None)
        if owner is None:
            owner = sdk._exception_identity_owner = object()
        operation = sdk._operations.current()
        scope = operation.scope["operation_id"] if operation is not None else None
        if scope is None:
            return True
        now = time.monotonic()
        previous = fields.get(_SLOT)
        if type(previous) is tuple and len(previous) == 4:
            token, prior_scope, at, prior_automatic = previous
            if (
                not accepted
                and token is owner
                and type(prior_scope) is str
                and scope == prior_scope
                and type(at) is float
                and type(prior_automatic) is bool
                and now - at < 30
                and automatic != prior_automatic
            ):
                fields.pop(_SLOT, None)
                return False
        if accepted:
            fields[_SLOT] = (owner, scope, now, automatic)
    except BaseException:
        # Unavailable identity must preserve the failure, never infer sameness.
        pass
    return True
