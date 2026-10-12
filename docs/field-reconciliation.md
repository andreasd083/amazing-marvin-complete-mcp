# Field-by-field reconciliation against Marvin's data model

A reconciliation (2026-08-29) of [Marvin Data Types](https://help.amazingmarvin.com/amazing-marvin/api-data-types)
(then in the GitHub wiki; the documentation moved to the help center 2026-10-04)
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
- Project icons render only when Master List → Configure View → "Show
  Custom Icon On" includes projects: "Categories & Projects" (verified
  2026-08-31) or "Just Projects" (verified 2026-09-18); the earlier
  "projects never render an own icon" was our test error. timeBlockSection was listed here until 2026-09-17: the "no visible
  section link" finding was our own test error (the day view was never
  grouped by time block) and is withdrawn — see the field row below.
- Wiki corrections found: snoozed tasks are hidden from the
  category view too (not just 'everywhere except the master list').
  (Until 1.8.0 this list also said that a project's timeEstimate is NOT
  aggregated with the children's estimates. That test was mis-designed — it
  set estimates on both the project and its children. The aggregation
  exists; the project's own estimate takes precedence, per Marvin support's
  reply to our report.)
- Project-only fields (day/dueDate/priority/isFrogged) written onto a
  category are silently accepted by the server (live-tested 2026-09-11).
  `update_category_or_project` blocks them anyway (type check since
  1.2.0); since 1.6.0 the reason given is structural — a category is never
  completed, so those fields belong to things that can be finished — not
  "unrepairable from the UI" (the edit panel has a Due Date field on
  categories too, 2026-09-11). `labelIds` applies to categories as much as
  to projects.
- Clearing `plannedWeek`/`plannedMonth` with Planning Ahead on: the task
  stayed in the month view even after switching views (2026-08-29) — the
  client must be reloaded.
- The app's view (2026-09-12, PWA + desktop 1.70.0 side by side):
  create/update/move render immediately in both; a deletion never renders
  without a reload — F5 suffices in the PWA, the desktop app needs a
  restart (no refresh). Side finding: an API-deleted task came back on a
  new `_rev` after the still-shown row was edited in the app (observed
  once, in one of the two clients; reported upstream) — see the README bullet "The app's view after
  API writes".

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

- **Read endpoints are pure date filters:** `/todayItems` lists open tasks
  with `day` <= the date (earlier-scheduled items included, not only the
  day itself — live-tested 2026-09-11 with rollover enabled; items with a
  clock time untested) regardless of `backburner=true` or a future
  `startDate` — all such hiding is client logic. Completed items drop out
  immediately (2026-09-11).
- **`day` = `""`/`null` hides nothing:** `/dueItems` returns open items
  with a past `dueDate` whether `day` is `"unassigned"`, `""`, `null` or a
  date; `/addTask` turns `""` into `"unassigned"`, `/doc/update` stores
  `""` verbatim (2026-09-11). `day` + `plannedWeek` at the same
  time is allowed; `/markDone` leaves `plannedWeek` untouched.
- **Orphans** (dead `parentId`) show up in `/todayItems`/`/dueItems` if
  they have `day`/`dueDate`, but NOT under `/children?parentId=unassigned`
  — without a date they are unreachable via the API.
