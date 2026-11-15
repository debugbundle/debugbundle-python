from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FakeResponse:
    status_code: int
    retry_after_ms: int | None = None
    body: object | None = None


class FakeTransport:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = responses or [FakeResponse(status_code=202)]
        self.calls: list[dict[str, object]] = []

    def __call__(self, request: dict[str, object]) -> FakeResponse:
        self.calls.append(request)
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


class ManualClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
