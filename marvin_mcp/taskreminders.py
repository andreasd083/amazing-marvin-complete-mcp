"""Task times and reminders in the app's own shape.

Verified by setting the shape in the app and then writing the same shape
via the API (which produced notifications), including cleanup. A task's
reminders consist of fields on the task plus server-side reminder records:
- the time of day: `taskTime` "HH:MM", title prefix "HH:MM ", `reminderTime`
  (seconds), `reminderOffset` 0, `snooze` 5, `autoSnooze` false,
  `bugMeInterval` 0, and a server record of type T with reminderId = the
  task's id;
- custom reminders: the list `reminders` [{reminderId (13 characters),
  time (s)}] and one server record of type M per reminder with taskId = the
  task's id.
Times in `reminders[].time` and `reminderTime` are seconds (verified).
"""

from __future__ import annotations

import re
import secrets
import string
from datetime import datetime

from .config import TIMEZONE

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
TITLE_PREFIX_RE = re.compile(r"^\d{1,2}:\d{2} ")
BEFORE_RE = re.compile(r"^before:(\d{1,4})$")

TIME_FIELDS = ("taskTime", "reminderTime", "reminderOffset", "snooze", "autoSnooze")


def rid13() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(13))


def check_time(value: str) -> None:
    if not TIME_RE.match(value):
        from .client import MarvinError

        raise MarvinError(f"time must be HH:MM (00:00-23:59), got {value!r}.")


def epoch_s(day: str, hhmm: str) -> int:
    dt = datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
    return int(dt.timestamp())


def strip_time_prefix(title: str) -> str:
    return TITLE_PREFIX_RE.sub("", title, count=1)


def with_time_prefix(title: str, hhmm: str) -> str:
    return f"{hhmm} {strip_time_prefix(title)}"


def time_fields(day: str, hhmm: str, title: str) -> dict:
    return {
        "title": with_time_prefix(title, hhmm),
        "taskTime": hhmm,
        "reminderTime": epoch_s(day, hhmm),
        "reminderOffset": 0,
        "snooze": 5,
        "autoSnooze": False,
        "bugMeInterval": 0,
    }


def cleared_time_fields(title: str) -> dict:
    """The app's shape for a removed time of day."""
    return {"title": strip_time_prefix(title), **{k: None for k in TIME_FIELDS}}


_BASE = {
    "offset": 0,
    "snooze": 5,
    "autoSnooze": False,
    "bugMe": False,
    "Count": 0,
    "sendReminders": False,
    "reminderDays": None,
    "reminderTimes": None,
    "timeZone": 0,
    "timeZoneName": "",
    "activeStart": "",
    "activeEnd": "",
    "activeDays": None,
    "stopAt": 0,
}


def server_t(task_id: str, title: str, time_s: int) -> dict:
    return {**_BASE, "time": time_s, "reminderId": task_id, "taskId": "", "type": "T",
            "title": title[:200], "sound": "", "canTrack": True}


def server_m(task_id: str, reminder_id: str, title: str, time_s: int) -> dict:
    return {**_BASE, "time": time_s, "reminderId": reminder_id, "taskId": task_id, "type": "M",
            "title": title[:200], "sound": "default", "canTrack": False}


def resolve_reminder(spec: str, day: str, task_time: str | None) -> int:
    """'HH:MM' = absolute time on the task's day; 'before:N' = N minutes
    before the task's time. The app also stores 'before' choices as an
    absolute time."""
    from .client import MarvinError

    m = BEFORE_RE.match(spec)
    if m:
        if not task_time:
            raise MarvinError(f"{spec!r} requires a time of day (time) on the task.")
        return epoch_s(day, task_time) - int(m.group(1)) * 60
    check_time(spec)
    return epoch_s(day, spec)


def reminder_ids_of(doc: dict) -> list[str]:
    return [r.get("reminderId") for r in (doc.get("reminders") or []) if isinstance(r, dict) and r.get("reminderId")]


def has_reminder_state(doc: dict) -> bool:
    return bool(doc.get("reminderTime")) or bool(doc.get("reminders"))
