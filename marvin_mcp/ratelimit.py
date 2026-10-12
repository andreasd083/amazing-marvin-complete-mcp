"""Global rate limiter for Marvin's API.

Marvin's documented limits (help center, Getting started with the Marvin
API, read 2026-10-11): "No bursts faster than 1 request every 3 seconds",
bulk creation at 1 per second, 1440 calls per day on average, and every call
counts. Only creating calls (/addTask, /addProject, /addEvent, /doc/create)
use the short 1.1 s spacing; everything else - reads, /doc/update,
/markDone, deletions - waits 3.1 s. The spacing is set by the kind of the
call about to be made, counted from the previous call.

The limiter is global for the whole server process: a single asyncio-locked
queue is shared by every tool and session. The daily counter is persisted to
disk so it survives restarts, and rolls over at midnight in the configured
timezone (MARVIN_TIMEZONE, default: system local).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from .config import TIMEZONE

logger = logging.getLogger(__name__)

CALL_INTERVAL_S = 3.1  # "1 request every 3 seconds", with margin
CREATE_INTERVAL_S = 1.1  # bulk creation "1 per second", with margin
DAILY_LIMIT = 1440
MAX_QUEUE_WAIT_S = 120.0
# Marvin enforces the daily average (1440/day = "1 per minute") in a shorter,
# undocumented window as well: a 429 was observed 2026-08-30 after ~100 calls
# within one hour despite 3 s spacing. After a 429 ALL calls pause for this
# long (or Retry-After if the server sends a longer value).
COOLDOWN_S = 60.0


class DailyBudgetExceeded(Exception):
    pass


class QueueTimeout(Exception):
    pass


class RateLimiter:
    def __init__(self, state_file: Path | None = None) -> None:
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0  # monotonic; floor set by a 429 pause
        self._last_mono = float("-inf")  # monotonic, last released call
        self._cooldown_until = 0.0  # monotonic
        self._state_file = state_file
        self._count_date = self._today()
        self._count = 0
        self._last_call_at: float | None = None  # epoch seconds, last released call
        if state_file is not None:
            self._load_state()

    @staticmethod
    def _today() -> str:
        return datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            if data.get("date") == self._today():
                self._count = int(data.get("count", 0))
                self._count_date = data["date"]
            if isinstance(data.get("last_call_at"), (int, float)):
                self._last_call_at = float(data["last_call_at"])
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, OSError):
            logger.warning("Could not read rate-limit state, restarting at 0")

    def _save_state(self) -> None:
        if self._state_file is None:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps({"date": self._count_date, "count": self._count,
                            "last_call_at": self._last_call_at}),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Could not write rate-limit state")

    def _roll_day(self) -> None:
        today = self._today()
        if today != self._count_date:
            self._count_date = today
            self._count = 0

    @property
    def calls_today(self) -> int:
        # Read-only on purpose: mutating via _roll_day() here would race
        # with acquire() (which mutates under self._lock). If the day has
        # rolled over since the last acquire, today's count is simply 0.
        if self._today() != self._count_date:
            return 0
        return self._count

    @property
    def last_call_at(self) -> float | None:
        """Wall-clock time (epoch seconds) of the last call the limiter
        released, from any client of this server process."""
        return self._last_call_at

    def note_429(self, retry_after_s: float | None = None) -> float:
        """The server responded 429: pause all calls for COOLDOWN_S (or
        Retry-After if longer). Returns the pause length."""
        secs = max(COOLDOWN_S, retry_after_s or 0.0)
        until = time.monotonic() + secs
        self._cooldown_until = max(self._cooldown_until, until)
        self._next_allowed_at = max(self._next_allowed_at, self._cooldown_until)
        return secs

    @property
    def cooldown_remaining(self) -> float:
        """Seconds left of a 429 pause (0 when none is active)."""
        return max(0.0, self._cooldown_until - time.monotonic())

    async def acquire(self, *, is_create: bool) -> None:
        """Waits until the next call is allowed. Raises DailyBudgetExceeded
        when the daily budget is spent and QueueTimeout when the queue is
        too long."""
        deadline = time.monotonic() + MAX_QUEUE_WAIT_S
        async with self._lock:
            self._roll_day()
            if self._count >= DAILY_LIMIT:
                raise DailyBudgetExceeded(
                    f"The daily budget ({DAILY_LIMIT} calls) is spent; "
                    "it resets at midnight in the configured timezone."
                )
            # Spacing counts from the previous call and is set by THIS call's
            # kind: an update right after a create waits 3.1 s, not 1.1 s.
            interval = CREATE_INTERVAL_S if is_create else CALL_INTERVAL_S
            now = time.monotonic()
            wait = max(self._next_allowed_at, self._last_mono + interval) - now
            if now + max(wait, 0) > deadline:
                raise QueueTimeout(
                    f"The rate-limit queue is longer than {MAX_QUEUE_WAIT_S:.0f}s; "
                    "try again in a moment."
                )
            if wait > 0:
                logger.info("Rate limit: waiting %.1fs in queue", wait)
                await asyncio.sleep(wait)
            self._last_mono = time.monotonic()
            self._count += 1
            self._last_call_at = time.time()
            self._save_state()
