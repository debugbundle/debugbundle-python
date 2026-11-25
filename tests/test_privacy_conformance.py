from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from helpers.sdk import FakeTransport

from debugbundle.core import DebugBundleSdk
from debugbundle.event_privacy import ALLOWED_HTML_RESOURCE_TAGS, protect_event_fields
from debugbundle.evidence_wire import FEATURES, validate_evidence
from debugbundle.exception_evidence import snapshot_exception
from debugbundle.redaction import UnsafeTelemetry, has_safe_event_identity, sanitize_telemetry

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "privacy-conformance.json").read_text())
PIN = {"version": 1, "features": list(FEATURES)}


def test_policy_corpus() -> None:
    assert CORPUS["policy"] == "telemetry-privacy-v1"
    for case in CORPUS["cases"]:
        original = json.dumps(case["input"])
        result = sanitize_telemetry(case["input"])
        assert result == case["expected"], case["id"]
        assert json.dumps(case["input"]) == original, case["id"]
        assert sanitize_telemetry(result) == result, case["id"]

    assert CORPUS["exact_key_cases"]
    for case in CORPUS["exact_key_cases"]:
        original = json.dumps(case["input"])
        additional = case.get("additional_keys", [])
        exact = case.get("exact_keys", [])
        result = sanitize_telemetry(case["input"], additional, exact)
        assert result == case["expected"], case["id"]
        assert json.dumps(case["input"]) == original, case["id"]
        assert sanitize_telemetry(result, additional, exact) == result, f"{case['id']} idempotent"


def test_additive_baseline_and_failure() -> None:
    assert sanitize_telemetry({"password": "secret", "businessField": 42}, {"businessField"}) == {
        "password": "[REDACTED]",
        "businessField": "[REDACTED]",
    }
    try:
        sanitize_telemetry({"many": {f"key_{i}": [0] * 20 for i in range(220)}})
    except UnsafeTelemetry as error:
        assert str(error) == "budget_exceeded"
    else:
        raise AssertionError("unscannable input must fail closed")


def test_exact_key_bounds_literal_matching_and_ascii_folding() -> None:
    half_broad = [f"broad_{index}" for index in range(64)]
    half_exact = [f"exact_{index}" for index in range(64)]
    assert sanitize_telemetry({"safe": 1}, half_broad, half_exact) == {"safe": 1}

    with pytest.raises(UnsafeTelemetry, match="unsafe_input"):
        sanitize_telemetry({"safe": 1}, half_broad, [*half_exact, "exact_64"])

    for bad_additional, bad_exact in (
        ([], [""]),
        ([], ["   "]),
        ([], ["x" * 65]),
        (["   "], []),
        (["x" * 65], []),
        ([], [123]),
        ("tag", []),
        ([], "tag"),
    ):
        with pytest.raises(UnsafeTelemetry, match="unsafe_input"):
            sanitize_telemetry({"safe": 1}, bad_additional, bad_exact)  # type: ignore[arg-type]

    # Metacharacters are matched literally and never executed as regex patterns.
    literal = sanitize_telemetry(
        {
            "a+b": "SYNTHETIC_SECRET",
            "aaab": "keep-non-regex",
            "message": "a+b=SYNTHETIC_SECRET aaab=keep-non-regex",
        },
        [],
        ["a+b"],
    )
    assert literal == {
        "a+b": "[REDACTED]",
        "aaab": "keep-non-regex",
        "message": "a+b=[REDACTED] aaab=keep-non-regex",
    }

    # Exact-key folding is ASCII-only (Kelvin sign must not fold to ASCII 'k').
    folded = sanitize_telemetry(
        {"k": "SYNTHETIC_SECRET", "K": "SYNTHETIC_UPPER", "\u212a": "keep-unicode"},
        [],
        ["k"],
    )
    assert folded == {
        "k": "[REDACTED]",
        "K": "[REDACTED]",
        "\u212a": "keep-unicode",
    }


def test_protocol_identity_values_are_checked_without_masking_identity_names() -> None:
    assert has_safe_event_identity({"correlation": {"session_id": "session-123"}})
    assert not has_safe_event_identity({"correlation": {"trace_id": "dbundle_proj_SYNTHETIC_SECRET"}})
    assert not has_safe_event_identity({"sdk_version": "password=SYNTHETIC_SECRET"})
    assert not has_safe_event_identity({"sdk_version": "tag=SYNTHETIC_SECRET"}, [], ["tag"])
    assert has_safe_event_identity({"sdk_version": "tag_name=script"}, [], ["tag"])
    assert not has_safe_event_identity({"sdk_version": "1.0.0"}, [], ["   "])
    assert not has_safe_event_identity(
        {"sdk_version": "1.0.0"},
        [f"b_{i}" for i in range(65)],
        [f"e_{i}" for i in range(64)],
    )


