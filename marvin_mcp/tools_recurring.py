"""Recurring tasks: create_recurring_task and update_recurring_task.

Documents are written in the same shape the Marvin app writes them, so all
eleven repeat types show correctly in the app's repeat dialog. After
completion needs a first occurrence written by the tool; existing
occurrences are not corrected when the rule changes. Only values the
app's dialog offers are accepted.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from . import chain as chainmod
from . import recurrence as rec
from . import taskreminders as tr
from .chain import Chain
from .client import MarvinError, local_today
from .config import TIMEZONE
from .server import (
    ADDITIVE,
    FROG_DESC,  # noqa: F401  (same naming series in descriptions)
    IDEMPOTENT_WRITE,
    SizeArg,
    build_setters,
    check_iso_date,
    clean_title,
    get_client,
    invalidate_missed_mode,
    make_setters,
    mcp,
    now_ms,
    protected,
    size_value,
    subtask_setters,
    tool_error,
)

RepeatArg = Literal[
    "daily", "weekly", "monthly", "x_times_per_week", "every_x_days", "after_completion",
    "every_x_weeks", "every_x_months", "every_x_years", "on_off", "custom",
]
WeekdayArg = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MissArg = Literal["default", "keep_them_all", "keep_just_one", "only_the_current_one"]
AheadArg = Literal["default", "0", "1", "3", "7", "14", "30"]

REPEAT_DESC = (
    "The dialog's eleven types: daily, weekly (weekday), monthly (month_day), x_times_per_week "
    "(weekdays), every_x_days / every_x_weeks / every_x_months / every_x_years (interval), "
    "after_completion (after_days), on_off (on_days, off_days), custom (custom: an RRULE string, "
    "e.g. 'RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR')"
)

CUSTOM_RRULE_ERROR = (
    "custom accepts only an RRULE string, e.g. 'RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR'. "
    "Rules in words are not accepted: the app does not reliably understand them."
)


def check_custom_rrule(custom: str) -> None:
    """Refuse a custom rule that is not an RRULE string."""
    if not custom.strip().startswith("RRULE:"):
        raise MarvinError(CUSTOM_RRULE_ERROR)


def type_fields(
    repeat: str, start: date, weekday: str | None, month_day: int | None, weekdays: list[str] | None,
    interval: int | None, after_days: int | None, on_days: int | None, off_days: int | None, custom: str | None,
) -> dict[str, Any]:
    """Validate the type's sub-fields against the dialog and build Marvin's fields."""
    t = rec.REPEAT_TYPES[repeat]
    f: dict[str, Any] = {"type": t}
    if repeat == "weekly":
        if not weekday:
            raise MarvinError("weekly requires weekday (mon-sun).")
        f["day"] = rec.WEEKDAY_NAMES[weekday]
    elif repeat == "monthly":
        if not month_day:
            raise MarvinError("monthly requires month_day (1-31).")
        f["date"] = month_day
    elif repeat == "x_times_per_week":
        if not weekdays:
            raise MarvinError("x_times_per_week requires weekdays (one or more of mon-sun).")
        f["weekDays"] = sorted({rec.WEEKDAY_NAMES[w] for w in weekdays})
    elif repeat in ("every_x_days", "every_x_weeks", "every_x_months", "every_x_years"):
        if not interval:
            raise MarvinError(f"{repeat} requires interval (>= 1).")
        f["repeat"] = interval
    elif repeat == "after_completion":
        if not after_days:
            raise MarvinError("after_completion requires after_days (>= 1).")
        f["echoDays"] = after_days
        f["repeat"] = after_days  # the app's shape
    elif repeat == "on_off":
        if not on_days or off_days is None:
            raise MarvinError("on_off requires on_days and off_days.")
        f["onCount"], f["offCount"] = on_days, off_days
    elif repeat == "custom":
        if not custom or not custom.strip():
            raise MarvinError("custom requires custom (an RRULE string, e.g. 'RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR').")
        check_custom_rrule(custom)
        f["customRecurrence"] = custom.strip()
    return f


def common_fields(
    if_you_miss_one: str | None, create_occurrences: str | None, hard_deadline_days: int | None,
    ends_on: str | None, only_weekdays: bool | None, repeat: str | None, daily_section: str | None,
    bonus_section: str | None, label_ids: list[str] | None, priority: int | None, size: str | None,
    note: str | None, notes: list[str], *, creating: bool,
) -> tuple[dict[str, Any], bool]:
    """Return (fields, create_occurrences_set_to_default)."""
    f: dict[str, Any] = {}
    to_default = False
    if if_you_miss_one is not None:
        f["missedMode"] = rec.MISSED_MODES[if_you_miss_one]
    if create_occurrences is not None:
        if create_occurrences == "default":
            to_default = True  # the field is omitted (create) or removed (update)
        else:
            f["createAhead"] = int(create_occurrences)
    if hard_deadline_days is not None:
        f["dueIn"] = hard_deadline_days or None
    if ends_on is not None:
        if ends_on:
            check_iso_date(ends_on, "ends_on")
        f["endDate"] = ends_on or None
    if only_weekdays is not None:
        if only_weekdays and repeat is not None and repeat not in rec.WEEKDAYS_ONLY_TYPES:
            raise MarvinError(
                "only_weekdays is offered in the dialog only for monthly, every_x_days, after_completion, "
                "every_x_months and every_x_years."
            )
        f["limitToWeekdays"] = only_weekdays
    if daily_section is not None:
        f["dailySection"] = daily_section or None
    if bonus_section is not None:
        f["bonusSection"] = bonus_section or None
    if label_ids is not None:
        f["labelIds"] = label_ids
    if priority is not None:
        f["isStarred"] = priority or False
    if size is not None:
        f["taskSize"] = size_value(size) if size else (0 if creating else False)
    if note is not None:
        f["note"] = protected(note, notes)
    return f, to_default


def inherited_subtasks(subs: dict, day: str) -> dict:
    """Occurrences inherit subtasks with a date prefix in the id, '<day>_<subid>'."""
    out = {}
    for sid, sub in (subs or {}).items():
        nid = f"{day}_{sid}"
        out[nid] = {**sub, "_id": nid}
    return out


async def add_deleted_dates(c: Chain, gen_id: str, gen: dict, days: list[str]) -> list[str]:
    """The app's shape when an occurrence is deleted: the occurrence's day is
    added to the recurring task's deletedDates (ISO dates), stamped as usual."""
    cur = list(gen.get("deletedDates") or [])
    new = cur + [d for d in days if d and d not in cur]
    if new != cur:
        await c.step("write deletedDates", lambda: get_client().update_doc(gen_id, make_setters({"deletedDates": new})))
        invalidate_missed_mode(gen_id)
    return new


