"""MCP server for Amazing Marvin with complete public-API coverage.

50 tools covering all ~31 documented public endpoints (plus the undocumented
/doneItems): core CRUD + priority, recurring tasks, notebooks,
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

import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from . import chain as chainmod
from . import taskreminders as tr
from .chain import Chain, ChainError
from .client import MarvinClient, MarvinError, local_today
from .config import TIMEZONE, Settings, load_settings
from .notes import protect_markdown, protection_note
from .ratelimit import DailyBudgetExceeded, QueueTimeout, RateLimiter

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Amazing Marvin MCP — 50 tools. Areas: tasks (create/update/mark_done/"
    "unmark_done/delete_task, mark_project_done, set_priority, get_today/due/done_items; size, "
    "subtasks, times and reminders in the app's form), recurring tasks "
    "(create/update_recurring_task), notebooks (get_notebooks, get_notebook, "
    "search_notebooks, create/update/delete_notebook, create/update/"
    "delete_notebook_page), structure (get_categories, get_children, create/update/"
    "convert_category_or_project, get_labels), habits (list_habits, get_habit, "
    "record_habit), time (get_today_time_blocks, create_time_block, start/"
    "stop_tracking, get_tracked_item, get_time_tracks), rewards (get_kudos, "
    "claim/unclaim/spend/reset_reward_points), reminders (get/set/delete_reminder), "
    "goals (get_goals), account (get_account_info, test_connection, get_rate_limit_status), "
    "calendar (create_event). Cannot via MCP but in the app: create habits, labels "
    "and goals. Notes: bullets with '*', checklists with '- [ ]'. "
    "Call list_capabilities for the full map with can/cannot per tool before "
    "assuming something is impossible. Marvin budget: 1440 calls/day, at most one "
    "call every three seconds."
)

mcp: FastMCP = FastMCP(name="amazing-marvin", instructions=INSTRUCTIONS)

_client: MarvinClient | None = None
_limiter: RateLimiter | None = None

# Cache for get_done_items: (date, lookback) -> (expires monotonic, result).
# Only complete results are cached; a daily-summary routine may run several
# times a day and a cold run costs lookback+1 read calls.
DONE_CACHE_TTL_S = 30 * 60
_done_cache: dict[tuple[str, int], tuple[float, dict]] = {}
# Modules with their own caches (tools_notebooks) register their reset here.
RESET_HOOKS: list = []


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
    _missed_cache.clear()
    for hook in RESET_HOOKS:
        hook()


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


# ---------------------------------------------------------------- Helpers (task form)

# Size (T-shirt): XS-XL is stored as taskSize 1-5, removed = false (read from
# the app's data, 2026-10; projects use the same field).
SIZES = {"XS": 1, "S": 2, "M": 3, "L": 4, "XL": 5}
SizeArg = Literal["XS", "S", "M", "L", "XL", ""]

FROG_DESC = (
    "Dread/frog 1-4 (read in the app, 2026-10): 1=I dread this a little (A little), "
    "2=I dread this (Dread), 3=I dread this a lot (A lot), 4=This might kill me "
    "(Toxic — requires 'Enable toxic frog level' in the account; Simple mode shows only one level)"
)

TIME_DESC = (
    "Clock time HH:MM (requires a day). Writes the app's form: taskTime, title prefix "
    "'HH:MM ', reminderTime and a server entry of type T — the app reminds at that time. "
    "'' removes the time (update_task)"
)
REMINDERS_DESC = (
    "Own reminders: 'HH:MM' on the task's day, or 'before:N' = N minutes before "
    "the clock time. Stored as the reminders list + one server entry of type M per reminder; "
    "before values are saved as an absolute time, as in the app. Without a day, today applies"
)


def size_value(size: str) -> int | bool:
    if size == "":
        return False
    return SIZES[size]


def build_setters(fields: dict[str, Any], raw: list[dict] | None = None) -> list[dict]:
    """Like make_setters, plus raw setters (dotted keys for subtasks).
    Raw keys under subtasks.* are stamped as fieldUpdates.subtasks (the
    app's form when a subtask is added)."""
    ts = now_ms()
    setters: list[dict] = []
    for key, val in fields.items():
        setters.append({"key": key, "val": val})
        setters.append({"key": f"fieldUpdates.{key}", "val": ts})
    for s in raw or []:
        setters.append(s)
    if any(str(s["key"]).startswith("subtasks.") for s in raw or []):
        setters.append({"key": "fieldUpdates.subtasks", "val": ts})
    setters.append({"key": "updatedAt", "val": ts})
    return setters


def subtask_setters(
    existing: dict | None,
    add: list[str] | None = None,
    done_ids: list[str] | None = None,
    rename: dict[str, str] | None = None,
) -> list[dict]:
    """Subtasks with dotted keys: {_id (13 characters), title, done, rank},
    ranked after the existing ones."""
    existing = existing or {}
    setters: list[dict] = []
    ranks = [v.get("rank") or 0 for v in existing.values() if isinstance(v, dict)]
    rank = max(ranks + [0])
    for title in add or []:
        rank += 1
        sid = tr.rid13()
        setters.append({"key": f"subtasks.{sid}", "val": {"_id": sid, "title": clean_title(title), "done": False, "rank": rank}})
    ts = now_ms()
    for sid in done_ids or []:
        if sid not in existing:
            raise MarvinError(f"Subtask {sid!r} does not exist on the task.")
        setters.append({"key": f"subtasks.{sid}.done", "val": True})
        setters.append({"key": f"subtasks.{sid}.doneAt", "val": ts})
    for sid, title in (rename or {}).items():
        if sid not in existing:
            raise MarvinError(f"Subtask {sid!r} does not exist on the task.")
        setters.append({"key": f"subtasks.{sid}.title", "val": clean_title(title)})
    return setters


def is_instance(doc: dict) -> bool:
    return bool(doc.get("recurring") or doc.get("recurringTaskId"))


def protected(note: str | None, notes: list[str]) -> str | None:
    text, changed = protect_markdown(note)
    msg = protection_note(changed)
    if msg:
        notes.append(msg)
    return text


def child_kind(doc: dict) -> str:
    """A child's kind in mark_project_done and delete_task: only 'task' is closed."""
    if doc.get("db") == "Categories":
        return doc.get("type") or "category"
    if doc.get("habitId"):
        return "habit_task"
    if is_instance(doc):
        return "occurrence"
    return "task"


def child_summary(doc: dict) -> dict:
    return {"id": doc.get("_id"), "title": doc.get("title"), "kind": child_kind(doc)}


def iso_day(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return None


async def read_doc(c: Chain, item_id: str, label: str = "read the document") -> dict:
    doc = await c.step(label, lambda: get_client().get_doc(item_id))
    return doc if isinstance(doc, dict) else {}


def day_noon_ms(day: str) -> int:
    """12:00 on the day — the app's time for a habit record."""
    dt = datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=TIMEZONE)
    return int(dt.timestamp() * 1000)


async def clear_task_reminders(c: Chain, doc: dict, item_id: str, *, clear_list: bool) -> list[str]:
    """Clean up server entries T (the task's id) and M (ids from reminders)
    and clear reminderTime; the reminders list is cleared only when
    clear_list (deletion). Returns the ids removed on the server."""
    if not tr.has_reminder_state(doc):
        return []
    fields: dict[str, Any] = {"reminderTime": None}
    if clear_list:
        fields["reminders"] = []
    await c.step("clear the reminder fields", lambda: get_client().update_doc(item_id, make_setters(fields)))
    ids = [item_id] + tr.reminder_ids_of(doc)
    await c.step("remove the server entries", lambda: get_client().delete_reminders(ids))
    return ids


async def write_time_and_reminders(
    c: Chain, item_id: str, title: str, day: str | None, time_hhmm: str | None, reminders: list[str] | None,
    existing_doc: dict | None = None, result_box: dict | None = None,
) -> tuple[dict, list[str]]:
    """Fields + server entries for a clock time and own reminders in the app's form.
    time_hhmm None = untouched, '' = remove. reminders None = untouched, [] = remove all."""
    doc = existing_doc or {}
    notes: list[str] = []
    fields: dict[str, Any] = {}
    posts: list[dict] = []
    delete_ids: list[str] = []
    cur_title = title
    task_time = doc.get("taskTime") or None
    if time_hhmm is not None:
        if time_hhmm == "":
            fields.update(tr.cleared_time_fields(cur_title))
            delete_ids.append(item_id)
            task_time = None
            notes.append("Clock time removed in the app's form; own reminders remain.")
        else:
            tr.check_time(time_hhmm)
            if not day:
                raise MarvinError("A clock time requires a day — provide day (Time requires a day in the app).")
            fields.update(tr.time_fields(day, time_hhmm, cur_title))
            posts.append(tr.server_t(item_id, fields["title"], fields["reminderTime"]))
            task_time = time_hhmm
            own = doc.get("reminders") or []
            if own and reminders is None:
                notes.append(
                    "Own reminders (including 'X min before') keep their absolute time when the clock time "
                    f"changes, as in the app: {[r.get('time') for r in own]}."
                )
        cur_title = fields.get("title", cur_title)
    if reminders is not None:
        rday = day or local_today()
        delete_ids.extend(tr.reminder_ids_of(doc))
        new_list = []
        for spec in reminders:
            t = tr.resolve_reminder(spec, rday, task_time)
            rid = tr.rid13()
            new_list.append({"reminderId": rid, "time": t})
            posts.append(tr.server_m(item_id, rid, cur_title, t))
        fields["reminders"] = new_list
        if not day:
            notes.append("The task has no day: the app also shows the reminder's time under Time.")
    if fields:
        written = await c.step("write clock time and reminders", lambda: get_client().update_doc(item_id, make_setters(fields)))
        if result_box is not None:
            result_box["doc"] = written
    if delete_ids:
        await c.step("remove old server entries", lambda: get_client().delete_reminders(delete_ids))
    if posts:
        await c.step("write server entries", lambda: get_client().set_reminders(posts))
    return fields, notes


# Filter for hidden occurrences (If you miss one). There is no bulk read of
# recurring tasks (/recurringTasks and similar return 404, verified against
# the live API, 2026-10), so the mode is read per recurring task with /doc —
# but only for recurring tasks with an open occurrence before today, the only
# ones that can be hidden. The cache is valid until local midnight (the
# configured timezone, MARVIN_TIMEZONE) and is cleared at once when this
# server itself writes the recurring task (invalidate_missed_mode). Changes
# made in the app therefore take effect by the next day at the latest.
_missed_cache: dict[str, tuple[str, str]] = {}  # gid -> (day, missedMode)


def invalidate_missed_mode(gen_id: str | None) -> None:
    if gen_id:
        _missed_cache.pop(gen_id, None)


async def missed_modes(gen_ids: set[str]) -> tuple[dict[str, str], list[str], int]:
    """Returns (modes, failed ids, number of /doc reads)."""
    modes: dict[str, str] = {}
    failed: list[str] = []
    reads = 0
    today = local_today()
    for gid in sorted(gen_ids):
        hit = _missed_cache.get(gid)
        if hit and hit[0] == today:
            modes[gid] = hit[1]
            continue
        reads += 1
        try:
            gen = await get_client().get_doc(gid)
        except (MarvinError, DailyBudgetExceeded, QueueTimeout):
            failed.append(gid)
            continue
        mode = (gen.get("missedMode") or "") if isinstance(gen, dict) else ""
        _missed_cache[gid] = (today, mode)
        modes[gid] = mode
    return modes, failed, reads


def hidden_occurrences(items: list, modes: dict[str, str], target: str, kind: str) -> set[str]:
    """Ids of occurrences the app hides: carry shows the oldest open one,
    skip (day view only) shows the date's own (as the app does)."""
    by_gen: dict[str, list[dict]] = {}
    for it in items:
        if isinstance(it, dict) and it.get("recurringTaskId") and not it.get("done"):
            by_gen.setdefault(it["recurringTaskId"], []).append(it)
    hidden: set[str] = set()
    for gid, occ in by_gen.items():
        mode = modes.get(gid)
        if mode == "carry" and len(occ) > 1:
            occ_sorted = sorted(occ, key=lambda o: str(o.get("day")))
            hidden.update(str(o.get("_id")) for o in occ_sorted[1:])
        elif mode == "skip" and kind == "today":
            hidden.update(str(o.get("_id")) for o in occ if o.get("day") != target)
    return hidden


async def filtered_listing(items: list, target: str, kind: str, include_hidden: bool, head: dict) -> dict:
    today = local_today()
    gen_ids = {
        it["recurringTaskId"] for it in items
        if isinstance(it, dict) and it.get("recurringTaskId") and not it.get("done")
        and isinstance(it.get("day"), str) and it["day"] < today
    }
    modes, failed, reads = await missed_modes(gen_ids) if gen_ids else ({}, [], 0)
    logger.debug("Filter %s: %d recurring-task reads (%d candidates)", kind, reads, len(gen_ids))
    hidden = hidden_occurrences(items, modes, target, kind)
    shown = items if include_hidden else [it for it in items if str(it.get("_id")) not in hidden]
    out: dict[str, Any] = {**head, "count": len(shown), "items": shown, "hidden_count": len(hidden)}
    if hidden:
        out["hidden"] = [
            {"id": it.get("_id"), "title": it.get("title"), "day": it.get("day")}
            for it in items if str(it.get("_id")) in hidden
        ]
        out["hidden_included"] = include_hidden
    if any(m == "" for m in modes.values()):
        out["assumption"] = "Recurring tasks using the account default ('Use my default') are assumed to be Keep them all and are not filtered."
    if failed:
        out["filter_warning"] = (
            f"The mode could not be read for {len(failed)} recurring task(s); their occurrences are shown "
            "rather than hidden (unfiltered)."
        )
    out["recurring_reads"] = reads
    habit_ids = [it.get("_id") for it in shown if isinstance(it, dict) and it.get("habitId")]
    if habit_ids:
        out["habit_task_ids"] = habit_ids
    return out


def tool_error(exc: Exception) -> dict:
    if isinstance(exc, ChainError):
        return exc.as_dict()
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
    """Check that the API token works against Marvin's API.
    Returns OK if the apiToken
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
        Field(description="ID of the category/project the task belongs in (from get_categories). Omit for the Inbox. NOTE: the server does not validate the ID — a wrong parentId yields an orphan reachable only via date reads (live-tested 2026-08-29); repaired by running FIX_CYCLES() in the app's console"),
    ] = None,
    day: Annotated[
        str | None,
        Field(description="Schedule on date YYYY-MM-DD, 'today', or 'unassigned' (= unscheduled, same as omitting). Same rules as update_task."),
    ] = None,
    priority: Annotated[
        int | None,
        Field(description="Priority (isStarred): 3=Most important/red, 2=Very important/orange, 1=Important/yellow, -1=Low priority (down arrow; shown in the app only with 'Enable low priority' on in the Priorities strategy — the value is stored regardless). 0 is not valid here; omit for no priority", ge=-1, le=3),
    ] = None,
    frog: Annotated[int | None, Field(description=FROG_DESC, ge=1, le=4)] = None,
    note: Annotated[str | None, Field(description="Note (markdown; '- ' at line start is written as '* ', see list_capabilities.note_format)")] = None,
    size: Annotated[
        SizeArg | None,
        Field(description="Size XS/S/M/L/XL (taskSize 1-5). XS and XL can be disabled in the account. Written with /doc/update after creation (/addTask drops the field)"),
    ] = None,
    subtasks: Annotated[list[str] | None, Field(description="Subtasks (titles), written after creation")] = None,
    time: Annotated[str | None, Field(description=TIME_DESC)] = None,
    reminders: Annotated[list[str] | None, Field(description=REMINDERS_DESC)] = None,
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
        Field(description="Time block ID (from get_today_time_blocks, or time_block_id from create_time_block). Points the task at a time block; the task then appears under that block's section in Today. Three conditions (verified in the app 2026-09-17, 1.70.0.0, PWA + desktop): (1) the Time Block Sections strategy is on, (2) the day view is grouped by time block (Group by → Group by time block section — set per device, not synced; help article 1950243), (3) the task is scheduled on the block's day (day ≤ that date) — an unscheduled task with the field set is stored but does not appear in Today at all. Without (2) no sections render and the field looks inert. The field is sufficient on its own: the block needs no label/category/smart list, and blocks from create_time_block behave like blocks created in the app. The block's own Smart Time Block mapping (label/category) is a second, independent route that catches matching tasks without this field. The section shows before the block's start time (after its end: untested). The field is not exposed in the app's task settings — the app sets it when a task is added directly inside a block section"),
    ] = None,
) -> dict:
    """Create a task with category, day, priority, size, time and reminders.
    The title is stored verbatim: the server's shortcut parsing is disabled
    (X-Auto-Complete: false), so '#Category', '~15', '*m' and '*p2' are NOT
    parsed — '#' in titles is safe. Use the parameters instead.
    startDate/endDate are set with update_task afterwards (/addTask ignores them).
    Size, subtasks, clock time and reminders are written in a second step
    with /doc/update and /reminder/set in the app's form; if a step fails the
    response says which ones were done (the task then already exists). A
    clock time requires day. Strategy-dependent fields (planned_week/month,
    review_date, backburner, reward_points, the sections) are stored even
    when the strategy is off in the app."""
    try:
        data: dict[str, Any] = {"title": clean_title(title), "done": False}
        if parent_id:
            data["parentId"] = parent_id
        if day:
            check_day(day)
            data["day"] = local_today() if day == "today" else day
        if priority is not None:
            if priority == 0:
                raise MarvinError("priority=0 is not valid when creating — omit the parameter.")
            data["isStarred"] = priority
        if frog is not None:
            data["isFrogged"] = frog
        notes: list[str] = []
        if note:
            data["note"] = protected(note, notes)
        if time is not None:
            tr.check_time(time)
            if not day or day == "unassigned":
                raise MarvinError("A clock time requires a day — provide day (Time requires a day in the app).")
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
        out: dict[str, Any] = {"created": created}
        if notes:
            out["notes"] = notes
        if not (size is not None or subtasks or time or reminders):
            return out
        task_id = created.get("_id") if isinstance(created, dict) else None
        if not task_id:
            out["warning"] = "The task was created but the response had no id; size, subtasks, time and reminders were not written."
            return out
        c = Chain()
        c.completed.append("create the task")
        c.extra["created"] = created
        fields: dict[str, Any] = {}
        if size is not None:
            fields["taskSize"] = size_value(size)
        raw = subtask_setters({}, add=subtasks) if subtasks else []
        if fields or raw:
            await c.step("write size and subtasks", lambda: get_client().update_doc(task_id, build_setters(fields, raw)))
        if time or reminders:
            _, more = await write_time_and_reminders(
                c, task_id, data["title"], iso_day(data.get("day")), time or None, reminders
            )
            out.setdefault("notes", []).extend(more)
        out["completed_steps"] = c.completed
        return out
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mark_done(
    item_id: Annotated[str, Field(description="Task ID (NOT a project — mark_project_done closes those)")],
    value: Annotated[
        float | None,
        Field(description="Habit task with a count only: the value to record (default 1)", gt=0),
    ] = None,
) -> dict:
    """Mark a task as done and clean up its reminders as the app does.
    Reads the task, runs /markDone (configured timezone) and then does what
    the app does: the server entries T and M are removed and reminderTime is
    cleared; the reminders list remains. Projects: /markDone responds 400
    (tasks only); projects are closed with mark_project_done.
    404 = does not exist, 400 = already done or a project.
    Habit task (field habitId): the habit is also recorded with
    /updateHabit at 12:00 on the task's day, and habitRepAt is set. Habit
    with a count (recordType number): value is recorded with the current
    time; the task is checked off only once the day's total reaches the
    target — otherwise it stays open and the response shows the total.
    Recurring occurrence: safe (deterministic id). After completion: the
    next occurrence is created +N days later. With a future day the tool
    does what the app does: the occurrence is moved to today before it is
    checked off, and the next one is counted from today.
    If a step fails the response says which steps were done. Running time
    tracking is stopped and written to task.times. /markDone does not set
    workedOnAt and does not stamp fieldUpdates, which the app does."""
    try:
        c = Chain()
        doc = await read_doc(c, item_id)
        out: dict[str, Any] = {}
        habit_id = doc.get("habitId")
        if habit_id:
            habit = await read_doc(c, habit_id, "read the habit")
            day = iso_day(doc.get("day")) or local_today()
            if habit.get("recordType") == "number":
                v = value if value is not None else 1
                t = now_ms()
                v_send = int(v) if float(v).is_integer() else v
                await c.step("record the habit", lambda: get_client().update_habit(
                    {"habitId": habit_id, "updateDB": True, "time": t, "value": v_send}))
                lo, hi = day_bounds_ms(day)
                hist = habit.get("history") or []
                total = sum(hist[k + 1] for k in range(0, len(hist) - 1, 2) if lo <= hist[k] < hi) + v
                target = habit.get("target") or 1
                out["habit"] = {"habit_id": habit_id, "registered": v, "day_total": total, "target": target}
                if total < target:
                    out["note"] = f"The habit has {total} of {target} today; the task stays open until the target is reached."
                    out["completed_steps"] = c.completed
                    return out
                rep_at = t
            else:
                rep_at = day_noon_ms(day)
                await c.step("record the habit", lambda: get_client().update_habit(
                    {"habitId": habit_id, "updateDB": True, "time": rep_at, "value": 1}))
                out["habit"] = {"habit_id": habit_id, "registered_at": rep_at}
        today = local_today()
        occ_day = iso_day(doc.get("day"))
        if doc.get("echo") and doc.get("recurringTaskId") and occ_day and occ_day > today:
            from . import recurrence as rec

            gen = await read_doc(c, doc["recurringTaskId"], "read the recurring task")
            n = int(gen.get("echoDays") or 1)
            plus = lambda d: (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")
            if rec.ECHO_EARLY_DONE_MODE == "app":
                await c.step("move the occurrence to today", lambda: get_client().update_doc(
                    item_id, make_setters({"day": today})))
                out["echo"] = (f"The occurrence was moved to today as in the app; the next occurrence comes "
                               f"{plus(today)}, counted from today.")
            else:
                out["echo"] = (f"The next occurrence comes {plus(occ_day)}, counted from the occurrence's day "
                               f"{occ_day} — the app would have counted from today ({plus(today)}).")
        result = await c.step("mark done", lambda: get_client().mark_done(item_id))
        remember_done(result)
        out["completed"] = result
        if habit_id:
            await c.step("set habitRepAt", lambda: get_client().update_doc(item_id, make_setters({"habitRepAt": rep_at})))
        removed = await clear_task_reminders(c, doc, item_id, clear_list=False)
        if removed:
            out["reminders_removed"] = removed
        out["completed_steps"] = c.completed
        return out
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def unmark_done(
    item_id: Annotated[str, Field(description="Task ID (not a project — those are reopened in the app)")],
) -> dict:
    """Undo a task's completion, and the habit's record.
    Projects and categories: refuses without writing — a closed project is
    reopened in the app. Sets done=false and clears doneAt via /doc/update
    (Full Access Token); the app's own undo leaves doneAt in place. A task
    that never had a day (firstScheduled 'unassigned' and no
    fieldUpdates.day stamp) got its day from /markDone: the day that
    mark_done set is removed (stored as 'unassigned'; the app's own undo
    writes null — both mean no day). Otherwise the day stays.
    As in the app: the clock time's reminder (T) is not restored — the time
    does not remind until it is set again — but own reminders in the
    reminders list still apply. Habit task: the habit's latest record is
    undone with /updateHabit undo and habitRepAt is cleared, but only if
    the history's last entry is the task's habitRepAt; otherwise the
    response says so and the habit is left untouched (undo always takes the
    latest entry). Open habit task (a partial record on a habit with a
    count): nothing is written — undo the record with record_habit
    undo=true, as the app's undo does. Kudos are not adjusted; reward
    points are undone with unclaim_reward_points. A permanent 500 = the
    document does not exist."""
    try:
        c = Chain()
        doc = await read_doc(c, item_id)
        if doc.get("db") == "Categories":
            return {
                "error": "The document is a project or category; nothing changed. Reopen a closed project in the "
                "app - unmark_done would only reset done and doneAt and leave doneDate, day and workedOnAt.",
                "completed_steps": c.completed,
            }
        fields: dict[str, Any] = {"done": False, "doneAt": None}
        out: dict[str, Any] = {}
        habit_id = doc.get("habitId")
        if habit_id and not doc.get("done"):
            # The app writes nothing on the task for a partial record or its undo.
            return {
                "error": "The habit task is not marked done; nothing changed. Undo a partial record with "
                "record_habit(habit_id, undo=true), which takes the habit's latest entry (as the app's undo does).",
                "habit_id": habit_id,
                "completed_steps": c.completed,
            }
        if habit_id:
            habit = await read_doc(c, habit_id, "read the habit")
            hist = habit.get("history") or []
            last = hist[-2] if len(hist) >= 2 else None
            rep = doc.get("habitRepAt")
            if rep and last == rep:
                await c.step("undo the habit record", lambda: get_client().update_habit(
                    {"habitId": habit_id, "updateDB": True, "undo": True}))
                fields["habitRepAt"] = None
                out["habit_undone"] = True
            else:
                out["habit_warning"] = (
                    "The habit's latest record is not this task's (habitRepAt "
                    f"{rep}, last entry {last}); the habit was not undone. Undo it in the app "
                    "(Habits → right-click → Undo last) if needed."
                )
        day_now = doc.get("day")
        if day_now and day_now != "unassigned" and not doc.get("recurringTaskId"):
            stamped = "day" in (doc.get("fieldUpdates") or {})
            if doc.get("firstScheduled") == "unassigned" and not stamped:
                # /markDone set the day without a stamp; the app's own undo writes null.
                fields["day"] = None
                out["day_note"] = (f"The day {day_now} that mark_done set was removed: the task had no day before "
                                   "it was checked off (stored as 'unassigned'; the app's own undo writes null — "
                                   "both mean no day).")
            elif doc.get("firstScheduled") == "unassigned":
                out["day_note"] = (f"The day {day_now} remains. If the task had no day before it was checked off, "
                                   "remove it with update_task(day='unassigned').")
        result = await c.step("undo the completion", lambda: get_client().update_doc(item_id, make_setters(fields)))
        forget_done(item_id)
        out["updated"] = result
        if doc.get("taskTime"):
            out["note"] = (
                "The clock time's reminder is not restored (as in the app); own reminders still apply."
            )
        out["completed_steps"] = c.completed
        return out
    except Exception as e:
        return tool_error(e)


def close_code(project_id: str, kids: list[dict]) -> str:
    """The code from step 1: computed from the project's id and the open
    children's ids, so that step 2 closes exactly what was shown."""
    ids = ",".join(sorted(str(k.get("_id")) for k in kids))
    return hashlib.sha256(f"{project_id}|{ids}".encode()).hexdigest()[:8]


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def mark_project_done(
    project_id: Annotated[str, Field(description="ID of the project to close")],
    confirm_code: Annotated[
        str | None,
        Field(description="The code from step 1. Without a code only the open children and the code are shown; nothing is closed"),
    ] = None,
) -> dict:
    """Close a project and its open tasks, in two steps.
    Step 1 (without confirm_code): reads the project and its open children,
    lists them with the warning that they too will be marked done, and
    returns a code. Nothing is written. Step 2 (confirm_code from step 1):
    reads the children again; if the code no longer matches, nothing is
    closed and the response gives the new list and code. Otherwise each
    open task is checked off like mark_done (/markDone and reminder
    cleanup) and the project is closed last with a /doc/update of the
    app's five fields: done, doneAt and workedOnAt (now), day (the
    project's day, or today if it has none) and doneDate (same as day) — as
    the app does. The deadline (dueDate) is not touched. The app itself
    refuses to close a project with open children; this tool closes them
    on purpose. The children are checked off with /markDone, which does not
    set workedOnAt and does not stamp fieldUpdates — the app sets
    workedOnAt on the children.
    Stops in step 1 without a code if an open child is a sub-project, a
    habit task or an occurrence of a recurring task — close those first
    (the app's behavior there is untested). Completed projects do not show
    in get_categories. Undo in the app; unmark_done refuses projects.
    If a step fails the response says which were done; the project then
    stays open. Cost: step 1 two calls; step 2 three, plus one per task and
    two more per task with reminders."""
    try:
        c = Chain()
        doc = await read_doc(c, project_id, "read the project")
        if doc.get("db") != "Categories" or doc.get("type") != "project":
            return {"error": "The document is not a project; tasks are closed with mark_done, categories are not closed.",
                    "completed_steps": c.completed}
        if doc.get("done"):
            return {"error": "The project is already done; nothing changed.", "completed_steps": c.completed}
        kids = await c.step("read the children", lambda: get_client().children(project_id))
        kids = [k for k in kids or [] if isinstance(k, dict)]
        listing = [child_summary(k) for k in kids]
        head = {"project": {"id": project_id, "title": doc.get("title")}, "open_children": listing}
        blocked = [x for x in listing if x["kind"] != "task"]
        if blocked:
            return {**head, "error": "The project has open children this tool does not close (sub-projects, "
                    "habit tasks, occurrences); nothing closed. Close them first.",
                    "blocking_children": blocked, "completed_steps": c.completed}
        code = close_code(project_id, kids)
        step1 = {**head, "confirm_code": code,
                 "warning": (f"These {len(kids)} tasks will also be marked done." if kids
                             else "The project has no open children."),
                 "next": "Close with mark_project_done(project_id, confirm_code) after the user's yes."}
        if confirm_code is None:
            return {**step1, "needs_confirm": True, "completed_steps": c.completed}
        if confirm_code != code:
            return {**step1, "needs_confirm": True, "completed_steps": c.completed,
                    "error": "The code does not match the project's open children now; nothing closed. Show the "
                    "list again and use the new code."}
        out: dict[str, Any] = {"project": head["project"]}
        closed: list[str] = []
        removed: list[str] = []
        c.extra["children_done"] = closed
        for k in kids:
            kid = k["_id"]
            result = await c.step(f"mark done {kid}", lambda kid=kid: get_client().mark_done(kid))
            remember_done(result)
            closed.append(kid)
            removed += await clear_task_reminders(c, k, kid, clear_list=False)
        ts = now_ms()
        day = iso_day(doc.get("day")) or local_today()
        fields = {"done": True, "doneAt": ts, "doneDate": day, "day": day, "workedOnAt": ts}
        result = await c.step("close the project", lambda: get_client().update_doc(project_id, make_setters(fields)))
        out.update({"closed": result, "children_done": closed, "completed_steps": c.completed,
                    "note": "The project no longer shows in get_categories (as in the app)."})
        if removed:
            out["reminders_removed"] = removed
        return out
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def update_task(
    item_id: Annotated[str, Field(description="Task ID")],
    title: Annotated[str | None, Field(description="New title")] = None,
    parent_id: Annotated[str | None, Field(description="Move to category/project ID (not validated by the server — a wrong ID yields an orphan, live-tested 2026-08-29; repaired by running FIX_CYCLES() in the app's console)")] = None,
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
        Field(description="Plan into a week: the week's Monday YYYY-MM-DD (Planning Ahead strategy; verified in the app 2026-08-29 — also shows in the month view), '' removes. The app's view: clearing propagates server-side, but with Planning Ahead on (2026-08-29) the task stayed in the month view even after switching views — ask the user to reload the client (F5 in the web app/PWA, restart of the desktop app) before a missing render is taken for an error"),
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
        Field(description="Time block ID (from get_today_time_blocks, or time_block_id from create_time_block), '' removes. Shows under the block's section in Today when Time Block Sections is on, the day view is grouped by time block (per device, not synced) and the task is scheduled on the block's day — see create_task"),
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
    size: Annotated[SizeArg | None, Field(description="Size XS/S/M/L/XL (taskSize 1-5), '' removes (false)")] = None,
    subtasks_add: Annotated[list[str] | None, Field(description="New subtasks (titles), added last")] = None,
    subtasks_done: Annotated[list[str] | None, Field(description="Subtask ids to check off (ids from the task's subtasks)")] = None,
    subtasks_rename: Annotated[dict[str, str] | None, Field(description="Subtask id → new title")] = None,
    time: Annotated[str | None, Field(description=TIME_DESC)] = None,
    reminders: Annotated[list[str] | None, Field(description=REMINDERS_DESC + ". Replaces the list; [] removes all own reminders")] = None,
) -> dict:
    """Update a task: title, day, time, reminders, size, subtasks.
    /doc/update (Full Access Token). Categories/projects:
    update_category_or_project; priority and frog: set_priority;
    completion: mark_done.
    Changing the day does what the app does: reminderTime and the server
    entry T move to the new day, own reminders (M) stay and are listed in
    the response. New clock time: new T, own reminders (including 'X min
    before') keep their time. time='' removes the clock time in the app's
    form. If the day is removed (day='unassigned') the clock time and T are
    removed as in the app; own reminders stay. Subtasks are written with
    dotted keys. parent_id or subtasks on a recurring occurrence give a
    warning: the recurring task stays as it is, so future occurrences
    follow it — change it with update_recurring_task. Strategy-dependent
    fields are stored even when the strategy is off in the app. A sporadic
    500 on /doc/update is transient (retried once); a permanent 500 = the
    document does not exist."""
    try:
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = clean_title(title)
        if parent_id is not None:
            fields["parentId"] = parent_id
        if day is not None:
            check_day(day)
            fields["day"] = local_today() if day == "today" else day
        notes: list[str] = []
        if note is not None:
            fields["note"] = protected(note, notes)
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
        if size is not None:
            fields["taskSize"] = size_value(size)
        wants_sub = bool(subtasks_add or subtasks_done or subtasks_rename)
        if not fields and not wants_sub and time is None and reminders is None:
            return {"error": "No fields to update were given."}
        c = Chain()
        out: dict[str, Any] = {}
        needs_doc = wants_sub or time is not None or reminders is not None or "day" in fields or "parentId" in fields
        doc = await read_doc(c, item_id) if needs_doc else {}
        if doc and is_instance(doc) and ("parentId" in fields or wants_sub):
            notes.append(
                "This is an occurrence of a recurring task: the change applies only to this occurrence. Future "
                "occurrences follow the recurring task — change it with update_recurring_task."
            )
        raw = subtask_setters(doc.get("subtasks"), subtasks_add, subtasks_done, subtasks_rename) if wants_sub else []
        new_day = iso_day(fields.get("day")) if "day" in fields else iso_day(doc.get("day"))
        # Day change with an existing clock time: move reminderTime and T.
        moved_t = None
        clear_t = False
        if "day" in fields and time is None and doc.get("taskTime"):
            if new_day:
                fields["reminderTime"] = tr.epoch_s(new_day, doc["taskTime"])
                moved_t = fields["reminderTime"]
                own = doc.get("reminders") or []
                if own and reminders is None:
                    notes.append(
                        "Own reminders stay at their old times, as in the app: "
                        f"{[r.get('time') for r in own]}. Move them with reminders=[...] or remove them with reminders=[]."
                    )
            else:
                # As the app does: the clock time is removed, T is removed on
                # the server, own reminders (M) stay.
                base = fields.get("title") or doc.get("title") or ""
                fields.update(tr.cleared_time_fields(base))
                fields.update({"autoSnoozeChosen": None, "remind": None})
                clear_t = True
                notes.append(
                    "The day was removed: the clock time was removed as in the app. Own reminders stay "
                    f"in reminders: {[r.get('time') for r in (doc.get('reminders') or [])]}; their server entries "
                    "are not touched (the server has already removed an entry whose time has passed)."
                )
        if fields or raw:
            result = await c.step("update", lambda: get_client().update_doc(item_id, build_setters(fields, raw)))
            out["updated"] = result
        if clear_t:
            await c.step("remove the server entry T", lambda: get_client().delete_reminders([item_id]))
        if moved_t is not None:
            title_now = fields.get("title") or doc.get("title") or ""
            await c.step("move the server entry T", lambda: get_client().set_reminders([tr.server_t(item_id, title_now, moved_t)]))
        if time is not None or reminders is not None:
            base_title = fields.get("title") or doc.get("title") or ""
            box: dict[str, Any] = {}
            tfields, more = await write_time_and_reminders(
                c, item_id, base_title, new_day, time, reminders, doc, result_box=box)
            notes.extend(more)
            out["time_and_reminders"] = tfields
            if "doc" in box:
                # updated shows the state after the last write.
                out["updated"] = box["doc"]
        if notes:
            out["notes"] = notes
        out["completed_steps"] = c.completed
        return out
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
        Field(description=FROG_DESC + ". 0 removes (stored as false)", ge=0, le=4),
    ] = None,
) -> dict:
    """Set priority (P1-P3 or low) or the dread level on a task.
    Set or change priority (isStarred) and/or the frog/dread level
    (isFrogged 1-4, read in the app 2026-10) on an existing TASK. Requires
    the Full Access Token. The app's four priority levels are stored as
    isStarred 3/2/1/-1; Low priority is shown only with 'Enable low
    priority' on. Does not apply to projects: they use the string field
    priority ('high'/'mid'/'low'); set it via update_category_or_project.
    A permanent 500 = the task does not exist (deleted or wrong ID)."""
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
    item_id: Annotated[str, Field(description="ID of the task (or of a project/category without open children) to delete")],
    scope: Annotated[
        Literal["this", "all"],
        Field(description="Occurrence of a recurring task: 'this' = only this one (default), 'all' = the whole recurring task with all open occurrences (completed ones are kept). The recurring task's own id is accepted only with 'all'"),
    ] = "this",
) -> dict:
    """Delete a task permanently, with no trash and no undo.
    Deletion via the API bypasses the app's trash and cannot be undone —
    use only when the user explicitly wants a deletion. If the task has
    reminders they are cleared and the server entries removed before the
    deletion, so that no notification arrives. An occurrence of a recurring
    task follows the app's two choices: 'this' adds the occurrence's day to
    the recurring task's deletedDates and deletes the occurrence; 'all'
    deletes all open occurrences and then the recurring task, completed
    occurrences are kept. If a step fails the response says which were
    done. THE APP'S VIEW: an open client does not render the deletion —
    reload the client (F5, restart of the desktop app) before the row is
    touched; a write from the app on the stale row recreates the document.
    A recurring task (db RecurringTasks) is deleted with scope 'all', on an
    occurrence or on the recurring task's own id — also before it has any
    occurrences (they are created only when a client opens after the day
    rolls over). Habit recurrences are not deleted here.
    Projects and categories: deleted only without open children — the tool
    reads /children first (one more call) and otherwise refuses, with the
    list, so that no children are left with a parent that does not exist.
    Completed children do not show in /children and are left behind with
    the parent gone. The project's notebook is not deleted (as in the app);
    the response gives its id for delete_notebook."""
    try:
        from . import tools_recurring as rct

        c = Chain()
        doc = await read_doc(c, item_id)
        if doc.get("db") == "Categories":
            kids = await c.step("read the children", lambda: get_client().children(item_id))
            kids = [k for k in kids or [] if isinstance(k, dict)]
            if kids:
                return {
                    "error": f"{'The project' if doc.get('type') == 'project' else 'The category'} has {len(kids)} "
                    "open children; nothing deleted. Move, complete or delete the children first "
                    "(mark_project_done closes a project together with its open tasks).",
                    "open_children": [child_summary(k) for k in kids],
                    "completed_steps": c.completed,
                }
        is_gen = doc.get("db") == "RecurringTasks"
        if is_gen and scope != "all":
            return {"error": "The document is a recurring task; use scope='all' to delete it with all open occurrences."}
        if is_gen and doc.get("recurringType") not in (None, "", "task"):
            return {"error": "Habit recurrences are not deleted here; remove the habit in the app."}
        out: dict[str, Any] = {}
        gen_id = item_id if is_gen else doc.get("recurringTaskId")
        if gen_id and scope == "all":
            gen = doc if is_gen else await read_doc(c, gen_id, "read the recurring task")
            occ = await c.step("read the occurrences", lambda: rct._occurrences({**gen, "_id": gen_id}))
            targets = {o.get("_id"): o for o in occ if not o.get("done")}
            if not is_gen and not doc.get("done"):
                targets.setdefault(item_id, doc)
            with_rem = [d for d in targets.values() if tr.has_reminder_state(d)]
            for d in with_rem:
                await clear_task_reminders(c, d, d["_id"], clear_list=True)
            if with_rem:
                await c.pause(chainmod.DELETE_SYNC_PAUSE_S)
            for oid in targets:
                await c.step(f"delete {oid}", lambda oid=oid: get_client().delete_doc(oid))
                forget_done(oid)
            await c.step("delete the recurring task", lambda: get_client().delete_doc(gen_id))
            invalidate_missed_mode(gen_id)
            out.update({"deleted_occurrences": list(targets), "deleted_recurring_task": gen_id,
                        "note": "Completed occurrences are kept. An open client shows the deletions only after a reload."})
            out["completed_steps"] = c.completed
            return out
        if gen_id:
            gen = await read_doc(c, gen_id, "read the recurring task")
            out["deleted_dates"] = await rct.add_deleted_dates(c, gen_id, gen, [iso_day(doc.get("day"))])
        removed = await clear_task_reminders(c, doc, item_id, clear_list=True)
        if removed:
            await c.pause(chainmod.DELETE_SYNC_PAUSE_S)
            out["reminders_removed"] = removed
        result = await c.step("delete", lambda: get_client().delete_doc(item_id))
        forget_done(item_id)
        out["deleted"] = result
        out["completed_steps"] = c.completed
        out["note"] = "An open Marvin client shows the task until it is reloaded; do not touch the row before then."
        if doc.get("db") == "Categories":
            out["note"] += (" Completed children do not show in /children and remain with the parent gone.")
            if doc.get("notebookId"):
                out["notebook_left"] = doc["notebookId"]
                out["note"] += (f" The notebook {doc['notebookId']} remains on the server (as when the app deletes"
                                " a project); remove it with delete_notebook if it is not needed.")
        return out
    except Exception as e:
        return tool_error(e)