def test_event_privacy_preserves_closed_protocol_structures_and_scrubs_nested_strings() -> None:
    broad = ["tag", "reference", "reason", "file", "limit", "status", "scope", "state"]
    failure_ref = str(uuid.uuid4())
    page_id = str(uuid.uuid4())
    source_event_id = str(uuid.uuid4())

    for html_tag in sorted(ALLOWED_HTML_RESOURCE_TAGS):
        frontend_event: dict[str, Any] = {
            "schema_version": "2026-03-01",
            "event_id": str(uuid.uuid4()),
            "event_type": "frontend_exception",
            "occurred_at": "2026-10-26T10:00:00Z",
            "sdk_name": "@debugbundle/sdk-browser",
            "sdk_version": "3.0.0",
            "service": {"name": "web", "environment": "production"},
            "context": {
                "evidence_reference": {
                    "version": 1,
                    "failure_ref": failure_ref,
                    "scope": {"kind": "page", "page_id": page_id},
                },
                "evidence_limits": {"version": 1, "execution": "unsupported"},
                "payout": {
                    "tag": "SYNTHETIC_TAG",
                    "reference": "SYNTHETIC_REF",
                    "reason": "SYNTHETIC_REASON",
                    "file": "SYNTHETIC_FILE",
                },
            },
            "payload": {
                "name": "Error",
                "message": "boom reason=SYNTHETIC_REASON",
                "stack": "at load (https://example.test/app.js?tag=SYNTHETIC_TAG:10:2)",
                "browser": {"name": "Chrome", "version": "120"},
                "browser_event": {
                    "kind": "resource_error",
                    "message": "failed reason=SYNTHETIC_REASON",
                    "file_name": "https://example.test/app.js?file=SYNTHETIC_FILE",
                    "line_number": 10,
                    "column_number": 2,
                    "target": {
                        "tag_name": html_tag.upper() if html_tag == "script" else html_tag,
                        "source_url": "https://cdn.example.test/asset.js?tag=SYNTHETIC_TAG&token=SYNTHETIC_TOKEN",
                        "attributes": {
                            "rel": "preload reason=SYNTHETIC_REASON",
                            "async": True,
                            "integrity_present": False,
                        },
                    },
                    "page": {
                        "url": "https://app.example.test/pay?reference=SYNTHETIC_REF",
                        "referrer": "https://app.example.test/cart?file=SYNTHETIC_FILE",
                        "ready_state": "complete",
                        "visibility_state": "visible",
                    },
                    "opaque": False,
                },
                "rejection_reason": {
                    "kind": "error",
                    "name": "TypeError",
                    "message": "rejected reason=SYNTHETIC_REASON",
                    "preview": "tag=SYNTHETIC_TAG",
                },
                "resource_observations": {
                    "version": 1,
                    "items": [
                        {
                            "kind": "timing",
                            "observed_at": "2026-10-26T10:00:00Z",
                            "source_url": "https://cdn.example.test/tag=SYNTHETIC_TAG/app.js",
                            "availability": "observed",
                            "response_status": 502,
                            "duration_ms": 14,
                            "transfer_size_bytes": 0,
                        },
                        {
                            "kind": "csp",
                            "observed_at": "2026-10-26T10:00:00Z",
                            "source_url": None,
                            "effective_directive": "script-src",
                            "disposition": "enforce",
                            "document_status_code": 200,
                        },
                    ],
                },
            },
        }
        protected = protect_event_fields(frontend_event, broad)
        assert protected is not None
        expected_tag = html_tag.upper() if html_tag == "script" else html_tag
        assert protected["payload"]["browser_event"]["target"]["tag_name"] == expected_tag
        assert protected["payload"]["browser_event"]["target"]["attributes"]["async"] is True
        assert protected["payload"]["browser_event"]["page"]["ready_state"] == "complete"
        assert protected["payload"]["rejection_reason"] == {
            "kind": "error",
            "name": "TypeError",
            "message": "rejected reason=[REDACTED]",
            "preview": "tag=[REDACTED]",
        }
        assert protected["payload"]["resource_observations"]["items"][0]["source_url"] == (
            "https://cdn.example.test/tag=[REDACTED]"
        )
        assert protected["payload"]["resource_observations"]["items"][1]["disposition"] == "enforce"
        assert protected["context"]["evidence_reference"] == {
            "version": 1,
            "failure_ref": failure_ref,
            "scope": {"kind": "page", "page_id": page_id},
        }
        assert protected["context"]["evidence_limits"] == {"version": 1, "execution": "unsupported"}
        assert protected["context"]["payout"] == {
            "tag": "[REDACTED]",
            "reference": "[REDACTED]",
            "reason": "[REDACTED]",
            "file": "[REDACTED]",
        }
        encoded = json.dumps(protected)
        for secret in ("SYNTHETIC_TAG", "SYNTHETIC_REF", "SYNTHETIC_REASON", "SYNTHETIC_FILE", "SYNTHETIC_TOKEN"):
            assert secret not in encoded

    # Non-allowlisted tag_name is redacted under broad redact_fields=["tag"] and preserved under exact ["tag"].
    custom_tag_event = json.loads(json.dumps(frontend_event))
    custom_tag_event["payload"]["browser_event"]["target"]["tag_name"] = "custom-widget"
    broad_custom = protect_event_fields(custom_tag_event, ["tag"])
    assert broad_custom is not None
    assert broad_custom["payload"]["browser_event"]["target"]["tag_name"] == "[REDACTED]"
    exact_custom = protect_event_fields(custom_tag_event, [], ["tag"])
    assert exact_custom is not None
    assert exact_custom["payload"]["browser_event"]["target"]["tag_name"] == "custom-widget"

    # Nullable browser_event target/tag_name and malformed closed blocks fall back cleanly.
    null_target_event = json.loads(json.dumps(frontend_event))
    null_target_event["payload"]["browser_event"]["target"] = None
    null_target_event["payload"]["browser_event"].pop("page")
    protected_null_target = protect_event_fields(null_target_event, broad)
    assert protected_null_target is not None
    assert protected_null_target["payload"]["browser_event"]["target"] is None

    invalid_blocks_event = json.loads(json.dumps(frontend_event))
    invalid_blocks_event["payload"]["browser_event"]["kind"] = "unknown_kind"
    invalid_blocks_event["payload"]["rejection_reason"]["kind"] = "bogus"
    invalid_blocks_event["context"]["evidence_limits"] = {"version": True}
    invalid_blocks_event["context"]["evidence_reference"] = {"not": "valid"}
    protected_invalid = protect_event_fields(invalid_blocks_event, ["reason", "reference", "limits"])
    assert protected_invalid is not None
    assert protected_invalid["payload"]["rejection_reason"] == "[REDACTED]"
    assert protected_invalid["context"]["evidence_limits"] == "[REDACTED]"
    assert protected_invalid["context"]["evidence_reference"] == "[REDACTED]"

    # diagnostic_context resource_observation + invalid diagnostic payload fail closed.
    diag_obs = {
        "schema_version": "2026-03-01",
        "event_id": str(uuid.uuid4()),
        "event_type": "diagnostic_context",
        "occurred_at": "2026-10-26T10:00:00Z",
        "sdk_name": "@debugbundle/sdk-browser",
        "sdk_version": "3.0.0",
        "service": {"name": "web", "environment": "production"},
        "correlation": {"session_id": "sess-1"},
        "payload": {
            "version": 1,
            "kind": "resource_observation",
            "failure_ref": failure_ref,
            "scope": {"kind": "page", "page_id": page_id},
            "source_event_id": source_event_id,
            "observations": frontend_event["payload"]["resource_observations"],
        },
    }
    protected_diag = protect_event_fields(diag_obs, broad)
    assert protected_diag is not None
    assert protected_diag["payload"]["failure_ref"] == failure_ref
    assert protected_diag["payload"]["observations"]["items"][0]["source_url"] == (
        "https://cdn.example.test/tag=[REDACTED]"
    )
    bad_diag = json.loads(json.dumps(diag_obs))
    bad_diag["payload"]["kind"] = "unknown"
    assert protect_event_fields(bad_diag, broad) is None

    # error_suppressed closed reason="queue_pressure" survives broad redact_fields=["reason"].
    suppressed_event = {
        "schema_version": "2026-03-01",
        "event_id": str(uuid.uuid4()),
        "event_type": "error_suppressed",
        "occurred_at": "2026-10-26T10:00:00Z",
        "sdk_name": "debugbundle-python",
        "sdk_version": "3.0.0",
        "service": {"name": "api", "environment": "production"},
        "payload": {
            "fingerprint": "err:1",
            "suppressed_count": 4,
            "window_seconds": 30,
            "first_seen": "2026-10-26T10:00:00Z",
            "last_seen": "2026-10-26T10:00:10Z",
            "reason": "queue_pressure",
        },
    }
    protected_suppressed = protect_event_fields(suppressed_event, ["reason"])
    assert protected_suppressed is not None
    assert protected_suppressed["payload"]["reason"] == "queue_pressure"

    # Non-dict structural fields and oversized combined payload fail closed.
    assert protect_event_fields({**suppressed_event, "payload": "bad"}, []) is None
    assert protect_event_fields({**suppressed_event, "context": "bad"}, []) is None
    oversized = {
        "schema_version": "2026-03-01",
        "event_id": str(uuid.uuid4()),
        "event_type": "log_event",
        "occurred_at": "2026-10-26T10:00:00Z",
        "sdk_name": "debugbundle-python",
        "sdk_version": "3.0.0",
        "service": {"name": "api", "environment": "production"},
        "payload": {"level": "error", "message": "big", "attributes": {f"p_{i}": "x" * 16000 for i in range(10)}},
        "context": {f"c_{i}": "x" * 16000 for i in range(10)},
    }
    with pytest.raises(UnsafeTelemetry, match="budget_exceeded"):
        protect_event_fields(oversized, [])


