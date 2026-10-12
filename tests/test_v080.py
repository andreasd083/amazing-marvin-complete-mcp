"""Tests for markdown protection, chains with simulated 500/429 in every
step, reminders and habits in the app's form, the recurring-occurrence
filter, notebooks and recurring tasks."""

import json
from datetime import date

import httpx
import pytest

from marvin_mcp import chain as chainmod
from marvin_mcp import ratelimit, recurrence, server
from marvin_mcp.notes import protect_markdown
from marvin_mcp import tools_notebooks as nbt
from marvin_mcp import tools_recurring as rct

from .conftest import RecordingTransport


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(chainmod, "DELETE_SYNC_PAUSE_S", 0.0)
    monkeypatch.setattr(ratelimit, "COOLDOWN_S", 0.0)


class Scripted:
    """Answers per (method, path) and can make call number k fail."""

    def __init__(self, routes, fail_at=None, status=500, persistent=True):
        self.routes = routes
        self.fail = set()
        if fail_at is not None:
            self.fail = {fail_at, fail_at + 1} if persistent else {fail_at}
        self.status = status
        self.n = 0
        self.log: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        k = self.n
        self.n += 1
        path = request.url.path.removeprefix("/api")
        body = json.loads(request.content) if request.content else {}
        self.log.append((request.method, path, body, dict(request.url.params)))
        if k in self.fail:
            return httpx.Response(self.status, text="boom")
        resp = self.routes(request.method, path, dict(request.url.params), body)
        return httpx.Response(200, json=resp)

    def calls(self, path=None):
        return [x for x in self.log if path is None or x[1] == path]


def init(settings, routes, **kw):
    s = Scripted(routes, **kw)
    server.init(settings, transport=RecordingTransport(handler=s))
    return s


def setters_of(body):
    return {x["key"]: x["val"] for x in body["setters"]}


# ------------------------------------------------------------ markdown protection


@pytest.mark.parametrize("src,expected,n", [
    ("- item", "* item", 1),
    ("- [ ] to do", "- [ ] to do", 0),
    ("- [x] done", "- [x] done", 0),
    ("- [X] done", "- [X] done", 0),
    ("---", "---", 0),
    ("  - indented", "  - indented", 0),
    ("```\n- in code\n```", "```\n- in code\n```", 0),
    ("text\n- a\n- [ ] b", "text\n* a\n- [ ] b", 1),
    ("-not an item", "-not an item", 0),
])
def test_protect_markdown_cases(src, expected, n):
    out, changed = protect_markdown(src)
    assert out == expected and changed == n


def test_protect_markdown_empty():
    assert protect_markdown(None) == (None, 0)
    assert protect_markdown("") == ("", 0)


# ------------------------------------------------------------ tasks


TASK = {
    "_id": "t1", "db": "Tasks", "title": "08:15 Task", "day": "2026-10-10", "taskTime": "08:15",
    "reminderTime": 1791612900, "reminders": [{"reminderId": "Mmmmmmmmmmmmm", "time": 1791612600}],
}


def task_routes(doc=TASK, habit=None):
    def routes(method, path, params, body):
        if path == "/doc" and method == "GET":
            if habit and params.get("id") == habit["_id"]:
                return habit
            return doc
        if path == "/markDone":
            return {**doc, "done": True, "doneAt": 1791620000000}
        return {"ok": True}
    return routes


async def test_mark_done_cleans_reminders_like_app(settings):
    s = init(settings, task_routes())
    out = await server.mark_done.fn(item_id="t1")
    assert "error" not in out
    paths = [x[1] for x in s.log]
    assert paths == ["/doc", "/markDone", "/doc/update", "/reminder/delete"]
    upd = setters_of(s.calls("/doc/update")[0][2])
    assert upd["reminderTime"] is None and "reminders" not in upd  # the list stays
    assert s.calls("/reminder/delete")[0][2]["reminderIds"] == ["t1", "Mmmmmmmmmmmmm"]


async def test_mark_done_plain_task_without_reminders(settings):
    s = init(settings, task_routes({"_id": "t2", "db": "Tasks", "title": "x"}))
    await server.mark_done.fn(item_id="t2")
    assert [x[1] for x in s.log] == ["/doc", "/markDone"]


async def test_mark_done_boolean_habit_registers_noon_and_habitrepat(settings):
    doc = {"_id": "h1t", "db": "Tasks", "title": "Habit", "day": "2026-10-10", "habitId": "H"}
    habit = {"_id": "H", "db": "Habits", "recordType": "boolean", "history": []}
    s = init(settings, task_routes(doc, habit))
    out = await server.mark_done.fn(item_id="h1t")
    noon = server.day_noon_ms("2026-10-10")
    uh = s.calls("/updateHabit")[0][2]
    assert uh == {"habitId": "H", "updateDB": True, "time": noon, "value": 1}
    assert setters_of(s.calls("/doc/update")[0][2])["habitRepAt"] == noon
    assert out["habit"]["registered_at"] == noon


async def test_mark_done_number_habit_below_target_leaves_task_open(settings):
    doc = {"_id": "n1", "db": "Tasks", "title": "Glasses", "day": server.local_today(), "habitId": "H"}
    habit = {"_id": "H", "recordType": "number", "target": 3, "history": []}
    s = init(settings, task_routes(doc, habit))
    out = await server.mark_done.fn(item_id="n1", value=1)
    assert not s.calls("/markDone")
    assert out["habit"]["day_total"] == 1 and "1 of 3" in out["note"]


