"""Canonical, owned hook results with explicit invalid and exhausted outcomes."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import cast

from .canonical_event import EXHAUSTED, INVALID, ValidationBudgetExceeded, own_json, valid_owned_event
from .evidence_wire import validate_evidence

BeforeSendHook = Callable[[dict[str, object]], dict[str, object] | None]
_MOBILE = ("@debugbundle/sdk-android", "@debugbundle/sdk-swift", "@debugbundle/sdk-react-native")


def _normalized(event: object) -> object:
    if type(event) is not dict:
        return INVALID
    owned = own_json(event)
    if owned is INVALID or owned is EXHAUSTED:
        return owned
    try:
        if not valid_owned_event(owned) or not validate_evidence(owned):
            return INVALID
        # The accepting Zod contract refines this condition outside JSON Schema.
        if owned["event_type"] == "frontend_exception":
            field = "device" if owned["sdk_name"] in _MOBILE else "browser"
            if field not in owned["payload"] or type(owned["payload"][field]) is not dict:
                return INVALID
        return owned
    except ValidationBudgetExceeded:
        return EXHAUSTED
    except Exception:
        return INVALID


def apply_before_send(
    event: dict[str, object],
    hook: BeforeSendHook | None,
    emit_diagnostic: Callable[[str, str], None],
) -> dict[str, object] | None:
    if hook is None:
        return event
    try:
        result = hook(copy.deepcopy(event))
    except Exception:
        emit_diagnostic("before_send_failed", "Python before_send hook failed")
        return event
    if result is None:
        return None
    checked = _normalized(result)
    if checked is EXHAUSTED:
        return None
    if checked is INVALID:
        emit_diagnostic("before_send_invalid_event", "Python before_send returned an invalid event")
        return event
    return cast(dict[str, object], checked)


def _is_valid_event(event: object) -> bool:
    result = _normalized(event)
    return result is not INVALID and result is not EXHAUSTED
