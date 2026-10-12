"""Recurring tasks: the repeat dialog's values, the rule's days and the
cleanup mode. Shapes verified against the live API and the Marvin app.

Values that may need to be switched live here, in one place:
- CLEANUP_MODE: the cleanup after a rule change. "list" (default) lists
  the occurrences that would be removed and removes them only with
  confirm=true; "auto" removes them directly.
- RULE_ANCHOR: which day the rule's counting starts from after a rule
  change. "repeatStart" is the default assumption (not yet verified).
- The pause length: chain.DELETE_SYNC_PAUSE_S.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

CLEANUP_MODE = "list"  # "list" | "auto"
# mark_done on an After completion occurrence scheduled in the future:
# "api" = as /markDone does, the completed occurrence keeps its day and the
# next one = day + echoDays; "app" = as the app does, the day is set to today
# before completion and the next one is counted from today.
ECHO_EARLY_DONE_MODE = "app"  # "app" | "api"
RULE_ANCHOR = "repeatStart"  # "repeatStart" | "today"

# The dialog's eleven types -> Marvin's `type`.
REPEAT_TYPES: dict[str, str] = {
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
    "x_times_per_week": "n per week",
    "every_x_days": "repeat",
    "after_completion": "echo",
    "every_x_weeks": "repeat week",
    "every_x_months": "repeat month",
    "every_x_years": "repeat year",
    "on_off": "onOff",
    "custom": "custom",
}
TYPE_NAMES = {v: k for k, v in REPEAT_TYPES.items()}

# Create occurrences: the values the dialog offers (1, 3, 7, 14, 30 and 0).
CREATE_AHEAD_VALUES = (0, 1, 3, 7, 14, 30)

MISSED_MODES: dict[str, str] = {
    "default": "",
    "keep_them_all": "stack",
    "keep_just_one": "carry",
    "only_the_current_one": "skip",
}

# *Only on weekdays* is offered in the dialog only for these types.
WEEKDAYS_ONLY_TYPES = {"monthly", "every_x_days", "after_completion", "every_x_months", "every_x_years"}

WEEKDAY_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def marvin_weekday(d: date) -> int:
    """Marvin's weekday: 0 = Sunday (a weekly Wednesday is stored as 3)."""
    return (d.weekday() + 1) % 7


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def default_generator_fields(start: date) -> dict[str, Any]:
    """The app's default fields on a new recurring task (as the app saves it)."""
    return {
        "customRecurrence": "",
        "day": marvin_weekday(start),
        "date": start.day,
        "weekDays": [marvin_weekday(start)],
        "repeat": 1,
        "echoDays": 1,
        "onCount": 7,
        "offCount": 7,
        "limitToWeekdays": False,
        "deletedDates": [],
        "sectionId": None,
        "endDate": None,
        "reminderTimes": [],
        "isFrogged": False,
        "taskSize": 0,
        "isUrgent": False,
        "mentalWeight": False,
        "positiveEnergy": False,
        "focusLevel": False,
        "energyAmount": False,
        "isPhysical": False,
        "orbit": False,
        "backburner": False,
        "isReward": False,
        "rewardPoints": 0,
        "timeEstimate": 0,
        "subtasks": {},
        "note": "",
        "labelIds": [],
        "isStarred": False,
        "rank": None,
        "fieldUpdates": {},
    }


def is_words_custom(gen: dict) -> bool:
    return gen.get("type") == "custom" and not str(gen.get("customRecurrence") or "").startswith("RRULE:")


def rule_computable(gen: dict) -> tuple[bool, str | None]:
    """Can the rule's days be computed safely? Otherwise occurrences are only listed."""
    t = gen.get("type")
    if t == "echo":
        return False, "After completion has no fixed days"
    if gen.get("limitToWeekdays"):
        return False, "Only on weekdays is not computed: a month day is moved to Friday, every_x_days counts in weekdays — listed only"
    if is_words_custom(gen):
        return False, "A custom rule that is not an RRULE cannot be computed safely"
    if t == "custom":
        try:
            import dateutil.rrule  # noqa: F401
        except ImportError:  # pragma: no cover - depends on the installation
            return False, "RRULE computation is not available in this installation"
    if t not in TYPE_NAMES:
        return False, f"unknown type {t!r}"
    return True, None


