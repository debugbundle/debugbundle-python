import builtins
import json

from debugbundle.exception_evidence import snapshot_exception


def test_native_causes_context_suppression_and_original_frame_locations() -> None:
    try:
        raise ValueError("inner")
    except ValueError as inner:
        try:
            raise RuntimeError("outer") from inner
        except RuntimeError as error:
            graph = snapshot_exception(error)
    assert graph["runtime"] == "python"
    assert [node["message"] for node in graph["nodes"]] == ["outer", "inner"]
    assert graph["nodes"][0]["relationships"] == [{"kind": "cause", "target_id": 1}]
    assert graph["nodes"][0]["frames"][0]["file"].endswith("test_exception_evidence.py")
    assert "locals" not in json.dumps(graph)
    try:
        raise ValueError("implicit")
    except ValueError:
        try:
            raise RuntimeError("context")
        except RuntimeError as error:
            assert snapshot_exception(error)["nodes"][0]["relationships"][0]["kind"] == "context"
        try:
            raise RuntimeError("suppressed") from None
        except RuntimeError as error:
            assert len(snapshot_exception(error)["nodes"]) == 1


def test_never_invokes_overridden_metadata_renderers_or_group_accessors() -> None:
    calls: list[str] = []

    class UnsafeError(RuntimeError):
        def __getattribute__(self, name: str) -> object:
            if name in {"args", "__traceback__", "__cause__", "__context__", "__suppress_context__", "__class__"}:
                calls.append(name)
                raise AssertionError("custom metadata")
            return super().__getattribute__(name)

        def __str__(self) -> str:
            calls.append("str")
            raise AssertionError("custom formatting")

    error = UnsafeError("password=SYNTHETIC_SECRET")
    graph = snapshot_exception(error)
    assert calls == []
    assert "SYNTHETIC_SECRET" not in json.dumps(graph)
    group_type = getattr(builtins, "ExceptionGroup", None)
    if group_type is not None:
        group = group_type("group", [error, ValueError("second")])
        graph = snapshot_exception(group)
        assert graph["nodes"][0]["message"] == "group"
        assert [edge["kind"] for edge in graph["nodes"][0]["relationships"]] == ["aggregate", "aggregate"]
        assert calls == []


def test_cycles_utf8_and_fatal_budgets_are_bounded_without_retaining_raw_errors() -> None:
    error = RuntimeError("😀" * 20000)
    error.__cause__ = error
    graph = snapshot_exception(error)
    assert graph["truncated"] is True
    assert len(json.dumps(graph, ensure_ascii=False).encode()) <= 32768
    assert len(graph["nodes"]) <= 8
    fatal = snapshot_exception(error, fatal=True, budget_bytes=3500)
    assert len(json.dumps(fatal, ensure_ascii=False).encode()) <= 3500
    assert len(fatal["nodes"]) <= 2
    assert all(len(node["frames"]) <= 8 for node in fatal["nodes"])


def test_native_filename_urls_are_sanitized_without_reading_source_or_locals() -> None:
    try:
        exec(
            compile("raise RuntimeError('synthetic')", "https://user:secret@cdn.example/fault.py?token=secret", "exec")
        )
    except RuntimeError as error:
        graph = snapshot_exception(error)
    assert any(frame["file"] == "https://cdn.example/fault.py" for frame in graph["nodes"][0]["frames"])
    assert "secret" not in json.dumps(graph)


def test_native_traceback_depth_and_group_width_do_not_exceed_the_contract() -> None:
    def recurse(depth: int) -> None:
        if depth == 0:
            raise ValueError("deep traceback")
        recurse(depth - 1)

    try:
        recurse(100)
    except ValueError as error:
        graph = snapshot_exception(error)
    assert graph["truncated"] is True
    assert len(graph["nodes"][0]["frames"]) == 32
    error = RuntimeError("leaf")
    for _ in range(10):
        outer = RuntimeError("parent")
        outer.__cause__ = error
        error = outer
    graph = snapshot_exception(error)
    assert graph["truncated"] is True
    assert len(graph["nodes"]) == 5
    group_type = getattr(builtins, "ExceptionGroup", None)
    if group_type is not None:
        graph = snapshot_exception(group_type("wide", [ValueError(str(index)) for index in range(30)]))
        assert graph["truncated"] is True
        assert len(graph["nodes"][0]["relationships"]) == 5


def test_escaped_messages_shrink_and_tiny_remaining_budgets_withhold_optional_evidence() -> None:
    group_type = getattr(builtins, "ExceptionGroup", None)
    if group_type is None:
        return
    group = group_type("original", [RuntimeError("\x01" * 2000) for _ in range(5)])
    graph = snapshot_exception(group, budget_bytes=1024)
    assert graph is not None
    assert graph["nodes"][0]["message"] == "original"
    assert graph["unavailable_reason"] == "budget_exceeded"
    assert len(json.dumps(graph, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024
    assert snapshot_exception(group, budget_bytes=100) is None
