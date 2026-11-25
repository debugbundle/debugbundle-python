"""Schema-aware protocol field preservation at SDK and browser-relay boundaries (SEC-23a)."""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

from .evidence_wire import _closed, _integer, diagnostic, execution, graph, reference, resources
from .redaction import UnsafeTelemetry, ascii_lower, has_safe_event_identity, sanitize_telemetry

MAX_PROTECTED_BYTES = 256 * 1024
ALLOWED_HTML_RESOURCE_TAGS = frozenset(
    {"script", "link", "img", "audio", "video", "source", "iframe", "object", "embed", "input"}
)
ALLOWED_READY_STATES = frozenset({"loading", "interactive", "complete"})
ALLOWED_VISIBILITY_STATES = frozenset({"visible", "hidden", "prerender", "unloaded"})
ALLOWED_REJECTION_KINDS = frozenset({"error", "string", "object", "null", "undefined", "unknown"})
ALLOWED_EVIDENCE_LIMIT_KEYS = frozenset({"version", "exception_evidence", "execution", "resource_observations"})
ALLOWED_EVIDENCE_LIMIT_REASONS = frozenset({"budget_exceeded", "unsafe", "unsupported", "unknown"})

_BROWSER_EVENT_REQUIRED = ("kind", "message", "file_name", "line_number", "column_number", "target", "opaque")
_BROWSER_EVENT_OPTIONAL = ("page",)
_TARGET_ATTR_STRING_KEYS = ("rel", "as", "type", "media", "cross_origin")
_TARGET_ATTR_BOOL_KEYS = ("async", "defer", "integrity_present")
_TARGET_ATTR_KEYS = _TARGET_ATTR_STRING_KEYS + _TARGET_ATTR_BOOL_KEYS
_MAX_SAFE_INT = 9007199254740991


def _scrub_string(
    value: str,
    additional_fields: Collection[object] | None,
    exact_fields: Collection[object] | None,
) -> str | None:
    scrubbed = sanitize_telemetry(value, additional_fields, exact_fields)
    return scrubbed if type(scrubbed) is str else None


def _scrub_nullable_string(
    value: Any,
    additional_fields: Collection[object] | None,
    exact_fields: Collection[object] | None,
) -> tuple[bool, str | None]:
    if value is None:
        return True, None
    if type(value) is not str:
        return False, None
    scrubbed = _scrub_string(value, additional_fields, exact_fields)
    return (False, None) if scrubbed is None else (True, scrubbed)


def _clone_reference(valid_ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": valid_ref["version"],
        "failure_ref": valid_ref["failure_ref"],
        "scope": dict(valid_ref["scope"]),
    }


def _is_valid_evidence_limits(raw: Any) -> bool:
    if type(raw) is not dict or "version" not in raw:
        return False
    version = raw["version"]
    if type(version) is not int or version != 1:
        return False
    for key, value in raw.items():
        if type(key) is not str or key not in ALLOWED_EVIDENCE_LIMIT_KEYS:
            return False
        if key == "version":
            continue
        if type(value) is not str or value not in ALLOWED_EVIDENCE_LIMIT_REASONS:
            return False
    return True


