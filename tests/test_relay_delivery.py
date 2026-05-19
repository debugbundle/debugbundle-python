from __future__ import annotations

from pathlib import Path

import pytest

import debugbundle.relay_delivery as relay_delivery


def test_resolves_default_delivery_directories_and_attaches_project_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    assert relay_delivery.resolve_default_local_events_dir() == str(tmp_path / ".debugbundle" / "local" / "events")
    assert relay_delivery.resolve_default_relay_spool_dir() == str(
        tmp_path / ".debugbundle" / "local" / "browser-relay-spool"
    )
    assert relay_delivery.attach_project_token([{"event_id": "evt-1"}], "dbundle_proj_test") == [
        {"event_id": "evt-1", "project_token": "dbundle_proj_test"}
    ]


def test_mark_spool_file_delivered_creates_marker_file(tmp_path: Path) -> None:
    written_file = tmp_path / "spool" / "batch.events.json"
    written_file.parent.mkdir(parents=True)
    written_file.write_text("[]", encoding="utf-8")

    relay_delivery.mark_spool_file_delivered(str(written_file))

    assert written_file.with_name(f"{written_file.name}{relay_delivery.RELAY_SPOOL_DELIVERED_MARKER_SUFFIX}").exists()


def test_mark_spool_file_delivered_ignores_marker_write_failures(tmp_path: Path) -> None:
    relay_delivery.mark_spool_file_delivered(str(tmp_path / "missing" / "batch.events.json"))


def test_atomic_relay_file_transport_handles_empty_batches() -> None:
    transport = relay_delivery.AtomicRelayFileTransport("/tmp/debugbundle-events", "web api")

    result = transport.write([])

    assert result.status_code == 202
    assert result.written_file_path is None


def test_atomic_relay_file_transport_returns_500_when_writes_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = relay_delivery.AtomicRelayFileTransport(str(tmp_path / "events"), "web api")

    def fail_write(_: str, __: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(relay_delivery, "_write_secure_temp_file", fail_write)

    result = transport.write([{"event_id": "evt-1"}])

    assert result.status_code == 500
    assert result.written_file_path is None


def test_relay_forward_transport_handles_missing_token_and_transport_errors() -> None:
    def raising_transport(_: dict[str, object]) -> object:
        raise RuntimeError("boom")

    transport = relay_delivery.RelayForwardTransport(
        "https://api.debugbundle.test/v1/events", transport=raising_transport
    )

    assert transport.send("", [{"event_id": "evt-1"}]) == (False, False)
    assert transport.send("dbundle_proj_test", [{"event_id": "evt-1"}]) == (True, False)


def test_assert_not_symlink_rejects_symbolic_links(tmp_path: Path) -> None:
    target = tmp_path / "target.events.json"
    target.write_text("[]", encoding="utf-8")
    symlink_path = tmp_path / "symlink.events.json"
    symlink_path.symlink_to(target)

    with pytest.raises(OSError, match="symlink_path_rejected"):
        relay_delivery._assert_not_symlink(str(symlink_path))


def test_cleanup_temp_files_ignores_missing_directories_and_remove_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    stale_tmp_file = events_dir / "stale.tmp-1"
    stale_tmp_file.write_text("stale", encoding="utf-8")
    blocked_tmp_file = events_dir / "blocked.tmp-2"
    blocked_tmp_file.write_text("blocked", encoding="utf-8")
    keep_file = events_dir / "keep.events.json"
    keep_file.write_text("keep", encoding="utf-8")

    original_remove = relay_delivery.os.remove

    def flaky_remove(path: str) -> None:
        if path.endswith(blocked_tmp_file.name):
            raise OSError("busy")
        original_remove(path)

    monkeypatch.setattr(relay_delivery.os, "remove", flaky_remove)

    relay_delivery._cleanup_temp_files(str(events_dir))
    relay_delivery._cleanup_temp_files(str(tmp_path / "missing"))

    assert not stale_tmp_file.exists()
    assert blocked_tmp_file.exists()
    assert keep_file.exists()