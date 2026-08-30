"""Async HTTP client for Amazing Marvin's public API.

- Least-privilege token routing: `X-Full-Access-Token` is only sent to the
  endpoints that require it (/doc*, /habits?raw=1, GET /reminders,
  /resetRewardPoints); everything else uses the limited `X-API-Token`.
- Every call goes through the global rate limiter.
- Never logs headers, tokens, or request bodies.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import httpx

from .config import MARVIN_API_BASE, TIMEZONE, Settings
from .ratelimit import RateLimiter

logger = logging.getLogger(__name__)

# Endpoints that require the Full Access Token per the official docs.
# /reminder/deleteAll is deliberately not covered: no tool exposes it
# (too blunt — it wipes every reminder in one call).
FULL_ACCESS_ENDPOINTS = {
    "/doc",
    "/doc/update",
    "/doc/create",
    "/doc/delete",
    "/reminders",
    "/resetRewardPoints",
}


class MarvinError(Exception):
    """Error from Marvin's API, with no sensitive details."""


class MarvinRateLimited(MarvinError):
    """Marvin responded 429 — the limiter has entered its cool-down."""


# Response headers that may be logged on a 429 (never cookies or anything
# auth-related).
SAFE_429_HEADERS = ("retry-after", "date", "content-type", "x-ratelimit-limit",
                    "x-ratelimit-remaining", "x-ratelimit-reset", "ratelimit-limit",
                    "ratelimit-remaining", "ratelimit-reset", "cf-ray")


