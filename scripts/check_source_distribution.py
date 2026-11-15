"""Keep the shipped source test suite runnable after functional test splits."""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
archive = Path(sys.argv[1])
with tarfile.open(archive) as source:
    names = set(source.getnames())
prefix = archive.name.removesuffix(".tar.gz")
required = [
    path.relative_to(root).as_posix()
    for path in (root / "tests").rglob("*")
    if path.is_file() and path.suffix in {".py", ".json", ".sha256"}
]
required.extend(["MIGRATION-2.0.md", "MIGRATION-3.0.md"])
missing = [name for name in required if f"{prefix}/{name}" not in names]
if missing:
    raise SystemExit("Source distribution is missing test resources: " + ", ".join(missing))
print(f"Source distribution includes all {len(required)} test modules and fixture resources.")