async def test_mark_done_number_habit_reaching_target_marks_done(settings):
    today = server.local_today()
    lo, _ = server.day_bounds_ms(today)
    doc = {"_id": "n1", "db": "Tasks", "title": "Glasses", "day": today, "habitId": "H"}
    habit = {"_id": "H", "recordType": "number", "target": 3, "history": [lo + 1000, 2]}
    s = init(settings, task_routes(doc, habit))
    out = await server.mark_done.fn(item_id="n1", value=1)
    assert s.calls("/markDone")
    assert "habitRepAt" in setters_of(s.calls("/doc/update")[0][2])
    assert out["habit"]["day_total"] == 3


async def test_unmark_done_habit_undo_only_when_last_is_tasks(settings):
    doc = {"_id": "h1t", "db": "Tasks", "habitId": "H", "habitRepAt": 111, "done": True}
    habit = {"_id": "H", "history": [111, 1]}
    s = init(settings, task_routes(doc, habit))
    out = await server.unmark_done.fn(item_id="h1t")
    assert s.calls("/updateHabit")[0][2]["undo"] is True
    st = setters_of(s.calls("/doc/update")[0][2])
    assert st["done"] is False and st["habitRepAt"] is None
    assert out["habit_undone"] is True


async def test_unmark_done_habit_refuses_foreign_registration(settings):
    doc = {"_id": "h1t", "db": "Tasks", "habitId": "H", "habitRepAt": 111, "done": True}
    habit = {"_id": "H", "history": [111, 1, 222, 1]}
    s = init(settings, task_routes(doc, habit))
    out = await server.unmark_done.fn(item_id="h1t")
    assert not s.calls("/updateHabit")
    assert "habit_warning" in out


async def test_delete_task_four_steps_in_order(settings):
    s = init(settings, task_routes())
    out = await server.delete_task.fn(item_id="t1")
    assert [x[1] for x in s.log] == ["/doc", "/doc/update", "/reminder/delete", "/doc/delete"]
    st = setters_of(s.calls("/doc/update")[0][2])
    assert st["reminderTime"] is None and st["reminders"] == []
    assert out["reminders_removed"] == ["t1", "Mmmmmmmmmmmmm"]


async def test_delete_task_refuses_generator(settings):
    s = init(settings, task_routes({"_id": "g", "db": "RecurringTasks"}))
    out = await server.delete_task.fn(item_id="g")
    assert "error" in out and not s.calls("/doc/delete")


async def test_update_task_day_change_moves_t_keeps_m(settings):
    s = init(settings, task_routes())
    out = await server.update_task.fn(item_id="t1", day="2026-10-12")
    st = setters_of(s.calls("/doc/update")[0][2])
    from marvin_mcp import taskreminders as tr
    assert st["reminderTime"] == tr.epoch_s("2026-10-12", "08:15")
    posts = s.calls("/reminder/set")[0][2]["reminders"]
    assert posts[0]["type"] == "T" and posts[0]["reminderId"] == "t1"
    assert any("old times" in n for n in out["notes"])
    assert not s.calls("/reminder/delete")


async def test_update_task_remove_day_clears_time_like_app(settings):
    s = init(settings, task_routes())
    out = await server.update_task.fn(item_id="t1", day="unassigned")
    st = setters_of(s.calls("/doc/update")[0][2])
    assert st["day"] == "unassigned" and st["title"] == "Task"
    for k in ("taskTime", "reminderTime", "reminderOffset", "snooze", "autoSnooze", "autoSnoozeChosen", "remind"):
        assert st[k] is None, k
    assert "reminders" not in st and "dueDate" not in st
    assert s.calls("/reminder/delete")[0][2]["reminderIds"] == ["t1"]  # only T; M stays
    assert not s.calls("/reminder/set")
    assert any("Own reminders stay" in n for n in out["notes"])


async def test_update_task_time_requires_day(settings):
    s = init(settings, task_routes({"_id": "u", "db": "Tasks", "title": "x", "day": "unassigned"}))
    out = await server.update_task.fn(item_id="u", time="09:00")
    assert "requires a day" in out["error"]
    assert not s.calls("/doc/update")


async def test_update_task_set_and_clear_time(settings):
    s = init(settings, task_routes({"_id": "u", "db": "Tasks", "title": "x", "day": "2026-10-10"}))
    await server.update_task.fn(item_id="u", time="09:30")
    st = setters_of(s.calls("/doc/update")[-1][2])
    assert st["title"] == "09:30 x" and st["taskTime"] == "09:30" and st["snooze"] == 5
    assert s.calls("/reminder/set")[-1][2]["reminders"][0]["type"] == "T"
    s2 = init(settings, task_routes({"_id": "u", "db": "Tasks", "title": "09:30 x", "day": "2026-10-10", "taskTime": "09:30"}))
    await server.update_task.fn(item_id="u", time="")
    st = setters_of(s2.calls("/doc/update")[-1][2])
    assert st["title"] == "x" and st["taskTime"] is None
    assert s2.calls("/reminder/delete")[0][2]["reminderIds"] == ["u"]


async def test_update_task_reminders_before_and_absolute(settings):
    s = init(settings, task_routes({"_id": "u", "db": "Tasks", "title": "x", "day": "2026-10-10", "taskTime": "09:30"}))
    await server.update_task.fn(item_id="u", reminders=["before:10", "07:00"])
    st = setters_of(s.calls("/doc/update")[-1][2])
    from marvin_mcp import taskreminders as tr
    assert [r["time"] for r in st["reminders"]] == [tr.epoch_s("2026-10-10", "09:20"), tr.epoch_s("2026-10-10", "07:00")]
    posts = s.calls("/reminder/set")[-1][2]["reminders"]
    assert all(p["type"] == "M" and p["taskId"] == "u" for p in posts)


