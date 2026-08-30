# Field-by-field reconciliation against Marvin's data model

A reconciliation (2026-08-29) of [Marvin Data Types](https://github.com/amazingmarvin/MarvinAPI/wiki/Marvin-Data-Types)
(the *Tasks* and *Categories (and projects)* sections) against this server's
tools. All "live-tested 2026-08-29" claims were verified against the live
API with throwaway test artifacts (created, verified, deleted).

Coverage criterion: every writable field is either supported by at least one
tool, or explicitly listed as **unsupported** with a reason. Read-only /
computed fields (`_rev`, `fieldUpdates`, `updatedAt`, `createdAt`, …) are
listed as *system fields*.

## UI verification findings (2026-08-29)

Verified against the live API and the app's UI. Method notes that matter
for reproducing: toggling a strategy requires an app restart before UI
verification is valid, and auto-orbit pulls newly scheduled tasks into the
Orbit view unless `noAutoOrbit` is set.

- Works as documented: orbit, backburner (unscheduled items only — day
  trumps the flag), startDate (hides backburner items), rewardPoints
  (strategy on), project priority/frog/timeEstimate rendering, snooze
  fields, reviewDate, plannedWeek/Month.
- Stored but not rendered: timeBlockSection (no visible section link in
  Today), project icons (projects never render an own icon).
- Wiki corrections found: project timeEstimate is NOT aggregated with the
  children's estimates in the UI; snoozed tasks are hidden from the
  category view too (not just 'everywhere except the master list').
- Project-only fields written onto a category are silently accepted by the
  server but make the category unrepairable from the app's UI — hence the
  type-check guard in `update_category_or_project`.

## Key findings from the live tests

- `/addTask` and `/addProject` **ignore `startDate`/`endDate`** (the fields
  are also missing from the wiki's CreateTaskRequest/CreateProjectRequest) —
  they can only be set afterwards via `/doc/update`.
- `/addProject` **ignores `color`/`icon`**; `/doc/create` (categories) and
  `/doc/update` (both) persist them.
- Projects are prioritized with the **string field `priority`**
  (`"high"`/`"mid"`/`"low"` = Most/Very/Important); `isStarred` is never
  set on projects. Tasks use `isStarred` (-1, 1–3; -1 = Low priority).
- `/doc/update` with `val: null` **clears** a field (the tools' `''`/`0`
  conventions build on this).
- `orbit`, `noAutoOrbit` (bool) and `firstOrbitDate` (`YYYY-MM-DD`) exist in
  live data but are **entirely missing from the wiki's data types** — the
  bool fields are exposed as explicitly-undocumented passthrough,
  `firstOrbitDate` is left to the app.

## Level 2 — edge values (live-tested 2026-08-29)

**The server validates essentially nothing.** Via `/doc/update` everything
is stored verbatim with 200: invalid dates (`2026-02-31`, `31-12-2026`,
`2026-13-01`, year `0025`, the literal `"today"`, surrounding whitespace),
negative numbers (`timeEstimate -1`, `rewardPoints -5`), out-of-range
values (`isStarred 7`, `isFrogged -2`), extreme numbers (`timeEstimate
1e15`), mistyped values (`isStarred "3"`, `timeEstimate "900000"`,
`labelIds "string"`, `done "true"`), empty/5000-character/multi-line
titles, 20,000-character notes, dead references (`parentId`/`labelIds`
that do not exist — producing orphans) and entirely unknown fields.
Projects: `priority "urgent"`/`"HIGH"` are stored just as silently.
`/addTask` is almost as permissive (empty title, invalid `day`, negative
estimate, `isStarred 9`, dead references) — only pure type errors
(`timeEstimate "abc"`, `isStarred "high"`) give 400. Empty setters => 200.
`/doc/delete` gives 200 for IDs that never existed and for already deleted
ones (idempotent, no 404 — inconsistent with `/doc/update`, which gives 500).

**Consequence (1.4.0):** all validation lives in the tools, before the API
call: strict `YYYY-MM-DD` + valid calendar date + year 2000-2100 on every
date parameter (`'today'`/`'unassigned'` where allowed), titles are trimmed
and must not be empty, plus the pre-existing Monday check on plannedWeek
and Literal/ge/le on numbers and priority. Dead references
(parent_id/label_ids) are NOT validated (it would cost a read call per
write) — documented in the descriptions as an orphan risk.

## Level 3 — interactions/side effects (live-tested 2026-08-29)

- **Read endpoints are pure date filters:** `/todayItems` lists tasks with
  `day` = the date regardless of `backburner=true` or a future `startDate`
  — all such hiding is client logic. `day` + `plannedWeek` at the same
  time is allowed; `/markDone` leaves `plannedWeek` untouched.
- **Orphans** (dead `parentId`) show up in `/todayItems`/`/dueItems` if
  they have `day`/`dueDate`, but NOT under `/children?parentId=unassigned`
  — without a date they are unreachable via the API.
- **`/markDone` stops time tracking** (receipt in `/tracks`, 2 entries)
  but does NOT write `task.times` — `/tracks` is the only source of truth,
  even after completion (the wiki's "updated when marked done" applies to
  the client).
- **`/markDone` error codes:** already done => 400; missing ID => **404**
  (unlike `/doc/update` => 500 and `/doc/delete` => 200 — three different
  answers to the same error).
- **Pinned task + `/markDone`:** the original stays open and pinned
  (documented behavior); the completed copy gets its own ID and can be
  found via `/doneItems` (see Level 4).
- Recurrence generators deliberately NOT tested (they touch the app's
  `createdUpTo` bookkeeping).

## Level 4 — completed items and priority levels (live-tested 2026-08-30)

- **Completed tasks ARE readable:** `/doc?id=` returns the item with
  `done: true` and `doneAt`, and the **undocumented**
  `GET /doneItems?date=YYYY-MM-DD` (200 with a list, only `db: "Tasks"`,
  the limited token is sufficient) lists completed tasks. Without `date`
  => today. `/doneTasks`, `/completedItems` => 404; `done=1`/
  `includeDone=true` on `/todayItems` and `/children` are ignored. Zero
  mentions of the endpoint in the wiki, the OpenAPI spec and the issues —
  it may disappear without notice.
- **`/doneItems` filters on `day`, not `doneAt`:** a task with `day` =
  yesterday / 10 days ago / tomorrow completed today is listed under its
  `day`. `/markDone` sets `day` = today on an UNSCHEDULED task but leaves
  an existing `day` untouched; the app's markDone does the same for past
  `day` values and sets today only on unscheduled and FUTURE tasks (app
  code: `k = getDay(task); if (!k || k === "unassigned" || k > today)
  k = today`). An overdue task ticked off in the app therefore stays under
  its old date — hence the 7-day lookback + doneAt filter in
  `get_done_items`.
- **429 without breaking the 3 s rule** (2026-08-30): `/doneItems`
  answered `429 Too many AM API requests` on the eighth consecutive read
  at 3.05–3.1 s spacing, after ~100 calls in the same hour. Reading:
  the daily average is enforced in a rolling window of roughly an hour.
  No `Retry-After` known — response headers are now logged on a 429. The
  limiter enters a 60 s cool-down; `get_done_items` returns a partial
  result instead of an error.
- **Low priority = `isStarred: -1`** (app code: the `priorityLow` action
  sets -1, magic words `*low`/`*p0`/"low priority"; shown only with
  `priorities.lowPriority` enabled). `/addTask` and `/doc/update` store
  -1 and read it back.
- **Project `priority` string <-> star level** (app code, unambiguous in
  the edit dialog, Smart List filters, sorting and conversion):
  `high` = 3 = Most important, `mid` = 2 = Very important,
  `low` = 1 = **Important** (yellow). A task with -1 converted into a
  project gets `priority: null` — projects cannot be Low priority. (One
  tooltip string in the app calls `low` "Low priority"; it is contradicted
  by all other code and by the UI verification "priority=high -> red".)
- Live data: `isStarred` occurs as `0`, `1`, `2`, `3`, `False` and the
  string `'3'` (imports) — read with `int()` tolerance.

## Tasks

| Field | Status | Tool / reason |
|---|---|---|
| `title` | supported | `create_task`, `update_task` |
| `parentId` | supported | `create_task`, `update_task` |
| `day` | supported | `create_task`, `update_task` |
| `dueDate` | supported | `create_task`, `update_task` |
| `startDate` | supported (update only) | `update_task` — `/addTask` ignores the field; the strategy hides BACKBURNER items until their start date, not scheduled tasks (verified in the app 2026-08-29) |
| `endDate` | supported (update only) | `update_task` — as `startDate` |
| `plannedWeek` | supported | `create_task`, `update_task` (client-side Monday validation) — correct week in the UI, also shows in the month view; clearing propagates server-side but client cache may linger (verified in the app 2026-08-29) |
| `plannedMonth` | supported | `create_task`, `update_task` — correct month in the UI (verified in the app 2026-08-29) |
| `reviewDate` | supported | `create_task`, `update_task` — Review view + badge verified in the app 2026-08-29; the day-view banner additionally requires the Review Alert workflow snippet |
| `timeEstimate` | supported | `create_task`, `update_task` (minutes → ms) |
| `note` | supported | `create_task`, `update_task` |
| `labelIds` | supported | `create_task`, `update_task` |
| `isStarred` | supported | `create_task` (`priority` -1/1–3), `set_priority` (-1/0–3) — -1 = Low priority (down arrow), verified 2026-08-30 |
| `isFrogged` | supported | `create_task` (`frog`), `set_priority` |
| `done`/`doneAt` | supported | `mark_done` (via `/markDone` — never `/doc/update`, MarvinAPI issue #6), `unmark_done`; reading via `get_done_items` (undocumented `/doneItems`, Level 4) |
| `backburner` | supported | `create_task`, `update_task` — only effective on unscheduled tasks; day trumps the flag in the UI (verified in the app 2026-08-29) |
| `isReward` | supported (discouraged) | `create_task` — documented Task field with no observed function: the app's purchasable rewards are separate `db="Rewards"` documents that the public API cannot reach (live-tested 2026-08-29: /rewards and variants 404, no rewards profile documents, app rewards are not Tasks); the flag produced no UI effect |
| `rewardPoints` | supported | `create_task`, `update_task` — points the task AWARDS; coin + points in the list row with the Rewards strategy on (verified in the app 2026-08-29) |
| `dailySection` | supported | `create_task`, `update_task` (dailyStructure strategy) |
| `bonusSection` | supported | `create_task`, `update_task` (bonusStructure strategy) |
| `customSection` | supported | `create_task`, `update_task` (customStructure strategy) |
| `timeBlockSection` | supported (stored but not rendered) | `create_task`, `update_task` — no visible section link in Today even with the strategy active (verified in the app 2026-08-29) |
| `itemSnoozeTime` | supported (update only) | `update_task` (`snooze_until_unix_ms`) — hides from Today AND the category view (verified in the app 2026-08-29; the wiki's 'except the master list' does not hold for the category view) |
| `permaSnoozeTime` | supported (update only) | `update_task` — `/addTask` ignores the field; hides from Today (verified in the app 2026-08-29) |
| `orbit`, `noAutoOrbit` | supported (update only) | `update_task` — undocumented in the wiki; orbit=true verified in the app 2026-08-29 (Orbit view + icon in Today). Auto-orbit pulls in scheduled tasks unless noAutoOrbit is set |
| `rewardId` | **unsupported** | no tool exposes Reward IDs to point at; set in the app |
| `dependsOn` | **unsupported** | complex object shape (ID⇒bool) with dependency logic in the app; set in the app |
| `subtasks` | **unsupported** | complex object shape; managed in the app |
| `rank`, `masterRank`, `dayRank` | **unsupported** | the app's sort bookkeeping — writing risks invisible/mis-sorted views |
| `firstScheduled` | **unsupported** | the app's bookkeeping for the procrastination counter |
| `taskTime`, `reminderTime`, `reminderOffset`, `snooze`, `autoSnooze`, `remindAt`, `reminder` | **unsupported** | task reminders require a double write kept in sync with the server-side entry — see the warning in `set_reminder`; set in the app |
| `isPinned`, `pinId`, `recurring`, `recurringTaskId`, `echo`, `echoId`, `generatedAt`, `echoedAt` | **unsupported** | recurrence/pinning machinery — never edited via the API (see the `update_task` warning) |
| `calId`, `calURL`, `etag`, `calData` | **unsupported** | calendar-sync internals |
| `times`, `duration`, `firstTracked` | **unsupported** | time-tracking cache — the source of truth is the `/track` endpoints (`start_tracking` etc.) |
| `g_in_*`, `g_sec_*`, `g_rank_*` | **unsupported** | dynamic goal-attachment keys; managed in the app |
| `marvinPoints`, `mpNotes` | system fields | kudos outcome, written by the server |
| `deletedAt`, `restoredAt`, `workedOnAt`, `completedAt`, `link`, `onboard`, `imported`, `email`, `colorBar` (deprecated), `sprintId` (unused) | system fields / **unsupported** | server/app bookkeeping resp. deprecated fields |

## Categories and projects

| Field | Status | Tool / reason |
|---|---|---|
| `title` | supported | `create_category_or_project`, `update_category_or_project` |
| `parentId` | supported | `create_category_or_project`, `update_category_or_project` |
| `note` | supported | `create_category_or_project`, `update_category_or_project` |
| `color` | supported | `create_category_or_project` (category only — `/addProject` ignores the field), `update_category_or_project` (both) |
| `icon` | supported (categories only meaningful) | as `color`; format = library-prefixed names ('lucide-Rocket', 'huge-happy') or emoji, API-set values render (verified in the app 2026-08-29). Projects NEVER render their own icon — the app offers the picker but only the color is used |
| `timeEstimate` | supported | `create_category_or_project`, `update_category_or_project` — the project's OWN estimate renders; no aggregation with the children's despite the wiki's claim (verified in the app 2026-08-29) |
| `startDate` | supported (update only) | `update_category_or_project` — `/addProject` ignores the field (live-tested 2026-08-29) |
| `endDate` | supported (update only) | as `startDate` |
| `plannedWeek`, `plannedMonth` | supported | `create_category_or_project`, `update_category_or_project` |
| `reviewDate` | supported | `create_category_or_project`, `update_category_or_project` |
| `day` | supported (projects only) | `create_category_or_project`, `update_category_or_project` — categories cannot be scheduled |
| `dueDate` | supported (projects only) | as `day` |
| `priority` | supported (projects only) | the string field `"high"`/`"mid"`/`"low"` = Most/Very/Important (3/2/1 stars; Level 4) — not `isStarred`, no Low level; `high` renders as a red ring in lists / red star in the panel (verified in the app 2026-08-29) |
| `isFrogged` | supported (projects only) | `create_category_or_project` (`frog`), `update_category_or_project` |
| `labelIds` | supported (projects only) | `create_category_or_project`, `update_category_or_project` — categories: the field is not part of the data model and the tools block it; the app attaches labels to categories only through its own Kanban drag flow (observed 2026-08-29: an API-created category could not be given a label any other way) |
| `backburner` | supported (update only) | `update_category_or_project` — same condition as Tasks (unscheduled item) |
| `type` | supported | `convert_category_or_project` (experimental — in-place conversion; no official endpoint) |
| `orbit`, `noAutoOrbit` | supported (update only) | `update_category_or_project` — undocumented, see Tasks |
| `done`, `doneDate` | **unsupported** | projects are completed in the app (`/markDone` rejects projects; `done` via `/doc/update` skips the app's side effects) |
| `firstScheduled` | supported (update only) | `update_category_or_project` (`first_scheduled`) — for restoring the value from the convert tool's `removed_project_fields`; nothing backfills it when day is set via /doc/update, neither server nor app (verified 2026-08-29) |
| `rank`, `dayRank` | **unsupported** | the app's sorting fields |
| `recurring`, `recurringTaskId`, `echo` | **unsupported** | recurrence machinery |
| `marvinPoints`, `mpNotes` | system fields | written by the server on completion |
| `sprintId` | **unsupported** | "Not used yet" per the wiki |
| `updatedAt`, `workedOnAt`, `_id`, `_rev`, `db` | system fields | — |