def midnight_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TIMEZONE).timestamp() * 1000)


@mcp.tool(annotations=ADDITIVE)
async def create_recurring_task(
    title: Annotated[str, Field(description="Title")],
    repeat: Annotated[RepeatArg, Field(description=REPEAT_DESC)],
    parent_id: Annotated[str | None, Field(description="Category/project ID; omit for the Inbox")] = None,
    weekday: Annotated[WeekdayArg | None, Field(description="weekly: weekday")] = None,
    month_day: Annotated[int | None, Field(description="monthly: day of the month", ge=1, le=31)] = None,
    weekdays: Annotated[list[WeekdayArg] | None, Field(description="x_times_per_week: weekdays")] = None,
    interval: Annotated[int | None, Field(description="every_x_*: interval", ge=1, le=365)] = None,
    after_days: Annotated[int | None, Field(description="after_completion: days after completion", ge=1, le=365)] = None,
    on_days: Annotated[int | None, Field(description="on_off: days on", ge=1, le=365)] = None,
    off_days: Annotated[int | None, Field(description="on_off: days off", ge=0, le=365)] = None,
    custom: Annotated[str | None, Field(description="custom: an RRULE string, e.g. 'RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR'")] = None,
    starts_on: Annotated[str | None, Field(description="Starts on YYYY-MM-DD; defaults to today")] = None,
    ends_on: Annotated[str | None, Field(description="Stop repeating on YYYY-MM-DD")] = None,
    create_occurrences: Annotated[AheadArg | None, Field(description="Create occurrences: default (the account's default) or 0/1/3/7/14/30 days ahead")] = None,
    if_you_miss_one: Annotated[MissArg | None, Field(description="If you miss one; default = the account's setting")] = None,
    hard_deadline_days: Annotated[int | None, Field(description="Hard deadline: days after the occurrence's day", ge=0, le=365)] = None,
    only_weekdays: Annotated[bool | None, Field(description="Only on weekdays (Mon-Fri), only for the types the dialog offers it on. A month day on a weekend is moved to Friday (verified); every_x_days then counts in weekdays (inferred)")] = None,
    daily_section: Annotated[str | None, Field(description="Day section 'Morning'/'Afternoon'/'Evening'")] = None,
    bonus_section: Annotated[str | None, Field(description="'Essential' or 'Bonus'")] = None,
    label_ids: Annotated[list[str] | None, Field(description="Label IDs")] = None,
    priority: Annotated[int | None, Field(description="isStarred 3/2/1/-1", ge=-1, le=3)] = None,
    size: Annotated[SizeArg | None, Field(description="Size XS-XL")] = None,
    note: Annotated[str | None, Field(description="Note (markdown)")] = None,
    subtasks: Annotated[list[str] | None, Field(description="Subtasks (titles)")] = None,
) -> dict:
    """Create a recurring task of any of the repeat dialog's eleven types.
    Written via /doc/create in the app's own shape, so every type shows
    correctly in the app's repeat dialog. Occurrences are created the first
    time a Marvin client opens after midnight, in the window today to today +
    Create occurrences — not immediately.
    Without create_occurrences the account's default applies. After
    completion: the tool writes the first occurrence on the start day, also
    when the start day is in the future. Custom accepts only an RRULE string
    (e.g. 'RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR'). New occurrences inherit
    label, priority, size, note, subtasks, sections and hard deadline
    (verified against the live API).
    Only on weekdays: a month day on a weekend is moved to Friday (documented
    for monthly, verified); every_x_days then counts in weekdays (inferred
    from a series; the documentation does not mention the type)."""
    try:
        notes: list[str] = []
        start_s = starts_on or local_today()
        check_iso_date(start_s, "starts_on")
        start = rec.parse_day(start_s)
        tf = type_fields(repeat, start, weekday, month_day, weekdays, interval, after_days, on_days, off_days, custom)
        cf, _ = common_fields(if_you_miss_one, create_occurrences, hard_deadline_days, ends_on, only_weekdays,
                              repeat, daily_section, bonus_section, label_ids, priority, size, note, notes, creating=True)
        now = now_ms()
        gid = str(uuid.uuid4())
        doc: dict[str, Any] = {
            "_id": gid, "db": "RecurringTasks", "recurringType": "task", "title": clean_title(title),
            "parentId": parent_id or "unassigned", "repeatStart": start_s, "missedMode": "",
            "createdAt": now, "updatedAt": now, **rec.default_generator_fields(start), **tf, **cf,
        }
        if subtasks:
            subs = {}
            for k, t in enumerate(subtasks, start=1):
                sid = tr.rid13()
                subs[sid] = {"_id": sid, "title": clean_title(t), "done": False, "rank": k}
            doc["subtasks"] = subs
        if rec.is_words_custom(doc):
            notes.append("Custom rule is not an RRULE: check the interpretation under Understood as in the app's dialog.")
        c = Chain()
        await c.step("create the recurring task", lambda: get_client().create_doc(doc))
        out: dict[str, Any] = {"recurring_task_id": gid, "type": doc["type"]}
        if repeat == "after_completion":
            inst = {
                "_id": f"{start_s}_{gid}", "db": "Tasks", "title": doc["title"], "createdAt": midnight_ms(start_s),
                "generatedAt": now, "parentId": doc["parentId"], "day": start_s, "firstScheduled": start_s,
                "done": False, "recurring": True, "recurringTaskId": gid, "rank": 9999,
                "subtasks": inherited_subtasks(doc["subtasks"], start_s), "note": doc["note"],
                "labelIds": doc["labelIds"],
                "timeEstimate": 0, "isReward": False, "isStarred": doc["isStarred"], "isFrogged": False,
                "taskSize": doc["taskSize"], "isUrgent": False, "mentalWeight": False, "positiveEnergy": False,
                "focusLevel": False, "energyAmount": False, "isPhysical": False, "orbit": False,
                "backburner": False, "rewardPoints": 0, "dailySection": doc.get("dailySection"),
                "bonusSection": doc.get("bonusSection"), "customSection": None, "sectionId": None,
                "echo": True, "echoId": None, "dueDate": None, "fieldUpdates": {}, "updatedAt": now,
            }
            await c.step("create the first occurrence", lambda: get_client().create_doc(inst))
            out["first_occurrence_id"] = inst["_id"]
            notes.append("After completion: the next occurrence is created +N days after this one is completed.")
        else:
            notes.append("Occurrences are created the first time a Marvin client opens after midnight — not immediately.")
        out["notes"] = notes
        out["completed_steps"] = c.completed
        return out
    except Exception as e:
        return tool_error(e)