async def test_update_task_subtasks_and_instance_warning(settings):
    doc = {"_id": "2026-10-10_g", "db": "Tasks", "title": "x", "recurring": True, "recurringTaskId": "g",
           "subtasks": {"Aaaaaaaaaaaaa": {"_id": "Aaaaaaaaaaaaa", "title": "a", "done": False, "rank": 1}}}
    s = init(settings, task_routes(doc))
    out = await server.update_task.fn(item_id="2026-10-10_g", subtasks_add=["new"], subtasks_done=["Aaaaaaaaaaaaa"])
    keys = [x["key"] for x in s.calls("/doc/update")[0][2]["setters"]]
    assert any(k.startswith("subtasks.") and k.count(".") == 1 for k in keys)
    assert "subtasks.Aaaaaaaaaaaaa.done" in keys and "fieldUpdates.subtasks" in keys
    assert any("only to this occurrence" in n for n in out["notes"])


async def test_update_task_unknown_subtask_rejected(settings):
    s = init(settings, task_routes({"_id": "u", "db": "Tasks", "subtasks": {}}))
    out = await server.update_task.fn(item_id="u", subtasks_done=["nope"])
    assert "does not exist" in out["error"] and not s.calls("/doc/update")


async def test_create_task_follow_up_size_time(settings):
    def routes(method, path, params, body):
        if path == "/addTask":
            return {"_id": "new", **body}
        return {"ok": True}
    s = init(settings, routes)
    out = await server.create_task.fn(title="x", day="2026-10-10", size="M", time="10:00", note="- a")
    add = s.calls("/addTask")[0][2]
    assert add["note"] == "* a"
    assert setters_of(s.calls("/doc/update")[0][2])["taskSize"] == 3
    assert s.calls("/reminder/set")
    assert out["notes"]


async def test_create_task_time_without_day_rejected(settings):
    s = init(settings, lambda *a: {"ok": True})
    out = await server.create_task.fn(title="x", time="10:00")
    assert "requires a day" in out["error"] and not s.log


async def test_set_priority_frog_four(settings):
    s = init(settings, lambda *a: {"ok": True})
    await server.set_priority.fn(item_id="t", frog=4)
    assert setters_of(s.calls("/doc/update")[0][2])["isFrogged"] == 4


async def test_set_reminder_on_task_app_form(settings):
    s = init(settings, task_routes({"_id": "u", "db": "Tasks", "title": "x", "reminders": []}))
    out = await server.set_reminder.fn(time_unix_seconds=1791612600, task_id="u")
    st = setters_of(s.calls("/doc/update")[0][2])
    assert st["reminders"][0]["reminderId"] == out["reminder_id"]
    post = s.calls("/reminder/set")[0][2]["reminders"][0]
    assert post["type"] == "M" and post["taskId"] == "u"


async def test_delete_reminder_with_task_removes_from_list(settings):
    s = init(settings, task_routes())
    await server.delete_reminder.fn(reminder_ids=["Mmmmmmmmmmmmm"], task_id="t1")
    assert setters_of(s.calls("/doc/update")[0][2])["reminders"] == []
    assert s.calls("/reminder/delete")[0][2]["reminderIds"] == ["Mmmmmmmmmmmmm"]


# ------------------------------------------------------------ the filter


def listing_routes(items, modes):
    def routes(method, path, params, body):
        if path in ("/todayItems", "/dueItems"):
            return items
        if path == "/doc":
            return {"_id": params["id"], "missedMode": modes.get(params["id"], "")}
        return {}
    return routes


ITEMS = [
    {"_id": "2026-10-08_c", "recurringTaskId": "c", "day": "2026-10-08"},
    {"_id": "2026-10-09_c", "recurringTaskId": "c", "day": "2026-10-09"},
    {"_id": "2026-10-08_s", "recurringTaskId": "s", "day": "2026-10-08"},
    {"_id": "2026-10-10_s", "recurringTaskId": "s", "day": "2026-10-10"},
    {"_id": "2026-10-08_k", "recurringTaskId": "k", "day": "2026-10-08"},
    {"_id": "2026-10-09_k", "recurringTaskId": "k", "day": "2026-10-09", "habitId": "H"},
    {"_id": "plain", "day": "2026-10-01"},
]


@pytest.fixture
def today_1010(monkeypatch):
    monkeypatch.setattr(server, "local_today", lambda now=None: "2026-10-10")


async def test_filter_future_occurrences_read_nothing(settings, today_1010):
    items = [{"_id": "2026-10-10_f", "recurringTaskId": "f", "day": "2026-10-10"},
             {"_id": "2026-10-11_f", "recurringTaskId": "f", "day": "2026-10-11"}]
    s = init(settings, listing_routes(items, {"f": "carry"}))
    out = await server.get_due_items.fn(by="2026-10-11")
    assert not s.calls("/doc") and out["recurring_reads"] == 0 and out["count"] == 2


async def test_filter_cache_hit_reads_nothing_and_midnight_resets(settings, monkeypatch):
    day = ["2026-10-10"]
    monkeypatch.setattr(server, "local_today", lambda now=None: day[0])
    s = init(settings, listing_routes(ITEMS, {"c": "carry"}))
    await server.get_today_items.fn(date="2026-10-10")
    n = len(s.calls("/doc"))
    out = await server.get_today_items.fn(date="2026-10-10")
    assert len(s.calls("/doc")) == n and out["recurring_reads"] == 0
    day[0] = "2026-10-11"  # day rollover
    out = await server.get_today_items.fn(date="2026-10-10")
    assert len(s.calls("/doc")) > n and out["recurring_reads"] > 0