def _sanitize_browser_event_block(
    raw: Any,
    additional_fields: Collection[object] | None,
    exact_fields: Collection[object] | None,
) -> dict[str, Any] | None:
    if not _closed(raw, _BROWSER_EVENT_REQUIRED, _BROWSER_EVENT_OPTIONAL):
        return None
    kind = raw["kind"]
    opaque = raw["opaque"]
    line_number = raw["line_number"]
    column_number = raw["column_number"]
    if (
        type(kind) is not str
        or kind not in ("window_error", "resource_error")
        or type(opaque) is not bool
        or (line_number is not None and not _integer(line_number, 0, _MAX_SAFE_INT))
        or (column_number is not None and not _integer(column_number, 0, _MAX_SAFE_INT))
    ):
        return None
    msg_ok, message = _scrub_nullable_string(raw["message"], additional_fields, exact_fields)
    file_ok, file_name = _scrub_nullable_string(raw["file_name"], additional_fields, exact_fields)
    if not msg_ok or not file_ok:
        return None

    target_out: dict[str, Any] | None = None
    raw_target = raw["target"]
    if raw_target is not None:
        if not _closed(raw_target, ("tag_name", "source_url"), ("attributes",)):
            return None
        raw_tag_name = raw_target["tag_name"]
        tag_name_out: str | None
        if raw_tag_name is None:
            tag_name_out = None
        elif type(raw_tag_name) is str:
            if ascii_lower(raw_tag_name) in ALLOWED_HTML_RESOURCE_TAGS:
                tag_name_out = raw_tag_name
            else:
                sanitized_tag = sanitize_telemetry({"tag_name": raw_tag_name}, additional_fields, exact_fields)
                if type(sanitized_tag) is not dict or type(sanitized_tag.get("tag_name")) is not str:
                    return None
                tag_name_out = sanitized_tag["tag_name"]
        else:
            return None
        source_ok, source_url = _scrub_nullable_string(raw_target["source_url"], additional_fields, exact_fields)
        if not source_ok:
            return None
        target_out = {"tag_name": tag_name_out, "source_url": source_url}
        if "attributes" in raw_target:
            raw_attrs = raw_target["attributes"]
            if not _closed(raw_attrs, (), _TARGET_ATTR_KEYS):
                return None
            attrs_out: dict[str, Any] = {}
            for key in _TARGET_ATTR_STRING_KEYS:
                if key in raw_attrs:
                    attr_value = raw_attrs[key]
                    if type(attr_value) is not str:
                        return None
                    scrubbed = _scrub_string(attr_value, additional_fields, exact_fields)
                    if scrubbed is None:
                        return None
                    attrs_out[key] = scrubbed
            for key in _TARGET_ATTR_BOOL_KEYS:
                if key in raw_attrs:
                    if type(raw_attrs[key]) is not bool:
                        return None
                    attrs_out[key] = raw_attrs[key]
            target_out["attributes"] = attrs_out

    out: dict[str, Any] = {
        "kind": kind,
        "message": message,
        "file_name": file_name,
        "line_number": line_number,
        "column_number": column_number,
        "target": target_out,
        "opaque": opaque,
    }
    if "page" in raw:
        raw_page = raw["page"]
        if not _closed(raw_page, ("url", "referrer", "ready_state", "visibility_state")):
            return None
        url_ok, page_url = _scrub_nullable_string(raw_page["url"], additional_fields, exact_fields)
        ref_ok, page_referrer = _scrub_nullable_string(raw_page["referrer"], additional_fields, exact_fields)
        ready_state = raw_page["ready_state"]
        visibility_state = raw_page["visibility_state"]
        if (
            not url_ok
            or not ref_ok
            or (ready_state is not None and (type(ready_state) is not str or ready_state not in ALLOWED_READY_STATES))
            or (
                visibility_state is not None
                and (type(visibility_state) is not str or visibility_state not in ALLOWED_VISIBILITY_STATES)
            )
        ):
            return None
        out["page"] = {
            "url": page_url,
            "referrer": page_referrer,
            "ready_state": ready_state,
            "visibility_state": visibility_state,
        }
    return out


def _sanitize_rejection_reason_block(
    raw: Any,
    additional_fields: Collection[object] | None,
    exact_fields: Collection[object] | None,
) -> dict[str, Any] | None:
    if not _closed(raw, ("kind",), ("name", "message", "preview")):
        return None
    kind = raw["kind"]
    if type(kind) is not str or kind not in ALLOWED_REJECTION_KINDS:
        return None
    out: dict[str, Any] = {"kind": kind}
    for field in ("name", "message", "preview"):
        if field in raw:
            value = raw[field]
            if type(value) is not str or not value:
                return None
            scrubbed = _scrub_string(value, additional_fields, exact_fields)
            if scrubbed is None:
                return None
            out[field] = scrubbed
    return out


