# Changelog

## [Unreleased]

## [0.1.9] - 2026-05-29

### Fixed
- Added `OPTIONS /debugbundle/browser` preflight handling plus matching CORS headers for explicitly allowed split-host browser relay traffic across the Django, Flask, and FastAPI relay helpers.

## [0.1.6] - 2026-05-19

### Added
- Remote capture-policy parsing now honors `immediate_client_error_statuses` so configured `4xx` responses are emitted as immediate `request_event` incident signals even when generic request capture is disabled.
- Full browser relay delivery parity, including local-only event-file writes, connected durable spool writes, connected cloud forwarding with server-side project credentials, Django/Flask/FastAPI helper coverage, and shared relay compliance fixtures.

## [0.1.2] - 2026-05-12

### Added
- Safe backend runtime process facts on `backend_exception.payload.runtime`, including platform, architecture, pid, cwd, uptime, hostname, thread id, and best-effort memory metadata without reading environment variables.

## [0.1.1] - 2026-05-11

### Changed
- Aligned Python SDK capture-policy fallback defaults with the service presets so minimal and balanced modes capture 5xx request failures by default.

### Fixed
- Preserved 5xx request-event capture even when standalone request capture is otherwise disabled.
- Python browser relay validation now accepts browser-originated `request_event` payloads for promoted 5xx request failures.

## [0.1.0] - 2026-05-07

### Added
- Initial Python SDK foundation with the universal SDK interface, buffered transport, redaction, duplicate suppression, probe buffering, and vanilla Python hooks.
- Django middleware, Flask request/error hooks, and FastAPI middleware integrations with auto-registered stdlib logging capture.
- Remote config parsing and ETag refresh handling for `GET /v1/sdk/config`, remote heavy-probe activation, and capture-policy enforcement for logs and standalone request events.
- Optional logger auto-detection for `structlog` and `loguru` during `capture_logging()`, plus concurrent request-capture coverage to lock in thread-safe event buffering.
- Contract-aligned `EventEnvelope` emission across the Python SDK core, including schema/version identifiers, service metadata objects, normalized log/request/exception payloads, and inline probe timestamps that match the shared contract.
- Explicit module-level wrapper signatures for the public singleton API, `py.typed` inclusion in the package tree, and Docker-validated sdist/wheel builds for the publishable artifact.
- Real HTTP integration coverage using a lightweight mock ingestion server to validate end-to-end event POSTs and `Retry-After` parsing without relying on the full DebugBundle stack.
- Vendored JSON Schema validation coverage for emitted Python SDK events, locking backend_exception, request_event, log_event, error_suppressed, and probe_event payloads against a machine-readable contract fixture.
- Standalone CI scaffolding with Ruff, mypy, pytest, and `python -m build`, plus a corrected declared Python support floor of 3.10+ to match the syntax and type-hinting used by the package.
- Per-file coverage enforcement for the standalone SDK workflow, including new wrapper/logger tests that lift package-root and logger helper coverage above the required 80% floor, plus a case-insensitive logger level normalizer so optional integrations map alias levels consistently.
- Request-local framework correlation propagation via `ContextVar`-backed scoped context binding, so Flask, FastAPI, and Django now read `X-DebugBundle-Trace-Id` (and request-id fallbacks) from incoming request headers and attach the resulting correlation metadata to request, log, and exception events emitted during that request.
