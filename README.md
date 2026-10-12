# amazing-marvin-complete-mcp

<!-- mcp-name: io.github.andreasd083/amazing-marvin-complete-mcp -->

An MCP ([Model Context Protocol](https://modelcontextprotocol.io)) server for
[Amazing Marvin](https://amazingmarvin.com) with **complete coverage of the
public API**: 50 tools over all ~31 documented endpoints (plus the
undocumented `/doneItems`), a global rate
limiter that respects Marvin's documented limits, least-privilege token
routing, and MCP tool annotations. As of 1.1.0 every writable field in
Marvin's official data model (Tasks and Categories/Projects) is either
supported by a tool or explicitly documented as unsupported — see
[docs/field-reconciliation.md](docs/field-reconciliation.md). Every
non-obvious behavior claim in the tool descriptions was verified against
the live API — the findings are documented below in
[Marvin API quirks & findings](#marvin-api-quirks--findings),
which may be useful even if you never run this server.

> **Maintenance status:** Bug reports are welcome and appreciated — they
> help keep this working for everyone. Please note this is a side project
> maintained when time allows: bug reports get looked at, but response
> times vary and feature requests are unlikely to be picked up. For
> installation help, paste this README into your AI assistant — it can walk
> you through setup and troubleshooting far faster than I can. Provided
> as-is, without guarantees — it's MIT, fork freely.

## Tools (50)

| Group | Tools |
|---|---|
| Core | `test_connection`, `create_task`, `mark_done`, `mark_project_done`, `unmark_done`, `update_task`, `set_priority`, `delete_task` |
| Reading | `get_today_items`, `get_due_items`, `get_done_items`, `get_children`, `get_categories` |
| Recurring tasks | `create_recurring_task`, `update_recurring_task` |
| Notebooks | `get_notebooks`, `get_notebook`, `search_notebooks`, `create_notebook`, `update_notebook`, `delete_notebook`, `create_notebook_page`, `update_notebook_page`, `delete_notebook_page` |
| Structure | `create_category_or_project`, `update_category_or_project`, `convert_category_or_project` (experimental) |
| Habits | `list_habits`, `get_habit`, `record_habit` |
| Time blocks | `get_today_time_blocks`, `create_time_block` (experimental) |
| Time tracking | `get_tracked_item`, `start_tracking`, `stop_tracking`, `get_time_tracks` |
| Kudos/rewards | `get_kudos`, `claim_reward_points`, `unclaim_reward_points`, `spend_reward_points`, `reset_reward_points` |
| Misc | `get_labels`, `get_goals`, `get_reminders`, `set_reminder`, `delete_reminder`, `create_event` (experimental), `get_account_info`, `get_rate_limit_status`, `list_capabilities` |

### Capability overview

`list_capabilities` (no Marvin API call) returns the same map as the table
below, per tool with *can* and *cannot*, plus the list of things that only
work in the app. Call it before assuming something is impossible via MCP.
The server also sends a short `instructions` text at `initialize` (shown by
Claude Code, not by the claude.ai clients). The first line of every tool
description is a whole sentence of at most 75 characters stating the
capability — the clients show only that line in their catalog; the test
`tests/test_discoverability.py` guards it. The same test keeps every
description and the `instructions` text under 2,048 characters — Claude Code
silently truncates longer text since v2.1.280.

| Area | Can via MCP | Cannot via MCP (done in the app) |
|---|---|---|
| Tasks | create/update/complete/delete, priority and frog 1–4, size, subtasks, a time of day and own reminders in the app's form, day, deadline, labels, estimate, note, sections, planned week/month, review date, backburner, orbit, snooze, time block; complete a project with its open tasks (two steps); read today's, due and done, filtered as the app shows them | startDate at creation (update_task afterwards); fixing occurrences that already exist; reopening a completed project |
| Recurring tasks | create all eleven types (custom only as an RRULE string), change rule/window/place/properties, clean up occurrences that no longer fit (list, confirm) | reminders/frog on recurring tasks, recurring projects, custom rules in words |
| Notebooks | list, read, search, create, rename, sections, pages: create/edit/move/delete, delete a notebook in two steps | undo (no trash via the API) |
| Structure | the hierarchy, children, create/update/convert categories and projects, labels on both, color, icon, note; read labels and goals | creating/editing labels, goals, smart lists, strategies |
| Habits | list, tracking record, record/undo; habit tasks are recorded on mark_done and undone on unmark_done | creating/editing habits |
| Time | today's time blocks with mapping, create a block, time tracking start/stop/history | the smart-time-block mapping on a block |
| Rewards | kudos, award/undo/spend/reset reward points | undoing a MANUAL award; reading the app's Rewards |
| Reminders | own reminders on a task (list + server entry), standalone, delete; time of day via update_task | — |
| Calendar | create an event (experimental) | reading/editing events |
| Account | test, account info, call budget, capability overview | — |

Deliberately **not** included: Smart List / task-picking logic (Marvin's own
Spotlight does the picking; the server gives your assistant hands, not
opinions), and the `/reminder/deleteAll` endpoint — the one documented
endpoint without a tool, deliberately: it wipes every reminder in a single
call and `delete_reminder` already covers targeted cleanup.

Every tool carries [MCP tool annotations](https://modelcontextprotocol.io/specification/2025-06-18/server/tools#tool-annotations)
(`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) so
capable clients can treat `delete_task` and `reset_reward_points` with the
respect they deserve.

## Getting your Marvin tokens

Both tokens live in Amazing Marvin under **Settings → API**
([app.amazingmarvin.com/pre?api](https://app.amazingmarvin.com/pre?api)):

- **API Token** (`MARVIN_API_TOKEN`, required for use) — limited access;
  enough for reading and creating tasks. The server does start without it
  (so MCP clients and directories can list the tools), but every tool call
  returns a clear error until the token is set.
- **Full Access Token** (`MARVIN_FULL_ACCESS_TOKEN`, optional but
  recommended) — required by all `/doc*`-based tools: `update_task`,
  `set_priority`, `unmark_done`, `delete_task`, category creation, time
  blocks, `list_habits`, reminders, `reset_reward_points`.

Treat them like passwords; see [SECURITY.md](SECURITY.md).

## Install & run

Requires Python 3.12+.

**From PyPI** (recommended): with [uv](https://docs.astral.sh/uv/) installed
there is nothing to set up — point your MCP client at
`uvx amazing-marvin-complete-mcp` as shown below.

**From source:**

```bash
git clone <this repo> && cd amazing-marvin-complete-mcp
python -m venv .venv && .venv/bin/pip install .
# then use /path/to/.venv/bin/marvin-mcp as the command below
```

### Local (stdio) — Claude Desktop, Claude Code, any MCP client

The default transport is stdio, so the client starts the server itself:

```json
{
  "mcpServers": {
    "amazing-marvin": {
      "command": "uvx",
      "args": ["amazing-marvin-complete-mcp"],
      "env": {
        "MARVIN_API_TOKEN": "…",
        "MARVIN_FULL_ACCESS_TOKEN": "…",
        "MARVIN_TIMEZONE": "Europe/Stockholm"
      }
    }
  }
}
```

(For Claude Code: `claude mcp add amazing-marvin -e MARVIN_API_TOKEN=… --
uvx amazing-marvin-complete-mcp`.)

### Remote (Streamable HTTP)

```bash
MCP_TRANSPORT=http PORT=8787 MCP_AUTH_TOKEN_FILE=/path/to/token \
MARVIN_API_TOKEN_FILE=/path/to/api-token .venv/bin/marvin-mcp
```

The MCP endpoint is `/mcp`. HTTP mode **fails closed**: without
`MCP_AUTH_TOKEN` (or `MCP_AUTH_TOKEN_FILE`) the server refuses to start,
with instructions in the error message; set `MCP_ALLOW_UNAUTHENTICATED=true`
only to deliberately run an open instance on localhost. The built-in bearer
check protects every path but is an internal barrier, not a complete auth
story:
put a reverse proxy with TLS in front, and for Claude custom connectors an
OAuth 2.1-capable MCP auth proxy. A `Dockerfile` for HTTP mode is included
(runs as a non-root user; mount a volume on `/data` to persist the daily
rate-limit counter across restarts).

### Configuration

All settings via environment variables — see [.env.example](.env.example)
for the full annotated list. Highlights: every secret supports a `*_FILE`
variant (recommended); `MARVIN_TIMEZONE` should match the timezone your
Marvin account lives in (defaults to the system timezone, which is UTC in
most containers).

### Rate limiting

Marvin's documented limits — no bursts faster than one request every 3
seconds, bulk creation at one per second, 1440 calls/day — are enforced by a
single process-global queue shared by all tools and sessions, with margin:
only creating calls (`/addTask`, `/addProject`, `/addEvent`, `/doc/create`)
use 1.1 s; everything else (reads, updates, `/markDone`, deletions) waits
3.1 s. The spacing is set by the kind of the call about to be made. After a
429 all calls pause 60 s (or Retry-After). The daily counter and the time of
the last call persist across restarts (`STATE_DIR`) and the counter rolls
over at midnight in the configured timezone. `get_rate_limit_status` shows
today's usage and how long ago the last call went out. Multi-step tools
(chains) retry a step once after a 500 or 429 and report which steps were
done if they still fail.

## Marvin API quirks & findings

Everything below was verified against the live API (2026-08-19 through
2026-10-12). This is the half of the repo you can use without running it.

**Habits**
- Non-raw `GET /habits` does **not** read your habit documents. It reads a
  server-side tracking registry that is created *lazily on the first
  recording* — a habit that has never been recorded is missing from the
  response entirely, and the entries carry no titles (only `habitId` +
  history). Use `?raw=1` (Full Access Token) to list actual habit documents.
  `GET /habit?id=…` returns the tracking record — history but no title.
- `POST /updateHabit` rejects integers serialized as floats:
  `"value": 1.0` → 400 Bad request, `"value": 1` → 200. Send ints as ints.

**Tasks & projects**
- `POST /markDone` works for tasks only — projects get
  `400 "Can only mark Tasks done with this API"`.
- By default `/addTask` parses *some* of Marvin's quick-add shortcut syntax
  server-side: `~15` becomes a 15-minute `timeEstimate`, `+YYYY-MM-DD` sets
  `day` (scheduling — **not** the deadline) and `*p1`..`*p3` set priority.
  All three are stripped from the title. Note the priority mapping is
  inverted relative to the stored field: `*p1` (highest) → `isStarred: 3`,
  `*p2` → `2`, `*p3` (lowest) → `isStarred: 1`. The other magic words (`*urgent`,
  `*fire`, `*heavy`, `*weight`, `*love`, `*lowfocus`, `*physical`) and
  `$`-words (e.g. `$MONTH` on a non-recurring task) are **not** parsed —
  they are stored literally in the title with no fields set; they only work
  in the app's quick-add.
  But the `#` shortcut is outright dangerous: **any** `#word` in the title
  (a ticket reference like `#123` included) is stored literally as
  `parentId` (greedy up to the first hyphen, e.g. `#MCP-TEST` →
  `parentId: "#MCP"` and a corrupted title) without resolving any ID —
  **even overriding an explicitly supplied `parentId` in the same request**.
  The task then lives outside every category *and* outside the Inbox —
  effectively invisible. (First reported by
  [lucasoeth/marvin-mcp](https://github.com/lucasoeth/marvin-mcp);
  independently reproduced and expanded here.)
  **This server is not affected:** `create_task` sends the undocumented
  `X-Auto-Complete: false` header (added in
  [MarvinAPI#50](https://github.com/amazingmarvin/MarvinAPI/issues/50)),
  which disables all shortcut parsing — titles are stored verbatim, and the
  `time_estimate_minutes` parameter replaces the `~15` shortcut
  (`timeEstimate` is milliseconds: 15 min = `900000`).
- `/addProject` has the same `#word` corruption bug but **ignores the
  `X-Auto-Complete` header** (live-tested: the title is stripped and
  `parentId` corrupted even with the header set). This server therefore
  blocks `#` in project titles locally (in the client layer, before any API
  call) with an explanatory error. Category titles are safe — they go
  through `/doc/create`, which parses nothing.
- `/addEvent` is **unaffected** (live-tested 2026-08-25): event titles with
  `#word` are stored verbatim, with and without the header — the quick-add
  parsing bug exists only in `/addTask` and `/addProject`.
- Generated instances of recurring tasks have deterministic IDs
  (`YYYY-MM-DD_<recurringTaskId>`), which is why marking them done/undone
  through the API cannot create duplicates. The instances are generated by
  the Marvin *client*, so today's recurring tasks can be missing from
  `/todayItems` until the app has been running.
- `/doc/update` can sporadically return a transient 500; the write is
  atomic (no partial state) — just retry. Project renames, moves, label
  changes etc. all work through it.
- `/doc/update` returns **500 instead of 404** for documents that do not
  exist (deleted or never created; live-tested 2026-08-29) — a *permanent*
  500 therefore means "wrong/dead ID", not a server error or a corrupted
  document.
- `startDate`/`endDate` are ignored by `/addTask` and `/addProject`
  (live-tested 2026-08-29) — they can only be set afterwards via
  `/doc/update` (the update tools). `/addProject` also ignores
  `color`/`icon` (set them via `update_category_or_project`), and
  `/addTask` silently drops `noAutoOrbit` (live-tested 2026-09-13) — set it
  with `update_task` after creating the task.
- A clock time on a task (Time/`taskTime`) is fully supported by Marvin,
  and with auto-created reminders a set Time automatically becomes a
  reminder at the same time (live data 2026-09-02). Until 1.8.0 this MCP
  left it to the app; since 1.8.0 the tools write it in the app's own form
  (fields and server entries together — see **Reminders and time of day**
  below).
- Projects are prioritized with the string field `priority`
  (`"high"`/`"mid"`/`"low"`), not `isStarred` like tasks (live-tested
  2026-08-29) — which is why `set_priority` is task-only. Mapping (verified
  against the app's code 2026-08-30): `high` = Most important (red),
  `mid` = Very important (orange), `low` = **Important** (yellow, the
  one-star level). The app's fourth level *Low priority* (down arrow) is
  stored on tasks as `isStarred: -1` (magic words `*low`/`*p0`); projects
  do not have it — the app clears the priority when converting a low
  priority task into a project. `set_priority`/`create_task` accept `-1`.
- **Completed tasks are readable** via the **undocumented** endpoint
  `GET /doneItems?date=YYYY-MM-DD` (missing from the OpenAPI spec and the
  wiki; live-tested 2026-08-30, may disappear without notice). It filters
  on the task's `day`, not on `doneAt`, and a past `day` survives
  completion both in the app and via `/markDone` (the app sets `day` to
  today only on unscheduled and future-dated tasks). `get_done_items`
  therefore fetches the date plus a 7-day lookback window and filters on
  `doneAt`; the response states its coverage (`covers_from`,
  `days_fetched`), complete results are cached for 30 minutes, and on a
  429 the tool returns what it got, flagged `incomplete`/`days_missing`.
  Single completed tasks can also be read with `/doc?id=`. `/todayItems`,
  `/dueItems` and `/children` exclude completed items; `/doneTasks` and
  `/completedItems` are 404.
- **Marvin returns 429 even with 3 s spacing** when the daily average
  (1440/day = "1 per minute") is exceeded within a shorter, undocumented
  window — observed 2026-08-30 after ~100 calls in one hour. After a 429
  the limiter pauses all calls for 60 s (or `Retry-After`) and logs the
  response headers (allow-listed names only).
- **The server validates no writes** (live-tested 2026-08-29): invalid
  dates, negative/out-of-range numbers, mistyped values, empty titles,
  dead parentId/labelIds and unknown fields are stored verbatim via
  `/doc/update` (and almost everything via `/addTask`). The tools therefore
  validate dates (strict YYYY-MM-DD, year 2000-2100), titles and numeric
  ranges client-side; references are not validated (orphan risk documented
  in the descriptions).
- `/doc/delete` responds 200 even for IDs that never existed or are already
  deleted — idempotent, no 404 (unlike `/doc/update`). `/markDone` on the
  other hand gives a proper 404 for a missing ID and 400 for an already
  completed task — three endpoints, three different answers to "does not
  exist" (live-tested 2026-08-29).
- Read endpoints (`/todayItems`, `/dueItems`) are pure date filters:
  backburner, startDate and orphan status (dead parentId) do not affect
  them — and orphans never show up under `unassigned` (live-tested
  2026-08-29). `/todayItems?date=X` returns all open items with `day` <= X,
  not just day X (live-tested 2026-09-11, rollover enabled). `/markDone`
  stops running time tracking and now also
  writes `task.times` (live-tested 2026-09-02; it did not on 2026-08-29 —
  server behavior changed). A direct `/track STOP` still does not write
  `times`; there `/tracks` is the only record.
- `orbit`/`noAutoOrbit` are missing from the wiki's data types but present
  in live data (bool, verified 2026-08-29) — exposed as explicitly
  undocumented passthrough parameters on the update tools.
- Project↔category conversion happens **in place**: `_id`, `createdAt` and
  the children remain (verified 2026-08-29, both via an app field test and
  via the API). The app has two conversion paths with different behavior
  (verified 2026-08-30/31): the Edit Settings button permanently clears
  `day`/`dueDate`/`priority`/`isFrogged` and leaves `firstScheduled` behind
  (a bug in Marvin's tracker), while the right-click/hover path is a
  lossless round trip — but that button is not in the menu by default (add
  it via the gear icon in the right-click menu → Add action).
  `convert_category_or_project` is lossless by default since 1.5.0; pass
  `clear_project_fields=True` for a clean category (the previous values are
  returned in `removed_project_fields`). There is no official conversion
  endpoint — the tool sets `type` directly, which is undocumented server
  behavior and marked experimental.
- `/doc/create` does not echo back a server-generated `_id` — supply your
  own if you need to reference the document afterwards.
- Deletion via `/doc/delete` is permanent; Marvin's trash is client-side.
- **The app's view after API writes** (live-tested 2026-09-12, PWA +
  Windows desktop app 1.70.0, both open at the same time): creating,
  field changes and moves between category/Inbox render immediately with
  no action needed. **Deletions do not render at all** — neither waiting
  nor switching views helps. The same held for a cleared
  `plannedWeek`/`plannedMonth` with Planning Ahead on (2026-08-29) and for
  strategy toggling (method rule since 2026-08-29). Rule of action:
  (1) confirm the server with a read (`get_children` on the parent — the
  task should be missing); (2) if the app still shows the row, that is the
  app's view, not an error — reload the client *before* the row is touched
  in the app: **F5 in the web app/PWA**, **restart of the desktop app** (it
  has no refresh). The reason for "before the row is touched": editing a
  row in the app whose document is already deleted on the server can
  recreate the document through the client's conflict resolution
  (observed once, 2026-09-12, in one of the two clients — which one is
  not recorded; reported upstream). This is the Marvin
  client's view — not to be confused with `get_done_items`' own 30-minute
  cache, which goes the other way (app → MCP).

**Reward points & kudos**
- Kudos (XP/level, read via `/kudos`) and reward points
  (claim/unclaim/spend/reset) are two separate systems. `/kudos` lacks
  `nextMultiplier` (MarvinAPI issue #5) — it's in `/me`.
- `/markDone` does **not** award a task's reward points (cf. issue #6 for
  kudos) — `claimRewardPoints` is a separate call.
- A `MANUAL` claim (`itemId: "MANUAL"`) **cannot be undone**: the server
  stores no entry for it, so `/unclaimRewardPoints` returns
  `404 "No such entry"` (with or without a `points` field), and claiming
  negative points is rejected with 400. The Marvin web app never uses
  `MANUAL` — it is an API-only facility. The only compensation is spending
  the same amount, which inflates the spent statistics.
- `/spendRewardPoints` returns a 500 if the balance would go negative.
- The app's purchasable rewards are separate `db="Rewards"` documents that
  the public API **cannot reach at all** (live-tested 2026-08-29: `/rewards`
  and every variant 404, no rewards profile documents, and `/doc` needs an
  ID you can't discover). The Task field `isReward` is decoupled from the
  app's reward flow and produced no UI effect when set via the API.

**Reminders**
- A *task* reminder in Marvin is two writes that only the app keeps in
  sync: reminder fields on the task document (`taskTime`, `reminderTime`,
  `reminderOffset`, `snooze`, `autoSnooze`) **and** a server-side entry via
  `/reminder/set`. Writing only one side (all the API lets you do
  comfortably) produces entries the app UI won't show on the task, or
  server-side orphans. Standalone reminders (type `M`) are the safe use of
  the raw API; since 1.8.0 the tools write both sides the way the app does. (Risk first documented by
  [Recon2026/marvin-mcp](https://github.com/Recon2026/marvin-mcp);
  confirmed by the official wiki's own warning.)

**Time & planning**
- `/todayTimeBlocks` omits the block↔category link (issue #65); this
  server recovers the mapping from the `strategySettings.plannerSmartLists`
  profile document.
- Stopping time tracking via a direct `/track STOP` does not update the
  task's own `times`/`duration` fields; `/tracks` is the source of truth.
  `/markDone` during active tracking does write both `times` (live-tested
  2026-09-02) and `duration` (verified 2026-09-13; the app shows the
  tracked time under Completed Today when Time Tracking is on).
- Calendar events created via `/addEvent` sync onwards only while the
  Marvin app is running somewhere (client-side calendar sync).

**UI behavior of API-set fields (verified in the app, 2026-08-29)**
- Toggling a strategy requires an app restart before its fields render —
  without one, freshly enabled strategies show nothing and look broken.
- `backburner` is only effective on unscheduled items: scheduling (`day`)
  trumps the flag in the UI. Set `day: "unassigned"` together with
  `backburner: true`.
- `startDate` hides *backburner* items until their start date (the Start
  Dates strategy's actual mechanic) — it does not hide scheduled tasks.
- Icon names are library-prefixed (`lucide-Rocket`, `huge-happy`) or emoji.
  Projects render their icon only when Master List → Configure View →
  "Show Custom Icon On" includes projects — "Categories & Projects"
  (verified 2026-08-31) or "Just Projects" (verified 2026-09-18); the
  earlier "projects never render an own icon" was our own test error.
- A project's own `timeEstimate` takes precedence; without one the project
  shows the sum of its children's estimates (per Marvin support's reply to
  our report — our earlier "no aggregation" came from a mis-designed test
  that set estimates on both the project and its children).
- Snoozed tasks (`itemSnoozeTime`) are hidden from the category view too —
  the wiki's "everywhere except the master list" doesn't hold there.
- `timeBlockSection` links a task to a time block's section in Today — but
  only if the Time Block Sections strategy is on, the day view is grouped
  by time block (Group by → Group by time block section; set per device,
  not synced) and the task is scheduled on the block's day. Unscheduled
  tasks with the field set do not appear in Today at all. The block needs
  no label/category mapping of its own; that mapping (Smart Time Block) is
  a second, independent route. Verified in the app 2026-09-17 (1.70.0.0,
  PWA + desktop); the earlier "stored but not rendered" finding was our own
  test error (grouping never set) and is withdrawn; the correction to
  Marvin support is pending. Since
  1.7.0 `create_time_block` returns `time_block_id` (set client-side, as
  `/doc/create` does not echo the server id), usable directly as
  `time_block_section`.
- `reviewDate` shows in the Review view; the day-view banner additionally
  requires the "Review Alert" workflow snippet.
- Auto-orbit (if enabled) pulls newly scheduled tasks into Orbit unless
  `noAutoOrbit` is set.
- Project-only fields (`day`, `dueDate`, `priority`, `isFrogged`) written
  onto a category are silently accepted by the server (live-tested
  2026-09-11). `update_category_or_project` still type-checks and blocks
  them, for a structural reason: a category is never completed, so those
  fields belong to projects and tasks. `labelIds` is different — categories
  have labels in the same field as projects, and an API-set label is
  stored and rendered (live-tested + verified in the app 2026-09-11), so
  `label_ids` is allowed on categories since 1.6.0.

**Recurring tasks and occurrences** (live-tested 2026-10-09 to 2026-10-11)
- Occurrences have the id `YYYY-MM-DD_<recurringTaskId>`; an occurrence
  created by ticking off an *after completion* task gets a random id, and
  only the first one is written via the API (`echo: true`, `echoId: null`).
  The next one after `/markDone` lands on day + `echoDays`.
- `createAhead` missing or `null` = the account's default. The app shows a
  `custom` rule correctly only as an RRULE string, so the tools accept only
  RRULE (rules in words like "every other friday" are refused).
- Deleting one occurrence the way the app's *Just this one* does adds its
  ISO date to the recurring task's `deletedDates`.
- With *Only on weekdays*, a month day on a weekend moves to Friday.
- Occurrences inherit labels, priority, size, note, sections, subtasks (id
  `<day>_<subid>`) and `dueIn` → `dueDate`.

**Notebooks**
- `GET /notebook?parentId=` answered 404 "Notebook not found" 5 and 12 s
  after the notebook was created with `/doc/create`, while `?id=` answered
  200 at the same time (cause unknown). `get_notebook` with `parent_id`
  then returns an error suggesting `notebook_id`.
- A project's `notePageId` is computed by the server from the page with the
  lowest `rank`; writing the project's `note` writes that page.
- Deleting a project in the app (with its notes) leaves its notebook and
  page on the server — still listed by `/notebooks` and
  `/notebook?parentId=`. `get_notebooks` flags such notebooks
  `parent_missing` (and `parent_done` for a done project).

**Reminders and time of day**
- A clock time = `taskTime` "HH:MM" + `reminderTime` (seconds) and friends
  on the task, plus a server entry of type `T` whose `reminderId` is the
  task's id. Own reminders = a `reminders` list on the task plus one type
  `M` entry each.
- Completing a task in the app removes `T` and `M` on the server; undoing
  the completion does not put `T` back, while `M` still notifies. Before
  deleting a task with reminders, delete its server entries too, or an
  open client keeps reminding.

**Habits**
- `/updateHabit` with `undo` always removes the *latest* history entry; if
  `time` is sent with it, `[time, 0]` is appended as well. Never send
  `time` with `undo`.
- A recording below a numeric target writes nothing on the habit task
  (`habitRepAt` stays null).

**Completing projects and other document behavior**
- When the app completes a project it writes `done`, `doneAt` =
  `workedOnAt`, `day` (today if unset) and `doneDate` = `day` in one
  stamped write, and refuses while children are open.
  `mark_project_done` mirrors this.
- `/categories` does not list done projects.
- `/markDone` sets no `workedOnAt` and stamps no `fieldUpdates`; on a task
  without a day it sets `day` to today, also unstamped.
- `GET /doc` on a deleted document answers 200 with
  `{"error":"not_found","reason":"deleted"}`; `/doc/update` on it gives 500.
- `/addTask` drops `taskSize` (XS–XL = 1–5); it is written afterwards with
  `/doc/update`.

**Rate limits**
- Getting started says "No bursts faster than 1 request every 3 seconds".
  The limiter spaces creates (`/addTask`, `/addProject`, `/addEvent`,
  `/doc/create`) 1.1 s and everything else 3.1 s, keyed on the *upcoming*
  call's kind.

## How this differs from existing alternatives

Several good Amazing Marvin MCP servers exist; this one was built fresh
(no shared code) after studying them, with a different goal — *complete*
coverage of the public API rather than a curated subset:

- [bgheneti/Amazing-Marvin-MCP](https://github.com/bgheneti/Amazing-Marvin-MCP)
  — the established Python server; broad but not complete coverage, no
  global rate limiting.
- [Recon2026/marvin-mcp](https://github.com/Recon2026/marvin-mcp) — smaller
  scope (19 tools), unusually careful research; chose to make reminders
  read-only over the two-write risk. This server ships reminder writes with
  explicit warnings instead.
- [lucasoeth/marvin-mcp](https://github.com/lucasoeth/marvin-mcp) — a
  different philosophy: a handful of consolidated workflow tools (brief/
  capture/…) rather than an API mirror, plus direct CouchDB reads for
  search and completed tasks (which the public API can't do at all). If you
  want opinionated workflows or search, use theirs; if you want raw,
  complete API access with the sharp edges documented, use this one.
- [LucaDeLeo/amazing-marvin-mcp](https://github.com/LucaDeLeo/amazing-marvin-mcp)
  — a Limited-API subset.

## Credits & sources

No code was copied from any of these — the build is fresh — but they
materially shaped it:

- **[amazingmarvin/MarvinAPI](https://github.com/amazingmarvin/MarvinAPI)**
  (+ [wiki](https://github.com/amazingmarvin/MarvinAPI/wiki)) — the official
  API documentation, OpenAPI spec, data types, and issue tracker this
  server is built against.
- **[bgheneti/Amazing-Marvin-MCP](https://github.com/bgheneti/Amazing-Marvin-MCP)**
  — architecture inspiration, endpoint reference during the initial gap
  analysis, and the MIT-licensing precedent.
- **[Recon2026/marvin-mcp](https://github.com/Recon2026/marvin-mcp)** — the
  reminder two-write integrity risk and the groundwork on recurring-task
  instances, both verified and documented here.
- **[lucasoeth/marvin-mcp](https://github.com/lucasoeth/marvin-mcp)** — the
  `#Category` shortcut bug (reproduced here) and the insight that Marvin's
  sync database is a real CouchDB usable for reads.
- **[LucaDeLeo/amazing-marvin-mcp](https://github.com/LucaDeLeo/amazing-marvin-mcp)**
  — the pointer that `/addTask` parses shortcut syntax server-side (partly
  confirmed, partly refuted — see the `#Category` finding), and the idea of
  MCP tool annotations.

Built with [Claude Code](https://claude.com/claude-code) (Claude Fable 5).

## License

[MIT](LICENSE).
