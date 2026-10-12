"""Composite tools: steps with one retry and an account of what was done.

Marvin's server sporadically answers 500 on /doc/update and 429 under load.
A step that gets such an answer is retried once; if it fails again the
chain stops and the error says which steps were already completed.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .client import MarvinError, MarvinRateLimited
from .ratelimit import DailyBudgetExceeded, QueueTimeout

# Extra wait between cleaning up reminders and the deletion. Verified
# 2026-10-10: with 3.5 s between cleanup and deletion no notification fired.
# The deletion itself already waits the limiter's 3.1 s (CALL_INTERVAL_S),
# so 2.0 s extra gives >= 5.1 s; lower values are untested. Defined in one
# place.
DELETE_SYNC_PAUSE_S = 2.0


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, MarvinRateLimited):
        return True
    return isinstance(exc, MarvinError) and "responded 500" in str(exc)


class ChainError(MarvinError):
    def __init__(self, message: str, completed: list[str], failed: str, extra: dict | None = None):
        super().__init__(message)
        self.completed = list(completed)
        self.failed = failed
        self.extra = extra or {}

    def as_dict(self) -> dict:
        out: dict[str, Any] = {
            "error": str(self),
            "completed_steps": self.completed,
            "failed_step": self.failed,
        }
        out.update(self.extra)
        return out


class Chain:
    def __init__(self) -> None:
        self.completed: list[str] = []
        self.extra: dict[str, Any] = {}

    async def step(self, name: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        for attempt in (1, 2):
            try:
                result = await fn()
            except (MarvinError, DailyBudgetExceeded, QueueTimeout) as exc:
                if attempt == 1 and is_retryable(exc):
                    continue
                done = ", ".join(self.completed) or "none"
                raise ChainError(
                    f"Step '{name}' failed: {exc}. Completed steps: {done}.",
                    self.completed, name, self.extra,
                ) from None
            self.completed.append(name)
            return result

    async def pause(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)