def _sanitize_exception_evidence_block(
    raw: Any,
    additional_fields: Collection[object] | None,
    exact_fields: Collection[object] | None,
) -> dict[str, Any] | None:
    if not graph(raw, check_byte_budget=False):
        return None
    nodes: list[dict[str, Any]] = []
    for node in raw["nodes"]:
        name = _scrub_string(node["name"], additional_fields, exact_fields)
        message = _scrub_string(node["message"], additional_fields, exact_fields)
        stack = None if node["stack"] is None else _scrub_string(node["stack"], additional_fields, exact_fields)
        if name is None or message is None or (node["stack"] is not None and stack is None):
            return None
        frames: list[dict[str, Any]] = []
        for frame in node["frames"]:
            fn = (
                None
                if frame["function"] is None
                else _scrub_string(frame["function"], additional_fields, exact_fields)
            )
            file_path = (
                None
                if frame["file"] is None
                else _scrub_string(frame["file"], additional_fields, exact_fields)
            )
            if (frame["function"] is not None and fn is None) or (frame["file"] is not None and file_path is None):
                return None
            frames.append(
                {
                    "function": fn,
                    "file": file_path,
                    "line": frame["line"],
                    "column": frame["column"],
                    "classification": frame["classification"],
                }
            )
        nodes.append(
            {
                "id": node["id"],
                "name": name,
                "message": message,
                "stack": stack,
                "trace_origin": node["trace_origin"],
                "parsing_status": node["parsing_status"],
                "frames": frames,
                "relationships": [
                    {"kind": edge["kind"], "target_id": edge["target_id"]} for edge in node["relationships"]
                ],
            }
        )
    return {
        "version": raw["version"],
        "runtime": raw["runtime"],
        "root_id": raw["root_id"],
        "nodes": nodes,
        "truncated": raw["truncated"],
        "unavailable_reason": raw["unavailable_reason"],
    }


def _sanitize_execution_block(
    raw: Any,
    additional_fields: Collection[object] | None,
    exact_fields: Collection[object] | None,
) -> dict[str, Any] | None:
    if not execution(raw):
        return None
    name = None if raw["name"] is None else _scrub_string(raw["name"], additional_fields, exact_fields)
    if raw["name"] is not None and name is None:
        return None
    return {
        "version": raw["version"],
        "kind": raw["kind"],
        "name": name,
        "elapsed_ms": raw["elapsed_ms"],
        "limit_ms": raw["limit_ms"],
    }


def _sanitize_resource_observations_block(
    raw: Any,
    additional_fields: Collection[object] | None,
    exact_fields: Collection[object] | None,
) -> dict[str, Any] | None:
    if not resources(raw):
        return None
    items: list[dict[str, Any]] = []
    for item in raw["items"]:
        source_url = (
            None
            if item["source_url"] is None
            else _scrub_string(item["source_url"], additional_fields, exact_fields)
        )
        if item["source_url"] is not None and source_url is None:
            return None
        if item["kind"] == "timing":
            items.append(
                {
                    "kind": item["kind"],
                    "observed_at": item["observed_at"],
                    "source_url": source_url,
                    "availability": item["availability"],
                    "response_status": item["response_status"],
                    "duration_ms": item["duration_ms"],
                    "transfer_size_bytes": item["transfer_size_bytes"],
                }
            )
        else:
            items.append(
                {
                    "kind": item["kind"],
                    "observed_at": item["observed_at"],
                    "source_url": source_url,
                    "effective_directive": item["effective_directive"],
                    "disposition": item["disposition"],
                    "document_status_code": item["document_status_code"],
                }
            )
    return {"version": raw["version"], "items": items}


def _sanitize_diagnostic_context_payload(
    raw: Any,
    additional_fields: Collection[object] | None,
    exact_fields: Collection[object] | None,
) -> dict[str, Any] | None:
    if not diagnostic(raw):
        return None
    if raw["kind"] == "recovery":
        out: dict[str, Any] = {
            "version": raw["version"],
            "kind": raw["kind"],
            "failure_ref": raw["failure_ref"],
            "scope": dict(raw["scope"]),
            "attempt_id": raw["attempt_id"],
            "state": raw["state"],
        }
        if "operation" in raw:
            operation = _scrub_string(raw["operation"], additional_fields, exact_fields)
            if operation is None:
                return None
            out["operation"] = operation
        if "status_code" in raw:
            out["status_code"] = raw["status_code"]
        if "duration_ms" in raw:
            out["duration_ms"] = raw["duration_ms"]
        return out
    observations = _sanitize_resource_observations_block(raw["observations"], additional_fields, exact_fields)
    if observations is None:
        return None
    return {
        "version": raw["version"],
        "kind": raw["kind"],
        "failure_ref": raw["failure_ref"],
        "scope": dict(raw["scope"]),
        "source_event_id": raw["source_event_id"],
        "observations": observations,
    }


