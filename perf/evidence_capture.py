"""Synthetic SDK capture comparison; fixture construction and cleanup are excluded."""

from __future__ import annotations

import gc
import json
import sys
import time
import tracemalloc
import uuid
from typing import Any

from debugbundle.core import DebugBundleSdk
from debugbundle.evidence_wire import FEATURES
from debugbundle.transport import TransportResponse


def native_error(index: int) -> BaseException:
    try:
        try:
            raise ValueError(f"inner {index}")
        except ValueError as cause:
            raise RuntimeError(f"outer {index}") from cause
    except RuntimeError as error:
        return error


def calls() -> list[tuple[BaseException, dict[str, Any], dict[str, Any]]]:
    result = []
    for index in range(20):
        context = {
            "failure_ref": str(uuid.uuid4()),
            "evidence_scope": {"kind": "operation", "operation_id": str(uuid.uuid4())},
        }
        observation = {**context, "attempt_id": str(uuid.uuid4()), "state": "attempt_completed", "status_code": 200}
        result.append((native_error(index), context, observation))
    return result


def compare(enabled: bool, pressure: bool, trace_allocations: bool) -> dict[str, Any]:
    sdk = DebugBundleSdk(transport=lambda _request: TransportResponse(202))
    sdk.init(
        "synthetic",
        environment="development",
        batch_size=1001,
        flush_interval=3600,
        capture_exception_evidence=enabled,
        capture_recovery_observations=enabled,
        evidence_capabilities={"version": 1, "features": list(FEATURES)},
    )
    try:
        if pressure:
            for index in range(1000):
                sdk.capture_log(f"warning {index}", level="warning")
            assert len(sdk._buffer) == 1000, "warning queue was not filled"
        inputs = calls()
        gc.collect()
        if trace_allocations:
            tracemalloc.start()
        started = time.perf_counter_ns()
        for error, context, observation in inputs:
            sdk.capture_exception(error, context)
            sdk.capture_recovery(observation)
        elapsed = time.perf_counter_ns() - started
        retained_allocation, peak_allocation = tracemalloc.get_traced_memory() if trace_allocations else (None, None)
        if trace_allocations:
            tracemalloc.stop()
        failures = [event for event in sdk._buffer if event["event_type"] == "backend_exception"]
        assert len(failures) == len(inputs), "original errors were displaced"
        for event in failures:
            assert ("exception_evidence" in event["payload"]) == enabled
            if enabled:
                assert len(event["payload"]["exception_evidence"]["nodes"]) == 2
        diagnostics = sum(event["event_type"] == "diagnostic_context" for event in sdk._buffer)
        assert diagnostics == (len(inputs) if enabled and not pressure else 0)
        assert len(sdk._buffer) <= 1000 and sdk._buffer_bytes <= 8 * 1024 * 1024
        assert sdk._diagnostics.accounted_bytes <= 65536
        return {
            "python": sys.version.split()[0],
            "evidence": enabled,
            "pressure": pressure,
            "allocation_instrumentation": trace_allocations,
            "pairs": len(inputs),
            "capture_ns_per_pair": elapsed / len(inputs),
            "peak_traced_allocated_bytes": peak_allocation,
            "retained_traced_allocated_bytes": retained_allocation,
            "retained_count": len(sdk._buffer),
            "retained_wire_bytes": sdk._buffer_bytes,
            "diagnostic_bytes": sdk._diagnostics.accounted_bytes,
        }
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        sdk.dispose()


if __name__ == "__main__":
    compare(True, False, False)
    for repeat in range(1, 4):
        for pressure in (False, True):
            for enabled in (False, True):
                for trace in (False, True):
                    print(json.dumps({"repeat": repeat, **compare(enabled, pressure, trace)}), flush=True)
