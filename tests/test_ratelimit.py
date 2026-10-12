import asyncio
import json
import time

import pytest

from marvin_mcp import ratelimit
from marvin_mcp.ratelimit import DailyBudgetExceeded, RateLimiter


async def test_spacing_enforced_between_calls(monkeypatch):
    monkeypatch.setattr(ratelimit, "CALL_INTERVAL_S", 0.1)
    limiter = RateLimiter()
    t0 = time.monotonic()
    await limiter.acquire(is_create=False)
    await limiter.acquire(is_create=False)
    await limiter.acquire(is_create=False)
    elapsed = time.monotonic() - t0
    # Two waits of 0.1s between three calls
    assert elapsed >= 0.2


async def test_creates_have_shorter_interval(monkeypatch):
    monkeypatch.setattr(ratelimit, "CALL_INTERVAL_S", 0.5)
    monkeypatch.setattr(ratelimit, "CREATE_INTERVAL_S", 0.05)
    limiter = RateLimiter()
    await limiter.acquire(is_create=True)
    t0 = time.monotonic()
    await limiter.acquire(is_create=True)
    assert time.monotonic() - t0 < 0.3


async def test_daily_budget_exhausted():
    limiter = RateLimiter()
    limiter._count = ratelimit.DAILY_LIMIT
    with pytest.raises(DailyBudgetExceeded):
        await limiter.acquire(is_create=False)


async def test_counter_persisted_and_reloaded(tmp_path):
    state = tmp_path / "state.json"
    limiter = RateLimiter(state_file=state)
    await limiter.acquire(is_create=True)
    await limiter.acquire(is_create=True)
    assert json.loads(state.read_text())["count"] == 2

    reloaded = RateLimiter(state_file=state)
    assert reloaded.calls_today == 2


async def test_stale_state_from_other_day_ignored(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"date": "2000-01-01", "count": 999}))
    limiter = RateLimiter(state_file=state)
    assert limiter.calls_today == 0


async def test_calls_today_is_readonly_across_rollover(monkeypatch):
    """calls_today must not mutate shared state (that would race with
    acquire, which mutates under the lock) - it reports 0 after a day
    rollover and leaves the actual reset to the next acquire."""
    limiter = RateLimiter()
    await limiter.acquire(is_create=True)
    assert limiter.calls_today == 1
    limiter._count_date = "2000-01-01"  # simulate: last acquire was another day
    assert limiter.calls_today == 0
    assert limiter._count == 1  # unchanged - no mutation outside the lock


async def test_concurrent_acquires_serialized(monkeypatch):
    monkeypatch.setattr(ratelimit, "CALL_INTERVAL_S", 0.05)
    limiter = RateLimiter()
    t0 = time.monotonic()
    await asyncio.gather(*(limiter.acquire(is_create=False) for _ in range(4)))
    assert time.monotonic() - t0 >= 0.15
    assert limiter.calls_today == 4


async def test_note_429_sets_cooldown_and_delays_next_call(monkeypatch):
    monkeypatch.setattr(ratelimit, "COOLDOWN_S", 0.2)
    limiter = RateLimiter()
    assert limiter.cooldown_remaining == 0
    assert limiter.note_429(None) == 0.2
    assert 0 < limiter.cooldown_remaining <= 0.2
    t0 = time.monotonic()
    await limiter.acquire(is_create=False)
    assert time.monotonic() - t0 >= 0.15
    assert limiter.note_429(5.0) == 5.0  # a longer Retry-After wins


async def test_last_call_at_persisted(tmp_path):
    """The time of the last call is reported and survives a restart."""
    state = tmp_path / "state.json"
    limiter = RateLimiter(state_file=state)
    assert limiter.last_call_at is None
    t0 = time.time()
    await limiter.acquire(is_create=False)
    assert t0 <= limiter.last_call_at <= time.time()
    assert RateLimiter(state_file=state).last_call_at == limiter.last_call_at


async def test_only_create_endpoints_get_short_interval(settings, monkeypatch):
    """/doc/update, /markDone and deletions wait the full interval; only creates are short."""
    from marvin_mcp.client import CREATE_ENDPOINTS, MarvinClient

    assert CREATE_ENDPOINTS == {"/addTask", "/addProject", "/addEvent", "/doc/create"}
    seen: list[bool] = []

    class Spy(RateLimiter):
        async def acquire(self, *, is_create: bool) -> None:
            seen.append(is_create)

    import httpx

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    client = MarvinClient(settings, Spy(), transport=transport)
    await client.update_doc("x", [])
    await client.delete_doc("x")
    await client.request("POST", "/markDone", json={})
    await client.create_doc({"db": "Tasks"})
    await client.request("POST", "/addTask", json={})
    assert seen == [False, False, False, True, True]
    await client.aclose()


async def test_call_after_create_waits_full_interval(monkeypatch):
    """Spacing is set by the coming call's kind: an update after a create waits
    CALL_INTERVAL_S, two creates CREATE_INTERVAL_S."""
    monkeypatch.setattr(ratelimit, "CALL_INTERVAL_S", 0.3)
    monkeypatch.setattr(ratelimit, "CREATE_INTERVAL_S", 0.05)
    limiter = RateLimiter()
    await limiter.acquire(is_create=True)
    t0 = time.monotonic()
    await limiter.acquire(is_create=True)
    assert time.monotonic() - t0 < 0.2
    t1 = time.monotonic()
    await limiter.acquire(is_create=False)
    assert time.monotonic() - t1 >= 0.25