def local_tz_offset_minutes(now: datetime | None = None) -> int:
    """Marvin's timeZoneOffset: UTC offset in minutes (Pacific = -480)."""
    now = now or datetime.now(TIMEZONE)
    offset = now.astimezone(TIMEZONE).utcoffset()
    return int(offset.total_seconds() // 60)


def local_today(now: datetime | None = None) -> str:
    now = now or datetime.now(TIMEZONE)
    return now.astimezone(TIMEZONE).strftime("%Y-%m-%d")


class MarvinClient:
    def __init__(
        self,
        settings: Settings,
        limiter: RateLimiter,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._limiter = limiter
        self._http = httpx.AsyncClient(
            base_url=MARVIN_API_BASE,
            timeout=30.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    def _headers_for(self, endpoint: str, *, force_full_access: bool = False) -> dict:
        needs_full = force_full_access or endpoint in FULL_ACCESS_ENDPOINTS
        if needs_full:
            if not self._settings.full_access_token:
                raise MarvinError(
                    f"{endpoint} requires the Full Access Token, "
                    "which is not configured."
                )
            return {"X-Full-Access-Token": self._settings.full_access_token}
        if not self._settings.api_token:
            raise MarvinError(
                "MARVIN_API_TOKEN is not configured. Set MARVIN_API_TOKEN (or "
                "MARVIN_API_TOKEN_FILE) in the server's environment and restart "
                "— the token lives in Amazing Marvin under Settings -> API."
            )
        return {"X-API-Token": self._settings.api_token}

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict | None = None,
        json: Any | None = None,
        is_write: bool | None = None,
        force_full_access: bool = False,
        expect_json: bool = True,
        extra_headers: dict | None = None,
    ) -> Any:
        if is_write is None:
            is_write = method.upper() != "GET"
        await self._limiter.acquire(is_write=is_write)
        headers = self._headers_for(endpoint, force_full_access=force_full_access)
        if extra_headers:
            headers = {**headers, **extra_headers}
        logger.info("Marvin API: %s %s", method.upper(), endpoint)
        try:
            resp = await self._http.request(
                method, endpoint, params=params, json=json, headers=headers
            )
        except httpx.RequestError as exc:
            raise MarvinError(
                f"Network error talking to Marvin: {type(exc).__name__}"
            ) from None
        if resp.status_code == 429:
            retry_after = None
            try:
                retry_after = float(resp.headers.get("retry-after", ""))
            except ValueError:
                pass
            secs = self._limiter.note_429(retry_after)
            # Log the headers once per 429 so we learn Marvin's window; only
            # allow-listed headers, never cookies/auth.
            shown = {k: v for k, v in resp.headers.items() if k.lower() in SAFE_429_HEADERS}
            logger.warning(
                "Marvin 429 on %s — all calls paused for %.0fs. Headers: %s (others: %s)",
                endpoint, secs, shown,
                sorted(k for k in resp.headers if k.lower() not in SAFE_429_HEADERS),
            )
            raise MarvinRateLimited(
                f"Marvin responded 429 (Too many requests) on {endpoint}; "
                f"all Marvin calls are paused for {secs:.0f}s."
            )
        if resp.status_code >= 400:
            # The response body can contain useful error info but never tokens.
            body = resp.text[:300]
            raise MarvinError(
                f"Marvin responded {resp.status_code} on {endpoint}: {body}"
            )
        if not expect_json or not resp.content:
            return resp.text.strip()
        try:
            return resp.json()
        except ValueError:
            return resp.text.strip()

    # ---- Thin endpoint wrappers ----

    async def test_credentials(self) -> str:
        return await self.request("POST", "/test", expect_json=False)

    async def add_task(self, data: dict) -> dict:
        # X-Auto-Complete: false (MarvinAPI issue #50) disables server-side
        # shortcut parsing in the title. Without it, every '#word' in the
        # title corrupts the task: the string is stored unresolved as
        # parentId (overriding even an explicitly set parentId) and the task
        # becomes invisible in the app.
        return await self.request(
            "POST", "/addTask", json=data, extra_headers={"X-Auto-Complete": "false"}
        )

    async def mark_done(self, item_id: str) -> dict:
        return await self.request(
            "POST",
            "/markDone",
            json={"itemId": item_id, "timeZoneOffset": local_tz_offset_minutes()},
        )

    async def add_project(self, data: dict) -> dict:
        # /addProject has the same '#word' corruption bug as /addTask
        # (the string is stored unresolved as parentId, the project becomes
        # invisible) but ignores the X-Auto-Complete header (live-tested
        # 2026-08-20). The guard lives here so every caller is covered;
        # raising before the limiter runs means no API call is spent.
        if re.search(r"#\S", data.get("title", "")):
            raise MarvinError(
                "Project titles containing '#word' are blocked: /addProject "
                "stores the string unresolved as parentId (making the "
                "project invisible) and ignores the X-Auto-Complete header. "
                "Rephrase the title without '#'."
            )
        return await self.request("POST", "/addProject", json=data)

    async def add_event(self, data: dict) -> dict:
        return await self.request("POST", "/addEvent", json=data)

    async def get_doc(self, doc_id: str) -> Any:
        return await self.request("GET", "/doc", params={"id": doc_id})

    async def update_doc(self, item_id: str, setters: list[dict]) -> dict:
        return await self.request(
            "POST", "/doc/update", json={"itemId": item_id, "setters": setters}
        )

    async def create_doc(self, doc: dict) -> dict:
        return await self.request("POST", "/doc/create", json=doc)

    async def delete_doc(self, item_id: str) -> Any:
        return await self.request("POST", "/doc/delete", json={"itemId": item_id})

    async def today_items(self, date: str | None = None) -> list:
        return await self.request(
            "GET", "/todayItems", params={"date": date or local_today()}
        )

    async def done_items(self, date: str | None = None) -> list:
        # UNDOCUMENTED endpoint (missing from the OpenAPI spec and the wiki;
        # live-tested 2026-08-30): lists completed tasks whose `day` is the
        # date. Filters on day, not doneAt — see get_done_items. The limited
        # API token is sufficient.
        return await self.request(
            "GET", "/doneItems", params={"date": date or local_today()}
        )

    async def due_items(self, by: str | None = None) -> list:
        return await self.request(
            "GET", "/dueItems", params={"by": by or local_today()}
        )

    async def children(self, parent_id: str) -> list:
        return await self.request("GET", "/children", params={"parentId": parent_id})

    async def today_time_blocks(self, date: str | None = None) -> list:
        return await self.request(
            "GET", "/todayTimeBlocks", params={"date": date or local_today()}
        )

    async def categories(self) -> list:
        return await self.request("GET", "/categories")

    async def labels(self) -> list:
        return await self.request("GET", "/labels")

    async def goals(self) -> list:
        return await self.request("GET", "/goals")

    async def me(self) -> dict:
        return await self.request("GET", "/me")

    async def tracked_item(self) -> Any:
        return await self.request("GET", "/trackedItem")

    async def track(self, task_id: str, action: str) -> dict:
        return await self.request(
            "POST", "/track", json={"taskId": task_id, "action": action}
        )

    async def tracks(self, task_ids: list[str]) -> list:
        return await self.request("POST", "/tracks", json={"taskIds": task_ids})

    async def habits(self, raw: bool = False) -> list:
        params = {"raw": "1"} if raw else None
        return await self.request(
            "GET", "/habits", params=params, force_full_access=raw
        )

    async def habit(self, habit_id: str) -> dict:
        return await self.request("GET", "/habit", params={"id": habit_id})

    async def update_habit(self, data: dict) -> Any:
        return await self.request("POST", "/updateHabit", json=data)

    async def reminders(self) -> list:
        return await self.request("GET", "/reminders")

    async def set_reminders(self, reminders: list[dict]) -> Any:
        return await self.request(
            "POST", "/reminder/set", json={"reminders": reminders}
        )

    async def delete_reminders(self, reminder_ids: list[str]) -> Any:
        return await self.request(
            "POST", "/reminder/delete", json={"reminderIds": reminder_ids}
        )

    async def kudos(self) -> dict:
        return await self.request("GET", "/kudos")

    async def claim_reward_points(self, points: float, item_id: str, date: str) -> dict:
        return await self.request(
            "POST",
            "/claimRewardPoints",
            json={"points": points, "itemId": item_id, "date": date, "op": "CLAIM"},
        )

    async def unclaim_reward_points(self, item_id: str, date: str) -> dict:
        return await self.request(
            "POST",
            "/unclaimRewardPoints",
            json={"itemId": item_id, "date": date, "op": "UNCLAIM"},
        )

    async def spend_reward_points(self, points: float, date: str) -> dict:
        return await self.request(
            "POST",
            "/spendRewardPoints",
            json={"points": points, "date": date, "op": "SPEND"},
        )

    async def reset_reward_points(self) -> dict:
        return await self.request("POST", "/resetRewardPoints")