async def test_filter_cache_invalidated_by_own_write(settings, today_1010):
    s = init(settings, listing_routes(ITEMS, {"c": "carry"}))
    await server.get_today_items.fn(date="2026-10-10")
    assert "c" in server._missed_cache
    gen = {"_id": "c", "db": "RecurringTasks", "type": "daily", "parentId": "P", "missedMode": "carry"}
    s2 = init(settings, rec_routes(gen, []))
    server._missed_cache["c"] = ("2026-10-10", "carry")
    await rct.update_recurring_task.fn(recurring_task_id="c", if_you_miss_one="keep_them_all")
    assert "c" not in server._missed_cache


async def test_filter_read_failure_shows_occurrences(settings, today_1010):
    def handler(request):
        path = request.url.path.removeprefix("/api")
        if path == "/doc":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=ITEMS)
    server.init(settings, transport=RecordingTransport(handler=handler))
    out = await server.get_today_items.fn(date="2026-10-10")
    assert out["count"] == len(ITEMS) and out["hidden_count"] == 0  # always included
    assert "shown rather than hidden" in out["filter_warning"]


async def test_today_filter_carry_skip_stack(settings, today_1010):
    init(settings, listing_routes(ITEMS, {"c": "carry", "s": "skip", "k": "stack"}))
    out = await server.get_today_items.fn(date="2026-10-10")
    ids = [i["_id"] for i in out["items"]]
    assert ids == ["2026-10-08_c", "2026-10-10_s", "2026-10-08_k", "2026-10-09_k", "plain"]
    assert out["hidden_count"] == 2
    assert out["habit_task_ids"] == ["2026-10-09_k"]


async def test_today_filter_include_hidden_and_cache(settings, today_1010):
    s = init(settings, listing_routes(ITEMS, {"c": "carry"}))
    out = await server.get_today_items.fn(date="2026-10-10", include_hidden=True)
    assert out["count"] == len(ITEMS) and out["hidden_count"] == 1
    assert "assumption" in out
    n_doc = len(s.calls("/doc"))
    await server.get_today_items.fn(date="2026-10-10")
    assert len(s.calls("/doc")) == n_doc  # cached


async def test_due_filter_only_carry(settings, today_1010):
    init(settings, listing_routes(ITEMS, {"c": "carry", "s": "skip"}))
    out = await server.get_due_items.fn(by="2026-10-10")
    assert out["hidden_count"] == 1 and "2026-10-08_s" in [i["_id"] for i in out["items"]]


# ------------------------------------------------------------ notebooks


def nb_routes(nb, pages, parent_doc=None, by_parent=None):
    def routes(method, path, params, body):
        if path == "/notebook":
            if "parentId" in params:
                return by_parent if by_parent is not None else {"notebook": None, "pages": []}
            return {"notebook": nb, "pages": pages}
        if path == "/notebooks":
            return [{**nb, "pages": pages}]
        if path == "/doc" and method == "GET":
            if parent_doc and params["id"] == parent_doc["_id"]:
                return parent_doc
            return next((p for p in pages if p["_id"] == params["id"]), {})
        return {"ok": True}
    return routes


NB_PARENT = {"_id": "nb_A", "title": "", "parentId": "proj", "parentType": "project", "sections": []}
PAGES2 = [
    {"_id": "pg_1", "db": "Pages", "notebookId": "nb_A", "rank": 1, "title": "Notes", "note": "page one"},
    {"_id": "pg_2", "db": "Pages", "notebookId": "nb_A", "rank": 2, "title": "Two", "note": "page two"},
]


async def test_delete_notebook_two_step_then_write_back_after(settings):
    s = init(settings, nb_routes(NB_PARENT, PAGES2))
    out = await nbt.delete_notebook.fn(notebook_id="nb_A")
    assert out["needs_confirm"] and not s.calls("/doc/delete")
    out = await nbt.delete_notebook.fn(notebook_id="nb_A", confirm=True)
    seq = [(x[1], x[2].get("itemId")) for x in s.log if x[0] == "POST"]
    assert seq == [("/doc/delete", "pg_1"), ("/doc/delete", "pg_2"), ("/doc/delete", "nb_A"), ("/doc/update", "proj")]
    assert setters_of(s.calls("/doc/update")[0][2])["note"] == "page one"
    assert out["saved_text"] == "page one"


async def test_delete_notebook_single_page_parent_no_confirm(settings):
    s = init(settings, nb_routes(NB_PARENT, PAGES2[:1]))
    out = await nbt.delete_notebook.fn(notebook_id="nb_A")
    assert "needs_confirm" not in out and out["note_written_back_to"] == "proj"


async def test_update_notebook_refuses_title_on_parented(settings):
    s = init(settings, nb_routes(NB_PARENT, PAGES2))
    out = await nbt.update_notebook.fn(notebook_id="nb_A", title="New")
    assert "only standalone notebooks" in out["error"] and not s.calls("/doc/update")


async def test_update_notebook_delete_section_unsections_pages_first(settings):
    nb = {"_id": "nb_F", "title": "F", "parentId": None, "sections": [{"id": "sec_1", "title": "S", "rank": 1}]}
    pages = [{"_id": "pg_x", "db": "Pages", "notebookId": "nb_F", "rank": 1, "sectionId": "sec_1", "note": ""}]
    s = init(settings, nb_routes(nb, pages))
    await nbt.update_notebook.fn(notebook_id="nb_F", sections_delete=["sec_1"])
    ups = [x[2]["itemId"] for x in s.calls("/doc/update")]
    assert ups == ["pg_x", "nb_F"]
    assert setters_of(s.calls("/doc/update")[1][2])["sections"] == []


