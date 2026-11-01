"""Bounded native snapshots. No source reads, locals, custom renderers or raw-error retention."""

from __future__ import annotations

import builtins
import json
from typing import TypedDict, cast
from urllib.parse import urlsplit, urlunsplit

from .exception_snapshot import _exception_name, _safe_message
from .redaction import UnsafeTelemetry, sanitize_telemetry


class Frame(TypedDict):
    function: str | None
    file: str | None
    line: int | None
    column: int | None
    classification: str


class Relationship(TypedDict):
    kind: str
    target_id: int


class Node(TypedDict):
    id: int
    name: str
    message: str
    stack: str | None
    trace_origin: str
    parsing_status: str
    frames: list[Frame]
    relationships: list[Relationship]


class ExceptionEvidence(TypedDict):
    version: int
    runtime: str
    root_id: int
    nodes: list[Node]
    truncated: bool
    unavailable_reason: str | None


_GROUP = vars(builtins).get("BaseExceptionGroup")
_GROUP_CHILDREN = vars(_GROUP).get("exceptions") if _GROUP is not None else None
_GROUP_MESSAGE = vars(_GROUP).get("message") if _GROUP is not None else None


def _bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def snapshot_exception(
    error: BaseException,
    *,
    fatal: bool = False,
    budget_bytes: int | None = None,
) -> ExceptionEvidence | None:
    maximum = min(4096 if fatal else 32768, budget_bytes if budget_bytes is not None else 32768)
    if maximum < 512:
        return None
    graph: ExceptionEvidence = {
        "version": 1,
        "runtime": "python",
        "root_id": 0,
        "nodes": [],
        "truncated": False,
        "unavailable_reason": None,
    }
    identities: dict[int, int] = {}

    def text(value: str, limit: int) -> str:
        # Native metadata may be very long; bound work before encoding/scrubbing.
        bounded = value[:limit]
        encoded = bounded.encode("utf-8", errors="replace")
        result = encoded[:limit].decode("utf-8", errors="ignore")
        if result != value:
            graph["truncated"] = True
        try:
            return cast(str, sanitize_telemetry(result))
        except UnsafeTelemetry:
            return "[withheld]"

    def location(value: str) -> str:
        result = text(value, 2048)
        if result.lower().startswith(("https://", "http://")):
            try:
                parts = urlsplit(result)
                result = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
            except ValueError:
                return "[withheld]"
        return result

    def height(index: int) -> int:
        return max((1 + height(edge["target_id"]) for edge in graph["nodes"][index]["relationships"]), default=0)

    def visit(current: BaseException, depth: int, ancestors: set[int]) -> int | None:
        identity = id(current)
        if identity in ancestors or depth > 4:
            graph["truncated"] = True
            return None
        existing = identities.get(identity)
        if existing is not None:
            if depth + height(existing) > 4:
                graph["truncated"] = True
                return None
            return existing
        if len(graph["nodes"]) >= (2 if fatal else 8):
            graph["truncated"] = True
            return None
        name = text(_exception_name(current), 256) or "Error"
        is_group = _GROUP is not None and issubclass(type(current), _GROUP)
        message = _GROUP_MESSAGE.__get__(current) if is_group and _GROUP_MESSAGE is not None else _safe_message(current)
        node: Node = {
            "id": len(graph["nodes"]),
            "name": name,
            "message": text(message, 512 if fatal else 2048) or name,
            "stack": None,
            "trace_origin": "unavailable",
            "parsing_status": "unavailable",
            "frames": [],
            "relationships": [],
        }
        graph["nodes"].append(node)
        identities[identity] = node["id"]
        # Final built-in descriptors bypass subclass properties/__getattribute__.
        traceback = BaseException.__dict__["__traceback__"].__get__(current)
        while traceback is not None and len(node["frames"]) < (8 if fatal else 32):
            code = traceback.tb_frame.f_code
            node["frames"].append(
                {
                    "function": text(code.co_name, 256) or None,
                    "file": location(code.co_filename) or None,
                    "line": traceback.tb_lineno if traceback.tb_lineno > 0 else None,
                    "column": None,
                    "classification": "unknown",
                }
            )
            traceback = traceback.tb_next
        if node["frames"]:
            node["trace_origin"] = "runtime_reported"
            node["parsing_status"] = "parsed"
        if traceback is not None:
            graph["truncated"] = True
            node["parsing_status"] = "truncated"
        next_ancestors = ancestors | {identity}

        def link(child: BaseException, kind: str) -> None:
            if len(node["relationships"]) >= 5:
                graph["truncated"] = True
                return
            target = visit(child, depth + 1, next_ancestors)
            if target is not None:
                edge: Relationship = {"kind": kind, "target_id": target}
                if edge not in node["relationships"]:
                    node["relationships"].append(edge)

        cause = BaseException.__dict__["__cause__"].__get__(current)
        if cause is not None:
            link(cause, "cause")
        elif not BaseException.__dict__["__suppress_context__"].__get__(current):
            context = BaseException.__dict__["__context__"].__get__(current)
            if context is not None:
                link(context, "context")
        if is_group and _GROUP_CHILDREN is not None:
            children = _GROUP_CHILDREN.__get__(current)
            # The built-in tuple needs no application iterator, len or item hooks.
            count = tuple.__len__(children)
            for index in range(min(count, 5)):
                link(tuple.__getitem__(children, index), "aggregate")
            if count > 5:
                graph["truncated"] = True
        return node["id"]

    try:
        visit(error, 0, set())
        if _bytes(graph) > maximum:
            graph["truncated"] = True
            graph["unavailable_reason"] = "budget_exceeded"
            for node in reversed(graph["nodes"]):
                while node["frames"] and _bytes(graph) > maximum:
                    node["frames"].pop()
                    node["parsing_status"] = "truncated"
            if _bytes(graph) > maximum:
                for node in graph["nodes"]:
                    node["name"] = text(node["name"], 64 if fatal else 128)
                    node["message"] = text(node["message"], 128 if fatal else 256)
            while len(graph["nodes"]) > 1 and _bytes(graph) > maximum:
                removed = graph["nodes"].pop()["id"]
                for node in graph["nodes"]:
                    node["relationships"] = [edge for edge in node["relationships"] if edge["target_id"] != removed]
        if graph["truncated"] and graph["unavailable_reason"] is None:
            graph["unavailable_reason"] = "budget_exceeded"
        return graph if graph["nodes"] and _bytes(graph) <= maximum else None
    except BaseException:
        return None
