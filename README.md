# debugbundle-python

Python SDK for DebugBundle.

After `make install-docker`, `make evidence-performance` compares identical
exception/recovery calls with evidence disabled and enabled, including a full
warning queue, with networking disabled. It reports capture timing separately from
Python `tracemalloc` peak/live allocation measurements, verifies native causes and
original-error priority, and checks existing retention ceilings. Setup and cleanup
are excluded; traced allocations are Python-managed memory, not cumulative churn
or total process RSS.

![PyPI](https://img.shields.io/pypi/v/debugbundle-python?label=pypi)
![CI](https://img.shields.io/github/actions/workflow/status/debugbundle/debugbundle-python/ci.yml?branch=main&label=ci)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)

Use this package to capture Python backend exceptions, request metadata, structured logs, runtime context, and probe data. It supports vanilla Python plus Django, Flask, FastAPI, Python logging, structlog, loguru, and browser relay helpers.

Requires Python 3.10 or newer.

## Evidence candidate (unreleased)

The source candidate adds `capture_exception_evidence=True` and
`capture_recovery_observations=False` installation options. Collection requires
verified delivery/reader support, discovered on existing config/ACK traffic with
`debugbundle_evidence=1`. No capture call performs discovery or adds polling.
`evidence_capabilities` is an optional descriptor pin asserting the **entire**
path, including local readers, relays and wrappers; later discovery can revoke it.
Without proof, exception events retain their legacy shape. These additions are
not available in the installed 2.0.1 release.

Native cause, implicit context and Python 3.11+ exception-group evidence excludes
locals, source excerpts, arbitrary renderers and application properties. Python
3.10 has no native exception groups. Graphs retain at most eight nodes, depth
four, five children and 32 frames per node; all optional exception additions fit
32 KiB. Uncaught native capture bypasses `before_send` and uses two nodes, eight
frames and a 4-KiB total including execution/reference metadata. The original
sanitised stack remains separate.

`with sdk.operation_context({"kind": "job", "name": "invoice.calculate",
"limit_ms": 5000}):` supplies a fresh operation scope and monotonic elapsed time
to synchronous or asynchronous work inside the block. Kinds are `request`,
`command`, `job` and `unknown`. The limit is a known configured limit, not time
remaining. Framework requests use their existing begin/end lifecycle. Completed
owners revoke copied async context; reinitialisation/fork discard prior ownership.
Application results and exceptions pass through the context manager unchanged.

The existing background sender remains best-effort at immediate process exit.
The uncaught hook collects bounded native evidence without waiting for transport;
applications needing a delivery attempt use explicit `flush()` in a controlled
shutdown window. This candidate does not add default crash-file persistence or
synchronous capture transport.

Pass a fresh UUID v4 `failure_ref` in exception/log/request context. A valid
`evidence_scope` can specify a fresh page or operation UUID; an active operation
provides the default. `capture_recovery({"failure_ref": failure_ref,
"attempt_id": attempt_id, "state": "attempt_completed"})` records an explicit
application observation. Optional fields are `evidence_scope`, technical
`operation`, `status_code`, `duration_ms` and primitive `correlation` metadata.
Page scopes require a nonblank session ID, including the valid string `"0"`.
Only explicit `recovery_observed` claims semantic recovery; HTTP 200 and
`attempt_completed` leave it unknown. The other states are `attempt_started`,
`attempt_failed` and `failure_persisted`.

Standalone recovery requires its local opt-in, `recovery_v1` support and server
`capture_diagnostic_context`; minimal policy disables it. Privacy, sampling,
resulting hook-event policy and queue limits still apply. Fifty failure windows,
five attempts and twenty observations per failure share 64 KiB of tracker and
pending-payload ownership, retained until ACK/drop even offline. Canonical hooks
may change any allowed identity/type field; ownership follows the replacement.
The complete closed envelope is checked against the vendored accepting Draft 7
schema, followed by evidence semantics and mandatory privacy. Only native JSON
containers/scalars are copied; custom serializers and container subclasses are
never invoked by hook validation. Invalid or throwing hooks keep the protected
original. Exhausted validation or an unfit valid replacement is withheld, so an
application redaction cannot be undone by a budget fallback.

Native HTTP requests select an ordered prefix by the actual compact UTF-8 JSON
serializer and remain within the receiving 256-KiB limit. Unsent finalized tails
stay charged and continue through the existing sender without repeating their
hooks. Optional SDK evidence is reduced before withholding an unfit primary;
explicit custom/local transports retain their established budgets. Browser
relays measure after server identity/token expansion, split downstream requests
and keep incomplete spools. A legacy custom success remains accepted but cannot
mark a durable spool delivered; that requires complete canonical acknowledgements.
Relay chunks share a five-second monotonic forwarding deadline, and spool markers
are created exclusively with private permissions without following symlinks.

Native HTTP ACK reads stop at 256 KiB and close the response. Requests ask for
identity encoding; unexpected compressed ACKs are withheld. The five-second
deadline is checked between streamed reads; HTTPX's synchronous per-read/connect/
write timeouts remain advisory and an in-progress native read can overrun that
deadline. Explicit custom transports/config fetchers retain their own timeout,
allocation and cancellation responsibilities. No additional timer thread or
inline capture transport is introduced.

Browser relays and Django/Flask/FastAPI relay factories accept the same optional
`evidence_capabilities` pin. They preserve closed DTOs, protected application
context and server-owned credentials, and advertise only verified downstream
support. Opted-in replies keep string `errors` and add indexed `evidence_errors`;
partial batches return 202, while legacy partial responses retain 400. A durable
spool accepts locally but is marked delivered only after a valid complete ACK.
Malformed ACKs cannot grant proof. Only negotiated `invalid_event` rejection of
an original exception can retry its stripped legacy form once; standalone
context, policy and authentication rejections do not use this fallback.
Framework relay adapters read at most the configured body limit plus one byte
before parsing, and preserve the negotiation query/header through the handler.

Version 2.0.0 protects capture callers with bounded, priority-aware background delivery. Review the [2.0 migration guide](MIGRATION-2.0.md) before upgrading from 1.x.

## Automatic capture and application filtering

`capture_logging()` attaches the stdlib logging handler and detects installed structlog/loguru integrations. Obtain structlog loggers after attachment. Structlog level filters and `DropEvent` processors run before capture; stdlib-backed structlog loggers also honor logger filters, disabled state and `logging.disable()`. Bound context and async logging are supported. SDK disposal stops capture through cached proxies. DebugBundle has its own minimum capture level; destination-specific filters on other handlers or Loguru sinks do not configure the DebugBundle handler.

## Installation

```bash
pip install debugbundle-python
```

Install the SDK alongside the framework you actually run:

```bash
pip install debugbundle-python django
pip install debugbundle-python flask
pip install debugbundle-python fastapi uvicorn
```

For local development:

```bash
pip install -e ".[dev]"
```

## Quick Start

```python
import os
import debugbundle

debugbundle.init(
    project_token=os.environ["DEBUGBUNDLE_PROJECT_TOKEN"],
    service="checkout-api",
    environment="production",
)

debugbundle.capture_exceptions()
debugbundle.capture_logging()
```

Capture handled errors, logs, messages, and probes explicitly:

```python
debugbundle.capture_exception(error)
debugbundle.capture_log("payment retry failed", level="warning", context={"order_id": order_id})
debugbundle.capture_message("worker started")
debugbundle.probe("checkout.cart", {"item_count": len(cart.items)})

# Use only during controlled teardown or tests, never on a request path.
debugbundle.flush()
```

## Framework Integrations

| Framework | Integration |
| --- | --- |
| Django | `DebugBundleDjangoMiddleware` |
| Flask | `instrument_flask(app)` |
| FastAPI | `DebugBundleFastAPIMiddleware` or `instrument_fastapi(app)` |
| Python logging | `capture_logging()` |
| asyncio | `capture_async()` |
| structlog/loguru | Auto-detected when log capture is enabled and the libraries are installed |

## Browser Relay

Python backends can host the browser relay endpoint used by `@debugbundle/sdk-browser`.

| Framework | Helper |
| --- | --- |
| Django | `create_django_relay_view()` |
| Flask | `create_flask_relay_handler()` |
| FastAPI | `create_fastapi_relay_handler()` |

The relay validates JSON batches, enforces same-origin or allowed origins, strips trust-sensitive browser fields, keeps the server-side project token private, and supports both local-only file writes and connected forwarding.

Relay defaults and limits:

- Same-origin requests are allowed by default when `allowed_origins` is omitted.
- Split frontend/backend deployments should set explicit `allowed_origins` values.
- Relay requests must use `Content-Type: application/json` and stay below `max_body_bytes` (default `262144`).
- Relay rate limiting defaults to `60` requests per IP per minute.
- `project_mode="local-only"` writes accepted browser events to `.debugbundle/local/events` or your configured `local_events_dir`.
- `project_mode="connected"` writes durable spool files by default and forwards with the server-side `project_token` only.
- Leaving relay `project_mode` unset disables local writes and forwarding; accepted batches are only surfaced through `on_accept`.
- Connected relay mode without a usable `project_token` keeps accepting and optionally spooling events, but forwarding remains disabled until the server provides credentials.

## Configuration Reference

Configuration sources and precedence:

- The SDK only reads the keyword arguments passed to `debugbundle.init(...)`.
- Environment variables, Django settings, Flask config, or FastAPI settings are convenience sources that your application maps into `debugbundle.init(...)`; the SDK does not read them directly.
- Explicit `debugbundle.init(...)` arguments always win because they are the only configuration source the runtime consumes.
- Capture-policy fields are server-owned and are not accepted in local SDK config. The SDK learns capture policy through `GET /v1/sdk/config` and applies it locally before transport.

| Option | Default | Purpose |
| --- | --- | --- |
| `project_token` | required for connected capture | Write-only DebugBundle project token. Blank or missing tokens disable connected capture and leave the SDK status at `disconnected`. |
| `service` | auto/default service | Service name shown on incidents and bundles. |
| `environment` | `development` | Runtime environment such as `production`, `staging`, or `development`. |
| `endpoint` | `https://api.debugbundle.com/v1/events` | Ingestion endpoint for connected mode or self-hosting. |
| `enabled` | `True` | Disable all capture without removing instrumentation. |
| `log_level` | `warning` | Minimum captured log severity. |
| `sample_rate` | `1.0` | Fraction of events to keep before transport. |
| `batch_size` | `25` | Events per batch before flushing. |
| `flush_interval` | `5.0` | Flush interval in seconds. |
| `redact_fields` | common sensitive fields | Additional field names to redact. |
| `max_probe_labels` | `50` | Maximum distinct probe labels buffered in memory. |
| `max_probe_entries_per_label` | `10` | Maximum entries retained per probe label. |
| `probe_flush_on_error` | `True` | Attach buffered probe data to captured exceptions. |
| `probes_poll_interval` | `60000` | Remote probe config poll interval in milliseconds. |
| `fetch_impl` | internal HTTP fetch | Custom remote-config fetch function for tests or advanced routing. |
| `on_diagnostic` | none | Callback for SDK internal diagnostics. |

Framework-native wiring:

- Django: initialize the SDK during startup, then add `DebugBundleDjangoMiddleware` to `MIDDLEWARE`.
- Flask: initialize the SDK during app creation, then call `instrument_flask(app)`.
- FastAPI: initialize the SDK during startup, then add `DebugBundleFastAPIMiddleware` or call `instrument_fastapi(app)`.
- There is no separate package-manager plugin, settings loader, or framework-only config surface in V1.

## Install Examples by Mode

Vanilla Python:

```python
import os
import debugbundle

debugbundle.init(
    project_token=os.environ["DEBUGBUNDLE_PROJECT_TOKEN"],
    service="worker",
    environment="production",
)

debugbundle.capture_exceptions()
debugbundle.capture_logging()
```

Flask:

```python
import os
from flask import Flask
import debugbundle

app = Flask(__name__)
debugbundle.init(
    project_token=os.environ["DEBUGBUNDLE_PROJECT_TOKEN"],
    service="checkout-api",
    environment="production",
)
debugbundle.instrument_flask(app)
```

FastAPI:

```python
import os
from fastapi import FastAPI
import debugbundle

app = FastAPI()
debugbundle.init(
    project_token=os.environ["DEBUGBUNDLE_PROJECT_TOKEN"],
    service="checkout-api",
    environment="production",
)
debugbundle.instrument_fastapi(app)
```

Logger integration:

```python
import logging
import debugbundle

debugbundle.capture_logging(logging.getLogger("checkout"))
```

Connected browser relay:

```python
from flask import Flask
import debugbundle

app = Flask(__name__)
debugbundle.create_flask_relay_handler(
    allowed_origins=["https://app.example.com"],
    project_mode="connected",
    project_token="dbundle_proj_...",
    endpoint="https://api.debugbundle.com/v1/events",
)(app)
```

Local-only browser relay:

```python
from flask import Flask
import debugbundle

app = Flask(__name__)
debugbundle.create_flask_relay_handler(
    allowed_origins=["http://localhost:3000"],
    project_mode="local-only",
    local_events_dir=".debugbundle/local/events",
)(app)
```

There is no zero-install fallback for the Python SDK itself in V1. The nearest low-friction path is the browser relay mounted on an existing Python web app.

## Runtime and Framework Support

| Surface | Minimum compatibility version | Recommended production version | Installed-base compatibility lane | Rolling CI lane | Out of scope |
| --- | --- | --- | --- | --- | --- |
| Python runtime | 3.10 | 3.14 | 3.10 through 3.13 remain supported for installed-base coverage | 3.10 through 3.14 | 3.9 and older |
| Django | 5.x | latest 6.1 patch | 5.x compatibility support | Python 3.14 suite exercises Django 6.1 | Django 4.x and older |
| Flask | 3.x | latest 3.x patch | 3.x compatibility support | repo release smoke installs Flask 3.x | Flask 2.x and older |
| FastAPI | 0.115+ | latest 0.115+ patch line | 0.115+ compatibility support | repo tests install FastAPI 0.115+ | standalone Starlette, older FastAPI lines |

Post-V1 planned expansions from `spec/sdk-language-targets.md` remain out of scope here: Celery, RQ, Dramatiq, standalone Starlette, Gunicorn/Uvicorn server hooks, and AWS Lambda Python.

## Dependency Alignment

`debugbundle-python` ships as one package in V1, so there is no multi-package version-alignment step like a BOM or plugin family lock.

- Pin one `debugbundle-python` version across your service and worker repos when you want identical SDK behavior everywhere.
- Keep framework dependencies inside the supported lanes above: Django 5.x or 6.x, Flask 3.x, and FastAPI 0.115+.
- The packaged HTTP client dependency is `httpx>=0.27,<0.29`; if you override transport behavior in tests or wrappers, stay inside that range unless you retest the SDK.

## Safety Defaults

- SDK failures are caught internally and do not crash the host process.
- A forked worker starts with a fresh SDK queue, lock, context and built-in HTTP client. Initialize the SDK in each child when remote configuration must poll there; custom transports must be fork-safe.
- Sensitive fields are redacted before transport.
- Duplicate event storms are suppressed locally.
- Full-queue capture sheds warning/request noise before context scans. Exceptions, ERROR logs, and 5xx request incidents have priority over pending lower-priority events; all-error overloads are bounded and summarized.
- Runtime context excludes environment variables.
- Browser relay requests cannot smuggle server-side credentials.

## Service Naming

- Use one stable backend service name per deployable, such as `checkout-api`, `billing-worker`, or `admin-api`.
- Keep browser relay traffic on the browser-owned service name by default, for example `checkout-web`; the Python relay preserves the browser service unless you explicitly override `service=` or `environment=` on the relay helper.
- When multiple Python deployables share one DebugBundle project, give each deployable its own `service` value instead of reusing one generic name.
- Reuse the same environment label across related surfaces, for example `production` on both `checkout-web` and `checkout-api`, so incident and bundle correlation stays readable.

## Safe Startup and Status

- The SDK never crashes the host process when configuration is invalid, transport calls fail, or remote config responses are malformed.
- `debugbundle.init(project_token="")` or any missing/blank connected token leaves capture disabled and `debugbundle.get_status()` returns `disconnected`.
- Rate-limited transports move the status to `degraded` until the retry window expires.
- Three consecutive transport failures also move the status to `disconnected` until a later successful flush.
- `debugbundle.get_last_event_at()` returns the Unix timestamp of the last successful delivery, or `None` before the first success.

## First-Event Verification

Use the repo-local smoke target to prove a fresh install end to end against a mock ingestion endpoint:

```bash
make smoke
```

That command builds the wheel, installs it into a fresh virtualenv, runs a Flask app that emits an application-owned `capture_message()` event, sends a browser relay batch through `/debugbundle/browser`, validates the emitted Python events against the SDK event-envelope fixture, and confirms both paths reach the mock ingestion endpoint with the expected `service`, `environment`, SDK metadata, and correlation fields.

For a manual verification snippet inside your own app:

```python
import os
import debugbundle

debugbundle.init(
    project_token=os.environ["DEBUGBUNDLE_PROJECT_TOKEN"],
    service="checkout-api",
    environment="staging",
)

debugbundle.capture_message("debugbundle first-event verification", level="error")
debugbundle.flush()
print(debugbundle.get_status(), debugbundle.get_last_event_at())
```

## Development

```bash
pip install -e ".[dev]"
ruff check .
mypy src
pytest
python -m build
```

CI validates Ruff, mypy, pytest, package build, event schema fixtures, and coverage gates.

## Documentation

- Python SDK docs: <https://debugbundle.com/docs/sdks/python>
- SDK overview: <https://debugbundle.com/docs/sdks>
- Browser relay: <https://debugbundle.com/docs/sdks/browser-relay>
- Repository: <https://github.com/debugbundle/debugbundle-python>

## License

Apache-2.0. See `LICENSE`.

## Delivery acknowledgement and retry limits

The built-in HTTP transport requires the canonical ingestion acknowledgement (`accepted`, `rejected`, and `errors`). Empty responses, unrelated JSON, malformed counts, or invalid rejection indices retain the full batch with backoff. Valid acknowledgements remove accepted and terminally rejected events and retry only the indexed retryable rejections; an all-rejected batch does not advance `lastEventAt`.

File transports and explicitly supplied custom/legacy transports retain their documented bodyless success fallback. A custom transport that returns acknowledgement fields must return the complete canonical shape. Retry hints (delay seconds or HTTP dates) are bounded to five minutes and measured from response receipt, including partial/protocol acknowledgements and retryable server failures. Without a server hint, existing retry timing is preserved. Failures remain contained within the SDK's existing delivery path.

## Probe checkpoint guidance

Application instrumentation supplies probe labels and bounded data. Deploy a new
checkpoint before activating it. Remote activation affects future matching calls
when policy permits; it does not inject code or recover unretained measurements.
Built-in runtime/error/device collectors keep their documented SDK settings.

Use fixed labels and allowlisted technical states: `media.preview.checkpoint`
(stage/input kind/attempt), `auth.session.checkpoint` (observed session presence
and result), `job.dependency.checkpoint` (named dependency/attempt/result), or
`cache.preview.checkpoint` (hit/miss/coarse age bucket). Omit customer identifiers,
tokens/cookies, cache keys, URLs and raw payloads. HTTP 200 or attempt completion
alone does not establish semantic recovery.

Empty probe data means no matching retained observation for the selected sample
window. It does not confirm activation delivery to every SDK or execution of a
label. Check the deployed SDK/wrapper version, instrumentation, scope, expiry,
policy, sampling and delivery receipts. See the [probe recipes](https://debugbundle.com/docs/probes#bounded-checkpoint-recipes).
