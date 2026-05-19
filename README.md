# debugbundle-python

DebugBundle SDK for Python.

## Installation

```bash
pip install debugbundle-python
```

## Quick Start

```python
import debugbundle

debugbundle.init(project_token="dbundle_proj_test", service="checkout-api")
debugbundle.capture_exception(RuntimeError("boom"))
debugbundle.flush()
```

## Status

This repository currently contains the full Phase 18 Python SDK scope in eleven implementation slices: core SDK surface, buffering, redaction, duplicate suppression, probe buffering, vanilla runtime hooks, framework integrations for Django, Flask, and FastAPI, remote config polling and capture-policy enforcement, optional `structlog` and `loguru` auto-detection when `capture_logging()` is enabled, contract-aligned `EventEnvelope` emission for log, request, exception, suppression, and probe payloads, explicit public wrapper signatures and a validated buildable typed package artifact, real HTTP integration coverage against a lightweight mock ingestion server, vendored machine-readable schema validation for all event types the Python SDK currently emits, a standalone CI workflow that validates Ruff, mypy, pytest, and package builds for the Python 3.10+ support floor actually used by the package, an enforced per-file coverage gate that keeps every shipped Python SDK module at or above the required 80% minimum, request-local framework correlation binding so `X-DebugBundle-Trace-Id` flows through Django, Flask, and FastAPI into the emitted event correlation metadata for cross-context linking, full browser relay handler parity with Django/Flask/FastAPI helpers plus local-only and connected delivery modes, and safe backend runtime process facts on exception payloads without reading environment variables.

## Runtime Context

Backend exception events now include safe runtime process facts when the host exposes them, including:

- Python version
- platform
- architecture
- pid
- cwd
- uptime
- hostname
- thread id
- best-effort memory metadata

The SDK does not read or emit environment variables in this runtime block.

## Browser Relay

The Python SDK includes a framework-agnostic `BrowserRelayHandler` plus framework helpers for the contract-required `POST /debugbundle/browser` endpoint in Python servers that also load `@debugbundle/sdk-browser`.

- `create_django_relay_view()` returns a Django view for the relay route.
- `create_flask_relay_handler()` registers the relay route on a Flask app.
- `create_fastapi_relay_handler()` registers the relay route on a FastAPI app.

The relay handler enforces same-origin or configured allowed origins, requires `Content-Type: application/json`, accepts the canonical `batch` body shape only, rejects bodies larger than `256 KB`, applies per-IP rate limiting, accepts only supported browser event types, strips trust-sensitive headers and fields, forces `sdk_name` to `@debugbundle/sdk-browser`, and preserves browser correlation fields (`request_id`, `trace_id`, `session_id`, and `user_id_hash`) when they are strings or `null`.

Delivery behavior matches the shared relay contract across the shipped server SDKs:

- `project_mode="local-only"` writes accepted browser events to local event files for CLI processing.
- `project_mode="connected"` with the default `durable_write=True` writes a durable relay spool record and then forwards to the ingestion API with the server-side project token.
- `project_mode="connected"` with `durable_write=False` uses the lower-latency forward-only path.

## Docs

https://debugbundle.com/docs/sdks/python

## License

AGPL-3.0-only