# --------------------------------------------------------------- Reading


@mcp.tool(annotations=READONLY)
async def get_today_items(
    date: Annotated[
        str | None, Field(description="Date YYYY-MM-DD; returns everything open with day <= the date; omit for today (MARVIN_TIMEZONE, else the system timezone)")
    ] = None,
    include_hidden: Annotated[
        bool, Field(description="True = include occurrences the app hides (If you miss one)")
    ] = False,
) -> dict:
    """Get open tasks and projects scheduled today or earlier.
    Open tasks/projects with day <= the date (default today in the
    configured timezone, MARVIN_TIMEZONE), not only exactly on the date.
    Deadline-only items: get_due_items.
    Filters as the app does: for a recurring task with If you miss one =
    Keep just one (carry) only the oldest open occurrence is shown, for
    Only the current one (skip) only the date's own; Keep them all and the
    account default ('', assumed to be Keep them all) show all. The mode is
    read (/doc) only for recurring tasks with an open occurrence before
    today, cached until local midnight; a mode changed in the app takes
    effect by the next day at the latest. If the mode cannot be read the
    occurrences are shown.
    The response gives hidden_count and which were hidden;
    include_hidden=true includes all. Habit tasks (field habitId) are
    listed in habit_task_ids. Recurring occurrences are created the first
    time a Marvin client opens after the day rolls over; read before that,
    today's occurrences are missing, and a recurring task created during
    the day may lack them."""
    try:
        target = date or local_today()
        items = await get_client().today_items(date)
        return await filtered_listing(items, target, "today", include_hidden, {"date": target})
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_due_items(
    by: Annotated[
        str | None, Field(description="Deadline up to and including YYYY-MM-DD; omit for today")
    ] = None,
    include_hidden: Annotated[
        bool, Field(description="True = include occurrences the app hides (Keep just one)")
    ] = False,
) -> dict:
    """Get open tasks and projects with a deadline today or earlier.
    Filters Keep just one (carry) as the app does: only the oldest open
    occurrence per recurring task is shown. Only the current one (skip) is
    not filtered here. The mode is read only for recurring tasks with an
    open occurrence before today and cached until local midnight; changes
    made in the app take effect by the next day at the latest. The response
    gives hidden_count; include_hidden=true includes all."""
    try:
        target = by or local_today()
        items = await get_client().due_items(by)
        return await filtered_listing(items, target, "due", include_hidden, {"by": target})
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
    """Get tasks completed on a given date.
    Tasks completed on a given date (doneAt within that day, configured
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
    """Get open tasks and subprojects in a category or project.
    Returns direct
    children only — call again for deeper levels. Note: orphans (tasks whose
    parentId points to a deleted/non-existent document) do NOT show up under
    'unassigned' — only in get_today_items/get_due_items if they have a
    day/dueDate (live-tested 2026-08-29). Orphans are repaired by running
    FIX_CYCLES() in the app's console (a documented troubleshooting path)."""
    try:
        items = await get_client().children(parent_id)
        return {"parent_id": parent_id, "count": len(items), "items": items}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_categories() -> dict:
    """Get all categories and projects as a hierarchy with id and parentId.
    Get all categories and projects (the whole hierarchy; parentId='root'
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
        Field(description="Icon name with a library prefix, e.g. 'lucide-Rocket' (Lucide, PascalCase) or 'huge-happy' (verified in the app 2026-08-29); the app's picker also allows emoji. Rendered directly on categories; on projects only when Master List → Configure View → 'Show Custom Icon On' includes projects ('Categories & Projects' verified in the app 2026-08-31, 'Just Projects' 2026-09-18; the earlier wording 'projects never render their own icon' was wrong)"),
    ] = None,
    time_estimate_minutes: Annotated[
        int | None,
        Field(description="Time estimate in minutes. The project's own estimate takes precedence; without one the project shows the sum of its children's (per Marvin support)", ge=1),
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
        Field(description="Projects ONLY: schedule on YYYY-MM-DD or 'today' (blocked for categories — a category is never completed)"),
    ] = None,
    due_date: Annotated[
        str | None, Field(description="Projects ONLY: deadline YYYY-MM-DD (blocked for categories — a category is never completed)")
    ] = None,
    priority: Annotated[
        Literal["high", "mid", "low"] | None,
        Field(description="Projects ONLY: priority as a string — high=Most important (red), mid=Very important (orange), low=Important (yellow, the one-star level — NOT the app's 'Low priority', which projects do not have). Projects do not use isStarred (verified live 2026-08-29; mapping verified against the app's code 2026-08-30)"),
    ] = None,
    frog: Annotated[
        int | None,
        Field(description="Projects ONLY: " + FROG_DESC, ge=1, le=4),
    ] = None,
    label_ids: Annotated[
        list[str] | None, Field(description="Label IDs (from get_labels) — categories AND projects: categories have labels, stored in the same field as projects' and rendered in the app (live-tested + verified in the app 2026-09-11)")
    ] = None,
) -> dict:
    """Create a category or project with color, icon, labels and note.
    Create a category (via /doc/create, Full Access Token) or a project
    (via /addProject). Categories can contain categories; projects cannot.
    day/due_date/priority/frog are rejected for kind='category' for a
    structural reason, not a technical one: a category can never be
    completed or checked off, and deadline, scheduling, priority and frog
    belong to things that can be finished — projects and tasks. The API
    accepts the fields on categories (live-tested 2026-09-11) but they are
    not meaningful there (rule 2026-09-11). label_ids applies to both
    categories and projects.
    startDate/endDate
    cannot be set at creation (/addProject ignores them, verified live
    2026-08-29) — use update_category_or_project afterwards.

    Note: project titles must not contain '#word' — /addProject has the same
    corruption bug as /addTask (the string is stored unresolved as parentId
    and the project becomes invisible) but ignores the X-Auto-Complete
    header (verified against the live API 2026-08-20), so the client blocks
    it locally before any API call. Category titles are unaffected
    (/doc/create parses nothing)."""
    try:
        notes: list[str] = []
        note = protected(note, notes)
        if kind == "category":
            rejected = {
                "day": day, "due_date": due_date, "priority": priority,
                "frog": frog,
            }
            given = [name for name, val in rejected.items() if val is not None]
            if given:
                return {
                    "error": (
                        f"The parameters {', '.join(given)} apply to projects only — "
                        "a category is never completed, so deadline, scheduling, "
                        "priority and frog belong to projects and tasks "
                        "(structural rule 2026-09-11). Labels are fine on categories."
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
            created = await get_client().add_project(data)
            return {"created": created, **({"notes": notes} if notes else {})}
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
        if label_ids:
            doc["labelIds"] = label_ids
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
        created = await get_client().create_doc(doc)
        return {"created": created, **({"notes": notes} if notes else {})}
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
        Field(description="Icon name with a library prefix ('lucide-Rocket', 'huge-happy'), '' removes. Rendered directly on categories; on projects only when Master List → Configure View → 'Show Custom Icon On' includes projects ('Categories & Projects' verified in the app 2026-08-31, 'Just Projects' 2026-09-18; the earlier wording 'projects never render their own icon' was wrong)"),
    ] = None,
    time_estimate_minutes: Annotated[
        int | None,
        Field(description="Time estimate in minutes, 0 removes it. On projects the own estimate takes precedence; without one the project shows the sum of its children's (per Marvin support)", ge=0),
    ] = None,
    start_date: Annotated[
        str | None, Field(description="Start date YYYY-MM-DD (Start & End Dates strategy), '' removes")
    ] = None,
    end_date: Annotated[
        str | None, Field(description="Soft deadline YYYY-MM-DD (Start & End Dates strategy), '' removes")
    ] = None,
    planned_week: Annotated[
        str | None,
        Field(description="Plan into a week: the week's Monday YYYY-MM-DD (Planning Ahead strategy), '' removes (the app's view may keep showing it until the client is reloaded — see update_task.planned_week)"),
    ] = None,
    planned_month: Annotated[
        str | None, Field(description="Plan into a month: YYYY-MM (Planning Ahead strategy), '' removes (the app's view may keep showing it until the client is reloaded — see update_task.planned_week)")
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
        Field(description="Projects ONLY: " + FROG_DESC + ". 0 removes", ge=0, le=4),
    ] = None,
    size: Annotated[
        SizeArg | None,
        Field(description="Projects ONLY: size XS/S/M/L/XL (taskSize 1-5), '' removes"),
    ] = None,
    label_ids: Annotated[
        list[str] | None,
        Field(description="New labels (replaces existing ones, [] removes all) — categories AND projects: categories have labels in the same field as projects, stored and rendered (live-tested + verified in the app 2026-09-11)"),
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
    """Update a category or project: labels, color, icon, note, project fields.
    Update fields on an existing CATEGORY or PROJECT via /doc/update
    (Full Access Token). For tasks, use update_task. Fields marked
    'Projects ONLY' (day/due_date/priority/frog/size) are blocked for
    categories: if any of them is given, the tool first reads the document
    (1 extra API call) and refuses if it is a category. The reason is
    structural, not technical: a category can never be completed or
    checked off, and deadline, scheduling, priority and frog belong to
    things that can be finished — projects and tasks. The API accepts the
    fields on categories (live-tested 2026-09-11) but they are not
    meaningful there (rule 2026-09-11). label_ids applies to both
    categories and projects.
    Strategy-dependent fields
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
            "frog": frog, "size": size,
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
                        "the document is a category. A category is never "
                        "completed, so deadline, scheduling, priority and frog "
                        "belong to projects and tasks (structural rule "
                        "2026-09-11); the write is blocked. Labels are fine."
                    )
                }
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = clean_title(title)
        if parent_id is not None:
            fields["parentId"] = parent_id
        notes: list[str] = []
        if note is not None:
            fields["note"] = protected(note, notes)
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
        if size is not None:
            fields["taskSize"] = size_value(size)
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
        out: dict[str, Any] = {"updated": result}
        if notes:
            out["notes"] = notes
        if note is not None:
            out["note_page"] = (
                "If the project/category has a notebook, note is written to its first page; "
                "if the notebook has no pages the server creates page 1."
            )
        return out
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
    clear_project_fields: Annotated[
        bool,
        Field(description="Only for to='category': True = clear day / dueDate / priority / isFrogged / firstScheduled (like the app's buggy Edit Settings path — yields a CLEAN category without e.g. a deadline badge, for a permanent conversion); the previous values are then returned in removed_project_fields. Default False = lossless, like the app's correct path"),
    ] = False,
) -> dict:
    """Convert a project to a category or back, in place and losslessly.
    EXPERIMENTAL: Convert project→category or category→project IN PLACE
    via /doc/update (Full Access Token; there is no official conversion
    endpoint, and this relies on undocumented server behavior that Marvin
    could change). Same _id, createdAt and children remain — conversion is a
    pure type change (verified against the live API 2026-08-29: the server
    accepts and persists the change in both directions, and the app renders
    correctly after an API-set change).
    LOSSLESS BY DEFAULT (since 1.5.0): only type is changed — the same
    semantics as the app's correct conversion path (the right-click/hover
    menu, verified as a lossless round trip 2026-08-30: all project fields
    incl. firstScheduled preserved through project→category→project).
    Project fields remaining on the category are then intentional round-trip
    data; the type guard in update_category_or_project only prevents NEW
    project fields from being written to it.
    If you want a clean category for a permanent conversion: set
    clear_project_fields=True (mimics the app's Edit Settings path — a bug
    in their tracker; also clears firstScheduled, which that path otherwise
    leaves behind) and receive the values in removed_project_fields.
    Note: the app's correct path (right-click/hover) is not in the menu by
    default — it is added via the gear icon directly in the right-click menu
    → Add action (app-verified 2026-08-31), so unmodified apps only show the
    buggy path.
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
        if to == "category" and clear_project_fields:
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
    """Get all habits with title, settings and history.
    Get all habits as full documents incl. title, settings and history
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
    """Get a habit's server tracking record with its full history.
    Get the server's tracking record for a single habit (habitId + full
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
    """Record or undo a habit check-in, synced to the app.
    Also updates the sync database
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
    """Get today's time blocks with their category or smart-list mapping.
    The API response lacks the category link
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
    """Create a time block (experimental) and get its id back.
    EXPERIMENTAL: Create a time block via /doc/create (db='PlannerItems',
    Full Access Token). No official endpoint exists. Verify in the app that
    the block looks right.

    The response carries `time_block_id` (the id is set client-side because
    /doc/create does not echo the server-generated id) and can be used
    directly as time_block_section in create_task/update_task — no extra
    get_today_time_blocks call needed. Client-set id live-tested 2026-09-13:
    accepted, listed in /todayTimeBlocks with the same id, and the block
    renders in the app like one created there (verified in the app
    2026-09-13, app 1.70.0.0, PWA + desktop)."""
    try:
        doc = {
            "_id": uuid.uuid4().hex,
            "db": "PlannerItems",
            "title": title,
            "date": date,
            "time": start_time,
            "duration": str(duration_minutes),
            "isSection": True,
            "createdAt": now_ms(),
        }
        created = await get_client().create_doc(doc)
        return {"created": created, "time_block_id": doc["_id"]}
    except Exception as e:
        return tool_error(e)


# --------------------------------------------------------- Time tracking


@mcp.tool(annotations=READONLY)
async def get_tracked_item() -> dict:
    """Show which task is being time-tracked right now."""
    try:
        item = await get_client().tracked_item()
        return {"tracked_item": item or None}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def start_tracking(
    task_id: Annotated[str, Field(description="Task ID")],
) -> dict:
    """Start time tracking on a task."""
    try:
        return {"tracking": await get_client().track(task_id, "START")}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def stop_tracking(
    task_id: Annotated[str, Field(description="Task ID")],
) -> dict:
    """Stop time tracking on a task; the time lands in get_time_tracks.
    Stop time tracking for a task. Note (documented API limitation,
    confirmed live 2026-09-02): the task's own times/duration fields are
    not updated by /track STOP — the tracking only lands in /tracks
    (get_time_tracks). Exception: mark_done during active tracking now
    writes task.times (see mark_done)."""
    try:
        return {"tracking": await get_client().track(task_id, "STOP")}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_time_tracks(
    task_ids: Annotated[list[str], Field(description="Up to 100 task IDs")],
) -> dict:
    """Get time-tracking history for up to 100 tasks.
    Get time-tracking history for the given tasks (the source of truth,
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
    """Get kudos, level and kudosRemaining from Marvin's XP system.
    Note: kudos
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
    """Award reward points for a completed task or manually.
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
    """Undo a reward-point award tied to a task.
    Undo a point award (e.g. after a misclick, or when the task was
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
    """Spend reward points on a reward.
    Note (verified live): the API
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
    """Reset reward points permanently, balance and history.
    Reset reward points PERMANENTLY: deletes the whole earn/spend history
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
    """Get all labels with ids, for label_ids and filtering."""
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
    """Get all server-side reminders, push notifications to the phone.
    Requires the Full Access Token."""
    try:
        return {"reminders": await get_client().reminders()}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def set_reminder(
    time_unix_seconds: Annotated[int, Field(description="Unix time (seconds) for the reminder")],
    task_id: Annotated[
        str | None,
        Field(description="The task's id: the reminder is added to the task in the app's form. Omit for a standalone reminder"),
    ] = None,
    title: Annotated[
        str | None, Field(description="Notification text (max 200 chars). Standalone: required. On a task: the task's title is used")
    ] = None,
) -> dict:
    """Set an own reminder on a task, or a standalone reminder.
    With task_id the reminder is written in the app's form: an entry in the
    task's reminders list (reminderId of 13 characters, time in seconds)
    and a server entry of type M with taskId — the app shows it under
    Reminders and the notification arrives, and mark_done/delete_task clean
    it up. A clock time on a task is set with update_task(time=...).
    Without task_id a standalone server entry of type M is written
    (requires Marvin's mobile app for the notification)."""
    try:
        c = Chain()
        if task_id:
            doc = await read_doc(c, task_id, "read the task")
            rid = tr.rid13()
            new_list = list(doc.get("reminders") or []) + [{"reminderId": rid, "time": time_unix_seconds}]
            await c.step("add to reminders", lambda: get_client().update_doc(task_id, make_setters({"reminders": new_list})))
            post = tr.server_m(task_id, rid, doc.get("title") or title or "", time_unix_seconds)
            await c.step("write the server entry", lambda: get_client().set_reminders([post]))
            return {"reminder_id": rid, "task_id": task_id, "completed_steps": c.completed}
        if not title:
            return {"error": "A standalone reminder requires title."}
        reminder = {
            "time": time_unix_seconds,
            "offset": 0,
            "reminderId": tr.rid13(),
            "type": "M",
            "title": title[:200],
            "snooze": 9,
            "autoSnooze": False,
            "canTrack": False,
        }
        return {"result": await get_client().set_reminders([reminder]), "reminder_id": reminder["reminderId"]}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_reminder(
    reminder_ids: Annotated[list[str], Field(description="IDs of reminders to delete")],
    task_id: Annotated[
        str | None,
        Field(description="The task's id when the reminders belong to a task: they are then also removed from the reminders list"),
    ] = None,
) -> dict:
    """Delete reminders, also from the task's list when task_id is given.
    With task_id the entries are removed from the task's reminders list and
    from the server (both are needed; an entry left in the list makes an
    open client notify). Without task_id only the server entries are
    removed — use it for standalone reminders or orphaned entries from
    get_reminders. The clock time's reminder (type T) is removed with
    update_task(time='')."""
    try:
        c = Chain()
        if task_id:
            doc = await read_doc(c, task_id, "read the task")
            kept = [r for r in (doc.get("reminders") or []) if r.get("reminderId") not in set(reminder_ids)]
            await c.step("remove from reminders", lambda: get_client().update_doc(task_id, make_setters({"reminders": kept})))
        result = await c.step("remove the server entries", lambda: get_client().delete_reminders(reminder_ids))
        return {"result": result, "completed_steps": c.completed}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def create_event(
    title: Annotated[str, Field(description="Event title")],
    start_iso: Annotated[str, Field(description="Start time, ISO 8601 with timezone, e.g. 2026-08-20T14:30:00+02:00")],
    length_minutes: Annotated[int, Field(description="Length in minutes", gt=0)],
    note: Annotated[str | None, Field(description="Note (markdown)")] = None,
) -> dict:
    """Create a calendar event (experimental), synced by the app.
    EXPERIMENTAL: Create a calendar event. Calendar sync happens in the
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
    """Get account info (/me): email, tracking status and points balance."""
    try:
        return {"account": await get_client().me()}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_rate_limit_status() -> dict:
    """Show today's Marvin calls against the budget and the last call time.
    Show how many Marvin API calls have been made today (budget 1440/day,
    shared by all tools) and when the latest one went out. The counter is
    shared by every client that goes through this server. If a call went
    out within the last minute, another client is active. This tool costs
    no Marvin call."""
    out: dict[str, Any] = {
        "calls_today": _limiter.calls_today if _limiter else 0,
        "daily_limit": 1440,
    }
    last = _limiter.last_call_at if _limiter else None
    if last is None:
        out["last_call_at"] = None
    else:
        out["last_call_at"] = datetime.fromtimestamp(last, TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
        out["seconds_since_last_call"] = max(0, int(time.time() - last))
    return out


# Capability overview. tests/test_discoverability.py requires every registered
# tool to be listed here.
CAPABILITIES: list[tuple[str, list[tuple[str, str, str | None]]]] = [
    ("Tasks", [
        ("create_task", "Create a task: category, day, deadline, priority, frog 1-4, size, subtasks, clock time, own reminders, labels, estimate, note, sections, planned week/month, review date, backburner, reward points",
         "startDate/endDate, orbit/noAutoOrbit (set with update_task afterwards)"),
        ("update_task", "Change title, note, day (moves the clock time's reminder as the app does), clock time, own reminders, size, subtasks (add, check off, rename), deadline, labels, estimate, sections, date fields, backburner, orbit, snooze, time block",
         "recurrence rules (update_recurring_task); completion (mark_done)"),
        ("set_priority", "Priority P1-P3/low (isStarred 3/2/1/-1) and dread/frog 1-4 on a task", None),
        ("mark_done", "Complete a task; cleans up reminders as the app does; records the habit on habit tasks (with a count: checked off when the target is reached)", "projects (mark_project_done); reward points are not awarded automatically"),
        ("mark_project_done", "Close a project in two steps: list open children with a code, then check them off and close the project with the app's fields",
         "sub-projects, habit tasks and occurrences among the children (close them first); workedOnAt on the children (which the app sets); undo (the app); kudos for the project"),
        ("unmark_done", "Undo a completion and the habit's record (if it is the task's)", "projects (reopened in the app); kudos; the clock time's reminder is not restored (as in the app); a partial record on an open habit task — use record_habit undo"),
        ("delete_task", "Delete permanently; clears reminders first; scope=all deletes a recurring task (via an occurrence or its own id) with its open occurrences; projects and categories only without open children", "undo; habit recurrences; projects with open children; the project's notebook (delete_notebook); an open client shows the row until reloaded"),
        ("get_today_items", "Open items with day <= date; hides occurrences as the app does (If you miss one)", "recurring occurrences before a Marvin client has opened after the day rolled over"),
        ("get_due_items", "Open items with deadline <= date; hides Keep just one as the app does", None),
        ("get_done_items", "Tasks completed on a date (doneAt), with lookback", "projects"),
    ]),
    ("Recurring tasks", [
        ("create_recurring_task", "All eleven types, Create occurrences, If you miss one, hard deadline, start/end, only weekdays, sections, label, priority, size, note, subtasks; After completion with the first occurrence; custom recurrence as RRULE strings only (rules in words are refused)",
         "occurrences right away (the app creates them at the first client start after the day rolls over); reminder/frog on a recurring task"),
        ("update_recurring_task", "Rule, window, place, end date, properties; cleanup of occurrences that no longer fit (list, remove with confirm)",
         "correcting existing occurrences; switching to/from After completion; custom rules in words (RRULE strings only) and cleanup for Only on weekdays"),
    ]),
    ("Notebooks", [
        ("get_notebooks", "All notebooks with page titles, order and sections; parent_missing/parent_done when the parent is deleted or done. Cost: 2 calls plus one per parent missing from /categories", "page text (get_notebook)"),
        ("get_notebook", "One notebook with page text, via notebook, parent or page id; a 404 on parent_id returns an error suggesting notebook_id", None),
        ("search_notebooks", "Search names, page titles and text (cached, at most 20 fetches per call)", None),
        ("create_notebook", "Standalone notebook with sections", "a notebook directly on a project (formed when a page is moved)"),
        ("update_notebook", "Name (standalone) and sections: create, rename, remove", "renaming a notebook that has a parent"),
        ("delete_notebook", "Delete a notebook and its pages; two steps when there is text; page 1 goes back as the note", "undo (no trash via the API)"),
        ("create_notebook_page", "New page last, optional section", None),
        ("update_notebook_page", "Text, append, title, order, section, move to a notebook or project/category", "moving the parent's first page out"),
        ("delete_notebook_page", "Delete a page; confirmation if it has text", "the last page in a notebook with a parent (delete_notebook)"),
    ]),
    ("Structure", [
        ("get_categories", "The whole hierarchy of categories and projects", None),
        ("get_children", "Direct children (open tasks, subprojects) of a category/project", "completed tasks; orphans; deeper levels in one call; does not filter hidden occurrences"),
        ("create_category_or_project", "Category or project with color, icon, labels, note; projects also day/deadline/priority/frog", "day/deadline/priority/frog on a category (structural rule); '#' in project titles"),
        ("update_category_or_project", "Labels, color, icon, note (page 1 if a notebook exists), parent, date fields, orbit; projects also day/deadline/priority/frog/size", "day/deadline/priority/frog/size on a category; completing projects (mark_project_done)"),
        ("convert_category_or_project", "Switch type project<->category in place, losslessly", "a category with subcategories -> project"),
        ("get_labels", "All labels with id and group", "creating/editing labels (app)"),
        ("get_goals", "All goals with status and check-ins", "creating/editing goals (app)"),
    ]),
    ("Habits", [
        ("list_habits", "All habits as documents: title, settings, history", "creating/editing habits (app)"),
        ("get_habit", "The server's tracking record for one habit (full history)", "title/settings (list_habits)"),
        ("record_habit", "Record or undo a habit, synced to the app", None),
    ]),
    ("Time", [
        ("get_today_time_blocks", "Today's time blocks with block->category/smart-list mapping", "blocks on days other than the given date"),
        ("create_time_block", "Create a time block (experimental); the response carries time_block_id", "smart-time-block mapping (set in the app)"),
        ("get_tracked_item", "The task being time-tracked right now", None),
        ("start_tracking", "Start time tracking on a task", None),
        ("stop_tracking", "Stop time tracking; the time lands in /tracks", "updating the task's own times/duration (only mark_done does that)"),
        ("get_time_tracks", "Tracking history for up to 100 tasks", "per day"),
    ]),
    ("Rewards", [
        ("get_kudos", "Kudos, level, kudosRemaining", "nextMultiplier (only in /me)"),
        ("claim_reward_points", "Award reward points for a completed task or MANUAL", "undoing a MANUAL award"),
        ("unclaim_reward_points", "Undo an award tied to a task", "MANUAL awards"),
        ("spend_reward_points", "Spend reward points", "a negative balance (the server answers 500)"),
        ("reset_reward_points", "Reset points and history permanently", "undo"),
    ]),
    ("Reminders", [
        ("get_reminders", "All server-side reminders", None),
        ("set_reminder", "Own reminder on a task in the app's form, or standalone", "clock time (update_task time)"),
        ("delete_reminder", "Delete reminders, also from the task's list with task_id", "the clock time's reminder (update_task time='')"),
    ]),
    ("Calendar", [
        ("create_event", "Calendar event (experimental), synced onwards by the app", "reading/editing events"),
    ]),
    ("Account and operations", [
        ("test_connection", "Check that the API token works", None),
        ("get_account_info", "Account info (/me): email, tracking status, points balance", None),
        ("get_rate_limit_status", "Today's calls against the 1440/day budget and the time of the last call (all clients of the server)", "calls that bypass the server"),
        ("list_capabilities", "This overview", None),
    ]),
]

CANNOT_VIA_MCP: list[str] = [
    "Creating or editing labels, goals, habits, smart lists and strategies — the app",
    "Correcting occurrences that already exist after a rule change via the API — the API does not, and neither does saving in the app afterwards; the app's dialog does it on a rule change made there",
    "Reminder, clock time and frog on recurring tasks, recurring projects, attachments, the archive, day notes, webhooks — not supported",
    "Reading the app's Rewards (rewards to buy) — no endpoint",
    "Undoing an API deletion — the app's trash is not reachable",
    "Undoing a MANUAL reward-point award — the server keeps no record",
    "Section strategies (time block/custom/daily/bonus) show in Today only if the strategy is on AND the day view is grouped by that section (per device)",
]

NOTE_FORMAT: dict[str, Any] = {
    "rule": "Bullet lists with '*', checklists with '- [ ]'. A list where '- ' bullets and '- [ ]' meet is deleted entirely when the app saves the text.",
    "auto_protection": "'- ' at line start is written as '* ' by the tools; '- [ ]', '- [x]', '---', indentation and code blocks are left untouched.",
    "renders": "Headings, '*' bullets, numbered lists, tables, :::warning box, page break (***), quotes and code blocks (read off a notebook page).",
    "app_rewrites": "The app tidies the text when it saves: blank lines, table separators and trailing '\\' — differences from what was written are not edits.",
    "limits": "Images cannot be uploaded via the API; a page holds up to 750 kB.",
}


@mcp.tool(annotations=READONLY)
async def list_capabilities() -> dict:
    """List every Marvin tool by area, with what it can and cannot do.
    Costs no Marvin API call. Call it before assuming something is impossible
    via MCP or requires manual work in the app; field conditions and
    live-tested details live in each tool's own description."""
    areas = [
        {"area": area, "tools": [{"name": n, "can": can, "cannot": cannot} for n, can, cannot in tools]}
        for area, tools in CAPABILITIES
    ]
    return {
        "tool_count": sum(len(t) for _, t in CAPABILITIES),
        "areas": areas,
        "cannot_via_mcp": CANNOT_VIA_MCP,
        "note_format": NOTE_FORMAT,
        "note": "Field conditions, live-tested limitations and dates live in each tool's description; docs/field-reconciliation.md carries the per-field reference.",
    }


# The notebook and recurring-task tools live in their own modules but are
# registered on the same server. Imported last: the modules read names from here.
from . import tools_notebooks, tools_recurring  # noqa: E402,F401
