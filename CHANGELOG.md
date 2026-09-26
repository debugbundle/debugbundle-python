# Changelog

## [Unreleased]

## [2.0.1] - 2026-09-26

### Fixed

- Measure retry deadlines when the response arrives and honor service-failure hints without changing no-hint retry timing.
- Require a valid canonical acknowledgement from built-in HTTP delivery; retain the full batch and back off for missing or malformed responses. Preserve bodyless file and explicit custom transport compatibility.
- Cap transport retry hints at five minutes before scheduling retries, including custom transport results.
- Parse numeric and HTTP-date Retry-After safely, bounding before conversion and ignoring invalid or nonfinite hints.

## [2.0.0] - 2026-09-25

### Breaking changes

- Reject logs below the effective threshold before event construction and `before_send`; admitted hooks run on the delivery path after sampling and duplicate suppression. Review [the 2.0 migration guide](MIGRATION-2.0.md) before upgrading from 1.x.
- Reaching the batch size now schedules background transport. Pending events and suppression fingerprints have finite bounds; queue pressure may discard events and produces a bounded aggregate when delivery permits.
- Initial remote configuration is fetched asynchronously. Capture uses a restrictive local policy until the response arrives.
- A forked child discards inherited pending events, request context, probes, timers, and sender ownership before using the SDK; it keeps the effective capture policy and creates a fresh HTTP client.
- Full queues reject lower-priority logs and requests before context scanning. Exceptions, ERROR logs, and 5xx request incidents can displace pending lower-priority events; an all-error overload still drops promptly within the same finite budget.
- A pressure-summary event that cannot fit in a full queue no longer increments the application-event drop count; the aggregate is sent once capacity returns.

### Safety

- Read exception arguments, traceback, and cause through built-in descriptors, preserving original evidence without running blocking application metadata or metaclass accessors on capture callers.

- Keep transport calls outside the capture lock, use bounded retry ownership, and avoid application-defined exception and stdlib logging renderers in automatic capture.
- Run probe suppliers, mapping traversal, privacy protection, and probe-event preparation outside the shared capture lock; a slow probe can no longer make a concurrent exception lose admission. Recheck activation and SDK generation before committing buffered or standalone probe data.
- Finalize valid hook replacements incrementally against policy and byte ownership. Drop an over-budget replacement instead of restoring pre-hook application content; replacement event IDs cannot merge retained-size accounting.
- Cache bounded event byte sizes at admission and prepare post-hook sizes outside the capture lock. Queue eviction and acknowledgement/retry accounting no longer serialize retained payloads while holding that lock.
- Prepare suppression and queue-pressure summaries outside the capture lock; concurrent exceptions remain admissible while a summary is privacy-scanned. Reconfiguration rejects summaries from the previous SDK generation, and pressure drops that arrive during preparation remain counted for a later report.
- Schedule one later pressure-summary send after a full queue drains, even if no further application event arrives.

## [1.5.0] - 2026-09-21

### Security

- Enforce bounded mandatory telemetry protection before context and probe retention, after `before_send`, and before queued or connected delivery. Custom redaction remains additive to the baseline.

## [1.4.2] - 2026-09-16

### Fixed

- Preserve the processed structlog message and context when a final renderer consumes fields in place, including the default ConsoleRenderer. Native filtering and renderer behavior remain intact.

## [1.4.1] - 2026-09-15

### Fixed

- Capture structlog records only after native level and processor filtering. Preserve bound context, async logging, native exceptions and return values.
- Honor stdlib logger filters and disablement without duplicate capture; isolate capture callback failures and deactivate cached proxies on disposal.

## [1.4.0] - 2026-09-12

### Changed

- License first-party SDK code under Apache-2.0 and ship consistent package licensing metadata and license text.

## [1.3.0] - 2026-07-28

### Added

- Added the universal `before_send` event hook and canonical object wrapping for scalar/list probe values.

### Fixed

- Reconcile connected ingestion acknowledgements per event, retaining only retryable rejections and withholding delivery health when no event was accepted.

### Changed

- Verify the supported Python 3.10, 3.11, and 3.12 runtime lanes in CI.

## [1.2.0] - 2026-07-17

### Added
- Corrected the semantic release line for browser-relay analytics support. Relay handlers accept credential-free `analytics_event` envelopes while preserving only the required analytics correlation fields and stripping browser-supplied credentials.

## [1.1.3] - 2026-07-17

### Added
- Added browser-relay support for `analytics_event` envelopes, preserving only the analytics correlation fields needed for aggregation while continuing to strip browser-supplied credentials.

## [1.1.2] - 2026-06-19

### Fixed
- Release packaging quality gates so the published Python SDK patch can ship cleanly without changing runtime behavior.

## [1.1.1] - 2026-06-19

### Fixed
- Normalized canonical event-envelope emission so custom app context now stays in envelope `context`, request events avoid legacy payload extras, and installed projects stop tripping malformed ingestion rejects after upgrade.

## [1.1.0] - 2026-06-08

### Added
- Added path-scoped immediate client-error incident promotion support in remote capture-policy parsing so explicitly configured `4xx` routes can emit standalone `request_event` incident signals without widening the status globally.

### Changed
- Unpromoted client-error request telemetry now remains context-only under repeated traffic, while `5xx` handling and explicitly promoted client-error behavior are preserved.

## [1.0.0] - 2026-05-31

### Changed
- Declared the Python SDK stable at `1.0.0` after release-hardening the public package, browser relay, framework integrations, and registry smoke coverage.

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
