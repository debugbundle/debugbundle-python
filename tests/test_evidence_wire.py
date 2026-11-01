import json
from pathlib import Path

import pytest

from debugbundle.evidence_json import canonical_size
from debugbundle.evidence_wire import (
    capture_reference,
    normalise_recovery,
    parse_capabilities,
    resources,
    strip_unsupported,
    validate_evidence,
)
from debugbundle.exception_evidence import snapshot_exception

FAILURE = "00000000-0000-4000-8000-000000000001"
SCOPE = {"kind": "operation", "operation_id": "00000000-0000-4000-8000-000000000002"}
ATTEMPT = "00000000-0000-4000-8000-000000000003"
CORPUS = json.loads(Path(__file__).with_name("fixtures").joinpath("evidence-wire-cases.json").read_text())


def test_generated_wire_schema_is_valid_for_its_declared_json_schema_draft() -> None:
    from jsonschema.validators import validator_for

    schema = json.loads(Path(__file__).with_name("fixtures").joinpath("event-envelope.schema.json").read_text())
    validator_for(schema).check_schema(schema)


@pytest.mark.parametrize("fixture", CORPUS["json_sizes"])
def test_counts_validated_evidence_using_accepting_json_number_and_utf8_forms(fixture) -> None:
    assert canonical_size(fixture["value"]) == fixture["bytes"]


def test_json_byte_counter_fails_closed_for_non_json_or_unbounded_inputs() -> None:
    assert canonical_size(float("inf")) > 32768
    assert canonical_size(10**400) > 32768
    assert canonical_size(object()) > 32768
    assert canonical_size("\ud800") > 32768
    assert canonical_size([None] * 257) > 32768
    assert canonical_size({0: "invalid key"}) > 32768
    value = None
    for _ in range(18):
        value = [value]
    assert canonical_size(value) > 32768
    assert canonical_size([[None] * 256] * 20) > 32768


@pytest.mark.parametrize("fixture", CORPUS["events"], ids=lambda fixture: fixture["id"])
def test_matches_shared_evidence_semantic_corpus(fixture) -> None:
    assert validate_evidence(fixture["event"]) == fixture["valid"]


@pytest.mark.parametrize("fixture", CORPUS["capabilities"], ids=lambda fixture: fixture["id"])
def test_matches_shared_capability_corpus(fixture) -> None:
    assert parse_capabilities(fixture["input"])["features"] == fixture["features"]


def test_resource_dtos_accept_unknown_availability_and_reject_empty_or_invalid_locations() -> None:
    item = {
        "kind": "timing",
        "observed_at": "2026-10-26T00:00:00Z",
        "source_url": "https://cdn.example/asset.js",
        "availability": "unknown",
        "response_status": None,
        "duration_ms": None,
        "transfer_size_bytes": None,
    }
    assert resources({"version": 1, "items": [item]}) is True
    assert resources({"version": 1, "items": [dict(item, availability="observed")]}) is True
    assert resources({"version": 1, "items": []}) is False
    for url in [
        "https://",
        "https://cdn.example:bad/asset.js",
        "https://user@cdn.example/asset.js",
        "https://cdn.example/a?secret=x",
    ]:
        assert resources({"version": 1, "items": [dict(item, source_url=url)]}) is False


def test_rejects_custom_enum_values_without_equality_hashing_or_string_coercion() -> None:
    calls: list[str] = []

    class Unsafe:
        def __eq__(self, other: object) -> bool:
            calls.append("eq")
            raise AssertionError("must not execute equality")

        def __hash__(self) -> int:
            calls.append("hash")
            raise AssertionError("must not execute hashing")

    value = Unsafe()
    assert (
        normalise_recovery({"failure_ref": FAILURE, "evidence_scope": SCOPE, "attempt_id": ATTEMPT, "state": value})
        is None
    )
    graph = snapshot_exception(RuntimeError("original"))
    graph["runtime"] = value
    assert validate_evidence({"event_type": "backend_exception", "payload": {"exception_evidence": graph}}) is False
    assert calls == []


