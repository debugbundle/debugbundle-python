from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO_ROOT / "smoke" / "run_app_driven_smoke.py"
SPEC = importlib.util.spec_from_file_location("debugbundle_python_smoke", SMOKE_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
SMOKE = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("debugbundle_python_smoke", SMOKE)
SPEC.loader.exec_module(SMOKE)


def test_install_with_retry_retries_until_success(monkeypatch) -> None:
    calls: list[list[str]] = []
    sleeps: list[int] = []
    outcomes = [
        subprocess.CalledProcessError(returncode=1, cmd=["pip", "install"]),
        None,
    ]

    def fake_run_subprocess(command: list[str]) -> None:
        calls.append(command)
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(SMOKE, "_run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(SMOKE.time, "sleep", sleeps.append)

    SMOKE._install_with_retry(
        ["python", "-m", "pip", "install", "debugbundle-python==0.1.9"],
        retries=3,
        retry_delay_seconds=7,
    )

    assert len(calls) == 2
    assert sleeps == [7]


def test_install_with_retry_raises_after_final_attempt(monkeypatch) -> None:
    def fake_run_subprocess(command: list[str]) -> None:
        raise subprocess.CalledProcessError(returncode=1, cmd=command)

    monkeypatch.setattr(SMOKE, "_run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(SMOKE.time, "sleep", lambda seconds: None)

    try:
        SMOKE._install_with_retry(
            ["python", "-m", "pip", "install", "debugbundle-python==0.1.9"],
            retries=2,
            retry_delay_seconds=1,
        )
    except subprocess.CalledProcessError as error:
        assert error.returncode == 1
    else:
        raise AssertionError("Expected install retry helper to re-raise after the final attempt.")