async def test_move_page_to_project_without_notebook_creates_notes_page(settings):
    nb = {"_id": "nb_F", "title": "F", "parentId": None, "sections": [{"id": "sec_1", "title": "S", "rank": 1}]}
    pages = [{"_id": "pg_x", "db": "Pages", "notebookId": "nb_F", "rank": 1, "sectionId": "sec_1", "note": "t"}]
    parent = {"_id": "proj", "db": "Categories", "type": "project", "note": "old note"}
    s = init(settings, nb_routes(nb, pages, parent_doc=parent))
    out = await nbt.update_notebook_page.fn(page_id="pg_x", move_to_parent_id="proj")
    creates = [x[2] for x in s.calls("/doc/create")]
    assert creates[0]["db"] == "Notebooks" and creates[0]["parentId"] == "proj"
    assert creates[1]["title"] == "Notes" and creates[1]["originalNoteFrom"] == "proj" and creates[1]["note"] == "old note"
    st = setters_of(s.calls("/doc/update")[0][2])
    assert st["rank"] == 2 and st["sectionId"] is None and st["notebookId"] == creates[0]["_id"]
    assert any("section does not follow" in n for n in out["notes"])


@pytest.mark.parametrize("ptype", ["project", "category"])
@pytest.mark.parametrize("note", ["", None])
async def test_move_page_to_parent_without_note(settings, ptype, note):
    nb = {"_id": "nb_F", "title": "F", "parentId": None, "sections": []}
    pages = [{"_id": "pg_x", "db": "Pages", "notebookId": "nb_F", "rank": 1, "sectionId": None, "note": "t"}]
    parent = {"_id": "par", "db": "Categories", "type": ptype, "note": note}
    s = init(settings, nb_routes(nb, pages, parent_doc=parent))
    out = await nbt.update_notebook_page.fn(page_id="pg_x", move_to_parent_id="par")
    creates = [x[2] for x in s.calls("/doc/create")]
    assert len(creates) == 1 and creates[0]["db"] == "Notebooks"
    assert creates[0]["parentType"] == ptype and creates[0]["rank"] == 1 and creates[0]["sections"] is None
    st = setters_of(s.calls("/doc/update")[0][2])
    assert st["rank"] == 1 and st["sectionId"] is None and st["notebookId"] == creates[0]["_id"]
    assert not any("untested" in n for n in out.get("notes", []))


async def test_move_page_to_category_with_note_creates_notes_page(settings):
    nb = {"_id": "nb_F", "title": "F", "parentId": None, "sections": []}
    pages = [{"_id": "pg_x", "db": "Pages", "notebookId": "nb_F", "rank": 1, "note": "t"}]
    parent = {"_id": "cat", "db": "Categories", "type": "category", "note": "category note"}
    s = init(settings, nb_routes(nb, pages, parent_doc=parent))
    await nbt.update_notebook_page.fn(page_id="pg_x", move_to_parent_id="cat")
    creates = [x[2] for x in s.calls("/doc/create")]
    assert creates[1]["title"] == "Notes" and creates[1]["originalNoteFrom"] == "cat" and "migratedAt" in creates[1]
    assert setters_of(s.calls("/doc/update")[0][2])["rank"] == 2


async def test_move_first_page_out_of_parented_refused(settings):
    s = init(settings, nb_routes(NB_PARENT, PAGES2))
    out = await nbt.update_notebook_page.fn(page_id="pg_1", move_to_notebook_id="nb_B")
    assert "first page" in out["error"] and not s.calls("/doc/update")


async def test_update_page_append_and_protection(settings):
    s = init(settings, nb_routes(NB_PARENT, PAGES2))
    out = await nbt.update_notebook_page.fn(page_id="pg_2", append="- new line")
    assert setters_of(s.calls("/doc/update")[0][2])["note"] == "page two\n\n* new line"
    assert out["notes"]


async def test_delete_page_last_in_parented_refused_and_text_confirm(settings):
    s = init(settings, nb_routes(NB_PARENT, PAGES2[:1]))
    out = await nbt.delete_notebook_page.fn(page_id="pg_1")
    assert "delete_notebook" in out["error"]
    s = init(settings, nb_routes(NB_PARENT, PAGES2))
    out = await nbt.delete_notebook_page.fn(page_id="pg_2")
    assert out["needs_confirm"] and not s.calls("/doc/delete")


async def test_create_page_rank_after_existing(settings):
    s = init(settings, nb_routes(NB_PARENT, PAGES2))
    await nbt.create_notebook_page.fn(notebook_id="nb_A", title="Three", text="x")
    doc = s.calls("/doc/create")[0][2]
    assert doc["rank"] == 3 and doc["_id"].startswith("pg_") and doc["hasBody"] is True


async def test_search_notebooks_caches_page_text(settings):
    s = init(settings, nb_routes(NB_PARENT, PAGES2))
    out = await nbt.search_notebooks.fn(query="two")
    assert out["count"] >= 1 and out["notebooks_fetched"] == 1
    out = await nbt.search_notebooks.fn(query="two")
    assert out["notebooks_fetched"] == 0


# ------------------------------------------------------------ recurring tasks


async def test_create_recurring_weekly_requires_weekday(settings):
    s = init(settings, lambda *a: {"ok": True})
    out = await rct.create_recurring_task.fn(title="x", repeat="weekly")
    assert "weekday" in out["error"] and not s.log


async def test_create_recurring_only_weekdays_validated(settings):
    init(settings, lambda *a: {"ok": True})
    out = await rct.create_recurring_task.fn(title="x", repeat="daily", only_weekdays=True)
    assert "only_weekdays" in out["error"]


async def test_create_recurring_doc_shape(settings):
    s = init(settings, lambda *a: {"ok": True})
    out = await rct.create_recurring_task.fn(title="x", repeat="x_times_per_week", weekdays=["mon", "wed", "fri"],
                                             create_occurrences="3", if_you_miss_one="keep_just_one", size="S")
    doc = s.calls("/doc/create")[0][2]
    assert doc["db"] == "RecurringTasks" and doc["type"] == "n per week" and doc["weekDays"] == [1, 3, 5]
    assert doc["createAhead"] == 3 and doc["missedMode"] == "carry" and doc["taskSize"] == 2
    assert out["recurring_task_id"] == doc["_id"]


