"""Notebooks: nine tools.

The document shapes were verified against the live API and the app.
Document types: `db: Notebooks` (`nb_...`; the sections are the list
`sections` with {id: 'sec_...', title, rank}) and `db: Pages` (`pg_...`, with
notebookId, sectionId, rank, title, note). A notebook with a parent (project
or category) has parentId/parentType; the parent shows and writes its `note`
to the page with the lowest rank. Everything requires the Full Access Token.
Nothing deleted via the API ends up in the trash.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from . import taskreminders as tr
from .chain import Chain
from .client import MarvinError
from .notes import protect_markdown, protection_note
from .server import (
    ADDITIVE,
    RESET_HOOKS,
    DESTRUCTIVE,
    IDEMPOTENT_WRITE,
    READONLY,
    get_client,
    make_setters,
    mcp,
    now_ms,
    tool_error,
)

SEARCH_MAX_FETCHES = 20
_page_text: dict[str, tuple[Any, str, str]] = {}  # page_id -> (updatedAt, title, text)
RESET_HOOKS.append(_page_text.clear)


def new_id(prefix: str) -> str:
    return prefix + tr.rid13()


def page_summary(p: dict, with_text: bool = False) -> dict:
    out = {
        "id": p.get("_id"),
        "title": p.get("title"),
        "rank": p.get("rank"),
        "section_id": p.get("sectionId"),
        "updated_at": p.get("updatedAt"),
    }
    if with_text:
        out["text"] = p.get("note") or ""
    return out


def sorted_pages(nb_resp: Any) -> list[dict]:
    pages = (nb_resp or {}).get("pages") or [] if isinstance(nb_resp, dict) else []
    return sorted(pages, key=lambda p: (p.get("rank") is None, p.get("rank") or 0))


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:80]
    return ""


async def load_notebook(c: Chain, notebook_id: str) -> tuple[dict, list[dict]]:
    resp = await c.step("read notebook", lambda: get_client().notebook(notebook_id=notebook_id))
    nb = (resp or {}).get("notebook") if isinstance(resp, dict) else None
    if not nb:
        raise MarvinError(f"Notebook {notebook_id!r} does not exist.")
    return nb, sorted_pages(resp)


def protect(text: str | None, notes: list[str]) -> str | None:
    out, changed = protect_markdown(text)
    msg = protection_note(changed)
    if msg:
        notes.append(msg)
    return out


async def parent_states(nbs: list) -> dict[str, str]:
    """notebook id -> 'missing' or 'done' for notebooks whose parent is not
    listed in /categories. /categories is read once; the parent is read with
    /doc only when it is missing there. A deleted parent (reason 'deleted',
    verified 2026-10-11) = missing, a done project = done. /categories does
    not list done projects (verified in one case)."""
    with_parent = [nb for nb in nbs if nb.get("parentId")]
    if not with_parent:
        return {}
    listed = {x.get("_id") for x in (await get_client().categories() or []) if isinstance(x, dict)}
    states: dict[str, str] = {}
    seen: dict[str, str | None] = {}
    for nb in with_parent:
        pid = nb["parentId"]
        if pid in listed:
            continue
        if pid not in seen:
            try:
                d = await get_client().get_doc(pid)
            except MarvinError:
                d = None
            if not isinstance(d, dict) or d.get("error") == "not_found":
                seen[pid] = "missing"
            elif d.get("done"):
                seen[pid] = "done"
            else:
                seen[pid] = None
        if seen[pid]:
            states[nb["_id"]] = seen[pid]
    return states


def mark_parent(entry: dict, state: str | None) -> dict:
    if state == "missing":
        entry["parent_missing"] = True
    elif state == "done":
        entry["parent_done"] = True
    return entry


PARENT_NOTE = ("parent_missing = the parent has been deleted in Marvin (the app does not show the notebook, "
               "the server still has it); parent_done = the parent is a done project.")


@mcp.tool(annotations=READONLY)
async def get_notebooks() -> dict:
    """List all notebooks with page titles, order and sections, without text.
    Standalone notebooks have parent_id null; a notebook that belongs to a
    project or category has parent_id and parent_type. A page's updated_at
    changes when the page is edited in the app. For text: get_notebook.
    A notebook whose parent has been deleted gets parent_missing, one on a
    done project gets parent_done. Costs 2 calls (1 when no notebook has a
    parent) plus one per distinct parent missing from /categories (deleted
    or done projects)."""
    try:
        nbs = await get_client().notebooks() or []
        states = await parent_states(nbs)
        out = []
        for nb in nbs:
            out.append(mark_parent({
                "id": nb.get("_id"),
                "title": nb.get("title"),
                "parent_id": nb.get("parentId"),
                "parent_type": nb.get("parentType"),
                "sections": nb.get("sections") or [],
                "pages": [page_summary(p) for p in sorted_pages(nb)],
            }, states.get(nb.get("_id"))))
        res: dict[str, Any] = {"count": len(out), "notebooks": out}
        if states:
            res["note"] = PARENT_NOTE
        return res
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def get_notebook(
    notebook_id: Annotated[str | None, Field(description="The notebook's id (nb_...)")] = None,
    parent_id: Annotated[str | None, Field(description="The project's or category's id, instead of notebook_id")] = None,
    page_id: Annotated[str | None, Field(description="A single page (pg_...), instead of the others")] = None,
    max_chars: Annotated[int, Field(description="Cap on page text in the response; pages beyond it are omitted", ge=1000, le=90000)] = 40000,
) -> dict:
    """Read a notebook including the text of its pages, or a single page.
    Give exactly one of notebook_id, parent_id (project/category) and
    page_id. Pages come in rank order; the first page of a notebook with a
    parent is the one the parent shows as its note. If the text exceeds
    max_chars the rest is omitted and the response says which pages."""
    try:
        given = [x for x in (notebook_id, parent_id, page_id) if x]
        if len(given) != 1:
            return {"error": "Give exactly one of notebook_id, parent_id and page_id."}
        if page_id:
            p = await get_client().get_doc(page_id)
            if not isinstance(p, dict) or p.get("db") != "Pages":
                return {"error": f"{page_id!r} is not a notebook page."}
            return {"page": {**page_summary(p, with_text=True), "notebook_id": p.get("notebookId")}}
        try:
            resp = await get_client().notebook(notebook_id=notebook_id, parent_id=parent_id)
        except MarvinError as e:
            if parent_id and " 404 " in str(e):
                return {"error": "Marvin answered 404 for parent_id. Read the notebook with notebook_id instead (get_notebooks lists the ids)."}
            raise
        nb = (resp or {}).get("notebook") if isinstance(resp, dict) else None
        if not nb:
            return {"notebook": None, "pages": [], "note": "No notebook (the project/category only has its regular note)." if parent_id else "The notebook does not exist."}
        used = 0
        pages, omitted = [], []
        for p in sorted_pages(resp):
            text = p.get("note") or ""
            if used + len(text) > max_chars:
                omitted.append(p.get("_id"))
                pages.append({**page_summary(p), "text_omitted": True, "chars": len(text)})
                continue
            used += len(text)
            pages.append(page_summary(p, with_text=True))
        out = {
            "notebook": {"id": nb.get("_id"), "title": nb.get("title"), "parent_id": nb.get("parentId"),
                         "parent_type": nb.get("parentType"), "sections": nb.get("sections") or []},
            "pages": pages,
        }
        if omitted:
            out["omitted_pages"] = omitted
            out["note"] = f"{len(omitted)} page(s) omitted above the cap; read them with page_id."
        return out
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=READONLY)
async def search_notebooks(
    query: Annotated[str, Field(description="Search text, case-insensitive")],
) -> dict:
    """Search notebooks: notebook names, page titles and page text.
    The list is fetched with one call; page text is cached in memory and
    fetched only for notebooks with pages changed since the previous search
    (updated_at), at most 20 notebooks per call. Notebooks whose text has
    not been fetched yet are listed in not_searched — search again to
    continue. Hits in a notebook whose parent has been deleted get
    parent_missing, on a done project parent_done (as in get_notebooks)."""
    try:
        q = query.strip().lower()
        if not q:
            return {"error": "query must not be empty."}
        nbs = await get_client().notebooks() or []
        fetches, not_searched, hits = 0, [], []
        for nb in nbs:
            pages = sorted_pages(nb)
            stale = [p for p in pages
                     if p.get("_id") not in _page_text or _page_text[p.get("_id")][0] != p.get("updatedAt")]
            if stale:
                if fetches >= SEARCH_MAX_FETCHES:
                    not_searched.append(nb.get("_id"))
                else:
                    fetches += 1
                    resp = await get_client().notebook(notebook_id=nb.get("_id"))
                    for p in sorted_pages(resp):
                        _page_text[p.get("_id")] = (p.get("updatedAt"), p.get("title") or "", p.get("note") or "")
            if q in str(nb.get("title") or "").lower():
                hits.append({"notebook_id": nb.get("_id"), "notebook": nb.get("title"), "match": "notebook-name"})
            for p in pages:
                cached = _page_text.get(p.get("_id"))
                title = p.get("title") or ""
                text = cached[2] if cached else ""
                where = "page-title" if q in title.lower() else ("text" if q in text.lower() else None)
                if where:
                    k = text.lower().find(q)
                    snippet = text[max(0, k - 60): k + len(q) + 60] if k >= 0 else ""
                    hits.append({"notebook_id": nb.get("_id"), "notebook": nb.get("title"), "page_id": p.get("_id"),
                                 "page": title, "match": where, "snippet": snippet})
        if hits:
            states = await parent_states(nbs)
            hits = [mark_parent(h, states.get(h["notebook_id"])) for h in hits]
        out: dict[str, Any] = {"query": query, "count": len(hits), "hits": hits, "notebooks_fetched": fetches}
        if hits and any("parent_missing" in h or "parent_done" in h for h in hits):
            out["note"] = PARENT_NOTE
        if not_searched:
            out["not_searched"] = not_searched
        return out
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def create_notebook(
    title: Annotated[str, Field(description="The notebook's name")],
    sections: Annotated[list[str] | None, Field(description="Sections to create (titles)")] = None,
) -> dict:
    """Create a standalone notebook, optionally with sections.
    A notebook for a project or category is not created here: it comes
    into being when a page is moved there (update_notebook_page
    move_to_parent_id) or when a second page is added in the app.
    Relies on undocumented writes (/doc/create and /doc/update, marked
    experimental by Marvin); the shape is matched against app 1.71."""
    try:
        now = now_ms()
        secs = [{"id": new_id("sec_"), "title": t.strip(), "rank": k + 1} for k, t in enumerate(sections or []) if t.strip()]
        doc = {"_id": new_id("nb_"), "db": "Notebooks", "title": title.strip(), "icon": None, "color": None,
               "rank": 99, "parentId": None, "parentType": None, "sections": secs, "createdAt": now, "updatedAt": now}
        if not doc["title"]:
            return {"error": "title must not be empty."}
        created = await get_client().create_doc(doc)
        return {"created": created, "notebook_id": doc["_id"], "sections": secs}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def update_notebook(
    notebook_id: Annotated[str, Field(description="The notebook's id (nb_...)")],
    title: Annotated[str | None, Field(description="New name — standalone notebooks only")] = None,
    sections_add: Annotated[list[str] | None, Field(description="New sections (titles), added last")] = None,
    sections_rename: Annotated[dict[str, str] | None, Field(description="Section id -> new title")] = None,
    sections_delete: Annotated[list[str] | None, Field(description="Section ids to remove; their pages become unsectioned")] = None,
) -> dict:
    """Rename a standalone notebook or change its sections.
    Renaming is refused on a notebook that belongs to a project or category
    (the app would otherwise show the notebook's name instead of the
    project's). The section list is read just before the write and is
    rewritten in full. On removal, sectionId is first set to null on the
    pages in that section, so that no page points to a missing section.
    Relies on undocumented writes (/doc/create and /doc/update, marked
    experimental by Marvin); the shape is matched against app 1.71."""
    try:
        c = Chain()
        nb, pages = await load_notebook(c, notebook_id)
        fields: dict[str, Any] = {}
        if title is not None:
            if nb.get("parentId"):
                return {"error": "The notebook belongs to a project or category; only standalone notebooks can be renamed."}
            if not title.strip():
                return {"error": "title must not be empty."}
            fields["title"] = title.strip()
        secs = [dict(x) for x in (nb.get("sections") or [])]
        ids = {x.get("id") for x in secs}
        for sid in list(sections_rename or {}) + list(sections_delete or []):
            if sid not in ids:
                return {"error": f"Section {sid!r} does not exist in the notebook."}
        if sections_rename or sections_delete or sections_add:
            for x in secs:
                if x.get("id") in (sections_rename or {}):
                    x["title"] = sections_rename[x["id"]].strip()
            secs = [x for x in secs if x.get("id") not in set(sections_delete or [])]
            top = max([x.get("rank") or 0 for x in secs] + [0])
            for k, t in enumerate(sections_add or []):
                secs.append({"id": new_id("sec_"), "title": t.strip(), "rank": top + k + 1})
            fields["sections"] = secs
        if not fields:
            return {"error": "Nothing to change was given."}
        for p in pages:
            if p.get("sectionId") in set(sections_delete or []):
                pid = p["_id"]
                await c.step(f"unsection page {pid}", lambda pid=pid: get_client().update_doc(pid, make_setters({"sectionId": None})))
        result = await c.step("write notebook", lambda: get_client().update_doc(notebook_id, make_setters(fields)))
        return {"updated": result, "sections": secs if "sections" in fields else None, "completed_steps": c.completed}
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_notebook(
    notebook_id: Annotated[str, Field(description="The notebook's id (nb_...)")],
    confirm: Annotated[bool, Field(description="True = delete after the pages have been shown")] = False,
) -> dict:
    """Delete a notebook and its pages permanently, in two steps if text.
    No trash via the API. The pages are deleted first and then the
    notebook, as the app does. Two steps: a notebook with a parent and more
    than one page, and a standalone notebook where any page has text, are
    only deleted with confirm=true — the first call shows the pages (title,
    size, first line). Notebook with a parent: page 1's text is written
    back as the project's/category's note after the deletion; the text is
    always included in the response, so it is not lost if a step fails.
    Relies on undocumented writes (/doc/create and /doc/update, marked
    experimental by Marvin); the shape is matched against app 1.71."""
    try:
        c = Chain()
        nb, pages = await load_notebook(c, notebook_id)
        parent = nb.get("parentId")
        has_text = any((p.get("note") or "").strip() for p in pages)
        need_confirm = (parent and len(pages) > 1) or (not parent and has_text)
        preview = [{"id": p.get("_id"), "title": p.get("title"), "chars": len(p.get("note") or ""),
                    "first_line": first_line(p.get("note") or "")} for p in pages]
        saved = (pages[0].get("note") or "") if (parent and pages) else None
        if need_confirm and not confirm:
            msg = "Nothing has been deleted. The pages below will be removed permanently; run again with confirm=true."
            if parent:
                msg += " Page 1's text will be written back as the project's/category's note."
            return {"needs_confirm": True, "pages": preview, "message": msg}
        if saved is not None:
            c.extra["saved_text"] = saved
        for p in pages:
            pid = p["_id"]
            await c.step(f"delete page {pid}", lambda pid=pid: get_client().delete_doc(pid))
        await c.step("delete notebook", lambda: get_client().delete_doc(notebook_id))
        out: dict[str, Any] = {"deleted": notebook_id, "pages_deleted": [p["_id"] for p in pages]}
        if parent and saved:
            await c.step("write back note", lambda: get_client().update_doc(parent, make_setters({"note": saved})))
            out["note_written_back_to"] = parent
        if saved is not None:
            out["saved_text"] = saved
        out["completed_steps"] = c.completed
        return out
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=ADDITIVE)
async def create_notebook_page(
    notebook_id: Annotated[str, Field(description="The notebook's id (nb_...)")],
    title: Annotated[str, Field(description="The page's title")],
    text: Annotated[str | None, Field(description="The page's text (markdown, see list_capabilities.note_format)")] = None,
    section_id: Annotated[str | None, Field(description="Section id to put the page in")] = None,
) -> dict:
    """Create a page at the end of a notebook, optionally in a section.
    Own pg_ id, rank after the existing pages. '- ' at the start of a line
    is written as '* ' (code protection against the app deleting lists).
    Relies on undocumented writes (/doc/create and /doc/update, marked
    experimental by Marvin); the shape is matched against app 1.71."""
    try:
        c = Chain()
        nb, pages = await load_notebook(c, notebook_id)
        if section_id and section_id not in {x.get("id") for x in nb.get("sections") or []}:
            return {"error": f"Section {section_id!r} does not exist in the notebook."}
        notes: list[str] = []
        body = protect(text, notes) or ""
        now = now_ms()
        doc = {"_id": new_id("pg_"), "db": "Pages", "notebookId": notebook_id, "sectionId": section_id,
               "rank": max([p.get("rank") or 0 for p in pages] + [0]) + 1, "title": title.strip(),
               "icon": None, "color": None, "note": body, "hasBody": bool(body), "titleAuto": False,
               "createdAt": now, "updatedAt": now}
        created = await c.step("create page", lambda: get_client().create_doc(doc))
        out = {"created": created, "page_id": doc["_id"], "completed_steps": c.completed}
        if notes:
            out["notes"] = notes
        return out
    except Exception as e:
        return tool_error(e)


async def _move_target(c: Chain, notebook_id: str | None, parent_id: str | None, notes: list[str]) -> tuple[str, int]:
    """Target notebook and rank for a moved page. If the parent has no
    notebook, one is created in the app's shape: if the parent has a note,
    it becomes page 1 'Notes' with originalNoteFrom and migratedAt and the
    moved page becomes page 2; without a note the moved page becomes the
    only page, rank 1."""
    if notebook_id:
        _nb, pages = await load_notebook(c, notebook_id)
        return notebook_id, max([p.get("rank") or 0 for p in pages] + [0]) + 1
    resp = await c.step("read parent's notebook", lambda: get_client().notebook(parent_id=parent_id))
    nb = (resp or {}).get("notebook") if isinstance(resp, dict) else None
    if nb:
        return nb["_id"], max([p.get("rank") or 0 for p in sorted_pages(resp)] + [0]) + 1
    parent = await c.step("read parent", lambda: get_client().get_doc(parent_id))
    if not isinstance(parent, dict) or parent.get("db") != "Categories":
        raise MarvinError(f"{parent_id!r} is neither a project nor a category.")
    ptype = parent.get("type") or "category"
    old_note = parent.get("note") or ""
    now = now_ms()
    nb_id = new_id("nb_")
    # The app's shape (verified 2026-10-10): the notebook gets rank 1; if the
    # parent has no note, the moved page becomes the only page (rank 1), with
    # no Notes page.
    await c.step("create notebook", lambda: get_client().create_doc({
        "_id": nb_id, "db": "Notebooks", "title": "", "icon": None, "color": None, "rank": 1,
        "parentId": parent_id, "parentType": ptype, "sections": None, "createdAt": now, "updatedAt": now}))
    if not old_note.strip():
        notes.append("The parent had no note: the page became the notebook's only page and is now the parent's note.")
        return nb_id, 1
    pg_id = new_id("pg_")
    await c.step("create Notes page", lambda: get_client().create_doc({
        "_id": pg_id, "db": "Pages", "notebookId": nb_id, "sectionId": None, "rank": 1, "title": "Notes",
        "icon": None, "color": None, "note": old_note, "hasBody": True, "titleAuto": False,
        "originalNoteFrom": parent_id, "migratedAt": now, "createdAt": now, "updatedAt": now}))
    notes.append("The parent got a notebook; its old note is page 1 'Notes' (YOUR ORIGINAL NOTE in the app).")
    return nb_id, 2


@mcp.tool(annotations=IDEMPOTENT_WRITE)
async def update_notebook_page(
    page_id: Annotated[str, Field(description="The page's id (pg_...)")],
    text: Annotated[str | None, Field(description="New text (replaces)")] = None,
    append: Annotated[str | None, Field(description="Text to add at the end (after a blank line)")] = None,
    title: Annotated[str | None, Field(description="New title")] = None,
    section_id: Annotated[str | None, Field(description="Section id, '' = no section")] = None,
    rank: Annotated[int | None, Field(description="New order (lowest rank = page 1)")] = None,
    move_to_notebook_id: Annotated[str | None, Field(description="Move the page to this notebook")] = None,
    move_to_parent_id: Annotated[str | None, Field(description="Move the page to a project's or category's notebook (created if needed)")] = None,
) -> dict:
    """Change a page's text, title, order or section, or move it.
    text replaces, append adds at the end; '- ' at the start of a line is
    written as '* '. New text shows up immediately in an open client. Move:
    the section does not follow (sectionId null). The first page of a
    notebook with a parent is not moved out — it is the parent's note.
    Moving to a project or category without a notebook creates one in the
    app's shape. The response says when an order change switches which
    page the parent's note points to.
    Relies on undocumented writes (/doc/create and /doc/update, marked
    experimental by Marvin); the shape is matched against app 1.71."""
    try:
        if text is not None and append is not None:
            return {"error": "Give text or append, not both."}
        if move_to_notebook_id and move_to_parent_id:
            return {"error": "Give move_to_notebook_id or move_to_parent_id, not both."}
        c = Chain()
        notes: list[str] = []
        page = await c.step("read page", lambda: get_client().get_doc(page_id))
        if not isinstance(page, dict) or page.get("db") != "Pages":
            return {"error": f"{page_id!r} is not a notebook page."}
        nb, pages = await load_notebook(c, page.get("notebookId"))
        is_first = bool(pages) and pages[0].get("_id") == page_id
        fields: dict[str, Any] = {}
        if text is not None:
            fields["note"] = protect(text, notes)
        if append is not None:
            add = protect(append, notes) or ""
            cur = page.get("note") or ""
            fields["note"] = (cur.rstrip("\n\\") + "\n\n" + add) if cur.strip() else add
        if title is not None:
            fields["title"] = title.strip()
        if section_id is not None:
            if section_id and section_id not in {x.get("id") for x in nb.get("sections") or []}:
                return {"error": f"Section {section_id!r} does not exist in the notebook."}
            fields["sectionId"] = section_id or None
        moving = bool(move_to_notebook_id or move_to_parent_id)
        if moving:
            if nb.get("parentId") and is_first:
                return {"error": "The page is the first page of a notebook with a parent (the parent's note) and is not moved out."}
            target, new_rank = await _move_target(c, move_to_notebook_id, move_to_parent_id, notes)
            fields.update({"notebookId": target, "sectionId": None, "rank": new_rank})
            if page.get("sectionId"):
                notes.append("The section does not follow the page when it is moved.")
        elif rank is not None:
            fields["rank"] = rank
            if nb.get("parentId"):
                others = [p.get("rank") or 0 for p in pages if p.get("_id") != page_id]
                becomes_first = not others or rank < min(others)
                if becomes_first != is_first:
                    notes.append("The order change switches which page the parent's note points to.")
        if not fields:
            return {"error": "Nothing to change was given."}
        result = await c.step("write page", lambda: get_client().update_doc(page_id, make_setters(fields)))
        out: dict[str, Any] = {"updated": result, "completed_steps": c.completed}
        if notes:
            out["notes"] = notes
        return out
    except Exception as e:
        return tool_error(e)


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_notebook_page(
    page_id: Annotated[str, Field(description="The page's id (pg_...)")],
    confirm: Annotated[bool, Field(description="True = delete a page with text after it has been shown")] = False,
) -> dict:
    """Delete a notebook page permanently, with confirmation if it has text.
    No trash via the API. A page with text is only deleted with
    confirm=true; the first call shows title, size and first line. The last
    page of a notebook with a parent is not deleted here — use
    delete_notebook. If the parent's first page is deleted, the note then
    points to whichever page is first.
    Relies on undocumented writes (/doc/create and /doc/update, marked
    experimental by Marvin); the shape is matched against app 1.71."""
    try:
        c = Chain()
        page = await c.step("read page", lambda: get_client().get_doc(page_id))
        if not isinstance(page, dict) or page.get("db") != "Pages":
            return {"error": f"{page_id!r} is not a notebook page."}
        nb, pages = await load_notebook(c, page.get("notebookId"))
        if nb.get("parentId") and len(pages) <= 1:
            return {"error": "The last page of a notebook with a parent is deleted with delete_notebook."}
        body = page.get("note") or ""
        if body.strip() and not confirm:
            return {"needs_confirm": True, "page": {"id": page_id, "title": page.get("title"), "chars": len(body),
                    "first_line": first_line(body)}, "message": "Nothing has been deleted; run again with confirm=true."}
        await c.step("delete page", lambda: get_client().delete_doc(page_id))
        out: dict[str, Any] = {"deleted": page_id, "completed_steps": c.completed}
        if nb.get("parentId") and pages and pages[0].get("_id") == page_id:
            out["note"] = "That was the parent's first page; the note now points to whichever page is first."
        return out
    except Exception as e:
        return tool_error(e)
