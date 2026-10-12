"""Tests for get_notebook on a 404 for parent_id, get_notebooks/search_notebooks
with missing or done parents, unmark_done on habit tasks and on the day set by
mark_done, and update_task's `updated` after the last write."""

import httpx

from marvin_mcp import server
from marvin_mcp import tools_notebooks as nbt

from .conftest import RecordingTransport
from .test_v080 import Scripted, init, setters_of, task_routes


def init_status(settings, handler):
    server.init(settings, transport=RecordingTransport(handler=handler))


async def test_get_notebook_parent_404_returns_error_pointing_to_notebook_id(settings):
    log = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        params = dict(request.url.params)
        log.append((path, params))
        if path == "/notebook" and "parentId" in params:
            return httpx.Response(404, text="Notebook not found")
        if path == "/doc":
            return httpx.Response(200, json={"_id": "P", "db": "Categories", "notebookId": "nb_x"})
        return httpx.Response(200, json={})

    init_status(settings, handler)
    out = await nbt.get_notebook.fn(parent_id="P")
    assert set(out) == {"error"} and "notebook_id" in out["error"]
    assert log == [("/notebook", {"parentId": "P"})]


async def test_get_notebook_404_on_notebook_id_is_not_retried(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(404, text="Notebook not found")

    init_status(settings, handler)
    out = await nbt.get_notebook.fn(notebook_id="nb_y")
    assert "error" in out and len(calls) == 1


async def test_get_notebook_parent_404_without_notebook_id_reports_404(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        if path == "/notebook":
            return httpx.Response(404, text="Notebook not found")
        return httpx.Response(200, json={"error": "not_found", "reason": "deleted"})

    init_status(settings, handler)
    out = await nbt.get_notebook.fn(parent_id="P")
    assert "404" in out["error"]


async def test_unmark_done_open_habit_task_writes_nothing(settings):
    doc = {"_id": "h1t", "db": "Tasks", "habitId": "H", "habitRepAt": None, "done": False}
    s = init(settings, task_routes(doc, {"_id": "H", "history": [1, 1]}))
    out = await server.unmark_done.fn(item_id="h1t")
    assert [x[1] for x in s.log] == ["/doc"]
    assert "record_habit" in out["error"] and out["habit_id"] == "H"


async def test_update_task_updated_shows_last_write(settings):
    doc = {"_id": "u", "db": "Tasks", "title": "x", "day": "2026-10-10"}

    def routes(method, path, params, body):
        if path == "/doc" and method == "GET":
            return doc
        if path == "/doc/update":
            keys = {x["key"]: x["val"] for x in body["setters"]}
            if "taskTime" in keys:
                return {**doc, "title": keys.get("title"), "taskTime": keys["taskTime"], "_rev": "3-b"}
            return {**doc, **keys, "_rev": "2-a"}
        return {"ok": True}

    s = Scripted(routes)
    server.init(settings, transport=RecordingTransport(handler=s))
    out = await server.update_task.fn(item_id="u", time="09:00", day="2026-10-12")
    assert len(s.calls("/doc/update")) == 2
    assert out["updated"]["taskTime"] == "09:00" and out["updated"]["_rev"] == "3-b"


def nb_list_routes(nbs, cats, docs):
    def routes(method, path, params, body):
        if path == "/notebooks":
            return nbs
        if path == "/categories":
            return cats
        if path == "/doc":
            return docs.get(params.get("id"), {"error": "not_found", "reason": "missing"})
        return {}
    return routes


NBS = [
    {"_id": "nb_a", "parentId": "LIVE", "parentType": "project", "pages": []},
    {"_id": "nb_b", "parentId": "GONE", "parentType": "project", "pages": []},
    {"_id": "nb_c", "parentId": "DONE", "parentType": "project", "pages": []},
    {"_id": "nb_d", "parentId": None, "pages": []},
]


async def test_get_notebooks_marks_missing_and_done_parents(settings):
    docs = {"GONE": {"error": "not_found", "reason": "deleted"},
            "DONE": {"_id": "DONE", "db": "Categories", "type": "project", "done": True}}
    s = init(settings, nb_list_routes(NBS, [{"_id": "LIVE"}], docs))
    out = await nbt.get_notebooks.fn()
    by = {n["id"]: n for n in out["notebooks"]}
    assert "parent_missing" not in by["nb_a"] and "parent_done" not in by["nb_a"]
    assert by["nb_b"]["parent_missing"] is True
    assert by["nb_c"]["parent_done"] is True
    assert "parent_missing" not in by["nb_d"]
    assert len(s.calls("/categories")) == 1
    assert sorted(x[3]["id"] for x in s.calls("/doc")) == ["DONE", "GONE"]
    assert "note" in out


async def test_get_notebooks_all_parents_listed_costs_one_extra_call(settings):
    s = init(settings, nb_list_routes(NBS[:1] + NBS[3:], [{"_id": "LIVE"}], {}))
    out = await nbt.get_notebooks.fn()
    assert len(s.log) == 2 and "note" not in out


async def test_search_notebooks_marks_hit_on_deleted_parent(settings):
    nbs = [{"_id": "nb_b", "title": "Project X", "parentId": "GONE", "pages": []}]
    s = init(settings, nb_list_routes(nbs, [], {"GONE": {"error": "not_found", "reason": "deleted"}}))
    out = await nbt.search_notebooks.fn(query="project")
    assert out["hits"][0]["parent_missing"] is True


async def test_unmark_done_clears_day_set_by_markdone(settings):
    doc = {"_id": "t", "db": "Tasks", "done": True, "day": "2026-10-11", "firstScheduled": "unassigned",
           "fieldUpdates": {"done": 1}}
    s = init(settings, task_routes(doc))
    out = await server.unmark_done.fn(item_id="t")
    st = setters_of(s.calls("/doc/update")[0][2])
    assert st["day"] is None and st["done"] is False and st["doneAt"] is None
    assert "was removed" in out["day_note"]


async def test_unmark_done_keeps_stamped_day(settings):
    doc = {"_id": "t", "db": "Tasks", "done": True, "day": "2026-10-11", "firstScheduled": "unassigned",
           "fieldUpdates": {"day": 5}}
    s = init(settings, task_routes(doc))
    out = await server.unmark_done.fn(item_id="t")
    assert "day" not in setters_of(s.calls("/doc/update")[0][2])
    assert "remains" in out["day_note"]


async def test_unmark_done_keeps_day_of_task_created_with_day_and_occurrences(settings):
    for doc in ({"_id": "t", "db": "Tasks", "done": True, "day": "2026-10-11", "firstScheduled": "2026-10-11"},
                {"_id": "t", "db": "Tasks", "done": True, "day": "2026-10-11", "recurringTaskId": "g"}):
        s = init(settings, task_routes(doc))
        out = await server.unmark_done.fn(item_id="t")
        assert "day" not in setters_of(s.calls("/doc/update")[0][2]) and "day_note" not in out
