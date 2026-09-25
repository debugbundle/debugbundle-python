"""Bounded exception details without invoking application-defined string renderers or source reads."""

from __future__ import annotations

MAX_CAUSES = 8
MAX_FRAMES = 64
MAX_STACK_CHARS = 16 * 1024


def exception_details(error: BaseException) -> tuple[str, str, str]:
    name = _exception_name(error)
    message = _safe_message(error)
    lines: list[str] = []
    visited: set[int] = set()
    current: BaseException | None = error
    frames_seen = 0

    while current is not None and len(visited) < MAX_CAUSES and id(current) not in visited:
        visited.add(id(current))
        current_name = _exception_name(current)
        lines.append(f"{current_name}: {_safe_message(current)}")
        try:
            tb = BaseException.__dict__["__traceback__"].__get__(current)
        except BaseException:
            tb = None
        while tb is not None and frames_seen < MAX_FRAMES:
            try:
                code = tb.tb_frame.f_code
                filename = code.co_filename[:256]
                function = code.co_name[:128]
                line = tb.tb_lineno
                tb = tb.tb_next
            except BaseException:
                break
            lines.append(f"  File {filename}, line {line}, in {function}")
            frames_seen += 1
        if frames_seen >= MAX_FRAMES:
            lines.append("  ... additional frames omitted")
            break
        try:
            next_error = BaseException.__dict__["__cause__"].__get__(current)
            if next_error is None and not BaseException.__dict__["__suppress_context__"].__get__(current):
                next_error = BaseException.__dict__["__context__"].__get__(current)
        except BaseException:
            next_error = None
        current = next_error
        if current is not None:
            lines.append("Caused by:")

    return name, message, "\n".join(lines)[:MAX_STACK_CHARS]


def _exception_name(error: BaseException) -> str:
    # Invoke built-in descriptors directly: subclass properties, __getattribute__,
    # metaclass accessors, and truthiness must never execute on capture callers.
    return str(type.__dict__["__name__"].__get__(type(error)))[:256]


def _safe_message(error: BaseException) -> str:
    try:
        args = BaseException.__dict__["args"].__get__(error)
        if not isinstance(args, tuple):
            return "[message unavailable]"
        parts: list[str] = []
        for value in args[:4]:
            if issubclass(type(value), str):
                parts.append(str.__getitem__(value, slice(0, 1024)))
            elif type(value) in (int, float, bool):
                parts.append(str(value))
            else:
                parts.append("[unsupported argument]")
        return ", ".join(parts)[:4096]
    except BaseException:
        return "[message unavailable]"
