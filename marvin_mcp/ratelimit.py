"""Global rate limiter for Marvin's API.

Marvin's documented limits (MarvinAPI wiki, Home):
  - max 1 creating call per second
  - max 1 query per 3 seconds
  - max 1440 calls per day

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

READ_INTERVAL_S = 3.1  # "1 query / 3 s", with margin
WRITE_INTERVAL_S = 1.1  # "1 create / 1 s", with margin
DAILY_LIMIT = 1440
MAX_QUEUE_WAIT_S = 120.0


class DailyBudgetExceeded(Exception):
    pass


class QueueTimeout(Exception):
    pass


class RateLimiter:
    def __init__(self, state_file: Path | None = None) -> None:
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0  # monotonic
        self._state_file = state_file
        self._count_date = self._today()
        self._count = 0
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
                json.dumps({"date": self._count_date, "count": self._count}),
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

    async def acquire(self, *, is_write: bool) -> None:
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
            now = time.monotonic()
            wait = self._next_allowed_at - now
            if now + max(wait, 0) > deadline:
                raise QueueTimeout(
                    f"The rate-limit queue is longer than {MAX_QUEUE_WAIT_S:.0f}s; "
                    "try again in a moment."
                )
            if wait > 0:
                logger.info("Rate limit: waiting %.1fs in queue", wait)
                await asyncio.sleep(wait)
            interval = WRITE_INTERVAL_S if is_write else READ_INTERVAL_S
            self._next_allowed_at = time.monotonic() + interval
            self._count += 1
            self._save_state()