def test_sdk_redact_exact_fields_end_to_end() -> None:
    transport = FakeTransport()
    diagnostics: list[dict[str, object]] = []

    def before_send(event: dict[str, object]) -> dict[str, object]:
        context = event.setdefault("context", {})
        assert isinstance(context, dict)
        context["post_hook"] = {"tag": "POST_HOOK_SECRET", "tag_name": "script", "user_token": "POST_HOOK_TOKEN"}
        return event

    sdk = DebugBundleSdk(transport=transport)
    sdk.init(
        project_token="dbundle_proj_test",
        service="billing",
        environment="production",
        redact_exact_fields=["tag", "token"],
        on_diagnostic=diagnostics.append,
        before_send=before_send,
    )
    try:
        sdk.set_context(
            "payout",
            {"tag": "SYNTHETIC_CTX_SECRET", "tag_name": "script", "user_token": "SYNTHETIC_TOKEN"},
        )
        sdk.probe("checkout.tag", {"tag": "SYNTHETIC_PROBE_SECRET", "tag_name": "link"})
        sdk.capture_log(
            "payout failed tag=SYNTHETIC_MSG_SECRET tag_name=script x-tag-name=img",
            level="error",
            context={
                "tag": "SYNTHETIC_LOG_SECRET",
                "TAG": "SYNTHETIC_UPPER_SECRET",
                "tag_name": "script",
                "tagName": "link",
                "tag-name": "img",
                "payout_tag": "keep-compound",
                "url": "https://example.test/api?tag=SYNTHETIC_QUERY&tag_name=script&tag-name=img",
                "body": '{"tag":"SYNTHETIC_JSON","tag_name":"script"}',
            },
        )
        sdk.capture_request(
            {
                "method": "POST",
                "path": "/payouts",
                "headers": {"tag": "SYNTHETIC_HDR", "x-tag-name": "keep-hdr"},
                "query": {"tag": "SYNTHETIC_REQ_Q", "tag_name": "script"},
                "body": {"tag": "SYNTHETIC_REQ_BODY", "tag_name": "script"},
            },
            {"status_code": 503, "duration_ms": 25},
        )
        sdk.capture_exception(RuntimeError("boom tag=SYNTHETIC_EXC_SECRET tag_name=script"))
        sdk._emit_diagnostic("exact_diag", "exact_diag", {"tag": "SYNTHETIC_DIAG_SECRET", "tag_name": "script"})
        sdk.flush()

        deadline = time.monotonic() + 1.0
        while not diagnostics and time.monotonic() < deadline:
            time.sleep(0.005)
        assert diagnostics
        assert diagnostics[0]["metadata"] == {"tag": "[REDACTED]", "tag_name": "script"}

        events = transport.calls[0]["events"]
        assert isinstance(events, list)
        log_event = next(event for event in events if event["event_type"] == "log_event")
        assert log_event["payload"]["message"] == "payout failed tag=[REDACTED] tag_name=script x-tag-name=img"
        assert log_event["payload"]["attributes"] == {
            "tag": "[REDACTED]",
            "TAG": "[REDACTED]",
            "tag_name": "script",
            "tagName": "link",
            "tag-name": "img",
            "payout_tag": "keep-compound",
            "url": "https://example.test/api?tag=%5BREDACTED%5D&tag_name=script&tag-name=img",
            "body": '{"tag":"[REDACTED]","tag_name":"script"}',
        }
        assert log_event["context"]["payout"] == {
            "tag": "[REDACTED]",
            "tag_name": "script",
            "user_token": "[REDACTED]",
        }
        assert log_event["context"]["post_hook"] == {
            "tag": "[REDACTED]",
            "tag_name": "script",
            "user_token": "[REDACTED]",
        }

        req_event = next(event for event in events if event["event_type"] == "request_event")
        assert req_event["payload"]["headers"] == {"tag": "[REDACTED]", "x-tag-name": "keep-hdr"}
        assert req_event["payload"]["query"] == {"tag": "[REDACTED]", "tag_name": "script"}
        assert req_event["payload"]["body"] == {"tag": "[REDACTED]", "tag_name": "script"}

        exc_event = next(event for event in events if event["event_type"] == "backend_exception")
        assert exc_event["payload"]["message"] == "boom tag=[REDACTED] tag_name=script"
        assert exc_event["payload"]["probe_data"]["items"][0]["data"] == {
            "tag": "[REDACTED]",
            "tag_name": "link",
        }

        encoded = json.dumps(events)
        for secret in (
            "SYNTHETIC_CTX_SECRET",
            "SYNTHETIC_TOKEN",
            "SYNTHETIC_PROBE_SECRET",
            "SYNTHETIC_MSG_SECRET",
            "SYNTHETIC_LOG_SECRET",
            "SYNTHETIC_UPPER_SECRET",
            "SYNTHETIC_QUERY",
            "SYNTHETIC_JSON",
            "SYNTHETIC_HDR",
            "SYNTHETIC_REQ_Q",
            "SYNTHETIC_REQ_BODY",
            "SYNTHETIC_EXC_SECRET",
            "POST_HOOK_SECRET",
            "POST_HOOK_TOKEN",
        ):
            assert secret not in encoded
    finally:
        sdk.dispose()