async def test_create_recurring_default_omits_createahead(settings):
    s = init(settings, lambda *a: {"ok": True})
    await rct.create_recurring_task.fn(title="x", repeat="daily", create_occurrences="default")
    assert "createAhead" not in s.calls("/doc/create")[0][2]


async def test_create_recurring_echo_writes_first_occurrence(settings):
    s = init(settings, lambda *a: {"ok": True})
    out = await rct.create_recurring_task.fn(title="x", repeat="after_completion", after_days=2)
    gen, inst = [x[2] for x in s.calls("/doc/create")]
    assert gen["type"] == "echo" and gen["repeat"] == 2 and gen["echoDays"] == 2
    assert inst["echo"] is True and inst["echoId"] is None and inst["recurringTaskId"] == gen["_id"]
    assert inst["subtasks"] == {}


async def test_echo_first_occurrence_inherits_like_app(settings):
    s = init(settings, lambda *a: {"ok": True})
    out = await rct.create_recurring_task.fn(title="x", repeat="after_completion", after_days=2, subtasks=["a"],
                                       label_ids=["L"], priority=2, size="S", note="n")
    gen, inst = [x[2] for x in s.calls("/doc/create")]
    (sid, sub), = gen["subtasks"].items()
    day = inst["day"]
    assert list(inst["subtasks"]) == [f"{day}_{sid}"] and inst["subtasks"][f"{day}_{sid}"]["_id"] == f"{day}_{sid}"
    assert inst["subtasks"][f"{day}_{sid}"]["title"] == "a"
    assert (inst["labelIds"], inst["isStarred"], inst["taskSize"], inst["note"]) == (["L"], 2, 2, "n")
    assert out["first_occurrence_id"] == inst["_id"]


async def test_create_recurring_echo_future_start_allowed(settings):
    s = init(settings, lambda *a: {"ok": True})
    out = await rct.create_recurring_task.fn(title="x", repeat="after_completion", after_days=2, starts_on="2099-01-01")
    assert "error" not in out
    assert s.calls("/doc/create")[1][2]["day"] == "2099-01-01"


async def test_create_recurring_custom_in_words_refused(settings):
    s = init(settings, lambda *a: {"ok": True})
    out = await rct.create_recurring_task.fn(title="x", repeat="custom", custom="every other friday")
    assert "RRULE" in out["error"] and not s.log


async def test_update_recurring_custom_in_words_refused(settings):
    s = init(settings, rec_routes(GEN, []))
    out = await rct.update_recurring_task.fn(recurring_task_id="g", custom="every other friday")
    assert "RRULE" in out["error"] and not s.log


async def test_create_recurring_custom_rrule_creates(settings):
    s = init(settings, lambda *a: {"ok": True})
    out = await rct.create_recurring_task.fn(title="x", repeat="custom", custom="RRULE:FREQ=WEEKLY;BYDAY=TU")
    assert "error" not in out
    doc = s.calls("/doc/create")[0][2]
    assert doc["type"] == "custom" and doc["customRecurrence"] == "RRULE:FREQ=WEEKLY;BYDAY=TU"
    assert out["recurring_task_id"] == doc["_id"]


def echo_routes(day):
    doc = {"_id": "e1", "db": "Tasks", "title": "Echo", "day": day, "echo": True, "recurringTaskId": "g"}
    gen = {"_id": "g", "db": "RecurringTasks", "type": "echo", "echoDays": 2}
    return task_routes(doc, gen)


async def test_mark_done_echo_future_api_mode(settings, monkeypatch):
    monkeypatch.setattr(recurrence, "ECHO_EARLY_DONE_MODE", "api")
    s = init(settings, echo_routes("2099-01-13"))
    out = await server.mark_done.fn(item_id="e1")
    assert not s.calls("/doc/update")  # no extra write
    assert "2099-01-15" in out["echo"] and "the app would have counted from today" in out["echo"]


async def test_mark_done_echo_future_app_mode(settings, monkeypatch):
    monkeypatch.setattr(recurrence, "ECHO_EARLY_DONE_MODE", "app")
    s = init(settings, echo_routes("2099-01-13"))
    out = await server.mark_done.fn(item_id="e1")
    ups = s.calls("/doc/update")
    assert setters_of(ups[0][2])["day"] == server.local_today()
    assert [x[1] for x in s.log].index("/doc/update") < [x[1] for x in s.log].index("/markDone")
    assert "moved to today" in out["echo"]


async def test_unmark_done_undo_never_sends_time(settings):
    doc = {"_id": "h1t", "db": "Tasks", "habitId": "H", "habitRepAt": 111, "done": True}
    habit = {"_id": "H", "history": [111, 1]}
    s = init(settings, task_routes(doc, habit))
    await server.unmark_done.fn(item_id="h1t")
    body = s.calls("/updateHabit")[0][2]
    assert body.get("undo") is True and "time" not in body and "value" not in body
    # the comparison happens before undo: the reads come first
    paths = [x[1] for x in s.log]
    assert paths.index("/updateHabit") > 1


GEN = {"_id": "g", "db": "RecurringTasks", "type": "daily", "repeatStart": "2026-10-09", "createAhead": 7,
       "parentId": "P", "missedMode": "stack", "day": 5, "date": 9, "weekDays": [5], "repeat": 1}


def rec_routes(gen, occ):
    def routes(method, path, params, body):
        if path == "/doc" and method == "GET":
            return gen
        if path == "/children":
            return occ
        return {"ok": True}
    return routes