def protect_event_fields(
    event: dict[str, Any],
    additional_fields: Collection[object] | None = None,
    exact_fields: Collection[object] | None = None,
    *,
    preserve_protocol: bool = True,
) -> dict[str, Any] | None:
    """Preserve validated closed SDK protocol structures while protecting captured application data (SEC-23a)."""
    if not has_safe_event_identity(event, additional_fields, exact_fields):
        return None
    raw_payload = event.get("payload")
    raw_service = event.get("service")
    has_context = "context" in event
    raw_context = event.get("context")
    if type(raw_payload) is not dict or type(raw_service) is not dict:
        return None
    if has_context and type(raw_context) is not dict:
        return None

    working_payload = dict(raw_payload)
    preserved_payload: dict[str, Any] = {}
    diagnostic_payload: dict[str, Any] | None = None
    event_type = event.get("event_type")

    if preserve_protocol:
        if event_type == "diagnostic_context":
            diagnostic_payload = _sanitize_diagnostic_context_payload(working_payload, additional_fields, exact_fields)
            if diagnostic_payload is None:
                return None
        else:
            if event_type == "frontend_exception":
                if "browser_event" in working_payload:
                    safe_browser_event = _sanitize_browser_event_block(
                        working_payload["browser_event"], additional_fields, exact_fields
                    )
                    if safe_browser_event is not None:
                        preserved_payload["browser_event"] = safe_browser_event
                        del working_payload["browser_event"]
                if "rejection_reason" in working_payload:
                    safe_rejection = _sanitize_rejection_reason_block(
                        working_payload["rejection_reason"], additional_fields, exact_fields
                    )
                    if safe_rejection is not None:
                        preserved_payload["rejection_reason"] = safe_rejection
                        del working_payload["rejection_reason"]
                if "resource_observations" in working_payload:
                    safe_observations = _sanitize_resource_observations_block(
                        working_payload["resource_observations"], additional_fields, exact_fields
                    )
                    if safe_observations is not None:
                        preserved_payload["resource_observations"] = safe_observations
                        del working_payload["resource_observations"]
            if event_type in ("backend_exception", "frontend_exception") and "exception_evidence" in working_payload:
                safe_graph = _sanitize_exception_evidence_block(
                    working_payload["exception_evidence"], additional_fields, exact_fields
                )
                if safe_graph is not None:
                    preserved_payload["exception_evidence"] = safe_graph
                    del working_payload["exception_evidence"]
            if event_type == "backend_exception" and "execution" in working_payload:
                safe_execution = _sanitize_execution_block(
                    working_payload["execution"], additional_fields, exact_fields
                )
                if safe_execution is not None:
                    preserved_payload["execution"] = safe_execution
                    del working_payload["execution"]
            if (
                event_type == "error_suppressed"
                and type(working_payload.get("reason")) is str
                and working_payload["reason"] == "queue_pressure"
            ):
                preserved_payload["reason"] = "queue_pressure"
                del working_payload["reason"]

    working_context = dict(raw_context) if has_context and type(raw_context) is dict else {}
    preserved_context: dict[str, Any] = {}
    if preserve_protocol and has_context:
        if "evidence_reference" in working_context:
            valid_ref = reference(working_context["evidence_reference"])
            if valid_ref is not None:
                preserved_context["evidence_reference"] = _clone_reference(valid_ref)
                del working_context["evidence_reference"]
        if "evidence_limits" in working_context and _is_valid_evidence_limits(working_context["evidence_limits"]):
            preserved_context["evidence_limits"] = dict(working_context["evidence_limits"])
            del working_context["evidence_limits"]

    sanitized_service = sanitize_telemetry(raw_service, additional_fields, exact_fields)
    if type(sanitized_service) is not dict:
        return None
    if diagnostic_payload is not None:
        protected_payload: Any = diagnostic_payload
    else:
        sanitized_payload = sanitize_telemetry(working_payload, additional_fields, exact_fields)
        if type(sanitized_payload) is not dict:
            return None
        protected_payload = {**sanitized_payload, **preserved_payload}

    assembled: dict[str, Any] = {"payload": protected_payload, "service": sanitized_service}
    if has_context:
        sanitized_context = sanitize_telemetry(working_context, additional_fields, exact_fields)
        if type(sanitized_context) is not dict:
            return None
        assembled["context"] = {**sanitized_context, **preserved_context}

    if len(json.dumps(assembled, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_PROTECTED_BYTES:
        raise UnsafeTelemetry("budget_exceeded")

    candidate = dict(event)
    candidate["payload"] = assembled["payload"]
    candidate["service"] = assembled["service"]
    if has_context:
        candidate["context"] = assembled["context"]
    return candidate