def test_sdk_invalid_redact_exact_fields_fails_closed_without_throwing() -> None:
    for invalid_config in (
        {"redact_exact_fields": ["   "]},
        {"redact_exact_fields": ["x" * 65]},
        {"redact_exact_fields": "tag"},
        {"redact_fields": [f"b_{i}" for i in range(65)], "redact_exact_fields": [f"e_{i}" for i in range(64)]},
    ):
        transport = FakeTransport()
        sdk = DebugBundleSdk(transport=transport)
        try:
            sdk.init(project_token="dbundle_proj_test", **invalid_config)  # type: ignore[arg-type]
            sdk.set_context("payout", {"tag": "SYNTHETIC_SECRET"})
            sdk.capture_log("must not leak tag=SYNTHETIC_SECRET", level="error")
            sdk.capture_exception(RuntimeError("must not leak"))
            sdk.flush()
            assert transport.calls == []
        finally:
            sdk.dispose()


def test_sdk_broad_redact_fields_preserves_protocol_evidence_while_redacting_app_data() -> None:
    transport = FakeTransport()
    failure_ref = str(uuid.uuid4())
    operation_id = str(uuid.uuid4())

    def hook(event: dict[str, object]) -> dict[str, object]:
        if event["event_type"] == "backend_exception":
            context = event.setdefault("context", {})
            assert isinstance(context, dict)
            context["evidence_limits"] = {"version": 1, "resource_observations": "unsupported"}
            context["post_hook_reference"] = "POST_HOOK_REF_SECRET"
        return event

    sdk = DebugBundleSdk(transport=transport)
    sdk.init(
        project_token="dbundle_proj_test",
        service="billing",
        environment="production",
        redact_fields=["tag", "reference", "reason", "file"],
        evidence_capabilities=PIN,
        capture_recovery_observations=True,
        before_send=hook,
    )
    try:
        with sdk.operation_context(
            {"kind": "job", "name": "payout.sync reason=SYNTHETIC_EXEC_REASON", "limit_ms": 5000}
        ):
            try:
                raise RuntimeError("boom reason=SYNTHETIC_REASON")
            except RuntimeError as error:
                sdk.capture_exception(
                    error,
                    context={
                        "failure_ref": failure_ref,
                        "evidence_scope": {"kind": "operation", "operation_id": operation_id},
                        "payout": {
                            "tag": "SYNTHETIC_TAG",
                            "reference": "SYNTHETIC_REF",
                            "reason": "SYNTHETIC_REASON",
                            "file": "SYNTHETIC_FILE",
                        },
                    },
                )
        sdk.capture_recovery(
            {
                "failure_ref": failure_ref,
                "evidence_scope": {"kind": "operation", "operation_id": operation_id},
                "attempt_id": str(uuid.uuid4()),
                "state": "recovery_observed",
                "operation": "retry reason=SYNTHETIC_REC_REASON",
                "status_code": 200,
                "duration_ms": 12,
            }
        )
        sdk.flush()

        events = transport.calls[0]["events"]
        assert isinstance(events, list)
        exc_event = next(event for event in events if event["event_type"] == "backend_exception")
        assert validate_evidence(exc_event)
        assert exc_event["context"]["evidence_reference"] == {
            "version": 1,
            "failure_ref": failure_ref,
            "scope": {"kind": "operation", "operation_id": operation_id},
        }
        assert exc_event["context"]["evidence_limits"] == {"version": 1, "resource_observations": "unsupported"}
        assert exc_event["context"]["payout"] == {
            "tag": "[REDACTED]",
            "reference": "[REDACTED]",
            "reason": "[REDACTED]",
            "file": "[REDACTED]",
        }
        assert exc_event["context"]["post_hook_reference"] == "[REDACTED]"

        graph = exc_event["payload"]["exception_evidence"]
        assert graph["unavailable_reason"] is None
        assert graph["nodes"][0]["message"] == "boom reason=[REDACTED]"
        assert graph["nodes"][0]["frames"]
        assert graph["nodes"][0]["frames"][0]["file"] not in (None, "[REDACTED]")
        assert exc_event["payload"]["execution"]["kind"] == "job"
        assert exc_event["payload"]["execution"]["name"] == "payout.sync reason=[REDACTED]"
        assert exc_event["payload"]["execution"]["limit_ms"] == 5000

        recovery_event = next(event for event in events if event["event_type"] == "diagnostic_context")
        assert validate_evidence(recovery_event)
        assert recovery_event["payload"]["failure_ref"] == failure_ref
        assert recovery_event["payload"]["state"] == "recovery_observed"
        assert recovery_event["payload"]["operation"] == "retry reason=[REDACTED]"

        encoded = json.dumps(events)
        for secret in (
            "SYNTHETIC_TAG",
            "SYNTHETIC_REF",
            "SYNTHETIC_REASON",
            "SYNTHETIC_FILE",
            "SYNTHETIC_EXEC_REASON",
            "SYNTHETIC_REC_REASON",
            "POST_HOOK_REF_SECRET",
        ):
            assert secret not in encoded
    finally:
        sdk.dispose()


