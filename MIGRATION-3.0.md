# Python 3.0 migration

Python 3.0 changes the timing and thread used by the optional `on_diagnostic`
callback. Keep the installed 2.x package available during your rollout.

## Optional diagnostic callbacks

Previously, the SDK invoked `on_diagnostic` inline and could hold an SDK lock
while calling application code. A slow or recursive callback could interrupt
capture or delivery. Diagnostics now run outside SDK locks on one bounded daemon
lane per client. The callback argument and synchronous return contract remain the
same; callbacks should return promptly and must not return a coroutine.

`flush()` drains event delivery and does not wait for optional diagnostic output.
Do not use diagnostic callback side effects as an event-delivery receipt or
assume the callback runs on the thread calling capture, logging, or `flush()`.
If your callback reads thread-local application state, capture the required
privacy-safe values in the callback closure instead. Synchronise shared state
with your application's own bounded queue or lock, outside SDK calls. Tests
that inspect callback output should use an application-owned completion signal
with a finite timeout rather than expecting output immediately after `flush()`.

The lane holds at most one pending diagnostic, admits at most ten per 60-second
window and sanitises each to at most 4 KiB of UTF-8 JSON, including configured
sensitive fields. Diagnostics are best effort and may be omitted. SDK capture
from the callback context is suppressed to prevent feedback. Reinitialisation
and disposal clear pending output; a callback already running may finish. A
stalled callback keeps its existing lane and never creates replacement workers.
Process shutdown can end daemon work without a final diagnostic callback.

## Event capture and delivery

Public capture methods, configuration defaults, hook timing, event schemas,
mandatory privacy, sampling and acknowledgement/retry decisions retain their
existing contracts. A valid hook replacement whose optional exception evidence
exceeds its combined 32 KiB budget sheds that optional evidence while preserving
the hook's primary fields and custom context. Malformed returns still retain the
protected original; unavailable ownership validation withholds the replacement.

Applications without `on_diagnostic` need no callback migration. Test your
framework and logging setup before updating the pinned package. The existing
[Python 2.0 migration](MIGRATION-2.0.md) still applies when upgrading from 1.x.
