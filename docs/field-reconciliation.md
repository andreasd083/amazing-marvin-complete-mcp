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
  (`"high"`/`"mid"`/`"low"`); `isStarred` is never set on projects. Tasks
  use `isStarred` (1–3).
- `/doc/update` with `val: null` **clears** a field (the tools' `''`/`0`
  conventions build on this).
- `orbit`, `noAutoOrbit` (bool) and `firstOrbitDate` (`YYYY-MM-DD`) exist in
  live data but are **entirely missing from the wiki's data types** — the
  bool fields are exposed as explicitly-undocumented passthrough,
  `firstOrbitDate` is left to the app.

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
| `isStarred` | supported | `create_task` (`priority` 1–3), `set_priority` |
| `isFrogged` | supported | `create_task` (`frog`), `set_priority` |
| `done`/`doneAt` | supported | `mark_done` (via `/markDone` — never `/doc/update`, MarvinAPI issue #6), `unmark_done` |
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
| `priority` | supported (projects only) | the string field `"high"`/`"mid"`/`"low"` — not `isStarred`; renders as a red ring in lists / red star in the panel (verified in the app 2026-08-29) |
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
