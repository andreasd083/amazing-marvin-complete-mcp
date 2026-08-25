"""MCP server for Amazing Marvin with complete public-API coverage.

34 tools covering all ~31 documented public endpoints: core CRUD + priority,
habits, time blocks (read + experimental create), time tracking, labels,
goals, reminders, and kudos/reward points. Deliberately no Smart List /
task-picking logic — Marvin's own Spotlight does the picking.

Many tool descriptions carry warnings and behavioral notes verified against
the live API (2026-08-19); see the "Marvin API quirks & findings" section
of the README for the full list.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from .client import MarvinClient, MarvinError, local_today
from .config import Settings, load_settings
from .ratelimit import DailyBudgetExceeded, QueueTimeout, RateLimiter

logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP(name="amazing-marvin")

_client: MarvinClient | None = None
_limiter: RateLimiter | None = None


def get_client() -> MarvinClient:
    if _client is None:
        raise RuntimeError("Client is not initialized (run via __main__)")
    return _client


def init(settings: Settings, transport=None) -> None:
    """Initialize the global client + limiter. `transport` is used in tests."""
    global _client, _limiter
    _limiter = RateLimiter(state_file=settings.state_dir / "ratelimit-state.json")
    _client = MarvinClient(settings, _limiter, transport=transport)


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
        Field(description="ID of the category/project the task belongs in (from get_categories). Omit for the Inbox."),
    ] = None,
    day: Annotated[
        str | None,
        Field(description="Schedule on date YYYY-MM-DD, or 'today'. Omit for unscheduled."),
    ] = None,
    priority: Annotated[
        int | None, Field(description="Priority 1-3 (3=red/highest, 2=orange, 1=yellow)", ge=1, le=3)
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
    time_estimate_minutes, label_ids."""
    try:
        data: dict[str, Any] = {"title": title, "done": False}
        if parent_id:
            data["parentId"] = parent_id
        if day:
            data["day"] = local_today() if day == "today" else day
        if priority is not None:
            data["isStarred"] = priority
        if frog is not None:
            data["isFrogged"] = frog
        if note:
            data["note"] = note
        if label_ids:
            data["labelIds"] = label_ids
        if due_date:
            data["dueDate"] = due_date
        if time_estimate_minutes is not None:
            data["timeEstimate"] = time_estimate_minutes * 60_000
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
    ('YYYY-MM-DD_<recurringTaskId>'), so no duplicates can occur."""
    try:
        return {"completed": await get_client().mark_done(item_id)}
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
    unclaim_reward_points."""
    try:
        result = await get_client().update_doc(
            item_id, make_setters({"done": False, "doneAt": None})
        )
        return {"updated": result}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def update_task(
    item_id: Annotated[str, Field(description="ID of the task/project")],
    title: Annotated[str | None, Field(description="New title")] = None,
    parent_id: Annotated[str | None, Field(description="Move to category/project ID")] = None,
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
) -> dict:
    """Update fields on an existing task via /doc/update (Full Access Token).
    For priority, use set_priority. Always complete tasks via mark_done,
    never here. Note on recurring tasks: never edit recurrence rules here —
    neither on a generated instance (recurring=true, _id 'YYYY-MM-DD_<id>')
    nor on the generator document. Do that editing in the Marvin app. Simple
    field changes (title, note) on a single instance are fine.
    Note: Marvin's server can sporadically respond 500 on /doc/update
    (transient and atomic — no partial write); just retry."""
    try:
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        if parent_id is not None:
            fields["parentId"] = parent_id
        if day is not None:
            fields["day"] = local_today() if day == "today" else day
        if note is not None:
            fields["note"] = note
        if due_date is not None:
            fields["dueDate"] = due_date or None
        if label_ids is not None:
            fields["labelIds"] = label_ids
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
        Field(description="Priority: 3=red/highest, 2=orange, 1=yellow, 0=remove", ge=0, le=3),
    ] = None,
    frog: Annotated[
        int | None,
        Field(description="Frog: 3=monster, 2=baby, 1=normal, 0=remove", ge=0, le=3),
    ] = None,
) -> dict:
    """Set or change priority (isStarred) and/or the frog marker on an
    existing task. Requires the Full Access Token."""
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
        return {"deleted": await get_client().delete_doc(item_id)}
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
async def get_children(
    parent_id: Annotated[
        str,
        Field(description="Category/project ID, 'unassigned' for the Inbox, or 'root' for the top level"),
    ],
) -> dict:
    """Get open tasks and subprojects in a category/project. Returns direct
    children only — call again for deeper levels."""
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
) -> dict:
    """Create a category (via /doc/create, Full Access Token) or a project
    (via /addProject). Categories can contain categories; projects cannot.

    Note: project titles must not contain '#word' — /addProject has the same
    corruption bug as /addTask (the string is stored unresolved as parentId
    and the project becomes invisible) but ignores the X-Auto-Complete
    header (verified against the live API 2026-08-20), so the client blocks
    it locally before any API call. Category titles are unaffected
    (/doc/create parses nothing)."""
    try:
        if kind == "project":
            data: dict[str, Any] = {"title": title, "parentId": parent_id, "done": False}
            if note:
                data["note"] = note
            return {"created": await get_client().add_project(data)}
        # Own _id: /doc/create does not echo back the server-generated id
        # (verified against the live API 2026-08-19), so we set it ourselves
        # in order to be able to return it.
        doc: dict[str, Any] = {
            "_id": uuid.uuid4().hex,
            "db": "Categories",
            "type": "category",
            "title": title,
            "parentId": parent_id,
            "createdAt": now_ms(),
        }
        if note:
            doc["note"] = note
        return {"created": await get_client().create_doc(doc)}
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
