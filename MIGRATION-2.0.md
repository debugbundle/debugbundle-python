# Python 2.0 migration

Version 2.0 changes capture and hook timing. Keep a pinned 1.x installation available during rollout.

## Capture and delivery

Logs below the effective SDK/server threshold now return before event construction, privacy traversal, and `before_send`. A hook can no longer promote a filtered INFO record. Admitted events are sampled and duplicate-suppressed before the hook runs. The hook runs on the background delivery timer when delivery is automatic, or in the explicit caller of `flush()`. It still receives a sanitized canonical event and may return a validated replacement or `None`. Invalid results and exceptions retain the safe original. A valid replacement is rechecked against capture policy and the shared byte budget; if it is no longer eligible or cannot fit, it is dropped, never replaced with pre-hook content. Do not rely on hook side effects before `capture_*` returns, and do not call `flush()` from request or logger paths.

Batch fullness schedules transport rather than invoking it inline. A single send is in flight per SDK instance. Pending events are capped at 1,000 and 8 MiB, including the active batch and final hook replacements; ERROR and exceptions can evict lower-priority pending events. An all-error overload can still drop events. Queue-pressure loss is summarized when the sender becomes available. Synchronous `flush()` remains an explicit lifecycle/testing drain and can wait for transport.

At a full queue, rejected logs and request events return before context privacy traversal. Exceptions, ERROR logs, and 5xx request incidents can displace pending lower-priority events, while an all-error queue fails admission promptly. 5xx request events keep normal event order when there is no pressure. These bounds protect the application; they do not guarantee lossless diagnostic capture during an outage.

Initial remote configuration now fetches in the background. Until it arrives, the SDK uses a restrictive local capture policy; a malformed/unavailable response leaves that fallback in place. Code that depends on a freshly fetched policy immediately after `init()` must wait for its own configuration readiness signal before emitting diagnostic test events. Normal application startup should not wait for DebugBundle's config endpoint.

After `fork()`, the child drops the parent's pending events, context, probe data, timers, and sender state before its first SDK operation. It preserves the effective capture restrictions and reopens the built-in HTTP transport in the child. Call `init()` in each child worker if remote configuration must resume there; automatic polling is not inherited. Custom transports and application callbacks must themselves be safe for use after a fork. Do not expect pre-fork buffered events to be delivered by child workers.

Probe suppliers still run synchronously in the caller that invokes `probe()`, but no longer hold the SDK-wide capture lock. Keep application-supplied probe callbacks short; another thread can continue capturing exceptions while one supplier is slow.

The stdlib logging adapter avoids application-defined argument stringification. Common `%s`, `%d`, `%i`, and `%f` placeholders with simple primitive arguments remain readable. Unsupported argument objects are represented by a fixed placeholder. Exception messages and stacks use direct built-in exception descriptors and bounded traceback frames without calling custom metadata properties, instance/metaclass attribute accessors, `__str__`, or reading source files. Original throw-site frames and cause chains remain available; application overrides of exception metadata are intentionally ignored. Review any code that expected custom rendering inside captured diagnostics.

The queue is intentionally lossy under pressure; it is not an audit trail. Keep the installed 1.x package available during migration, test all framework/logger hooks and privacy policy in a staging application, and use the server's existing ingestion compatibility for old and new event envelopes.
