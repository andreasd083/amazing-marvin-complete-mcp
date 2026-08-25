"""Error-path tests: what a user actually sees when things go wrong.

Three classes: rate-limit exhaustion through the tool layer, network
errors (no leaked details), and midnight rollover during an ongoing queue.
"""

import time

import httpx
import pytest

from marvin_mcp import ratelimit, server
from marvin_mcp.ratelimit import DailyBudgetExceeded, RateLimiter

from .conftest import RecordingTransport


# ---- (a) Rate-limit errors through the tool layer ----


async def test_daily_budget_exceeded_reaches_tool_as_readable_error(
    init_server, transport
):
    server._limiter._count = ratelimit.DAILY_LIMIT
    result = await server.get_today_items.fn()
    assert "daily budget" in result["error"]
    assert "midnight" in result["error"]  # says when it resets
    assert transport.requests == []  # no API call spent


async def test_queue_timeout_reaches_tool_as_readable_error(init_server, transport):
    server._limiter._next_allowed_at = (
        time.monotonic() + ratelimit.MAX_QUEUE_WAIT_S + 60
    )
    result = await server.create_task.fn(title="Queued out")
    assert "queue" in result["error"]
    assert "try again" in result["error"]
    assert transport.requests == []


# ---- (b) Network errors: readable, nothing leaked ----


async def test_network_error_reaches_tool_without_details(settings):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to 10.0.0.1:443")

    server.init(settings, transport=RecordingTransport(handler=refuse))
    result = await server.get_today_items.fn()
    assert "Network error" in result["error"]
    assert "ConnectError" in result["error"]  # only the exception type name
    assert "10.0.0.1" not in result["error"]  # no underlying details


async def test_timeout_error_reaches_tool_without_details(settings):
    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    server.init(settings, transport=RecordingTransport(handler=hang))
    result = await server.create_task.fn(title="Never lands")
    assert "Network error" in result["error"]
    assert "ReadTimeout" in result["error"]


# ---- (c) Midnight rollover during an ongoing queue ----


async def test_midnight_rollover_resets_budget_mid_queue(monkeypatch):
    limiter = RateLimiter()
    day = ["2026-08-25"]
    monkeypatch.setattr(limiter, "_today", lambda: day[0])
    limiter._count_date = "2026-08-25"
    limiter._count = ratelimit.DAILY_LIMIT - 1

    await limiter.acquire(is_write=True)  # last allowed call of the old day
    assert limiter._count == ratelimit.DAILY_LIMIT
    with pytest.raises(DailyBudgetExceeded):
        await limiter.acquire(is_write=True)

    day[0] = "2026-08-26"  # midnight passes while callers are queued
    await limiter.acquire(is_write=True)  # now allowed again
    assert limiter._count == 1
    assert limiter._count_date == "2026-08-26"