GEN_AT = 1791583200000


def occ(day, touched=False, **kw):
    fu = {"masterRank": GEN_AT + 999_999, "dueDate": GEN_AT + 999_999, "updatedAt": GEN_AT + 999_999}
    if touched:
        fu["title"] = GEN_AT + 120_000
    return {"_id": f"{day}_g", "recurringTaskId": "g", "day": day, "_rev": "2-x", "done": False,
            "generatedAt": GEN_AT, "fieldUpdates": fu, **kw}


async def test_update_recurring_rule_change_lists_then_confirm_removes(settings, monkeypatch):
    monkeypatch.setattr(rct, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: __import__("datetime").datetime(2026, 10, 11, 9, tzinfo=tz))}))
    occs = [occ("2026-10-11"), occ("2026-10-12"), occ("2026-10-13", touched=True), occ("2026-10-10")]
    s = init(settings, rec_routes(GEN, occs))
    out = await rct.update_recurring_task.fn(recurring_task_id="g", repeat="every_x_days", interval=2)
    cl = out["cleanup"]
    assert [x["id"] for x in cl["to_remove"]] == ["2026-10-12_g"]  # the 11th matches (9, 11, 13), the 13th was edited
    assert {x["id"] for x in cl["kept"]} == {"2026-10-13_g", "2026-10-10_g"}
    assert not s.calls("/doc/delete")
    out = await rct.update_recurring_task.fn(recurring_task_id="g", repeat="every_x_days", interval=2, confirm=True)
    assert [x[2]["itemId"] for x in s.calls("/doc/delete")] == ["2026-10-12_g"]
    dd = [setters_of(x[2]) for x in s.calls("/doc/update") if x[2]["itemId"] == "g" and "deletedDates" in setters_of(x[2])]
    assert not dd  # the cleanup does not write deletedDates


def test_untouched_definition():
    from marvin_mcp.recurrence import occurrence_untouched as u
    assert u(occ("2026-10-12"))  # _rev 2 with the client's stamps is untouched
    assert not u(occ("2026-10-12", touched=True))
    o = occ("2026-10-12"); o.pop("generatedAt")
    assert not u(o)
    o = occ("2026-10-12"); o["day"] = "2026-10-13"
    assert not u(o)
    o = occ("2026-10-12"); o["fieldUpdates"]["note"] = GEN_AT + 30_000
    assert u(o)  # within 60 s of generation


def del_routes(gen, occs, doc):
    def routes(method, path, params, body):
        if path == "/doc" and method == "GET":
            return gen if params["id"] == gen["_id"] else doc
        if path == "/children":
            return occs
        return {"ok": True}
    return routes


async def test_delete_task_occurrence_this_writes_deleted_dates(settings):
    gen = {"_id": "g", "db": "RecurringTasks", "parentId": "P", "deletedDates": ["2026-10-01"]}
    doc = occ("2026-10-13")
    s = init(settings, del_routes(gen, [], doc))
    out = await server.delete_task.fn(item_id="2026-10-13_g")
    ups = s.calls("/doc/update")
    assert ups[0][2]["itemId"] == "g"
    st = setters_of(ups[0][2])
    assert st["deletedDates"] == ["2026-10-01", "2026-10-13"] and "fieldUpdates.deletedDates" in st and "updatedAt" in st
    assert [x[2]["itemId"] for x in s.calls("/doc/delete")] == ["2026-10-13_g"]
    assert out["deleted_dates"][-1] == "2026-10-13"


async def test_delete_task_occurrence_all_keeps_done(settings):
    gen = {"_id": "g", "db": "RecurringTasks", "parentId": "P"}
    occs = [occ("2026-10-12"), occ("2026-10-13"), {**occ("2026-10-11"), "done": True}]
    s = init(settings, del_routes(gen, occs, occs[0]))
    out = await server.delete_task.fn(item_id="2026-10-12_g", scope="all")
    deleted = [x[2]["itemId"] for x in s.calls("/doc/delete")]
    assert deleted == ["2026-10-12_g", "2026-10-13_g", "g"]
    assert out["deleted_recurring_task"] == "g"


async def test_delete_task_recurring_id_all_without_occurrences(settings):
    # the recurring task's own id with scope=all, before any occurrences exist
    gen = {"_id": "g", "db": "RecurringTasks", "recurringType": "task", "parentId": "P"}
    s = init(settings, del_routes(gen, [], gen))
    out = await server.delete_task.fn(item_id="g", scope="all")
    assert [x[2]["itemId"] for x in s.calls("/doc/delete")] == ["g"]
    assert out["deleted_recurring_task"] == "g" and out["deleted_occurrences"] == []


async def test_delete_task_recurring_id_all_with_occurrences(settings):
    gen = {"_id": "g", "db": "RecurringTasks", "recurringType": "task", "parentId": "P"}
    occs = [occ("2026-10-12"), {**occ("2026-10-11"), "done": True}]
    s = init(settings, del_routes(gen, occs, gen))
    await server.delete_task.fn(item_id="g", scope="all")
    assert [x[2]["itemId"] for x in s.calls("/doc/delete")] == ["2026-10-12_g", "g"]


async def test_delete_task_recurring_id_refuses_this_and_habit(settings):
    gen = {"_id": "g", "db": "RecurringTasks", "recurringType": "task", "parentId": "P"}
    s = init(settings, del_routes(gen, [], gen))
    out = await server.delete_task.fn(item_id="g")
    assert "scope='all'" in out["error"] and not s.calls("/doc/delete")
    hab = {**gen, "recurringType": "habit"}
    s = init(settings, del_routes(hab, [], hab))
    out = await server.delete_task.fn(item_id="g", scope="all")
    assert "Habit recurrences" in out["error"] and not s.calls("/doc/delete")