- **`/markDone` stops time tracking** (receipt in `/tracks`, 2 entries)
  and now writes `task.times` (live-tested 2026-09-02, two runs — on
  2026-08-29 the field was NOT written; the server behavior has changed).
  A direct `/track STOP` still does not write `times` — there `/tracks`
  is the only source of truth, in line with the wiki's NOTE about caller
  responsibility. `duration` is also set by `/markDone` during active
  tracking (verified in the app 2026-09-13: 29m15s and 4m29s, `times` =
  [start, stop]; the app shows the time under Completed Today when Time
  Tracking is on).
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
| `plannedWeek` | supported | `create_task`, `update_task` (client-side Monday validation) — correct week in the UI, also shows in the month view; clearing propagates server-side but the app kept showing the task in the month view even after switching views — reload the client (verified in the app 2026-08-29) |
| `plannedMonth` | supported | `create_task`, `update_task` — correct month in the UI (verified in the app 2026-08-29) |
| `reviewDate` | supported | `create_task`, `update_task` — Review view + badge verified in the app 2026-08-29; the day-view banner additionally requires the Review Alert workflow snippet |
| `timeEstimate` | supported | `create_task`, `update_task` (minutes → ms) |
| `note` | supported | `create_task`, `update_task` |
| `labelIds` | supported | `create_task`, `update_task` |
| `isStarred` | supported | `create_task` (`priority` -1/1–3), `set_priority` (-1/0–3) — -1 = Low priority (down arrow), verified 2026-08-30 |
| `isFrogged` | supported | `create_task` (`frog`), `set_priority` — a 1–4 scale since 1.8.0: 1 = I dread this a little, 2 = I dread this, 3 = I dread this a lot, 4 = This might kill me (toxic); `false` = none (read 2026-10-09; level 4 read from a screenshot) |
| `done`/`doneAt` | supported | `mark_done` (via `/markDone` — never `/doc/update`, MarvinAPI issue #6), `unmark_done`; reading via `get_done_items` (`/doneItems`, Level 4) |
| `backburner` | supported | `create_task`, `update_task` — only effective on unscheduled tasks; day trumps the flag in the UI (verified in the app 2026-08-29) |
| `isReward` | supported (discouraged) | `create_task` — documented Task field with no observed function: the app's purchasable rewards are separate `db="Rewards"` documents that the public API cannot reach (live-tested 2026-08-29: /rewards and variants 404, no rewards profile documents, app rewards are not Tasks); the flag produced no UI effect |
| `rewardPoints` | supported | `create_task`, `update_task` — points the task AWARDS; coin + points in the list row with the Rewards strategy on (verified in the app 2026-08-29) |
| `dailySection` | supported | `create_task`, `update_task` (dailyStructure strategy) |
| `bonusSection` | supported | `create_task`, `update_task` (bonusStructure strategy) |
| `customSection` | supported | `create_task`, `update_task` (customStructure strategy) |
| `timeBlockSection` | supported | `create_task`, `update_task` — points the task at a time block; the task appears under that block's section in Today. Three conditions (verified in the app 2026-09-17, 1.70.0.0, PWA + desktop): the Time Block Sections strategy on; the day view grouped by time block (Group by → Group by time block section — per device, not synced; help article 1950243); the task scheduled on the block's day (`day` ≤ that date — an unscheduled task with the field set is stored but does not appear in Today). The field is sufficient on its own: the block needs no label/category/smart list, and blocks from `create_time_block` (client-set `_id`, live-tested 2026-09-13) behave like app-created ones. The block's own Smart Time Block mapping (label/category) is a second, independent route that catches matching tasks without the field. The section shows before the block's start time; after its end: untested. The field is not exposed in the app's task settings — the app sets it when a task is added directly inside a block section. The earlier "stored but not rendered" wording (2026-08-29, 2026-09-13) was our own test error — grouping never set — and is withdrawn 2026-09-17 (correction to Marvin support pending); Marvin support had already reproduced the field working 2026-09-14 |
| `itemSnoozeTime` | supported (update only) | `update_task` (`snooze_until_unix_ms`) — hides from Today AND the category view (verified in the app 2026-08-29; the wiki's 'except the master list' does not hold for the category view) |
| `permaSnoozeTime` | supported (update only) | `update_task` — `/addTask` ignores the field; hides from Today (verified in the app 2026-08-29) |
| `orbit`, `noAutoOrbit` | supported (update only) | `update_task` — undocumented in the wiki; orbit=true verified in the app 2026-08-29 (Orbit view + icon in Today). Auto-orbit pulls in scheduled tasks unless noAutoOrbit is set. **`/addTask` silently drops `noAutoOrbit`** (live-tested 2026-09-13: 200, the field is absent from both the echo and the stored document) — hence no create parameter; set it with update_task after creating |
| `rewardId` | **unsupported** | no tool exposes Reward IDs to point at; set in the app |
| `dependsOn` | **unsupported** | complex object shape (ID⇒bool) with dependency logic in the app; set in the app |
| `subtasks` | supported (1.8.0) | `create_task` (`subtasks`) and the recurring-task tools (`subtasks`, `subtasks_add`) — dotted keys `subtasks.<id>` = `{_id (13 characters), title, done, rank}`, plus `.done`/`.doneAt`/`.title`, stamped as `fieldUpdates.subtasks` (read 2026-10-09). Occurrences inherit them with a date prefix, `<day>_<subid>` (read) |
| `rank`, `masterRank`, `dayRank` | **unsupported** | the app's sort bookkeeping — writing risks invisible/mis-sorted views |
| `firstScheduled` | **unsupported** | the app's bookkeeping for the procrastination counter |
| `taskTime`, `reminderTime`, `reminderOffset`, `snooze`, `autoSnooze`, `bugMeInterval`, `reminders` | supported (1.8.0) | clock time and own reminders written in the app's form, both fields and server entries (`set_reminder`/`delete_reminder` with a task); see *Reminders* under 1.8.0 below (read 2026-10-09 to 2026-10-11). Facts (live data 2026-09-02): Time = a clock time on the day (`day` remains the only scheduling), creates no event/time block; with auto-created reminders enabled the app writes `reminderTime` = the clock time, `reminderOffset: 0`, snooze per settings + a server-side entry — Time and reminder are in practice the same thing |
| `remindAt`, `reminder` | **unsupported** | — |
| `taskSize` | supported (1.8.0) | `size` on the task and project tools — XS–XL = 1–5, removed = `false`, absent when never set; `/addTask` drops the field, so it is written with `/doc/update` (read 2026-10-05; projects read 2026-10-09) |
| `habitId`, `habitRepAt` | read and written (1.8.0) | `mark_done`/`unmark_done` on habit tasks; see *Habits* under 1.8.0 below |
| `recurring`, `recurringTaskId`, `echo`, `echoId`, `generatedAt` | read; written only on the first after-completion occurrence (1.8.0) | see *Recurring tasks* and *Occurrences* under 1.8.0 below |
| `isPinned`, `pinId`, `echoedAt` | **unsupported** | recurrence/pinning machinery — never edited via the API (see the `update_task` warning) |
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
| `icon` | supported | as `color`; format = library-prefixed names ('lucide-Rocket', 'huge-happy') or emoji, API-set values render (verified in the app 2026-08-29). On projects the icon renders only when Master List → Configure View → 'Show Custom Icon On' includes projects: 'Categories & Projects' (verified 2026-08-31) or 'Just Projects' (verified 2026-09-18); the Main/All Categories modes are untested for subcategory icons. 'Projects NEVER render their own icon' was our test error, corrected 2026-09-18 |
| `timeEstimate` | supported | `create_category_or_project`, `update_category_or_project` — the project's own estimate takes precedence; without one the project shows the sum of its children's (per Marvin support's reply to our report; our 2026-08-29 "no aggregation" was a mis-designed test, corrected in 1.8.0) |
| `startDate` | supported (update only) | `update_category_or_project` — `/addProject` ignores the field (live-tested 2026-08-29) |
| `endDate` | supported (update only) | as `startDate` |
| `plannedWeek`, `plannedMonth` | supported | `create_category_or_project`, `update_category_or_project` |
| `reviewDate` | supported | `create_category_or_project`, `update_category_or_project` |
| `day` | supported (projects only) | `create_category_or_project`, `update_category_or_project` — blocked for categories for a structural reason (a category is never completed; the server accepts the field, live-tested 2026-09-11) |
| `dueDate` | supported (projects only) | as `day` |
| `priority` | supported (projects only) | the string field `"high"`/`"mid"`/`"low"` = Most/Very/Important (3/2/1 stars; Level 4) — not `isStarred`, no Low level; `high` renders as a red ring in lists / red star in the panel (verified in the app 2026-08-29) |
| `isFrogged` | supported (projects only) | `create_category_or_project` (`frog`), `update_category_or_project` |
| `labelIds` | supported (categories and projects) | `create_category_or_project`, `update_category_or_project` — categories have labels in the same field as projects: the app's drag-assigned category labels live in `labelIds`, and an API-set label is stored, stays and renders in the app (live-tested + verified in the app 2026-09-11). The block up to 1.5.1 was the tool's, not Marvin's. Exclusive label groups are not enforced server-side — at most one label per exclusive group |
| `backburner` | supported (update only) | `update_category_or_project` — same condition as Tasks (unscheduled item) |
| `type` | supported | `convert_category_or_project` (experimental — in-place conversion; no official endpoint) |
| `orbit`, `noAutoOrbit` | supported (update only) | `update_category_or_project` — undocumented, see Tasks |
| `done`, `doneDate` | **`mark_project_done`** (1.8.0) | `/markDone` answers 400 on projects (tasks only). The tool writes the app's five fields in one stamped `/doc/update`: `done`, `doneAt` = `workedOnAt` = now, `day` = the project's day or today if it has none, `doneDate` = `day`. `dueDate` is not touched. The app refuses while children are open ("Mark subprojects/tasks done first"); the tool closes open tasks first, after a first step that lists them and returns a confirmation code. Done projects are missing from `/categories` (read, four cases). See *Completing projects* under 1.8.0 below |
| `firstScheduled` | supported (update only) | `update_category_or_project` (`first_scheduled`) — for restoring the value from the convert tool's `removed_project_fields`; nothing backfills it when day is set via /doc/update, neither server nor app (verified 2026-08-29) |
| `rank`, `dayRank` | **unsupported** | the app's sorting fields |
| `recurring`, `recurringTaskId`, `echo` | **unsupported** | recurrence machinery |
| `marvinPoints`, `mpNotes` | system fields | written by the server on completion |
| `sprintId` | **unsupported** | "Not used yet" per the wiki |
| `updatedAt`, `workedOnAt`, `_id`, `_rev`, `db` | system fields | — |

## 1.8.0 — recurring tasks, notebooks, reminders, habits, completing projects

Facts behind the 1.8.0 tools, gathered against the live API and the app
between 2026-10-05 and 2026-10-12. Every fact carries its level: **read**
(read from the API, a log or the app's data), **verified in the app**,
**derived** (inferred, not observed directly) or **untested**.

**Sources read before building** (help center pages, `dateModified` in UTC):
[API changelog](https://help.amazingmarvin.com/amazing-marvin/api-changelog) 2026-10-05,
[API endpoints](https://help.amazingmarvin.com/amazing-marvin/api-endpoints) 2026-10-06,
[Data types](https://help.amazingmarvin.com/amazing-marvin/api-data-types) 2026-10-04,
[Recurrence, saved and pinned items (data)](https://help.amazingmarvin.com/amazing-marvin/api-recurrence) 2026-10-04,
[Getting started with the Marvin API](https://help.amazingmarvin.com/amazing-marvin/api-getting-started) 2026-10-06,
[Recurring Tasks](https://help.amazingmarvin.com/amazing-marvin/recurring-tasks-feature) 2026-10-04,
[Stop recurring tasks from piling up](https://help.amazingmarvin.com/amazing-marvin/stop-recurring-tasks-from-piling-up) 2026-10-02,
[Notebooks](https://help.amazingmarvin.com/amazing-marvin/notebooks) 2026-10-04,
[Notebooks and Day Notes](https://help.amazingmarvin.com/amazing-marvin/notebooks-and-day-notes) 2026-10-08,
[Habits](https://help.amazingmarvin.com/amazing-marvin/habits) 2026-10-06,
[Adding habits and showing them as tasks](https://help.amazingmarvin.com/amazing-marvin/adding-habits-and-showing-them-as-tasks) 2026-10-04,
[Duration estimates](https://help.amazingmarvin.com/amazing-marvin/duration-estimates) 2026-10-06,
[Undo and the Trash](https://help.amazingmarvin.com/amazing-marvin/undo-and-the-trash) 2026-10-11
(new paragraph: "Deleting a project — When you delete a project, Marvin
shows what will be deleted with it and asks you to confirm."), and the two
weekday articles ([every week day](https://help.amazingmarvin.com/amazing-marvin/how-to-set-up-a-recurring-task-for-every-week-day),
[Mo–Fr](https://help.amazingmarvin.com/amazing-marvin/how-to-set-a-task-to-be-done-during-weekdays-eg-mo-fr)).
The app's public What's New ends at 1.67.1; no newer app version was known
(untested).

### Recurring tasks (`db: RecurringTasks`)

| Field | Status | Tool / source |
|---|---|---|
| `type` | supported | `create_recurring_task`/`update_recurring_task` (`repeat`): `daily`, `weekly`, `monthly`, `n per week` (`x_times_per_week`), `repeat` (`every_x_days`), `echo` (`after_completion`), `repeat week`, `repeat month`, `repeat year` (`every_x_weeks`/`_months`/`_years`), `onOff` (`on_off`), `custom` — all eleven display correctly in the app's dialog (verified in the app) |
| `day` (0 = Sunday), `date`, `weekDays`, `repeat`, `echoDays`, `onCount`, `offCount`, `customRecurrence` | supported | the type's sub-fields; After completion writes `repeat` = `echoDays` (read). `customRecurrence` is written only as an RRULE string (`RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=FR`); rules in words are refused by the tools, because the app did not reliably understand them (e.g. "every other friday") |
| `createAhead` | supported | `create_occurrences`: 0/1/3/7/14/30; a missing field = the account's default (read); changing back to the default writes `null`, which the app reads as the account's default (read) |
| `missedMode` | supported | `if_you_miss_one`: `""` (default) / `stack` (keep them all) / `carry` (keep just one) / `skip` (only the current one) (read); `get_today_items`/`get_due_items` filter on it |
| `limitToWeekdays` | supported | `only_weekdays`, only for the types the dialog offers it on. A month day on a weekend moves to Friday (documented for monthly/repeat month; read); `every_x_days` then counts in weekdays (derived from one series; undocumented). The occurrence clean-up in `update_recurring_task` only lists, never deletes |
| `dueIn`, `repeatStart`, `endDate`, `dailySection`, `bonusSection` | supported | `hard_deadline_days`, `starts_on`, `ends_on`, `daily_section`, `bonus_section` (read) |
| `labelIds`, `isStarred`, `taskSize`, `note`, `subtasks` | supported | properties of the recurring task; inheritance to occurrences created via the API read (also `dailySection`, `bonusSection`, and `dueIn` → `dueDate`) |
| `deletedDates` | written | ISO dates; deleting one occurrence adds its day here, as the app's *Just this one* does (read). The clean-up in `update_recurring_task` does not write it (read) |
| `parentId` | supported | moving it; the next occurrence lands in the new place (read) |

### Occurrences

| Field | Status | Source |
|---|---|---|
| `_id` = `YYYY-MM-DD_<recurring task id>` | read | after-completion occurrences created by ticking one off get a random id (read) |
| `generatedAt`, `fieldUpdates` | read | "unchanged" = no stamp later than `generatedAt` + 60 s except `masterRank`/`dueDate`/`updatedAt`, and `day` = the date in the id (read) |
| `echo`, `echoId` | written on the first after-completion occurrence | `echo: true`, `echoId: null` (read); the next occurrence after `/markDone` = day + `echoDays`; for an occurrence dated in the future the tool first moves it to today, as the app does (read) |
| `subtasks` | written on the first after-completion occurrence | id `<day>_<subid>` (read) |

### Notebooks (`db: Notebooks`) and pages (`db: Pages`)

| Field | Status | Source |
|---|---|---|
| Notebooks: `_id` (`nb_…`), `title`, `parentId`, `parentType`, `rank`, `sections` (`[{id: 'sec_…', title, rank}]`) | supported | sections are rewritten as a whole list (read); renaming only on standalone notebooks (a product decision) |
| Pages: `_id` (`pg_…`), `notebookId`, `sectionId`, `rank`, `title`, `note`, `hasBody`, `titleAuto` | supported | create, change, move, delete (read) |
| Pages: `originalNoteFrom`, `migratedAt` | written when moved to a parent that has a note | the app's *YOUR ORIGINAL NOTE* marker (verified in the app); a parent without a note gets no Notes page (read) |
| Project/category: `notebookId`, `notePageId` | read | computed by the server from the page with the lowest `rank`; `note` writes to that page, or creates page 1 if the notebook is empty (read) |

### Reminders

| Part | Shape | Source |
|---|---|---|
| Clock time | `taskTime` "HH:MM", title prefix, `reminderTime` (seconds), `reminderOffset` 0, `snooze` 5, `autoSnooze` false, `bugMeInterval` 0; server entry of type **T** with `reminderId` = the task's id, `taskId: ""` | read |
| Own reminder | `reminders: [{reminderId (13 characters), time (s)}]` + a server entry of type **M** with `taskId` | read |
| Completion | the app removes T and M on the server and clears `reminderTime`; the `reminders` list stays | read |
| Deletion | clear the fields, `/reminder/delete` T and M, then delete — otherwise an open client still reminds | read |
| New day / new clock time | T and `reminderTime` move, M stays (also *before* choices, which are stored as an absolute time) | read |
| Day removed | the clock-time fields and `autoSnoozeChosen`/`remind` = null, T removed, M stays | read |
| Undone completion | T is not put back; M still notifies | read |

### Habits

| Field | Behavior | Source |
|---|---|---|
| `history` (the habit) | `[time, value, …]`; the app's tick-off records 12:00 on the task's `day` | read |
| `recordType: number`, `target` | values are summed per day; the task is ticked off when the target is reached | read |
| `habitRepAt` (the task) | the recording's time; set by `mark_done` | read |
| `/updateHabit` `undo` | always removes the LATEST entry; with `time`, `[time, 0]` is appended as well — never send `time` with `undo`, and compare the last entry with `habitRepAt` first | read |
| Partial recording (below the target) | the app writes nothing on the task, neither on recording nor on undo (Ctrl+Z removes the habit's last entry); `habitRepAt` stays null — `unmark_done` therefore writes nothing on an open habit task | read 2026-10-11 |

### Completing projects

| Finding | Behavior | Level |
|---|---|---|
| Project completed in the app, no day | `day` today, `doneDate` today, `doneAt` = `workedOnAt`, a set `dueDate` stays | read 2026-10-12 |
| Project completed in the app, day in the past/future | `day` stays, `doneDate` = `day` (also in the future), `dueDate` "" stays | read 2026-10-12 |
| `fieldUpdates` on completion | stamped for `done`, `doneAt`, `doneDate`, `day`, `workedOnAt`, also when `day` does not change; `_rev` 2 = one write | read (three cases) |
| `dueDate` "" | became null in one test but stayed "" in two others — difference unexplained; the tool does not touch the field | read, cause untested |
| `/markDone` and `workedOnAt` on the children | the app sets `workedOnAt` on the children; `/markDone` sets no `workedOnAt` and stamps no `fieldUpdates` — a known deviation, stated in the tool descriptions | read |
| `/children` and reminder fields | the children's documents in `/children` carry `reminders`/`reminderTime`; `mark_project_done` cleans up their server entries from them | read (one case) |
| Deleting a project in the app | the app shows what will be deleted with the project and asks for confirmation (help article above); `delete_task` instead refuses while children are open | read in the article; what the app deletes with the project untested |
| `unmark_done` on a project/category | refuses without writing (a product decision) | read in the code and tests |

### Further findings

| Finding | Behavior | Level |
|---|---|---|
| `GET /notebook?parentId=` | 404 "Notebook not found" 5 and 12 s after the notebook was created with `/doc/create`, while `?id=` answered 200 at the same time. Later 200 for the same call on a project and a category. Cause untested. `get_notebook` then returns an error suggesting `notebook_id` (no fallback) | read (log 2026-10-11) |
| `get_notebook` with `page_id` | returns that single page | read in the code |
| `GET /doc` on a deleted document | 200 with `{"error":"not_found","reason":"deleted"}`, not 404 | read 2026-10-11 |
| Project deleted in the app with its notes | the project is deleted from the database; its notebook and page remain and are listed by `/notebooks` and `/notebook?parentId=` | read 2026-10-11 |
| `/categories` | lists no documents with `done: true` (64 documents, 2026-10-11) | read |
| `get_notebooks`, `search_notebooks` | `parent_missing` when the parent is absent from `/categories` and `/doc` answers `not_found`; `parent_done` when the parent is a done project | `deleted` read 2026-10-11; `/categories` not listing a done project read |
| `/markDone` on a task without a day | the server sets `day` to today without a `fieldUpdates` stamp; the app's undo writes `day: null` with a stamp and leaves `doneAt`. `unmark_done` does the opposite (the day stays, `doneAt` is cleared) | read |
| Task created without a day (`create_task`) | `day` and `firstScheduled` "unassigned", no `fieldUpdates` in the response; `update_task` with a day stamps `fieldUpdates.day` but leaves `firstScheduled` "unassigned" | read |
| `unmark_done` and `day` | clears `day` only when `firstScheduled` is "unassigned", `fieldUpdates.day` is missing and the task is not an occurrence; otherwise the day stays | rule derived; the full chain (never a day → `mark_done` → `unmark_done`) untested |
| `unmark_done` and `doneAt` | still cleared, although the app's undo leaves the field — a deliberate deviation | read; the effect in the app untested |
| The rate limiter | creates (`/addTask`, `/addProject`, `/addEvent`, `/doc/create`) are spaced 1.1 s, everything else 3.1 s, per the documentation's "No bursts faster than 1 request every 3 seconds". The spacing is set by the kind of the upcoming call: only a create after the previous call gets 1.1 s (an earlier version keyed it on the previous call, so a `/doc/update` went out 1.1 s after an `/addTask`) | read (Getting started, the log and the code) |