def test_snapshot_exception_preserves_frame_file_under_broad_file_redaction() -> None:
    try:
        raise ValueError("inner file=SYNTHETIC_FILE")
    except ValueError as error:
        graph = snapshot_exception(error)
    assert graph is not None
    event = {
        "schema_version": "2026-03-01",
        "event_id": str(uuid.uuid4()),
        "event_type": "backend_exception",
        "occurred_at": "2026-10-26T10:00:00Z",
        "sdk_name": "debugbundle-python",
        "sdk_version": "3.0.0",
        "service": {"name": "api", "environment": "production"},
        "payload": {
            "name": "ValueError",
            "message": "inner file=SYNTHETIC_FILE",
            "stack": "trace",
            "handled": True,
            "request": {"method": "GET", "path": "/", "query": {}, "headers": {}},
            "response": {"status_code": 500},
            "runtime": {"version": "3.12"},
            "exception_evidence": graph,
        },
    }
    protected = protect_event_fields(event, ["file", "reason"])
    assert protected is not None
    assert protected["payload"]["exception_evidence"]["nodes"][0]["message"] == "inner file=[REDACTED]"
    assert protected["payload"]["exception_evidence"]["nodes"][0]["frames"][0]["file"] not in (None, "[REDACTED]")
    assert "SYNTHETIC_FILE" not in json.dumps(protected)
