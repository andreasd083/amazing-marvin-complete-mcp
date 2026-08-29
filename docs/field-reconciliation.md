# Field-by-field reconciliation against Marvin's data model

A reconciliation (2026-08-29) of [Marvin Data Types](https://github.com/amazingmarvin/MarvinAPI/wiki/Marvin-Data-Types)
(the *Tasks* and *Categories (and projects)* sections) against this server's
tools. All "live-tested 2026-08-29" claims were verified against the live
API with throwaway test artifacts (created, verified, deleted).

Coverage criterion: every writable field is either supported by at least one
tool, or explicitly listed as **unsupported** with a reason. Read-only /
computed fields (`_rev`, `fieldUpdates`, `updatedAt`, `createdAt`, …) are
listed as *system fields*.

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
| `startDate` | supported (update only) | `update_task` — `/addTask` ignores the field (live-tested 2026-08-29) |
| `endDate` | supported (update only) | `update_task` — as `startDate` |
| `plannedWeek` | supported | `create_task`, `update_task` (client-side Monday validation) |
| `plannedMonth` | supported | `create_task`, `update_task` |
| `reviewDate` | supported | `create_task`, `update_task` (Review Date strategy) |
| `timeEstimate` | supported | `create_task`, `update_task` (minutes → ms) |
| `note` | supported | `create_task`, `update_task` |
| `labelIds` | supported | `create_task`, `update_task` |
| `isStarred` | supported | `create_task` (`priority` 1–3), `set_priority` |
| `isFrogged` | supported | `create_task` (`frog`), `set_priority` |
| `done`/`doneAt` | supported | `mark_done` (via `/markDone` — never `/doc/update`, MarvinAPI issue #6), `unmark_done` |
| `backburner` | supported | `create_task`, `update_task` |
| `isReward` | supported | `create_task` |
| `rewardPoints` | supported | `create_task`, `update_task` |
| `dailySection` | supported | `create_task`, `update_task` (dailyStructure strategy) |
| `bonusSection` | supported | `create_task`, `update_task` (bonusStructure strategy) |
| `customSection` | supported | `create_task`, `update_task` (customStructure strategy) |
| `timeBlockSection` | supported | `create_task`, `update_task` (Time Blocking) |
| `itemSnoozeTime` | supported (update only) | `update_task` (`snooze_until_unix_ms`) |
| `permaSnoozeTime` | supported (update only) | `update_task` — `/addTask` ignores the field (live-tested 2026-08-29) |
| `orbit`, `noAutoOrbit` | supported (update only) | `update_task` — undocumented in the wiki; bool type verified in live data 2026-08-29 |
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
| `icon` | supported | as `color` |
| `timeEstimate` | supported | `create_category_or_project`, `update_category_or_project` |
| `startDate` | supported (update only) | `update_category_or_project` — `/addProject` ignores the field (live-tested 2026-08-29) |
| `endDate` | supported (update only) | as `startDate` |
| `plannedWeek`, `plannedMonth` | supported | `create_category_or_project`, `update_category_or_project` |
| `reviewDate` | supported | `create_category_or_project`, `update_category_or_project` |
| `day` | supported (projects only) | `create_category_or_project`, `update_category_or_project` — categories cannot be scheduled |
| `dueDate` | supported (projects only) | as `day` |
| `priority` | supported (projects only) | the string field `"high"`/`"mid"`/`"low"` — not `isStarred` (live-tested 2026-08-29) |
| `isFrogged` | supported (projects only) | `create_category_or_project` (`frog`), `update_category_or_project` |
| `labelIds` | supported (projects only) | `create_category_or_project`, `update_category_or_project` |
| `backburner` | supported (update only) | `update_category_or_project` |
| `type` | supported | `convert_category_or_project` (experimental — in-place conversion; no official endpoint) |
| `orbit`, `noAutoOrbit` | supported (update only) | `update_category_or_project` — undocumented, see Tasks |
| `done`, `doneDate` | **unsupported** | projects are completed in the app (`/markDone` rejects projects; `done` via `/doc/update` skips the app's side effects) |
| `rank`, `dayRank`, `firstScheduled` | **unsupported** | the app's sorting/bookkeeping fields (`firstScheduled` is cleared by `convert_category_or_project` as leftover cleanup) |
| `recurring`, `recurringTaskId`, `echo` | **unsupported** | recurrence machinery |
| `marvinPoints`, `mpNotes` | system fields | written by the server on completion |
| `sprintId` | **unsupported** | "Not used yet" per the wiki |
| `updatedAt`, `workedOnAt`, `_id`, `_rev`, `db` | system fields | — |
