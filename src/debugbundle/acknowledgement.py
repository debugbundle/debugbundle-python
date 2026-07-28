from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeGuard

RETRYABLE_REASONS = {
    "rate_limited",
    "monthly_quota_exceeded",
    "analytics_quota_exceeded",
}


@dataclass(frozen=True)
class AcknowledgementDecision:
    kind: Literal["legacy", "protocol_failure", "acknowledged"]
    accepted: int = 0
    retryable_indices: tuple[int, ...] = ()
    terminal_errors: tuple[tuple[int, str], ...] = ()
    reason: str | None = None


def decide_acknowledgement(body: object | None, batch_length: int) -> AcknowledgementDecision:
    if not isinstance(body, dict) or not any(key in body for key in ("accepted", "rejected", "errors")):
        return AcknowledgementDecision(kind="legacy")

    accepted = body.get("accepted")
    rejected = body.get("rejected")
    errors = body.get("errors")
    if (
        not _is_count(accepted)
        or not _is_count(rejected)
        or not isinstance(errors, list)
        or accepted + rejected != batch_length
        or len(errors) != rejected
    ):
        return AcknowledgementDecision(kind="protocol_failure", reason="inconsistent_counts")

    seen: set[int] = set()
    retryable: list[int] = []
    terminal: list[tuple[int, str]] = []
    for error in errors:
        if not isinstance(error, dict):
            return AcknowledgementDecision(kind="protocol_failure", reason="invalid_error_index")
        index = error.get("index")
        reason = error.get("reason")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= batch_length
            or index in seen
            or not isinstance(reason, str)
            or not reason
        ):
            return AcknowledgementDecision(kind="protocol_failure", reason="invalid_error_index")
        seen.add(index)
        if reason in RETRYABLE_REASONS:
            retryable.append(index)
        else:
            terminal.append((index, reason))

    return AcknowledgementDecision(
        kind="acknowledged",
        accepted=accepted,
        retryable_indices=tuple(retryable),
        terminal_errors=tuple(terminal),
    )


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
