import threading
import time

from debugbundle.core import DebugBundleSdk
from debugbundle.exception_snapshot import exception_details


def test_public_capture_bypasses_blocked_exception_metadata_and_preserves_real_traceback() -> None:
    release = threading.Event()
    accessor_calls: list[str] = []

    class HostileType(type):
        def __getattribute__(cls, name: str) -> object:
            if name == "__name__":
                accessor_calls.append(name)
                release.wait(1)
            return super().__getattribute__(name)

    class HostileError(RuntimeError, metaclass=HostileType):
        def __getattribute__(self, name: str) -> object:
            if name in ("args", "__traceback__", "__cause__", "__context__", "__suppress_context__"):
                accessor_calls.append(name)
                release.wait(1)
            return super().__getattribute__(name)

        @property
        def args(self) -> tuple[str, ...]:
            accessor_calls.append("args property")
            release.wait(1)
            return ("wrong application representation",)

    sdk = DebugBundleSdk(transport=lambda _request: 202)
    sdk.init(project_token="dbundle_proj_test", batch_size=1000, flush_interval=3600)
    try:
        try:
            raise ValueError("original cause")
        except ValueError as cause:
            try:
                raise HostileError("original failure") from cause
            except HostileError as error:
                started = time.perf_counter()
                caller = threading.Thread(target=sdk.capture_exception, args=(error,))
                caller.start()
                caller.join(0.25)
                finished_without_release = not caller.is_alive()
                release.set()
                caller.join(2)
                assert finished_without_release
                assert time.perf_counter() - started < 0.5
        assert accessor_calls == []
        assert len(sdk._buffer) == 1
        payload = sdk._buffer[0]["payload"]
        assert payload["name"] == "HostileError"
        assert payload["message"] == "original failure"
        assert "test_public_capture_bypasses_blocked_exception_metadata" in payload["stack"]
        assert "ValueError: original cause" in payload["stack"]
    finally:
        release.set()
        sdk.dispose()


def test_hostile_exception_accessors_do_not_escape_or_run_custom_renderers() -> None:
    class HostileError(RuntimeError):
        def __getattribute__(self, name: str) -> object:
            if name in ("__traceback__", "__cause__", "__context__"):
                raise AssertionError("hostile accessor")
            return super().__getattribute__(name)

        def __str__(self) -> str:
            raise AssertionError("hostile renderer")

    name, message, stack = exception_details(HostileError(object()))
    assert name == "HostileError"
    assert message == "[unsupported argument]"
    assert stack.startswith("HostileError: [unsupported argument]")


def test_many_python_frames_and_causes_are_bounded() -> None:
    def raise_nested(depth: int) -> None:
        if depth == 0:
            raise ValueError("synthetic cause")
        raise_nested(depth - 1)

    try:
        try:
            raise_nested(90)
        except ValueError as cause:
            raise RuntimeError("synthetic root") from cause
    except RuntimeError as error:
        name, message, stack = exception_details(error)

    assert name == "RuntimeError"
    assert message == "synthetic root"
    assert len(stack) <= 16 * 1024
    assert "additional frames omitted" in stack
