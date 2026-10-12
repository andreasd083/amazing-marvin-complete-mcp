"""Tests for mark_project_done in two steps, delete_task refusing projects with
open children, unmark_done refusing projects, and the last-call time in
get_rate_limit_status."""

from marvin_mcp import server
from marvin_mcp.client import local_today

from .test_v080 import init, setters_of

PROJ = {"_id": "P", "db": "Categories", "type": "project", "title": "Proj", "day": "", "dueDate": "2026-10-20"}
T1 = {"_id": "t1", "db": "Tasks", "title": "One", "parentId": "P"}
T2 = {"_id": "t2", "db": "Tasks", "title": "Two", "parentId": "P",
      "reminderTime": 1791620000, "reminders": [{"reminderId": "abcdefghijklm", "time": 1791620000}]}


def project_routes(proj=PROJ, kids=(T1, T2)):
    state = {"kids": list(kids)}

    def routes(method, path, params, body):
        if path == "/doc" and method == "GET":
            return proj
        if path == "/children":
            return state["kids"]
        if path == "/markDone":
            return {"_id": body.get("itemId"), "done": True, "doneAt": 1791620000000}
        return {"ok": True}
    return routes, state


async def test_step1_lists_children_and_writes_nothing(settings):
    routes, _ = project_routes()
    s = init(settings, routes)
    out = await server.mark_project_done.fn(project_id="P")
    assert out["needs_confirm"] is True and len(out["confirm_code"]) == 8
    assert [x["id"] for x in out["open_children"]] == ["t1", "t2"]
    assert "2 tasks will also be marked done" in out["warning"]
    assert [x[1] for x in s.log] == ["/doc", "/children"]


async def test_step2_closes_children_then_project_with_app_fields(settings):
    routes, _ = project_routes()
    s = init(settings, routes)
    code = (await server.mark_project_done.fn(project_id="P"))["confirm_code"]
    s.log.clear()
    out = await server.mark_project_done.fn(project_id="P", confirm_code=code)
    paths = [x[1] for x in s.log]
    assert paths == ["/doc", "/children", "/markDone", "/markDone", "/doc/update", "/reminder/delete", "/doc/update"]
    assert out["children_done"] == ["t1", "t2"] and out["reminders_removed"]
    proj = setters_of(s.log[-1][2])
    today = local_today()
    assert s.log[-1][2]["itemId"] == "P"
    assert proj["done"] is True and proj["day"] == today and proj["doneDate"] == today
    assert proj["doneAt"] == proj["workedOnAt"]
    assert "dueDate" not in proj
    for f in ("done", "doneAt", "doneDate", "day", "workedOnAt"):
        assert f"fieldUpdates.{f}" in proj


async def test_existing_project_day_is_kept_and_becomes_done_date(settings):
    routes, _ = project_routes({**PROJ, "day": "2026-10-15"}, kids=())
    s = init(settings, routes)
    code = (await server.mark_project_done.fn(project_id="P"))["confirm_code"]
    await server.mark_project_done.fn(project_id="P", confirm_code=code)
    proj = setters_of(s.calls("/doc/update")[-1][2])
    assert proj["day"] == "2026-10-15" and proj["doneDate"] == "2026-10-15"


async def test_step2_with_changed_children_closes_nothing(settings):
    routes, state = project_routes()
    s = init(settings, routes)
    code = (await server.mark_project_done.fn(project_id="P"))["confirm_code"]
    state["kids"].append({"_id": "t3", "db": "Tasks", "title": "New"})
    s.log.clear()
    out = await server.mark_project_done.fn(project_id="P", confirm_code=code)
    assert "nothing closed" in out["error"] and out["confirm_code"] != code
    assert [x[1] for x in s.log] == ["/doc", "/children"]


async def test_made_up_code_closes_nothing(settings):
    routes, _ = project_routes(kids=())
    s = init(settings, routes)
    out = await server.mark_project_done.fn(project_id="P", confirm_code="00000000")
    assert "error" in out and not s.calls("/doc/update")


async def test_blocking_children_stop_in_step1_without_code(settings):
    kids = (T1, {"_id": "sp", "db": "Categories", "type": "project", "title": "Sub"},
            {"_id": "h", "db": "Tasks", "title": "Habit", "habitId": "H"},
            {"_id": "o", "db": "Tasks", "title": "Occurrence", "recurringTaskId": "R"})
    routes, _ = project_routes(kids=kids)
    s = init(settings, routes)
    out = await server.mark_project_done.fn(project_id="P")
    assert "confirm_code" not in out
    assert [b["kind"] for b in out["blocking_children"]] == ["project", "habit_task", "occurrence"]
    assert not s.calls("/markDone")


async def test_not_a_project_and_already_done(settings):
    routes, _ = project_routes({**PROJ, "type": "category"})
    init(settings, routes)
    assert "not a project" in (await server.mark_project_done.fn(project_id="P"))["error"]
    routes, _ = project_routes({**PROJ, "done": True})
    init(settings, routes)
    assert "already done" in (await server.mark_project_done.fn(project_id="P"))["error"]


async def test_failure_on_child_leaves_project_open(settings):
    routes, _ = project_routes()
    s = init(settings, routes)
    code = (await server.mark_project_done.fn(project_id="P"))["confirm_code"]
    s.log.clear()
    s.n = 0
    s.fail = {3, 4}  # the second /markDone, and its retry
    out = await server.mark_project_done.fn(project_id="P", confirm_code=code)
    assert out["failed_step"] == "mark done t2" and out["children_done"] == ["t1"]
    assert not [x for x in s.calls("/doc/update") if x[2].get("itemId") == "P"]


async def test_delete_task_refuses_project_with_open_children(settings):
    routes, _ = project_routes()
    s = init(settings, routes)
    out = await server.delete_task.fn(item_id="P")
    assert "nothing deleted" in out["error"] and len(out["open_children"]) == 2
    assert not s.calls("/doc/delete")


async def test_delete_task_childless_project_reports_notebook(settings):
    routes, _ = project_routes({**PROJ, "notebookId": "nb_x"}, kids=())
    s = init(settings, routes)
    out = await server.delete_task.fn(item_id="P")
    assert [x[1] for x in s.log] == ["/doc", "/children", "/doc/delete"]
    assert out["notebook_left"] == "nb_x" and "delete_notebook" in out["note"]


async def test_delete_task_on_task_reads_no_children(settings):
    routes, _ = project_routes(T1)
    s = init(settings, routes)
    await server.delete_task.fn(item_id="t1")
    assert not s.calls("/children")


async def test_rate_limit_status_shows_last_call(settings):
    routes, _ = project_routes()
    init(settings, routes)
    first = await server.get_rate_limit_status.fn()
    assert first["last_call_at"] is None
    await server.get_children.fn(parent_id="P")
    out = await server.get_rate_limit_status.fn()
    assert out["calls_today"] == 1 and out["seconds_since_last_call"] <= 1
    assert out["last_call_at"].endswith(("CEST", "CET"))


async def test_unmark_done_refuses_project(settings):
    """Projects are reopened in the app, not via unmark_done."""
    routes, _ = project_routes({**PROJ, "done": True})
    s = init(settings, routes)
    out = await server.unmark_done.fn(item_id="P")
    assert "Reopen a closed project in the app" in out["error"]
    assert [x[1] for x in s.log] == ["/doc"]