def test_capabilities_fail_closed_and_return_a_canonical_feature_intersection() -> None:
    assert parse_capabilities({"version": 1, "features": ["recovery_v1", "exception_evidence_v1"]}) == {
        "version": 1,
        "features": ["exception_evidence_v1", "recovery_v1"],
    }
    for value in [
        None,
        {"version": True, "features": []},
        {"version": 2, "features": []},
        {"version": 1, "features": ["unknown"]},
        {"version": 1, "features": ["recovery_v1"] * 2},
    ]:
        assert parse_capabilities(value) == {"version": 1, "features": []}


def test_recovery_is_closed_scoped_and_independent_of_arbitrary_application_data() -> None:
    observation = {
        "failure_ref": FAILURE,
        "evidence_scope": SCOPE,
        "attempt_id": ATTEMPT,
        "state": "attempt_completed",
        "status_code": 200,
        "duration_ms": 0,
    }
    payload = normalise_recovery(observation)
    assert payload == {
        "version": 1,
        "kind": "recovery",
        "failure_ref": FAILURE,
        "scope": SCOPE,
        "attempt_id": ATTEMPT,
        "state": "attempt_completed",
        "status_code": 200,
        "duration_ms": 0,
    }
    assert capture_reference({"failure_ref": FAILURE}, SCOPE) == {"version": 1, "failure_ref": FAILURE, "scope": SCOPE}
    assert normalise_recovery(dict(observation, failure_ref="user-id")) is None
    assert normalise_recovery(dict(observation, duration_ms=float("inf"))) is None
    assert (
        validate_evidence({"event_type": "diagnostic_context", "payload": dict(payload, data={"secret": True})})
        is False
    )


def test_validates_native_graphs_and_strips_only_optional_exception_additions_on_unknown_paths() -> None:
    graph = snapshot_exception(RuntimeError("original"))
    event = {
        "event_type": "backend_exception",
        "payload": {"message": "original", "exception_evidence": graph},
        "context": {"evidence_reference": {"version": 1, "failure_ref": FAILURE, "scope": SCOPE}},
    }
    assert validate_evidence(event) is True
    legacy = strip_unsupported(event, [])
    assert legacy["payload"] == {"message": "original"}
    assert "evidence_reference" not in legacy["context"]
    assert strip_unsupported({"event_type": "diagnostic_context", "payload": {}}, []) is None
    graph["nodes"][0]["relationships"] = [{"kind": "cause", "target_id": 0}]
    assert validate_evidence(event) is False


def test_page_context_requires_a_real_session_and_accepts_string_zero() -> None:
    payload = normalise_recovery(
        {
            "failure_ref": FAILURE,
            "attempt_id": ATTEMPT,
            "state": "recovery_observed",
            "evidence_scope": {"kind": "page", "page_id": SCOPE["operation_id"]},
        }
    )
    for session, expected in [(None, False), ("   ", False), ("0", True)]:
        assert (
            validate_evidence(
                {"event_type": "diagnostic_context", "payload": payload, "correlation": {"session_id": session}}
            )
            is expected
        )


@pytest.mark.parametrize(
    "value,valid",
    [
        ("2026-10-26T24:00:00.000Z", False),
        ("2026-10-26T23:60:00Z", False),
        ("2026-10-26T23:59:60Z", False),
        ("2026-02-29T12:00:00Z", False),
        ("2000-02-29T12:00:00Z", True),
        ("1900-02-29T12:00:00Z", False),
        ("0000-02-29T00:00:00Z", True),
        ("٢٠٢٦-10-26T00:00:00Z", False),
    ],
)
def test_calendar_acceptance_is_independent_of_native_fromisoformat_versions(value, valid) -> None:
    from debugbundle.evidence_wire import _timestamp

    assert _timestamp(value) is valid
