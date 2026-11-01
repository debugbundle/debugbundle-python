"""Per-destination proof and bounded optional evidence assembly."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from .evidence_wire import capture_reference, parse_capabilities, reference, strip_unsupported

if TYPE_CHECKING:
    from .core import DebugBundleSdk


class EvidenceDelivery:
    def __init__(self, pin: object = None) -> None:
        self.features: list[str] = parse_capabilities(pin)["features"]

    def supports(self, feature: str) -> bool:
        return feature in self.features

    def observe(self, body: object, required: bool = False, valid: bool = True) -> bool:
        if not required and (type(body) is not dict or "evidence_capabilities" not in body):
            return False
        descriptor = body.get("evidence_capabilities") if type(body) is dict else None
        previous = self.features
        self.features = parse_capabilities(descriptor if valid else None)["features"]
        return any(feature not in self.features for feature in previous)


def add_reference(sdk: DebugBundleSdk, merged: dict[str, object]) -> dict[str, object]:
    if not (sdk._evidence.supports("recovery_v1") or sdk._evidence.supports("resource_observations_v1")):
        return merged
    operation = sdk._operations.current()
    selected = capture_reference(merged, operation.scope if operation is not None else None)
    if selected is None:
        return merged
    return {
        **{key: value for key, value in merged.items() if key not in ("failure_ref", "evidence_scope")},
        "evidence_reference": selected,
    }


def filter_evidence(sdk: DebugBundleSdk, event: dict[str, Any]) -> dict[str, Any] | None:
    features = sdk._evidence.features
    if not sdk._capture_exception_evidence:
        features = [feature for feature in features if feature != "exception_evidence_v1"]
    return strip_unsupported(event, features)


def allows_recovery(sdk: DebugBundleSdk) -> bool:
    return (
        sdk._capture_recovery_observations
        and sdk._evidence.supports("recovery_v1")
        and sdk._capture_policy.preset != "minimal"
        and sdk._capture_policy.capture_diagnostic_context
    )


def reconcile_diagnostic(
    sdk: DebugBundleSdk, original: dict[str, Any] | None, replacement: dict[str, Any] | None
) -> bool:
    original_id = (
        original["event_id"] if original is not None and original["event_type"] == "diagnostic_context" else None
    )
    if replacement is None:
        if original_id is not None:
            sdk._diagnostics.release([original_id])
        return False
    now = sdk._time_provider() * 1000
    if replacement["event_type"] == "diagnostic_context":
        payload = replacement["payload"]
        if payload["kind"] != "recovery" or not allows_recovery(sdk):
            if original_id is not None:
                sdk._diagnostics.release([original_id])
            return False
        size = sdk._event_bytes(replacement)
        return (
            sdk._diagnostics.replace(original_id, replacement["event_id"], payload, size, now)
            if original_id is not None
            else sdk._diagnostics.reserve(replacement["event_id"], payload, size, now)
        )
    if original_id is not None:
        sdk._diagnostics.release([original_id])
    context = replacement.get("context")
    selected = reference(context.get("evidence_reference")) if type(context) is dict else None
    if selected is not None and original is not None:
        # Admission precedes the delivery hook. Register only its protected
        # replacement, so a canonical ID change cannot invent a second failure.
        try:
            occurred = datetime.fromisoformat(replacement["occurred_at"].replace("Z", "+00:00")).timestamp() * 1000
            sdk._diagnostics.register_failure(selected, replacement["event_id"], occurred)
        except (ValueError, TypeError, OverflowError):
            pass
    return True


def downgrade_rejections(
    sdk: DebugBundleSdk, batch: list[dict[str, object]], errors: tuple[tuple[int, str], ...], lost: bool
) -> list[dict[str, object]]:
    retries: list[dict[str, object]] = []
    if not lost:
        return retries
    for index, reason in errors:
        original = batch[index]
        if reason != "invalid_event" or original["event_type"] not in ("backend_exception", "frontend_exception"):
            continue
        reduced = filter_evidence(sdk, original)
        if reduced is None or reduced == original:
            continue
        size = sdk._event_bytes(reduced)
        sdk._buffer_bytes += size - sdk._buffer_sizes.pop(id(original))
        sdk._buffer_sizes[id(reduced)] = size
        sdk._hook_bypass.add(id(reduced))
        retries.append(reduced)
    return retries
