from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_standalone_repository_includes_required_governance_files() -> None:
    for relative_path in [
        "README.md",
        "LICENSE",
        "CHANGELOG.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
    ]:
        assert (REPO_ROOT / relative_path).is_file()


def test_pyproject_points_to_standalone_repository_urls() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'Repository = "https://github.com/debugbundle/debugbundle-python"' in pyproject
    assert 'Issues = "https://github.com/debugbundle/debugbundle-python/issues"' in pyproject


def test_standalone_changelog_and_security_policy_are_launch_ready() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "## [Unreleased]" in changelog
    assert (
        "## [1.2.0] - 2026-07-17\n\n"
        "### Added\n"
        "- Corrected the semantic release line for browser-relay analytics support. Relay handlers accept "
        "credential-free `analytics_event` envelopes while preserving only the required analytics correlation "
        "fields and stripping browser-supplied credentials.\n\n"
        "## [1.1.3] - 2026-07-17\n\n"
        "### Added\n"
        "- Added browser-relay support for `analytics_event` envelopes, preserving only the "
        "analytics correlation fields needed for aggregation while continuing to strip "
        "browser-supplied credentials.\n\n"
        "## [1.1.2] - 2026-06-19\n\n"
        "### Fixed\n"
        "- Release packaging quality gates so the published Python SDK patch can ship cleanly "
        "without changing runtime behavior.\n\n"
        "## [1.1.1] - 2026-06-19\n\n"
        "### Fixed\n"
        "- Normalized canonical event-envelope emission so custom app context now stays in envelope `context`, "
        "request events avoid legacy payload extras, and installed projects stop tripping malformed ingestion rejects "
        "after upgrade.\n\n"
        "## [1.1.0] - 2026-06-08" in changelog
    )
    assert "https://github.com/debugbundle/debugbundle-python/security/advisories/new" in security


def test_standalone_ci_workflow_covers_python_sdk_checks() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "actions/setup-python@v6" in workflow
    assert 'python-version: "3.12"' in workflow
    assert "python -m pip install -e .[dev]" in workflow
    assert "ruff check src tests" in workflow
    assert "mypy src" in workflow
    assert "pytest --cov=src/debugbundle --cov-report=term-missing --cov-report=json:coverage.json -q" in workflow
    assert "python scripts/check_coverage.py coverage.json" in workflow
    assert "python -m build" in workflow


def test_release_workflow_covers_python_package_publication() -> None:
    workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'tags:\n      - "v*"' in workflow
    assert "python -m build" in workflow
    assert "python -m twine check dist/*" in workflow
    assert "python smoke/run_app_driven_smoke.py --wheel dist/debugbundle_python-" in workflow
    assert "twine upload dist/*" in workflow
