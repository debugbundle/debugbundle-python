import json
from pathlib import Path

from debugbundle.redaction import UnsafeTelemetry, has_safe_event_identity, sanitize_telemetry

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "privacy-conformance.json").read_text())


def test_policy_corpus() -> None:
    assert CORPUS["policy"] == "telemetry-privacy-v1"
    for case in CORPUS["cases"]:
        original = json.dumps(case["input"])
        result = sanitize_telemetry(case["input"])
        assert result == case["expected"], case["id"]
        assert json.dumps(case["input"]) == original, case["id"]
        assert sanitize_telemetry(result) == result, case["id"]


def test_additive_baseline_and_failure() -> None:
    assert sanitize_telemetry({"password": "secret", "businessField": 42}, {"businessField"}) == {
        "password": "[REDACTED]", "businessField": "[REDACTED]",
    }
    try:
        sanitize_telemetry({"many": {f"key_{i}": [0] * 20 for i in range(220)}})
    except UnsafeTelemetry as error:
        assert str(error) == "budget_exceeded"
    else:
        raise AssertionError("unscannable input must fail closed")


def test_protocol_identity_values_are_checked_without_masking_identity_names() -> None:
    assert has_safe_event_identity({"correlation": {"session_id": "session-123"}})
    assert not has_safe_event_identity({"correlation": {"trace_id": "dbundle_proj_SYNTHETIC_SECRET"}})
    assert not has_safe_event_identity({"sdk_version": "password=SYNTHETIC_SECRET"})