def rule_matches(gen: dict, d: date, anchor: date) -> bool:
    """Does day d match the rule? anchor = the rule's start day (RULE_ANCHOR)."""
    end = gen.get("endDate")
    if end and d > parse_day(end):
        return False
    if d < anchor:
        return False
    t = gen.get("type")
    n = int(gen.get("repeat") or 1)
    delta = (d - anchor).days
    if t == "daily":
        return True
    if t == "weekly":
        return marvin_weekday(d) == int(gen.get("day") or 0)
    if t == "monthly":
        return d.day == int(gen.get("date") or 1)
    if t == "n per week":
        return marvin_weekday(d) in [int(x) for x in (gen.get("weekDays") or [])]
    if t == "repeat":
        return delta % max(n, 1) == 0
    if t == "repeat week":
        return delta % (7 * max(n, 1)) == 0
    if t == "repeat month":
        months = (d.year - anchor.year) * 12 + d.month - anchor.month
        return d.day == anchor.day and months % max(n, 1) == 0
    if t == "repeat year":
        return (d.month, d.day) == (anchor.month, anchor.day) and (d.year - anchor.year) % max(n, 1) == 0
    if t == "onOff":
        on = int(gen.get("onCount") or 1)
        off = int(gen.get("offCount") or 0)
        return delta % max(on + off, 1) < on
    if t == "custom":
        from dateutil.rrule import rrulestr

        rule = rrulestr(str(gen["customRecurrence"]).removeprefix("RRULE:"),
                        dtstart=datetime.combine(anchor, datetime.min.time()))
        start = datetime.combine(d, datetime.min.time())
        nxt = rule.after(start - timedelta(seconds=1), inc=True)
        return nxt is not None and nxt.date() == d
    return False


def rule_anchor(gen: dict, today: date) -> date:
    if RULE_ANCHOR == "today":
        return today
    start = gen.get("repeatStart")
    return parse_day(start) if start else today


# "Untouched" occurrence (verified against the live API): a client stamps
# masterRank, dueDate and updatedAt on untouched occurrences (they sit at
# _rev 2), so the revision says nothing. An occurrence is untouched when no
# other key in fieldUpdates has a stamp later than generatedAt +
# UNTOUCHED_GRACE_MS and day is the date in the id. Without generatedAt it
# counts as changed.
UNTOUCHED_IGNORED_STAMPS = frozenset({"masterRank", "dueDate", "updatedAt"})
UNTOUCHED_GRACE_MS = 60_000


def occurrence_untouched(occ: dict) -> bool:
    gen_at = occ.get("generatedAt")
    if not isinstance(gen_at, (int, float)):
        return False
    oid = str(occ.get("_id") or "")
    id_day = oid.split("_", 1)[0] if "_" in oid else None
    if id_day != occ.get("day"):
        return False
    limit = gen_at + UNTOUCHED_GRACE_MS
    for key, stamp in (occ.get("fieldUpdates") or {}).items():
        if key in UNTOUCHED_IGNORED_STAMPS:
            continue
        if isinstance(stamp, (int, float)) and stamp > limit:
            return False
    return True


def cleanup_plan(gen: dict, occurrences: list[dict], today: date) -> dict:
    """Split open occurrences into to remove, to keep and to list only.

    Candidate for removal: open, day today or later, untouched, and does not
    match the new rule or lies outside the window today..today +
    createAhead (the window is only checked when createAhead is set)."""
    ok, why = rule_computable(gen)
    anchor = rule_anchor(gen, today)
    ahead = gen.get("createAhead")
    window_end = today + timedelta(days=int(ahead)) if isinstance(ahead, int) else None
    remove, keep, listed = [], [], []
    for occ in sorted(occurrences, key=lambda o: str(o.get("day"))):
        if occ.get("done"):
            continue
        day_s = occ.get("day")
        row = {"id": occ.get("_id"), "day": day_s, "title": occ.get("title")}
        try:
            d = parse_day(day_s)
        except (TypeError, ValueError):
            keep.append({**row, "reason": "has no day"})
            continue
        if d < today:
            keep.append({**row, "reason": "before today"})
            continue
        if not occurrence_untouched(occ):
            keep.append({**row, "reason": "changed (stamped after generation, moved day or missing generatedAt)"})
            continue
        if not ok:
            listed.append({**row, "reason": why})
            continue
        if not rule_matches(gen, d, anchor):
            remove.append({**row, "reason": "does not match the new rule"})
        elif window_end is not None and d > window_end:
            remove.append({**row, "reason": "outside the window (Create occurrences)"})
    return {"remove": remove, "keep": keep, "list_only": listed, "anchor": anchor.isoformat(),
            "window_checked": window_end is not None}
