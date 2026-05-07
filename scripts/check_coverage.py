from __future__ import annotations

import json
import sys
from pathlib import Path

MINIMUM_PERCENT = 80.0
PACKAGE_PREFIX = "src/debugbundle/"


def main() -> int:
    coverage_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("coverage.json")
    if not coverage_path.is_file():
        print(f"Coverage file not found: {coverage_path}")
        return 1

    coverage_payload = json.loads(coverage_path.read_text())
    files = coverage_payload.get("files")
    if not isinstance(files, dict):
        print(f"Coverage file is missing a files map: {coverage_path}")
        return 1

    offenders: list[tuple[str, float]] = []
    for file_path in sorted(files):
        if not file_path.startswith(PACKAGE_PREFIX):
            continue
        file_payload = files[file_path]
        if not isinstance(file_payload, dict):
            continue
        summary = file_payload.get("summary")
        if not isinstance(summary, dict):
            continue
        percent = summary.get("percent_covered")
        if not isinstance(percent, (int, float)):
            continue
        if float(percent) < MINIMUM_PERCENT:
            offenders.append((file_path, float(percent)))

    if offenders:
        print(f"Per-file coverage check failed. Minimum required: {MINIMUM_PERCENT:.0f}%")
        for file_path, percent in offenders:
            print(f"- {file_path}: {percent:.2f}%")
        return 1

    print(f"Per-file coverage check passed for {PACKAGE_PREFIX} at >= {MINIMUM_PERCENT:.0f}%.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())