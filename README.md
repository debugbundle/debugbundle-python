# debugbundle-python

Python SDK for DebugBundle.

![PyPI](https://img.shields.io/pypi/v/debugbundle-python?label=pypi)
![CI](https://img.shields.io/github/actions/workflow/status/debugbundle/debugbundle-python/ci.yml?branch=main&label=ci)
![License](https://img.shields.io/badge/license-AGPL--3.0--only-blue)

Use this package to capture Python backend exceptions, request metadata, structured logs, runtime context, and probe data. It supports vanilla Python plus Django, Flask, FastAPI, Python logging, structlog, loguru, and browser relay helpers.

Requires Python 3.10 or newer.

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
| Python runtime | 3.10 | 3.12 | 3.10 and 3.11 remain supported for installed-base coverage | 3.12 | 3.9 and older |
| Django | 5.x | latest 5.x patch | 5.x compatibility support | repo release smoke and tests install Django 5.x | Django 4.x and older |
| Flask | 3.x | latest 3.x patch | 3.x compatibility support | repo release smoke installs Flask 3.x | Flask 2.x and older |
| FastAPI | 0.115+ | latest 0.115+ patch line | 0.115+ compatibility support | repo tests install FastAPI 0.115+ | standalone Starlette, older FastAPI lines |

Post-V1 planned expansions from `spec/sdk-language-targets.md` remain out of scope here: Celery, RQ, Dramatiq, standalone Starlette, Gunicorn/Uvicorn server hooks, and AWS Lambda Python.

## Dependency Alignment

`debugbundle-python` ships as one package in V1, so there is no multi-package version-alignment step like a BOM or plugin family lock.

- Pin one `debugbundle-python` version across your service and worker repos when you want identical SDK behavior everywhere.
- Keep framework dependencies inside the supported lanes above: Django 5.x, Flask 3.x, and FastAPI 0.115+.
- The packaged HTTP client dependency is `httpx>=0.27,<0.29`; if you override transport behavior in tests or wrappers, stay inside that range unless you retest the SDK.

## Safety Defaults

- SDK failures are caught internally and do not crash the host process.
- Sensitive fields are redacted before transport.
- Duplicate event storms are suppressed locally.
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

AGPL-3.0-only. See `LICENSE`.
