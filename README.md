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

## Configuration

| Option | Default | Purpose |
| --- | --- | --- |
| `project_token` | required | Write-only DebugBundle project token. |
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

## Safety Defaults

- SDK failures are caught internally and do not crash the host process.
- Sensitive fields are redacted before transport.
- Duplicate event storms are suppressed locally.
- Runtime context excludes environment variables.
- Browser relay requests cannot smuggle server-side credentials.

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