async def _occurrences(gen: dict) -> list[dict]:
    """Open occurrences with recurringTaskId, from the recurring task's parent.
    Occurrences moved to another place are not seen here."""
    parent = gen.get("parentId") or "unassigned"
    kids = await get_client().children(parent)
    return [k for k in kids or [] if isinstance(k, dict) and k.get("recurringTaskId") == gen.get("_id")]


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def update_recurring_task(
    recurring_task_id: Annotated[str, Field(description="The recurring task's ID (recurringTaskId on an occurrence)")],
    title: Annotated[str | None, Field(description="New title")] = None,
    parent_id: Annotated[str | None, Field(description="Move the recurring task (the next occurrences land there)")] = None,
    repeat: Annotated[RepeatArg | None, Field(description="New type (not to/from after_completion). " + REPEAT_DESC)] = None,
    weekday: Annotated[WeekdayArg | None, Field(description="weekly")] = None,
    month_day: Annotated[int | None, Field(description="monthly", ge=1, le=31)] = None,
    weekdays: Annotated[list[WeekdayArg] | None, Field(description="x_times_per_week")] = None,
    interval: Annotated[int | None, Field(description="every_x_*", ge=1, le=365)] = None,
    on_days: Annotated[int | None, Field(description="on_off", ge=1, le=365)] = None,
    off_days: Annotated[int | None, Field(description="on_off", ge=0, le=365)] = None,
    custom: Annotated[str | None, Field(description="custom: an RRULE string, e.g. 'RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR'")] = None,
    create_occurrences: Annotated[AheadArg | None, Field(description="default/0/1/3/7/14/30")] = None,
    if_you_miss_one: Annotated[MissArg | None, Field(description="If you miss one")] = None,
    hard_deadline_days: Annotated[int | None, Field(description="Hard deadline, 0 = off", ge=0, le=365)] = None,
    ends_on: Annotated[str | None, Field(description="End date YYYY-MM-DD, '' removes it")] = None,
    only_weekdays: Annotated[bool | None, Field(description="Only on weekdays: a month day on a weekend is moved to Friday (verified); every_x_days counts in weekdays (inferred)")] = None,
    daily_section: Annotated[str | None, Field(description="Day section, '' removes it")] = None,
    bonus_section: Annotated[str | None, Field(description="Essential/Bonus, '' removes it")] = None,
    label_ids: Annotated[list[str] | None, Field(description="Labels (replaces)")] = None,
    priority: Annotated[int | None, Field(description="isStarred 3/2/1/-1, 0 removes it", ge=-1, le=3)] = None,
    size: Annotated[SizeArg | None, Field(description="Size XS-XL, '' removes it")] = None,
    note: Annotated[str | None, Field(description="Note (replaces)")] = None,
    subtasks_add: Annotated[list[str] | None, Field(description="New subtasks on the recurring task")] = None,
    confirm: Annotated[bool, Field(description="True = remove the occurrences the cleanup listed")] = False,
) -> dict:
    """Change a recurring task: rule, window, place, end date, properties.
    Existing occurrences are not corrected when the rule changes via the API,
    and a later save in the app does not correct them either; they keep their
    old shape. When the rule is changed in the app's dialog, the app itself
    removes those that no longer match. When type, interval or Create
    occurrences change, the tool then works out which open, untouched
    occurrences from today on do not match the new rule or window. Mode
    'list' (default): they are listed and removed only with confirm=true
    (without deletedDates: they do not come back if they do not match the
    rule, and outside the window they should come back). Changed
    occurrences, occurrences before today, custom rules that are not RRULEs
    and Only on weekdays are listed only. Deleted occurrences can remain
    visible in an open client until it reloads. New occurrences follow a rule
    and properties changed via the API, counted from the start day.
    Custom accepts only an RRULE string.
    Lowering Create occurrences via the API removes nothing, not even at the
    next generation; the app's dialog removes untouched upcoming occurrences
    when the lowering is saved there. confirm=true does what the dialog
    does."""
    try:
        if custom:
            check_custom_rrule(custom)
        c = Chain()
        notes: list[str] = []
        gen = await c.step("read the recurring task", lambda: get_client().get_doc(recurring_task_id))
        if not isinstance(gen, dict) or gen.get("db") != "RecurringTasks":
            return {"error": "The document is not a recurring task (db RecurringTasks); pass a recurringTaskId."}
        cur_type = gen.get("type")
        rule_change = repeat is not None or any(
            x is not None for x in (weekday, month_day, weekdays, interval, on_days, off_days, custom))
        fields: dict[str, Any] = {}
        if rule_change:
            new_repeat = repeat or rec.TYPE_NAMES.get(cur_type, "")
            if new_repeat == "after_completion" or cur_type == "echo":
                return {"error": "Changing to or from After completion is not supported; create the recurring task again."}
            start = rec.parse_day(gen.get("repeatStart") or local_today())
            defaults = {
                "weekday": next((k for k, v in rec.WEEKDAY_NAMES.items() if v == gen.get("day")), None),
                "month_day": gen.get("date"),
                "weekdays": [k for k, v in rec.WEEKDAY_NAMES.items() if v in (gen.get("weekDays") or [])],
                "interval": gen.get("repeat"), "on_days": gen.get("onCount"), "off_days": gen.get("offCount"),
                "custom": gen.get("customRecurrence"),
            }
            fields.update(type_fields(
                new_repeat, start, weekday or defaults["weekday"], month_day or defaults["month_day"],
                weekdays or defaults["weekdays"], interval or defaults["interval"], None,
                on_days or defaults["on_days"], off_days if off_days is not None else defaults["off_days"],
                custom or defaults["custom"]))
        eff_repeat = repeat or rec.TYPE_NAMES.get(cur_type)
        cf, to_default = common_fields(if_you_miss_one, create_occurrences, hard_deadline_days, ends_on,
                                       only_weekdays, eff_repeat, daily_section, bonus_section, label_ids,
                                       priority, size, note, notes, creating=False)
        fields.update(cf)
        if title is not None:
            fields["title"] = clean_title(title)
        if parent_id is not None:
            fields["parentId"] = parent_id
            notes.append("Existing occurrences stay in the old place; the next ones land in the new place.")
        raw = subtask_setters(gen.get("subtasks"), add=subtasks_add) if subtasks_add else []
        if to_default:
            raw.append({"key": "createAhead", "val": None})
            notes.append("Create occurrences = the account's default: the field was set to null, which the app reads as the account's default (verified).")
        old_ahead = gen.get("createAhead")
        if isinstance(fields.get("createAhead"), int) and isinstance(old_ahead, int) and fields["createAhead"] < old_ahead:
            notes.append("Lowered Create occurrences: via the API nothing is removed, not even at the next generation. The app's dialog removes untouched upcoming occurrences when the lowering is saved there; confirm=true does the same.")
        if not fields and not raw:
            return {"error": "Nothing to change was given."}
        result = await c.step("write the recurring task", lambda: get_client().update_doc(recurring_task_id, build_setters(fields, raw)))
        invalidate_missed_mode(recurring_task_id)
        notes.append("Existing occurrences are not corrected after a rule change via the API.")
        out: dict[str, Any] = {"updated": result}
        if rule_change or "createAhead" in fields or to_default:
            new_gen = {**gen, **fields}
            if to_default:
                new_gen.pop("createAhead", None)
            occ = await c.step("read the occurrences", lambda: _occurrences(new_gen))
            plan = rec.cleanup_plan(new_gen, occ, datetime.now(TIMEZONE).date())
            out["cleanup"] = {"mode": rec.CLEANUP_MODE, "anchor": plan["anchor"], "window_checked": plan["window_checked"],
                              "to_remove": plan["remove"], "kept": plan["keep"], "list_only": plan["list_only"]}
            if plan["remove"] and (confirm or rec.CLEANUP_MODE == "auto"):
                docs = {o.get("_id"): o for o in occ}
                with_rem = [i["id"] for i in plan["remove"] if tr.has_reminder_state(docs.get(i["id"], {}))]
                for oid in with_rem:
                    await c.step(f"clear reminders {oid}", lambda oid=oid: get_client().update_doc(
                        oid, make_setters({"reminderTime": None, "reminders": []})))
                if with_rem:
                    ids = with_rem + [r for oid in with_rem for r in tr.reminder_ids_of(docs[oid])]
                    await c.step("delete server reminders", lambda: get_client().delete_reminders(ids))
                    await c.pause(chainmod.DELETE_SYNC_PAUSE_S)
                for item in plan["remove"]:
                    oid = item["id"]
                    await c.step(f"delete {oid}", lambda oid=oid: get_client().delete_doc(oid))
                out["cleanup"]["removed"] = [i["id"] for i in plan["remove"]]
                notes.append("An open client may show the deleted occurrences until it reloads (API bug).")
            elif plan["remove"]:
                notes.append("Nothing was removed: run again with confirm=true to remove to_remove.")
        out["notes"] = notes
        out["completed_steps"] = c.completed
        return out
    except Exception as e:
        return tool_error(e)