async def test_update_recurring_refuses_echo_switch(settings):
    s = init(settings, rec_routes(GEN, []))
    out = await rct.update_recurring_task.fn(recurring_task_id="g", repeat="after_completion")
    assert "After completion" in out["error"] and not s.calls("/doc/update")


async def test_update_recurring_properties_no_cleanup(settings):
    s = init(settings, rec_routes(GEN, []))
    out = await rct.update_recurring_task.fn(recurring_task_id="g", label_ids=["L"], ends_on="2026-12-31")
    assert "cleanup" not in out and not s.calls("/children")
    assert any("not corrected" in n for n in out["notes"])


def test_rule_matches_types():
    a = date(2026, 10, 9)  # Friday
    m = recurrence.rule_matches
    assert m({"type": "weekly", "day": 3}, date(2026, 10, 14), a)  # Wednesday
    assert m({"type": "monthly", "date": 20}, date(2026, 10, 20), a)
    assert m({"type": "n per week", "weekDays": [1, 3, 5]}, date(2026, 10, 12), a)
    assert m({"type": "repeat", "repeat": 4}, date(2026, 10, 13), a)
    assert not m({"type": "repeat", "repeat": 4}, date(2026, 10, 12), a)
    assert m({"type": "repeat week", "repeat": 2}, date(2026, 10, 23), a)
    assert m({"type": "repeat month", "repeat": 3}, date(2027, 1, 9), a)
    assert m({"type": "repeat year", "repeat": 1}, date(2027, 10, 9), a)
    assert m({"type": "onOff", "onCount": 2, "offCount": 5}, date(2026, 10, 10), a)
    assert not m({"type": "onOff", "onCount": 2, "offCount": 5}, date(2026, 10, 11), a)
    # Matches the app's 'Next: Tue 2026-10-20 · 2026-11-03' for this rule (start 2026-10-09)
    rr = {"type": "custom", "customRecurrence": "RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU"}
    assert m(rr, date(2026, 10, 20), a) and m(rr, date(2026, 11, 3), a)
    assert not m(rr, date(2026, 10, 27), a)
    assert not m({"type": "daily", "endDate": "2026-10-10"}, date(2026, 10, 11), a)


def test_rule_not_computable_for_words_and_weekdays():
    assert recurrence.rule_computable({"type": "custom", "customRecurrence": "2 weeks on Tuesday"})[0] is False
    assert recurrence.rule_computable({"type": "monthly", "limitToWeekdays": True})[0] is False
    assert recurrence.rule_computable({"type": "echo"})[0] is False


def test_constants_on_one_place():
    assert recurrence.CLEANUP_MODE == "list"
    assert recurrence.RULE_ANCHOR == "repeatStart"
    assert recurrence.ECHO_EARLY_DONE_MODE == "app"
    assert chainmod.DELETE_SYNC_PAUSE_S == 0.0  # the fixture; the module default is 20


# ------------------------------------------------------------ chains: 500/429 in every step


def _cases():
    nb_one = nb_routes(NB_PARENT, PAGES2[:1])
    move = nb_routes({"_id": "nb_F", "parentId": None, "sections": []},
                     [{"_id": "pg_x", "db": "Pages", "notebookId": "nb_F", "rank": 1, "note": ""}],
                     parent_doc={"_id": "proj", "db": "Categories", "type": "project", "note": "n"})

    def add_routes(method, path, params, body):
        return {"_id": "new", **body} if path == "/addTask" else {"ok": True}

    return [
        ("mark_done", task_routes(), lambda: server.mark_done.fn(item_id="t1"), 4),
        ("delete_task", task_routes(), lambda: server.delete_task.fn(item_id="t1"), 4),
        ("update_task_day", task_routes(), lambda: server.update_task.fn(item_id="t1", day="2026-10-12"), 3),
        ("create_task_follow", add_routes, lambda: server.create_task.fn(title="x", day="2026-10-10", size="M", time="10:00"), 4),
        ("delete_notebook", nb_one, lambda: nbt.delete_notebook.fn(notebook_id="nb_A"), 4),
        ("move_page", move, lambda: nbt.update_notebook_page.fn(page_id="pg_x", move_to_parent_id="proj"), 7),
        ("echo_create", lambda *a: {"ok": True}, lambda: rct.create_recurring_task.fn(title="x", repeat="after_completion", after_days=2), 2),
    ]


@pytest.mark.parametrize("status", [500, 429])
@pytest.mark.parametrize("case", _cases(), ids=lambda c: c[0])
async def test_every_step_failure_reports_completed_steps(settings, case, status):
    name, routes, call, n_requests = case
    for k in range(n_requests):
        s = init(settings, routes, fail_at=k, status=status)
        out = await call()
        assert "error" in out, (name, k)
        if not (name == "create_task_follow" and k == 0):
            assert "completed_steps" in out and "failed_step" in out, (name, k, out)


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c[0])
async def test_single_transient_500_is_retried(settings, case):
    name, routes, call, n_requests = case
    for k in range(n_requests):
        if name == "create_task_follow" and k == 0:
            continue  # /addTask is not a chain step; a failure there is just an error
        s = init(settings, routes, fail_at=k, status=500, persistent=False)
        out = await call()
        assert "error" not in out, (name, k, out)


async def test_delete_notebook_failure_keeps_saved_text(settings):
    s = init(settings, nb_routes(NB_PARENT, PAGES2[:1]), fail_at=3)  # the write-back fails
    out = await nbt.delete_notebook.fn(notebook_id="nb_A")
    assert out["failed_step"] == "write back note"
    assert out["saved_text"] == "page one"
    assert "delete notebook" in out["completed_steps"]
