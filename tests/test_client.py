from datetime import datetime, timezone

import pytest

from marvin_mcp.client import (
    MarvinClient,
    MarvinError,
    local_today,
    local_tz_offset_minutes,
)
from marvin_mcp.config import Settings
from marvin_mcp.ratelimit import RateLimiter

from .conftest import RecordingTransport


def make_client(transport, settings) -> MarvinClient:
    return MarvinClient(settings, RateLimiter(), transport=transport)


async def test_api_token_used_for_normal_endpoints(settings, transport):
    client = make_client(transport, settings)
    await client.today_items("2026-08-19")
    req = transport.requests[-1]
    assert req.headers.get("X-API-Token") == "testapitoken"
    assert "X-Full-Access-Token" not in req.headers


async def test_full_access_token_used_for_doc_endpoints(settings, transport):
    client = make_client(transport, settings)
    await client.update_doc("abc", [{"key": "done", "val": False}])
    req = transport.requests[-1]
    assert req.headers.get("X-Full-Access-Token") == "testfulltoken"
    assert "X-API-Token" not in req.headers


async def test_full_access_missing_gives_clear_error(transport, tmp_path):
    settings = Settings(
        api_token="testapitoken",
        full_access_token=None,
        mcp_auth_token=None,
        state_dir=tmp_path,
    )
    client = make_client(transport, settings)
    with pytest.raises(MarvinError, match="Full Access Token"):
        await client.get_doc("x")
    assert transport.requests == []  # no call was made


async def test_habits_raw_routes_to_full_access(settings, transport):
    client = make_client(transport, settings)
    await client.habits(raw=True)
    assert transport.requests[-1].headers.get("X-Full-Access-Token") == "testfulltoken"
    await client.habits(raw=False)
    assert transport.requests[-1].headers.get("X-API-Token") == "testapitoken"


async def test_mark_done_sends_local_offset(settings, transport):
    client = make_client(transport, settings)
    await client.mark_done("task1")
    body = transport.last_json()
    assert body["itemId"] == "task1"
    assert body["timeZoneOffset"] == local_tz_offset_minutes()


async def test_add_task_disables_autocomplete(settings, transport):
    # X-Auto-Complete: false protects against '#word' in the title
    # corrupting parentId (MarvinAPI issue #50, live-tested 2026-08-20)
    client = make_client(transport, settings)
    await client.add_task({"title": "Fix bug #123"})
    assert transport.requests[-1].headers.get("X-Auto-Complete") == "false"


async def test_add_project_blocks_hash_in_client_layer(settings, transport):
    # The '#word' guard lives in the client so every caller is covered
    # (/addProject ignores X-Auto-Complete, live-tested 2026-08-20)
    client = make_client(transport, settings)
    with pytest.raises(MarvinError, match="X-Auto-Complete"):
        await client.add_project({"title": "Campaign #2026", "parentId": "cat1"})
    assert transport.requests == []  # no API call spent


async def test_error_response_raises_without_leaking_headers(settings, transport):
    import httpx

    transport.responses["/addTask"] = httpx.Response(400, text="bad request")
    client = make_client(transport, settings)
    with pytest.raises(MarvinError) as exc:
        await client.add_task({"title": "x"})
    assert "token" not in str(exc.value).lower()
    assert "400" in str(exc.value)


def test_offset_summer_and_winter():
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert local_tz_offset_minutes(summer) == 120
    assert local_tz_offset_minutes(winter) == 60


def test_local_today_crosses_midnight_correctly():
    # 23:30 UTC on a summer evening = 01:30 the next day in Europe/Stockholm (pinned in conftest)
    t = datetime(2026, 8, 19, 23, 30, tzinfo=timezone.utc)
    assert local_today(t) == "2026-08-20"


async def test_today_items_defaults_to_local_today(settings, transport):
    client = make_client(transport, settings)
    await client.today_items()
    assert transport.requests[-1].url.params["date"] == local_today()
