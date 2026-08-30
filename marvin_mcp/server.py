"""MCP server for Amazing Marvin with complete public-API coverage.

37 tools covering all ~31 documented public endpoints (plus the undocumented
/doneItems): core CRUD + priority,
habits, time blocks (read + experimental create), time tracking, labels,
goals, reminders, and kudos/reward points. Deliberately no Smart List /
task-picking logic — Marvin's own Spotlight does the picking.

As of 1.1.0, every writable field in Marvin's official data model (Tasks and
Categories/Projects) is either supported by a tool or explicitly documented
as unsupported; see docs/field-reconciliation.md.

Many tool descriptions carry warnings and behavioral notes verified against
the live API (2026-08-19/29); see the "Marvin API quirks & findings" section
of the README for the full list.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from .client import MarvinClient, MarvinError, local_today
from .config import TIMEZONE, Settings, load_settings
from .ratelimit import DailyBudgetExceeded, QueueTimeout, RateLimiter

logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP(name="amazing-marvin")

_client: MarvinClient | None = None
_limiter: RateLimiter | None = None

# Cache for get_done_items: (date, lookback) -> (expires monotonic, result).
# Only complete results are cached; a daily-summary routine may run several
# times a day and a cold run costs lookback+1 read calls.
DONE_CACHE_TTL_S = 30 * 60
_done_cache: dict[tuple[str, int], tuple[float, dict]] = {}


def get_client() -> MarvinClient:
    if _client is None:
        raise RuntimeError("Client is not initialized (run via __main__)")
    return _client


def init(settings: Settings, transport=None) -> None:
    """Initialize the global client + limiter. `transport` is used in tests."""
    global _client, _limiter
    _limiter = RateLimiter(state_file=settings.state_dir / "ratelimit-state.json")
    _client = MarvinClient(settings, _limiter, transport=transport)
    _done_cache.clear()


def day_bounds_ms(date_str: str) -> tuple[float, float]:
    """[start, end) of a day in the configured timezone, epoch milliseconds."""
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=TIMEZONE)
    return start.timestamp() * 1000, (start + timedelta(days=1)).timestamp() * 1000


def remember_done(doc: Any) -> None:
    """mark_done bonus: insert the just-completed task into warm
    get_done_items caches whose day covers doneAt, so completions made
    through this server show up in the next call without cold reads."""
    if not isinstance(doc, dict) or not doc.get("done") or not doc.get("_id"):
        return
    done_at = doc.get("doneAt")
    if not isinstance(done_at, (int, float)):
        return
    for (date_str, _lookback), (_expires, result) in _done_cache.items():
        lo, hi = day_bounds_ms(date_str)
        if not (lo <= done_at < hi):
            continue
        items = result["items"]
        if any(i.get("_id") == doc["_id"] for i in items):
            continue
        items.append(doc)
        items.sort(key=lambda i: i.get("doneAt", 0))
        result["count"] = len(items)


def forget_done(item_id: str) -> None:
    """The inverse of remember_done: drop a task from every warm
    get_done_items cache (after delete_task/unmark_done), so a deleted or
    un-completed task does not linger in the completed list for up to
    30 minutes."""
    for _key, (_expires, result) in _done_cache.items():
        items = result["items"]
        kept = [i for i in items if i.get("_id") != item_id]
        if len(kept) != len(items):
            items[:] = kept
            result["count"] = len(items)


def now_ms() -> int:
    return int(time.time() * 1000)


def make_setters(fields: dict[str, Any]) -> list[dict]:
    """Build setters for /doc/update per the wiki's recommendation:
    each field + fieldUpdates.<field> + updatedAt, for correct conflict
    resolution and display in Marvin."""
    ts = now_ms()
    setters: list[dict] = []
    for key, val in fields.items():
        setters.append({"key": key, "val": val})
        setters.append({"key": f"fieldUpdates.{key}", "val": ts})
    setters.append({"key": "updatedAt", "val": ts})
    return setters


def check_planned_week(value: str) -> None:
    """plannedWeek must be the Monday of the ISO week (per the data-type
    docs); any other date yields a week entry Marvin does not display
    correctly."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise MarvinError("planned_week must have the format YYYY-MM-DD (the week's Monday).") from None
    if day.weekday() != 0:
        raise MarvinError(
            f"planned_week must be the week's Monday (YYYY-MM-DD); {value} is not a Monday."
        )


def check_iso_date(value: str, name: str) -> None:
    """The server validates no dates — '2026-02-31', '31-12-2026' and year
    0025 are stored verbatim (live-tested 2026-08-29), so all validation
    happens here."""
    # strptime is too lenient (accepts '2026-9-1' and year 0025) — require the
    # exact form and a plausible year; form data has produced year 0025 in
    # real use.
    ok = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    if ok:
        try:
            ok = 2000 <= datetime.strptime(value, "%Y-%m-%d").year <= 2100
        except ValueError:
            ok = False
    if not ok:
        raise MarvinError(
            f"{name} must be a valid date YYYY-MM-DD (year 2000-2100), got {value!r}. "
            "The server does not validate and stores invalid dates verbatim."
        )


def check_day(value: str, name: str = "day", allow_unassigned: bool = True) -> None:
    if value == "today" or (allow_unassigned and value == "unassigned"):
        return
    check_iso_date(value, name)


def clean_title(title: str) -> str:
    """The server accepts empty/whitespace titles and stores surrounding
    whitespace verbatim (live-tested 2026-08-29)."""
    cleaned = title.strip()
    if not cleaned:
        raise MarvinError("The title must not be empty (the server would otherwise create an untitled item).")
    return cleaned


def check_planned_month(value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError:
        raise MarvinError("planned_month must have the format YYYY-MM.") from None


def tool_error(exc: Exception) -> dict:
    if isinstance(exc, (DailyBudgetExceeded, QueueTimeout, MarvinError)):
        return {"error": str(exc)}
    logger.exception("Unexpected error")
    return {"error": f"Unexpected error: {type(exc).__name__}"}


# MCP tool annotations (hints to clients). Per the MCP spec,
# destructiveHint=true is the default for writing tools, so non-destructive
# writes must set it to false explicitly. openWorldHint=False everywhere:
# every tool only talks to Marvin's API.
READONLY = {"readOnlyHint": True, "openWorldHint": False}
ADDITIVE = {  # creates/adds; repeating produces duplicates
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
IDEMPOTENT_WRITE = {  # updates fields; repeating changes nothing more
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
DESTRUCTIVE = {  # deletes permanently; repeating changes nothing more
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}


# ------------------------------------------------------------------ Core


@mcp.tool(annotations=READONLY)
async def test_connection() -> dict:
    """Test authentication against Marvin's API. Returns OK if the apiToken
    works."""
    try:
        result = await get_client().test_credentials()
        return {"status": result, "calls_today": _limiter.calls_today}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def create_task(
    title: Annotated[str, Field(description="Task title")],
    parent_id: Annotated[
        str | None,
        Field(description="ID of the category/project the task belongs in (from get_categories). Omit for the Inbox. NOTE: the server does not validate the ID — a wrong parentId yields an orphan reachable only via date reads (live-tested 2026-08-29)"),
    ] = None,
    day: Annotated[
        str | None,
        Field(description="Schedule on date YYYY-MM-DD, or 'today'. Omit for unscheduled."),
    ] = None,
    priority: Annotated[
        int | None,
        Field(description="Priority (isStarred): 3=Most important/red, 2=Very important/orange, 1=Important/yellow, -1=Low priority (down arrow; shown in the app only with 'Enable low priority' on in the Priorities strategy — the value is stored regardless). 0 is not valid here; omit for no priority", ge=-1, le=3),
    ] = None,
    frog: Annotated[
        int | None, Field(description="Frog marker 1=normal, 2=baby, 3=monster", ge=1, le=3)
    ] = None,
    note: Annotated[str | None, Field(description="Note (markdown)")] = None,
    label_ids: Annotated[list[str] | None, Field(description="Label IDs (from get_labels)")] = None,
    due_date: Annotated[str | None, Field(description="Deadline YYYY-MM-DD (use sparingly)")] = None,
    time_estimate_minutes: Annotated[
        int | None, Field(description="Time estimate in minutes", ge=1)
    ] = None,
    planned_week: Annotated[
        str | None,
        Field(description="Plan into a week: the week's Monday YYYY-MM-DD (Planning Ahead strategy)"),
    ] = None,
    planned_month: Annotated[
        str | None, Field(description="Plan into a month: YYYY-MM (Planning Ahead strategy)")
    ] = None,
    review_date: Annotated[
        str | None, Field(description="Review date YYYY-MM-DD (Review Date strategy)")
    ] = None,
    backburner: Annotated[
        bool | None,
        Field(description="True = put in the backburner (dormant). NOTE: only effective on an UNSCHEDULED task — scheduling (day) trumps the flag in the UI (verified in the app 2026-08-29), so do not combine with day"),
    ] = None,
    is_reward: Annotated[
        bool | None,
        Field(description="Documented Task field with no observed function — normally do NOT use. The app's purchasable rewards are separate Rewards documents that the public API cannot reach at all (live-tested 2026-08-29: no endpoint exists, and app rewards are not Tasks); the flag on a Task produced no UI effect. Never combine with reward_points"),
    ] = None,
    reward_points: Annotated[
        float | None,
        Field(description="Reward points the task AWARDS on completion (coin + points in the list row when the Rewards strategy is on, verified in the app 2026-08-29; points are claimed via claim_reward_points). Do not set together with is_reward", ge=0),
    ] = None,
    daily_section: Annotated[
        str | None,
        Field(description="Day section: 'Morning', 'Afternoon' or 'Evening' (dailyStructure strategy)"),
    ] = None,
    bonus_section: Annotated[
        str | None, Field(description="'Essential' or 'Bonus' (bonusStructure strategy)")
    ] = None,
    custom_section: Annotated[
        str | None,
        Field(description="ID of a custom section from strategySettings.customStructure (customStructure strategy)"),
    ] = None,
    time_block_section: Annotated[
        str | None,
        Field(description="Time block ID (from get_today_time_blocks). NOTE: stored, but no visible section link renders in Today even with the Time Block Sections strategy active (verified in the app 2026-08-29) — visible section assignment is done in the app"),
    ] = None,
) -> dict:
    """Create a task in Amazing Marvin. Prefer priority/frog over dates
    where possible.

    The title is stored verbatim: this tool disables the server's shortcut
    parsing (X-Auto-Complete: false, verified against the live API
    2026-08-20), so quick-add syntax like '#Category', '~15', '+YYYY-MM-DD'
    and '*p2' is NOT parsed — '#' in titles (e.g. ticket references) is
    therefore safe. Without this, every '#word' would corrupt the task (the
    string is stored unresolved as parentId, making the task invisible).
    Use the parameters instead: parent_id, day, priority,
    time_estimate_minutes, label_ids.

    Note: startDate/endDate CANNOT be set here — /addTask ignores them
    (verified against the live API 2026-08-29). Set them with update_task
    after creation. Strategy-dependent fields (planned_week/month,
    review_date, backburner, is_reward/reward_points, the sections) are
    stored even when the strategy is disabled in the app — they just are
    not shown in the UI then."""
    try:
        data: dict[str, Any] = {"title": clean_title(title), "done": False}
        if parent_id:
            data["parentId"] = parent_id
        if day:
            check_day(day, allow_unassigned=False)
            data["day"] = local_today() if day == "today" else day
        if priority is not None:
            if priority == 0:
                raise MarvinError("priority=0 is not valid when creating — omit the parameter.")
            data["isStarred"] = priority
        if frog is not None:
            data["isFrogged"] = frog
        if note:
            data["note"] = note
        if label_ids:
            data["labelIds"] = label_ids
        if due_date:
            check_iso_date(due_date, "due_date")
            data["dueDate"] = due_date
        if time_estimate_minutes is not None:
            data["timeEstimate"] = time_estimate_minutes * 60_000
        if planned_week:
            check_planned_week(planned_week)
            data["plannedWeek"] = planned_week
        if planned_month:
            check_planned_month(planned_month)
            data["plannedMonth"] = planned_month
        if review_date:
            check_iso_date(review_date, "review_date")
            data["reviewDate"] = review_date
        if backburner is not None:
            data["backburner"] = backburner
        if is_reward is not None:
            data["isReward"] = is_reward
        if reward_points is not None:
            data["rewardPoints"] = reward_points
        if daily_section:
            data["dailySection"] = daily_section
        if bonus_section:
            data["bonusSection"] = bonus_section
        if custom_section:
            data["customSection"] = custom_section
        if time_block_section:
            data["timeBlockSection"] = time_block_section
        created = await get_client().add_task(data)
        return {"created": created}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mark_done(
    item_id: Annotated[str, Field(description="Task ID (NOT a project — see description)")],
) -> dict:
    """Mark a task as done (via /markDone, with the correct timezone offset).
    Tasks ONLY: for projects the API responds 400 'Can only mark Tasks done
    with this API' (verified live 2026-08-19) — projects are completed in the
    Marvin app (done=true via /doc/update would technically work but skips
    the app's side effects). Safe for generated instances of recurring tasks
    too (verified live): the instance ID is deterministic
    ('YYYY-MM-DD_<recurringTaskId>'), so no duplicates can occur.
    Error codes (live-tested 2026-08-29): 404 = the task does not exist
    (deleted/wrong ID — unlike /doc/update, which responds 500);
    400 = already marked done (harmless, nothing changes). Stops running
    time tracking on the task (receipt in /tracks; task.times is NOT
    written by the server). Pinned task: the original stays open and
    pinned as documented; the completed copy gets its own ID and can be
    found via get_done_items. Leaves `day` untouched; the app sets day =
    today only on unscheduled and future-dated tasks, a past day is kept
    there too (app code, 2026-08-30). Completed tasks can be read back with
    /doc (by ID) and listed with get_done_items."""
    try:
        result = await get_client().mark_done(item_id)
        remember_done(result)
        return {"completed": result}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def unmark_done(
    item_id: Annotated[str, Field(description="Task ID")],
) -> dict:
    """Undo a completion (sets done=false and clears doneAt via /doc/update).
    Requires the Full Access Token. Safe for generated instances of recurring
    tasks too (verified live). Note: any kudos from the completion are not
    adjusted; awarded reward points can however be undone with
    unclaim_reward_points. A permanent 500 = the document does not exist
    (deleted or wrong ID; the server responds 500 instead of 404, verified
    live 2026-08-29)."""
    try:
        result = await get_client().update_doc(
            item_id, make_setters({"done": False, "doneAt": None})
        )
        forget_done(item_id)
        return {"updated": result}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def update_task(
    item_id: Annotated[str, Field(description="Task ID")],
    title: Annotated[str | None, Field(description="New title")] = None,
    parent_id: Annotated[str | None, Field(description="Move to category/project ID (not validated by the server — a wrong ID yields an orphan, live-tested 2026-08-29)")] = None,
    day: Annotated[
        str | None,
        Field(description="Schedule on YYYY-MM-DD, 'today', or 'unassigned' to unschedule"),
    ] = None,
    note: Annotated[str | None, Field(description="New note (replaces the existing one)")] = None,
    due_date: Annotated[str | None, Field(description="Deadline YYYY-MM-DD, or '' to remove")] = None,
    label_ids: Annotated[
        list[str] | None,
        Field(description="New labels (IDs from get_labels; replaces existing ones, [] removes all)"),
    ] = None,
    time_estimate_minutes: Annotated[
        int | None, Field(description="Time estimate in minutes, 0 removes it", ge=0)
    ] = None,
    start_date: Annotated[
        str | None,
        Field(description="Start date YYYY-MM-DD, '' removes. Mechanics (verified in the app 2026-08-29): the Start Dates strategy hides BACKBURNER items until their start date — combine with backburner=true and day='unassigned'; a scheduled task is not affected"),
    ] = None,
    end_date: Annotated[
        str | None, Field(description="Soft deadline YYYY-MM-DD (Start & End Dates strategy), '' removes")
    ] = None,
    planned_week: Annotated[
        str | None,
        Field(description="Plan into a week: the week's Monday YYYY-MM-DD (Planning Ahead strategy; verified in the app 2026-08-29 — also shows in the month view), '' removes (client cache may linger until a view switch)"),
    ] = None,
    planned_month: Annotated[
        str | None, Field(description="Plan into a month: YYYY-MM (Planning Ahead strategy, verified in the app 2026-08-29), '' removes")
    ] = None,
    review_date: Annotated[
        str | None, Field(description="Review date YYYY-MM-DD, '' removes. Verified in the app 2026-08-29: shows in the Review view on the date; the day-view banner additionally requires the Review Alert workflow snippet")
    ] = None,
    backburner: Annotated[
        bool | None,
        Field(description="True = put in the backburner, False = take out. NOTE: only effective on an UNSCHEDULED task — set day='unassigned' at the same time; scheduling trumps the flag in the UI (verified in the app 2026-08-29)"),
    ] = None,
    reward_points: Annotated[
        float | None,
        Field(description="Reward points the task AWARDS on completion (coin + points in the list row when the Rewards strategy is on, verified in the app 2026-08-29), 0 removes. Do not set together with isReward", ge=0),
    ] = None,
    daily_section: Annotated[
        str | None,
        Field(description="Day section 'Morning'/'Afternoon'/'Evening' (dailyStructure strategy), '' removes"),
    ] = None,
    bonus_section: Annotated[
        str | None, Field(description="'Essential' or 'Bonus' (bonusStructure strategy), '' removes")
    ] = None,
    custom_section: Annotated[
        str | None,
        Field(description="ID of a custom section from strategySettings.customStructure, '' removes"),
    ] = None,
    time_block_section: Annotated[
        str | None,
        Field(description="Time block ID (from get_today_time_blocks), '' removes. NOTE: stored, but no visible section link renders in Today even with the strategy active (verified in the app 2026-08-29)"),
    ] = None,
    snooze_until_unix_ms: Annotated[
        int | None,
        Field(description="Snooze the task until unix time in milliseconds (itemSnoozeTime), 0 removes. Verified in the app 2026-08-29: hides from Today AND the category view (the wiki's 'everywhere except the master list' does not hold for the category view)", ge=0),
    ] = None,
    perma_snooze_time: Annotated[
        str | None,
        Field(description="Hide the task every day until HH:mm (permaSnoozeTime), '' removes. Verified in the app 2026-08-29"),
    ] = None,
    orbit: Annotated[
        bool | None,
        Field(description="Orbit strategy: True = put in orbit (verified in the app 2026-08-29: shows in the Orbit view + orbit icon in Today). UNDOCUMENTED field (missing from the official data types)"),
    ] = None,
    no_auto_orbit: Annotated[
        bool | None,
        Field(description="Orbit strategy: True = exempt the task from automatic orbiting (auto-orbit otherwise pulls in scheduled tasks). UNDOCUMENTED field (bool type verified in live data 2026-08-29)"),
    ] = None,
) -> dict:
    """Update fields on an existing TASK via /doc/update (Full Access Token).
    For categories/projects, use update_category_or_project. For priority,
    use set_priority. Always complete tasks via mark_done, never here.
    Strategy-dependent fields (start/end date, planned_week/month,
    review_date, backburner, orbit, the sections) can be set even when the
    strategy is disabled in the app — they just are not shown in the UI then.
    Note on recurring tasks: never edit recurrence rules here — neither on a
    generated instance (recurring=true, _id 'YYYY-MM-DD_<id>') nor on the
    generator document. Do that editing in the Marvin app. Simple field
    changes (title, note) on a single instance are fine.
    Note: Marvin's server can sporadically respond 500 on /doc/update
    (transient and atomic — no partial write); just retry. But a PERMANENT
    500 (persists across retries) means the document does not exist —
    deleted, or a wrong/never-existing ID (the server responds 500 instead
    of 404 for missing IDs, verified live 2026-08-29). Fetch a fresh ID via
    get_categories/get_children."""
    try:
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = clean_title(title)
        if parent_id is not None:
            fields["parentId"] = parent_id
        if day is not None:
            check_day(day)
            fields["day"] = local_today() if day == "today" else day
        if note is not None:
            fields["note"] = note
        if due_date is not None:
            if due_date:
                check_iso_date(due_date, "due_date")
            fields["dueDate"] = due_date or None
        if label_ids is not None:
            fields["labelIds"] = label_ids
        if time_estimate_minutes is not None:
            fields["timeEstimate"] = time_estimate_minutes * 60_000 or None
        if start_date is not None:
            if start_date:
                check_iso_date(start_date, "start_date")
            fields["startDate"] = start_date or None
        if end_date is not None:
            if end_date:
                check_iso_date(end_date, "end_date")
            fields["endDate"] = end_date or None
        if planned_week is not None:
            if planned_week:
                check_planned_week(planned_week)
            fields["plannedWeek"] = planned_week or None
        if planned_month is not None:
            if planned_month:
                check_planned_month(planned_month)
            fields["plannedMonth"] = planned_month or None
        if review_date is not None:
            if review_date:
                check_iso_date(review_date, "review_date")
            fields["reviewDate"] = review_date or None
        if backburner is not None:
            fields["backburner"] = backburner
        if reward_points is not None:
            fields["rewardPoints"] = reward_points or None
        if daily_section is not None:
            fields["dailySection"] = daily_section or None
        if bonus_section is not None:
            fields["bonusSection"] = bonus_section or None
        if custom_section is not None:
            fields["customSection"] = custom_section or None
        if time_block_section is not None:
            fields["timeBlockSection"] = time_block_section or None
        if snooze_until_unix_ms is not None:
            fields["itemSnoozeTime"] = snooze_until_unix_ms or None
        if perma_snooze_time is not None:
            fields["permaSnoozeTime"] = perma_snooze_time or None
        if orbit is not None:
            fields["orbit"] = orbit
        if no_auto_orbit is not None:
            fields["noAutoOrbit"] = no_auto_orbit
        if not fields:
            return {"error": "No fields to update were given."}
        result = await get_client().update_doc(item_id, make_setters(fields))
        return {"updated": result}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def set_priority(
    item_id: Annotated[str, Field(description="Task ID")],
    priority: Annotated[
        int | None,
        Field(description="Priority (isStarred): 3=Most important/red, 2=Very important/orange, 1=Important/yellow, -1=Low priority (down arrow), 0=remove", ge=-1, le=3),
    ] = None,
    frog: Annotated[
        int | None,
        Field(description="Frog: 3=monster, 2=baby, 1=normal, 0=remove", ge=0, le=3),
    ] = None,
) -> dict:
    """Set or change priority (isStarred) and/or the frog marker on an
    existing TASK. Requires the Full Access Token. The app's four levels are
    stored as isStarred 3/2/1/-1 (Most/Very/Important/Low priority; -1
    verified against the app's code and live-tested 2026-08-30). Low
    priority is shown in the app only with 'Enable low priority' on in the
    Priorities strategy; the value is stored regardless. Does not apply to
    projects: they use the string field priority ('high'/'mid'/'low' =
    Most/Very/Important; no Low level), not isStarred — set it via
    update_category_or_project. A permanent 500 = the task does not exist
    (deleted or wrong ID) — the server responds 500 instead of 404
    (verified live 2026-08-29); fetch a fresh ID."""
    try:
        fields: dict[str, Any] = {}
        if priority is not None:
            fields["isStarred"] = priority or False
        if frog is not None:
            fields["isFrogged"] = frog or False
        if not fields:
            return {"error": "Provide priority and/or frog."}
        result = await get_client().update_doc(item_id, make_setters(fields))
        return {"updated": result}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_task(
    item_id: Annotated[str, Field(description="ID of the document to delete")],
) -> dict:
    """Delete a task/document PERMANENTLY via /doc/delete (Full Access Token).
    Marvin's trash is client-side — this CANNOT be undone. Only use when the
    user explicitly wants a deletion. Never delete the generator document of
    a recurring task here (risk of the whole series disappearing without the
    app's cleanup logic) — remove the recurrence in the Marvin app instead."""
    try:
        result = await get_client().delete_doc(item_id)
        forget_done(item_id)
        return {"deleted": result}
    except Exception as e:
        return tool_error(e)


# --------------------------------------------------------------- Reading


@mcp.tool(annotations=READONLY)
async def get_today_items(
    date: Annotated[
        str | None, Field(description="Date YYYY-MM-DD; omit for today (server timezone)")
    ] = None,
) -> dict:
    """Get tasks/projects scheduled on a given date (default today, in the
    server's configured timezone). Note: today's recurring tasks may be
    missing if the Marvin app hasn't been running yet today (instances are
    generated by the client)."""
    try:
        items = await get_client().today_items(date)
        return {"date": date or local_today(), "count": len(items), "items": items}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_due_items(
    by: Annotated[
        str | None, Field(description="Deadline up to and including YYYY-MM-DD; omit for today")
    ] = None,
) -> dict:
    """Get open tasks/projects with a deadline today or earlier."""
    try:
        items = await get_client().due_items(by)
        return {"by": by or local_today(), "count": len(items), "items": items}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_done_items(
    date: Annotated[
        str | None,
        Field(description="Date YYYY-MM-DD on which tasks were completed (configured timezone); omit for today"),
    ] = None,
    lookback_days: Annotated[
        int,
        Field(description="Days before the date for which /doneItems is also fetched, to catch tasks scheduled earlier but completed on the date. Each day = one read call (~3.1 s in the queue). Default 7", ge=0, le=14),
    ] = 7,
) -> dict:
    """Tasks completed on a given date (doneAt within that day, configured
    timezone) — regardless of priority and deadline. Built on the
    UNDOCUMENTED endpoint GET /doneItems?date= (missing from the OpenAPI
    spec and the wiki; live-tested 2026-08-30, may disappear): it filters
    on the task's `day`, not on doneAt. A past `day` is kept on completion
    both in the app and via the API (the app sets day = today only on
    unscheduled and future-dated tasks) — hence the date plus lookback_days
    earlier are fetched and everything is filtered on doneAt.
    The response always states its coverage: covers_from (= date − lookback)
    and days_fetched. On a 429/error the fetch stops: incomplete=true,
    days_missing lists the days not fetched and warning explains; the
    date's own completions are always included because it is fetched
    first. Complete results are cached for 30 minutes (cached=true) —
    repeated calls then cost no API calls; mark_done inserts its task into
    the cache, delete_task and unmark_done remove theirs. Completions or
    deletions made in the app show up only once the cache expires. Not covered: a task with a FUTURE day completed via the API
    (it sits under its day). Tasks only — completed projects are not
    listed. Items without doneAt (older data) are excluded and counted in
    skipped_without_done_at. Sorted by doneAt. Cost: one read call per day
    (~3.1 s each in the queue)."""
    try:
        target = date or local_today()
        check_iso_date(target, "date")
        key = (target, lookback_days)
        cached = _done_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return {**cached[1], "cached": True}
        start = datetime.strptime(target, "%Y-%m-%d").replace(tzinfo=TIMEZONE)
        lo, hi = day_bounds_ms(target)
        client = get_client()
        seen: dict[str, dict] = {}
        skipped = 0
        fetched = 0
        missing: list[str] = []
        warning: str | None = None
        for back in range(lookback_days + 1):
            day = (start - timedelta(days=back)).strftime("%Y-%m-%d")
            if warning is not None or client.limiter.cooldown_remaining > 0:
                missing.append(day)
                if warning is None:
                    warning = (
                        f"Marvin calls paused after a 429 ({client.limiter.cooldown_remaining:.0f}s left)."
                    )
                continue
            try:
                day_items = await client.done_items(day)
            except (MarvinError, DailyBudgetExceeded, QueueTimeout) as e:
                warning = str(e)
                missing.append(day)
                continue
            fetched += 1
            for item in day_items:
                if not isinstance(item, dict) or not item.get("done"):
                    continue
                done_at = item.get("doneAt")
                if not isinstance(done_at, (int, float)):
                    skipped += 1
                    continue
                if lo <= done_at < hi:
                    seen.setdefault(str(item.get("_id")), item)
        items = sorted(seen.values(), key=lambda i: i["doneAt"])
        result: dict[str, Any] = {
            "date": target,
            "lookback_days": lookback_days,
            "covers_from": (start - timedelta(days=lookback_days)).strftime("%Y-%m-%d"),
            "days_fetched": fetched,
            "count": len(items),
            "items": items,
            "skipped_without_done_at": skipped,
        }
        if missing:
            result["incomplete"] = True
            result["days_missing"] = missing
            result["warning"] = (
                f"The list is incomplete: {len(missing)} of {lookback_days + 1} days "
                f"could not be fetched ({warning})"
            )
        else:
            _done_cache[key] = (time.monotonic() + DONE_CACHE_TTL_S, result)
        return result
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_children(
    parent_id: Annotated[
        str,
        Field(description="Category/project ID, 'unassigned' for the Inbox, or 'root' for the top level"),
    ],
) -> dict:
    """Get open tasks and subprojects in a category/project. Returns direct
    children only — call again for deeper levels. Note: orphans (tasks whose
    parentId points to a deleted/non-existent document) do NOT show up under
    'unassigned' — only in get_today_items/get_due_items if they have a
    day/dueDate (live-tested 2026-08-29)."""
    try:
        items = await get_client().children(parent_id)
        return {"parent_id": parent_id, "count": len(items), "items": items}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_categories() -> dict:
    """Get all categories and projects (the whole hierarchy; parentId='root'
    is the top level). Use to find the right parent_id when creating/moving."""
    try:
        cats = await get_client().categories()
        return {"count": len(cats), "categories": cats}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def create_category_or_project(
    title: Annotated[str, Field(description="Name")],
    kind: Annotated[Literal["category", "project"], Field(description="Kind")],
    parent_id: Annotated[
        str, Field(description="ID of the parent category, or 'root' for the top level")
    ] = "root",
    note: Annotated[str | None, Field(description="Note")] = None,
    color: Annotated[
        str | None,
        Field(description="Color '#rrggbb'. Categories ONLY at creation — /addProject ignores the field (verified live 2026-08-29); set project color with update_category_or_project afterwards"),
    ] = None,
    icon: Annotated[
        str | None,
        Field(description="Icon name with a library prefix, e.g. 'lucide-Rocket' (Lucide, PascalCase) or 'huge-happy' (verified in the app 2026-08-29); the app's picker also allows emoji. Categories ONLY — projects NEVER render their own icon (the flag stays; only the color is used)"),
    ] = None,
    time_estimate_minutes: Annotated[
        int | None,
        Field(description="Time estimate in minutes. NOTE: rendered as the project's OWN estimate — the UI does not aggregate it with the children's, despite the wiki's claim (verified in the app 2026-08-29)", ge=1),
    ] = None,
    planned_week: Annotated[
        str | None,
        Field(description="Plan into a week: the week's Monday YYYY-MM-DD (Planning Ahead strategy; mainly projects)"),
    ] = None,
    planned_month: Annotated[
        str | None,
        Field(description="Plan into a month: YYYY-MM (Planning Ahead strategy; mainly projects)"),
    ] = None,
    review_date: Annotated[
        str | None, Field(description="Review date YYYY-MM-DD (Review Date strategy)")
    ] = None,
    day: Annotated[
        str | None,
        Field(description="Projects ONLY: schedule on YYYY-MM-DD or 'today' (categories cannot be scheduled)"),
    ] = None,
    due_date: Annotated[
        str | None, Field(description="Projects ONLY: deadline YYYY-MM-DD (categories have no dueDate)")
    ] = None,
    priority: Annotated[
        Literal["high", "mid", "low"] | None,
        Field(description="Projects ONLY: priority as a string — high=Most important (red), mid=Very important (orange), low=Important (yellow, the one-star level — NOT the app's 'Low priority', which projects do not have). Projects do not use isStarred (verified live 2026-08-29; mapping verified against the app's code 2026-08-30)"),
    ] = None,
    frog: Annotated[
        int | None,
        Field(description="Projects ONLY: frog marker 1=normal, 2=baby, 3=monster", ge=1, le=3),
    ] = None,
    label_ids: Annotated[
        list[str] | None, Field(description="Projects ONLY: label IDs (from get_labels)")
    ] = None,
) -> dict:
    """Create a category (via /doc/create, Full Access Token) or a project
    (via /addProject). Categories can contain categories; projects cannot.
    Categories have no day/dueDate/priority/frog/labels in the data model —
    those parameters are rejected for kind='category'. startDate/endDate
    cannot be set at creation (/addProject ignores them, verified live
    2026-08-29) — use update_category_or_project afterwards.

    Note: project titles must not contain '#word' — /addProject has the same
    corruption bug as /addTask (the string is stored unresolved as parentId
    and the project becomes invisible) but ignores the X-Auto-Complete
    header (verified against the live API 2026-08-20), so the client blocks
    it locally before any API call. Category titles are unaffected
    (/doc/create parses nothing)."""
    try:
        if kind == "category":
            rejected = {
                "day": day, "due_date": due_date, "priority": priority,
                "frog": frog, "label_ids": label_ids,
            }
            given = [name for name, val in rejected.items() if val is not None]
            if given:
                return {
                    "error": (
                        f"The parameters {', '.join(given)} apply to projects only — "
                        "categories cannot be scheduled, prioritized or labeled "
                        "according to Marvin's data model."
                    )
                }
        if kind == "project":
            if color or icon:
                return {
                    "error": (
                        "color/icon cannot be set when creating a project — "
                        "/addProject ignores the fields (verified live 2026-08-29). "
                        "Create the project first, then set them with "
                        "update_category_or_project."
                    )
                }
            data: dict[str, Any] = {"title": clean_title(title), "parentId": parent_id, "done": False}
            if note:
                data["note"] = note
            if time_estimate_minutes is not None:
                data["timeEstimate"] = time_estimate_minutes * 60_000
            if planned_week:
                check_planned_week(planned_week)
                data["plannedWeek"] = planned_week
            if planned_month:
                check_planned_month(planned_month)
                data["plannedMonth"] = planned_month
            if review_date:
                check_iso_date(review_date, "review_date")
                data["reviewDate"] = review_date
            if day:
                check_day(day, allow_unassigned=False)
                data["day"] = local_today() if day == "today" else day
            if due_date:
                check_iso_date(due_date, "due_date")
                data["dueDate"] = due_date
            if priority:
                data["priority"] = priority
            if frog is not None:
                data["isFrogged"] = frog
            if label_ids:
                data["labelIds"] = label_ids
            return {"created": await get_client().add_project(data)}
        # Own _id: /doc/create does not echo back the server-generated id
        # (verified against the live API 2026-08-19), so we set it ourselves
        # in order to be able to return it.
        doc: dict[str, Any] = {
            "_id": uuid.uuid4().hex,
            "db": "Categories",
            "type": "category",
            "title": clean_title(title),
            "parentId": parent_id,
            "createdAt": now_ms(),
        }
        if note:
            doc["note"] = note
        if color:
            doc["color"] = color
        if icon:
            doc["icon"] = icon
        if time_estimate_minutes is not None:
            doc["timeEstimate"] = time_estimate_minutes * 60_000
        if planned_week:
            check_planned_week(planned_week)
            doc["plannedWeek"] = planned_week
        if planned_month:
            check_planned_month(planned_month)
            doc["plannedMonth"] = planned_month
        if review_date:
            check_iso_date(review_date, "review_date")
            doc["reviewDate"] = review_date
        return {"created": await get_client().create_doc(doc)}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def update_category_or_project(
    item_id: Annotated[str, Field(description="ID of the category/project (from get_categories)")],
    title: Annotated[str | None, Field(description="New title")] = None,
    parent_id: Annotated[
        str | None, Field(description="Move to parent category ID, or 'root'")
    ] = None,
    note: Annotated[str | None, Field(description="New note (replaces the existing one)")] = None,
    color: Annotated[str | None, Field(description="Color '#rrggbb', '' removes")] = None,
    icon: Annotated[
        str | None,
        Field(description="Icon name with a library prefix ('lucide-Rocket', 'huge-happy'), '' removes. ONLY meaningful on categories — projects never render their own icon (verified in the app 2026-08-29)"),
    ] = None,
    time_estimate_minutes: Annotated[
        int | None,
        Field(description="Time estimate in minutes, 0 removes it. On projects: rendered as the project's OWN estimate, no aggregation with the children's (verified in the app 2026-08-29)", ge=0),
    ] = None,
    start_date: Annotated[
        str | None, Field(description="Start date YYYY-MM-DD (Start & End Dates strategy), '' removes")
    ] = None,
    end_date: Annotated[
        str | None, Field(description="Soft deadline YYYY-MM-DD (Start & End Dates strategy), '' removes")
    ] = None,
    planned_week: Annotated[
        str | None,
        Field(description="Plan into a week: the week's Monday YYYY-MM-DD (Planning Ahead strategy), '' removes"),
    ] = None,
    planned_month: Annotated[
        str | None, Field(description="Plan into a month: YYYY-MM (Planning Ahead strategy), '' removes")
    ] = None,
    review_date: Annotated[
        str | None, Field(description="Review date YYYY-MM-DD (Review Date strategy), '' removes")
    ] = None,
    first_scheduled: Annotated[
        str | None,
        Field(description="The app's bookkeeping field firstScheduled YYYY-MM-DD, '' removes — mainly for restoring the value from the convert tool's removed_project_fields after a conversion round trip (nothing backfills it, neither server nor app — verified 2026-08-29). Otherwise leave alone"),
    ] = None,
    day: Annotated[
        str | None,
        Field(description="Projects ONLY: schedule YYYY-MM-DD, 'today', or 'unassigned' to unschedule"),
    ] = None,
    due_date: Annotated[
        str | None, Field(description="Projects ONLY: deadline YYYY-MM-DD, '' removes")
    ] = None,
    priority: Annotated[
        Literal["high", "mid", "low", ""] | None,
        Field(description="Projects ONLY: 'high'=Most important (red), 'mid'=Very important (orange), 'low'=Important (yellow, the one-star level — NOT the app's 'Low priority', which projects do not have), '' removes. Projects use the string field priority, not isStarred (verified live 2026-08-29; mapping verified against the app's code 2026-08-30)"),
    ] = None,
    frog: Annotated[
        int | None,
        Field(description="Projects ONLY: frog 3=monster, 2=baby, 1=normal, 0=remove", ge=0, le=3),
    ] = None,
    label_ids: Annotated[
        list[str] | None,
        Field(description="Projects ONLY: new labels (replaces existing ones, [] removes all)"),
    ] = None,
    backburner: Annotated[
        bool | None,
        Field(description="True = put in the backburner, False = take out. NOTE (verified in the app 2026-08-29 on tasks): only effective on unscheduled items — scheduling trumps the flag"),
    ] = None,
    orbit: Annotated[
        bool | None,
        Field(description="Orbit strategy: True = put in orbit (verified in the app 2026-08-29 on tasks: Orbit view + icon in Today). UNDOCUMENTED field"),
    ] = None,
    no_auto_orbit: Annotated[
        bool | None,
        Field(description="Orbit strategy: True = exempt from automatic orbiting. UNDOCUMENTED field (bool type verified in live data 2026-08-29)"),
    ] = None,
) -> dict:
    """Update fields on an existing CATEGORY or PROJECT via /doc/update
    (Full Access Token). For tasks, use update_task. Fields marked
    'Projects ONLY' do not exist in the category data model — if any of
    them is given, the tool first reads the document (1 extra API call) and
    refuses if it is a category: project fields on a category make it
    unrepairable from the app's UI (verified in the app 2026-08-29; repair
    then requires /doc/update with null). Strategy-dependent fields
    (start/end date, planned_week/month, review_date, orbit) can be set
    even when the strategy is disabled in the app. Do not complete projects
    here (done via /doc/update skips the app's side effects) — that is done
    in the Marvin app.
    Note: Marvin's server can sporadically respond 500 on /doc/update
    (transient and atomic); just retry. But a PERMANENT 500 (persists across
    retries) means the document does not exist — deleted, or a
    wrong/never-existing ID (the server responds 500 instead of 404 for
    missing IDs, verified live 2026-08-29). Fetch a fresh ID via
    get_categories/get_children."""
    try:
        project_only = {
            "day": day, "due_date": due_date, "priority": priority,
            "frog": frog, "label_ids": label_ids,
        }
        given = [name for name, val in project_only.items() if val is not None]
        if given:
            doc = await get_client().get_doc(item_id)
            if not isinstance(doc, dict) or doc.get("db") != "Categories":
                return {
                    "error": (
                        "The document is neither a category nor a project "
                        "(db='Categories' required) — check item_id."
                    )
                }
            if (doc.get("type") or "category") != "project":
                return {
                    "error": (
                        f"The parameters {', '.join(given)} apply to projects only — "
                        "the document is a category. Project fields on a category "
                        "make it unrepairable from the app's UI (verified "
                        "2026-08-29), so the write is blocked."
                    )
                }
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = clean_title(title)
        if parent_id is not None:
            fields["parentId"] = parent_id
        if note is not None:
            fields["note"] = note
        if color is not None:
            fields["color"] = color or None
        if icon is not None:
            fields["icon"] = icon or None
        if time_estimate_minutes is not None:
            fields["timeEstimate"] = time_estimate_minutes * 60_000 or None
        if start_date is not None:
            if start_date:
                check_iso_date(start_date, "start_date")
            fields["startDate"] = start_date or None
        if end_date is not None:
            if end_date:
                check_iso_date(end_date, "end_date")
            fields["endDate"] = end_date or None
        if planned_week is not None:
            if planned_week:
                check_planned_week(planned_week)
            fields["plannedWeek"] = planned_week or None
        if planned_month is not None:
            if planned_month:
                check_planned_month(planned_month)
            fields["plannedMonth"] = planned_month or None
        if review_date is not None:
            if review_date:
                check_iso_date(review_date, "review_date")
            fields["reviewDate"] = review_date or None
        if first_scheduled is not None:
            if first_scheduled:
                check_iso_date(first_scheduled, "first_scheduled")
            fields["firstScheduled"] = first_scheduled or None
        if day is not None:
            check_day(day)
            fields["day"] = local_today() if day == "today" else day
        if due_date is not None:
            if due_date:
                check_iso_date(due_date, "due_date")
            fields["dueDate"] = due_date or None
        if priority is not None:
            fields["priority"] = priority or None
        if frog is not None:
            fields["isFrogged"] = frog or False
        if label_ids is not None:
            fields["labelIds"] = label_ids
        if backburner is not None:
            fields["backburner"] = backburner
        if orbit is not None:
            fields["orbit"] = orbit
        if no_auto_orbit is not None:
            fields["noAutoOrbit"] = no_auto_orbit
        if not fields:
            return {"error": "No fields to update were given."}
        result = await get_client().update_doc(item_id, make_setters(fields))
        return {"updated": result}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def convert_category_or_project(
    item_id: Annotated[
        str, Field(description="ID of the project/category to convert (from get_categories)")
    ],
    to: Annotated[
        Literal["category", "project"], Field(description="Target type to convert to")
    ],
) -> dict:
    """EXPERIMENTAL: Convert project→category or category→project IN PLACE
    via /doc/update (Full Access Token; there is no official conversion
    endpoint, and this relies on undocumented server behavior that Marvin
    could change). Same _id, createdAt and children remain — conversion is a
    pure type change (verified against the live API 2026-08-29: the server
    accepts and persists the change in both directions, and the app renders
    correctly after an API-set change).
    When converting to a category, the project-only fields
    day/dueDate/priority/isFrogged are cleared (same as the app's 'Turn into
    Category') plus firstScheduled (a leftover the app's variant leaves
    behind); the previous values are returned in removed_project_fields so
    they can be restored with update_category_or_project after a possible
    back-conversion.
    Do NOT convert a category that contains subcategories into a project —
    projects cannot contain categories (risk of orphans/cycles; check
    get_children first)."""
    try:
        doc = await get_client().get_doc(item_id)
        if not isinstance(doc, dict) or doc.get("db") != "Categories":
            return {
                "error": (
                    "The document is neither a category nor a project "
                    "(db='Categories' required) — check item_id."
                )
            }
        current = doc.get("type") or "category"
        if current == to:
            return {"error": f"The document is already of type '{to}' — nothing to convert."}
        fields: dict[str, Any] = {"type": to}
        removed: dict[str, Any] | None = None
        if to == "category":
            removed = {
                k: doc.get(k)
                for k in ("day", "dueDate", "priority", "isFrogged", "firstScheduled")
            }
            for k in removed:
                fields[k] = None
        result = await get_client().update_doc(item_id, make_setters(fields))
        out: dict[str, Any] = {"updated": result, "converted_to": to}
        if removed is not None:
            out["removed_project_fields"] = removed
        return out
    except Exception as e:
        return tool_error(e)


# ---------------------------------------------------------------- Habits


@mcp.tool(annotations=READONLY)
async def list_habits() -> dict:
    """Get all habits as full documents incl. title, settings and history
    ([time1, value1, time2, value2, ...], unix ms). Requires the Full Access
    Token (the raw variant of /habits). Important (verified live 2026-08-19):
    non-raw /habits would be wrong here — it reads the server's tracking
    registry, which is created lazily on the first recording, so
    never-recorded habits are missing entirely, and the responses lack
    titles."""
    try:
        habits = await get_client().habits(raw=True)
        return {"count": len(habits), "habits": habits}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_habit(
    habit_id: Annotated[str, Field(description="Habit ID (from list_habits)")],
) -> dict:
    """Get the server's tracking record for a single habit (habitId + full
    history — the source of truth for recordings). Note: the response lacks
    title and settings; those are in list_habits."""
    try:
        return {"habit": await get_client().habit(habit_id)}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def record_habit(
    habit_id: Annotated[str, Field(description="Habit ID")],
    value: Annotated[float, Field(description="Value to record (1 for boolean habits)")] = 1,
    undo: Annotated[bool, Field(description="True to undo the latest recording instead")] = False,
) -> dict:
    """Record (or undo) a habit. Also updates the sync database
    (updateDB=true) so the Marvin app shows the change immediately."""
    try:
        data: dict[str, Any] = {"habitId": habit_id, "updateDB": True}
        if undo:
            data["undo"] = True
        else:
            data["time"] = now_ms()
            # /updateHabit rejects integers serialized as floats with
            # 400 Bad request ("value": 1.0 is refused, 1 is accepted) —
            # verified against the live API 2026-08-19.
            data["value"] = int(value) if float(value).is_integer() else value
        return {"result": await get_client().update_habit(data)}
    except Exception as e:
        return tool_error(e)


# ----------------------------------------------------------- Time blocks


@mcp.tool(annotations=READONLY)
async def get_today_time_blocks(
    date: Annotated[
        str | None, Field(description="Date YYYY-MM-DD; omit for today (server timezone)")
    ] = None,
    include_category_mapping: Annotated[
        bool,
        Field(description="Also look up the block→category/smartlist mapping (1 extra API call, requires Full Access Token)"),
    ] = True,
) -> dict:
    """Get today's time blocks. The API response lacks the category link
    (known limitation, MarvinAPI issue #65); the mapping is therefore fetched
    separately from the profile setting plannerSmartLists (key = normalized
    block title)."""
    try:
        blocks = await get_client().today_time_blocks(date)
        result: dict[str, Any] = {
            "date": date or local_today(),
            "count": len(blocks),
            "time_blocks": blocks,
        }
        if include_category_mapping:
            try:
                doc = await get_client().get_doc("strategySettings.plannerSmartLists")
                mapping = doc.get("val") if isinstance(doc, dict) else None
                result["title_to_category_or_smartlist"] = mapping or {}
            except MarvinError as exc:
                result["title_to_category_or_smartlist_error"] = str(exc)
        return result
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def create_time_block(
    title: Annotated[str, Field(description="Block name, e.g. 'Morning'")],
    date: Annotated[str, Field(description="Date YYYY-MM-DD")],
    start_time: Annotated[str, Field(description="Start time HH:mm (local time)")],
    duration_minutes: Annotated[int, Field(description="Length in minutes", gt=0)],
) -> dict:
    """EXPERIMENTAL: Create a time block via /doc/create (db='PlannerItems',
    Full Access Token). No official endpoint exists. Verify in the app that
    the block looks right."""
    try:
        doc = {
            "db": "PlannerItems",
            "title": title,
            "date": date,
            "time": start_time,
            "duration": str(duration_minutes),
            "isSection": True,
            "createdAt": now_ms(),
        }
        return {"created": await get_client().create_doc(doc)}
    except Exception as e:
        return tool_error(e)


# --------------------------------------------------------- Time tracking


@mcp.tool(annotations=READONLY)
async def get_tracked_item() -> dict:
    """Show which task is currently being time-tracked (if any)."""
    try:
        item = await get_client().tracked_item()
        return {"tracked_item": item or None}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def start_tracking(
    task_id: Annotated[str, Field(description="Task ID")],
) -> dict:
    """Start time tracking for a task."""
    try:
        return {"tracking": await get_client().track(task_id, "START")}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def stop_tracking(
    task_id: Annotated[str, Field(description="Task ID")],
) -> dict:
    """Stop time tracking for a task. Note (documented API limitation): the
    task's own times/duration fields are not updated automatically by the
    API."""
    try:
        return {"tracking": await get_client().track(task_id, "STOP")}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_time_tracks(
    task_ids: Annotated[list[str], Field(description="Up to 100 task IDs")],
) -> dict:
    """Get time-tracking history for the given tasks (the source of truth,
    max 100 per call)."""
    try:
        if len(task_ids) > 100:
            return {"error": "Max 100 task_ids per call."}
        return {"tracks": await get_client().tracks(task_ids)}
    except Exception as e:
        return tool_error(e)


# ------------------------------------------------- Kudos & reward points

# The profile response from the reward endpoints contains the whole user
# profile; we only return the point fields to keep responses small and
# relevant.
REWARD_PROFILE_FIELDS = (
    "rewardPointsEarned",
    "rewardPointsSpent",
    "rewardPointsEarnedToday",
    "rewardPointsSpentToday",
    "rewardPointsLastDate",
)


def reward_summary(profile: Any) -> dict:
    if not isinstance(profile, dict):
        return {"raw": profile}
    summary = {k: profile.get(k) for k in REWARD_PROFILE_FIELDS}
    earned = summary.get("rewardPointsEarned") or 0
    spent = summary.get("rewardPointsSpent") or 0
    summary["balance"] = earned - spent
    return summary


@mcp.tool(annotations=READONLY)
async def get_kudos() -> dict:
    """Get kudos, level and kudosRemaining (Marvin's XP system). Note: kudos
    is separate from reward points (the reward currency) — the point balance
    is in get_account_info. nextMultiplier only exists in /me, not here
    (known limitation, MarvinAPI issue #5)."""
    try:
        return {"kudos": await get_client().kudos()}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def claim_reward_points(
    points: Annotated[float, Field(description="Number of points to award", gt=0)],
    item_id: Annotated[
        str,
        Field(description="Task ID, or 'MANUAL' for a manual point award"),
    ],
    date: Annotated[
        str | None, Field(description="Date YYYY-MM-DD; omit for today (server timezone)")
    ] = None,
) -> dict:
    """Award reward points for a completed task (or a manual celebration).
    Note: mark_done does not award a task's rewardPoints automatically
    through the API (cf. issue #6 about kudos) — call this tool separately
    afterwards. WARNING: a MANUAL award CANNOT be undone through the API
    (verified live 2026-08-19: unclaim returns 404, negative points are
    rejected with 400). The only compensation is spend_reward_points for the
    same amount (which however inflates the spent statistics) — award MANUAL
    points thoughtfully."""
    try:
        profile = await get_client().claim_reward_points(
            points, item_id, date or local_today()
        )
        return {"reward_points": reward_summary(profile)}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def unclaim_reward_points(
    item_id: Annotated[
        str,
        Field(description="Task ID whose award should be undone (determines the point amount). 'MANUAL' is NOT supported — see description."),
    ],
    date: Annotated[
        str | None, Field(description="Date YYYY-MM-DD; omit for today (server timezone)")
    ] = None,
) -> dict:
    """Undo a point award (e.g. after a misclick, or when the task was
    un-completed with unmark_done). Only works for awards tied to a real
    task ID: Marvin's server stores no entry for MANUAL awards (verified
    live 2026-08-19, /unclaimRewardPoints responds 404 'No such entry').
    Compensate a MANUAL award with spend_reward_points for the same amount
    instead."""
    try:
        if item_id == "MANUAL":
            return {
                "error": (
                    "MANUAL awards cannot be undone through Marvin's API "
                    "(the server stores no entry to look up). Compensate "
                    "with spend_reward_points for the same amount instead."
                )
            }
        profile = await get_client().unclaim_reward_points(
            item_id, date or local_today()
        )
        return {"reward_points": reward_summary(profile)}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def spend_reward_points(
    points: Annotated[float, Field(description="Number of points to spend", gt=0)],
    date: Annotated[
        str | None, Field(description="Date YYYY-MM-DD; omit for today (server timezone)")
    ] = None,
) -> dict:
    """Spend reward points on a reward. Note (verified live): the API
    responds 500 Internal Server Error if the balance would go negative —
    check the balance (get_account_info) before large purchases."""
    try:
        profile = await get_client().spend_reward_points(
            points, date or local_today()
        )
        return {"reward_points": reward_summary(profile)}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=DESTRUCTIVE)
async def reset_reward_points() -> dict:
    """Reset reward points PERMANENTLY: deletes the whole earn/spend history
    and sets the balance to 0 (Full Access Token). CANNOT be undone — only
    use when the user explicitly asks for it."""
    try:
        profile = await get_client().reset_reward_points()
        return {"reward_points": reward_summary(profile)}
    except Exception as e:
        return tool_error(e)


# ------------------------------------------------------------------ Misc


@mcp.tool(annotations=READONLY)
async def get_labels() -> dict:
    """Get all labels (for label_ids when creating/filtering)."""
    try:
        labels = await get_client().labels()
        return {"count": len(labels), "labels": labels}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_goals() -> dict:
    """Get all goals with status and check-in data."""
    try:
        goals = await get_client().goals()
        return {"count": len(goals), "goals": goals}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_reminders() -> dict:
    """Get all server-side reminders (push notifications to the phone).
    Requires the Full Access Token."""
    try:
        return {"reminders": await get_client().reminders()}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def set_reminder(
    title: Annotated[str, Field(description="Text shown in the notification (max 200 chars)")],
    time_unix_seconds: Annotated[int, Field(description="Unix time (seconds) for the reminder")],
    reminder_id: Annotated[
        str | None,
        Field(description="Custom ID; randomized otherwise. Do NOT use a task ID here — see description."),
    ] = None,
) -> dict:
    """Set a standalone push reminder (type 'M', requires the Marvin mobile
    app to be logged in). WARNING — data integrity: a task reminder in Marvin
    consists of TWO writes that only the app keeps in sync — reminder fields
    on the task document itself (taskTime, reminderTime, reminderOffset,
    snooze, autoSnooze) AND a server-side entry via /reminder/set. This tool
    only writes the server-side entry. Setting reminder_id to a task ID
    therefore does NOT link the reminder to the task in the app's UI, and
    risks an orphaned/inconsistent server-side entry (only visible through
    get_reminders). Task-linked reminders are set in the Marvin app; use
    this tool for standalone reminders only."""
    try:
        reminder = {
            "time": time_unix_seconds,
            "offset": 0,
            "reminderId": reminder_id or str(uuid.uuid4()),
            "type": "M",
            "title": title[:200],
            "snooze": 9,
            "autoSnooze": False,
            "canTrack": False,
        }
        return {"result": await get_client().set_reminders([reminder])}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_reminder(
    reminder_ids: Annotated[list[str], Field(description="IDs of reminders to delete")],
) -> dict:
    """Delete one or more server-side reminders. Note: for a reminder that
    belongs to a task (set in the app), only the server-side entry is removed
    — the task document's reminder fields are not cleared, so the app may
    show it as active and recreate it. Prefer using this against standalone
    reminders (type 'M') or to clean up orphaned entries from get_reminders."""
    try:
        return {"result": await get_client().delete_reminders(reminder_ids)}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def create_event(
    title: Annotated[str, Field(description="Event title")],
    start_iso: Annotated[str, Field(description="Start time, ISO 8601 with timezone, e.g. 2026-08-20T14:30:00+02:00")],
    length_minutes: Annotated[int, Field(description="Length in minutes", gt=0)],
    note: Annotated[str | None, Field(description="Note (markdown)")] = None,
) -> dict:
    """EXPERIMENTAL: Create a calendar event. Calendar sync happens in the
    client — the Marvin app must be running on some device for the event to
    sync onwards to an external calendar."""
    try:
        data: dict[str, Any] = {
            "title": title,
            "start": start_iso,
            "length": length_minutes * 60_000,
        }
        if note:
            data["note"] = note
        return {"created": await get_client().add_event(data)}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_account_info() -> dict:
    """Get account info (/me): email, tracking status, etc."""
    try:
        return {"account": await get_client().me()}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_rate_limit_status() -> dict:
    """Show how many Marvin API calls have been made today (budget 1440/day,
    shared by all tools)."""
    return {
        "calls_today": _limiter.calls_today if _limiter else 0,
        "daily_limit": 1440,
    }
