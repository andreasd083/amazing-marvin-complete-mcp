# amazing-marvin-complete-mcp

An MCP ([Model Context Protocol](https://modelcontextprotocol.io)) server for
[Amazing Marvin](https://amazingmarvin.com) with **complete coverage of the
public API**: 34 tools over all ~31 documented endpoints, a global rate
limiter that respects Marvin's documented limits, least-privilege token
routing, and MCP tool annotations. Every non-obvious behavior claim in the
tool descriptions was verified against the live API — the findings are
documented below in [Marvin API quirks & findings](#marvin-api-quirks--findings),
which may be useful even if you never run this server.

> **Provided as-is.** This project is not actively maintained and comes with
> no support. Issues are disabled on purpose. Fork freely — it's MIT.

## Tools (34)

| Group | Tools |
|---|---|
| Core | `test_connection`, `create_task`, `mark_done`, `unmark_done`, `update_task`, `set_priority`, `delete_task` |
| Reading | `get_today_items`, `get_due_items`, `get_children`, `get_categories` |
| Structure | `create_category_or_project` |
| Habits | `list_habits`, `get_habit`, `record_habit` |
| Time blocks | `get_today_time_blocks`, `create_time_block` (experimental) |
| Time tracking | `get_tracked_item`, `start_tracking`, `stop_tracking`, `get_time_tracks` |
| Kudos/rewards | `get_kudos`, `claim_reward_points`, `unclaim_reward_points`, `spend_reward_points`, `reset_reward_points` |
| Misc | `get_labels`, `get_goals`, `get_reminders`, `set_reminder`, `delete_reminder`, `create_event` (experimental), `get_account_info`, `get_rate_limit_status` |

Deliberately **not** included: Smart List / task-picking logic. Marvin's own
Spotlight does the picking; the server gives your assistant hands, not
opinions.

Every tool carries [MCP tool annotations](https://modelcontextprotocol.io/specification/2025-06-18/server/tools#tool-annotations)
(`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) so
capable clients can treat `delete_task` and `reset_reward_points` with the
respect they deserve.

## Getting your Marvin tokens

Both tokens live in Amazing Marvin under **Settings → API**
([app.amazingmarvin.com/pre?api](https://app.amazingmarvin.com/pre?api)):

- **API Token** (`MARVIN_API_TOKEN`, required) — limited access; enough for
  reading and creating tasks.
- **Full Access Token** (`MARVIN_FULL_ACCESS_TOKEN`, optional but
  recommended) — required by all `/doc*`-based tools: `update_task`,
  `set_priority`, `unmark_done`, `delete_task`, category creation, time
  blocks, `list_habits`, reminders, `reset_reward_points`.

Treat them like passwords; see [SECURITY.md](SECURITY.md).

## Install & run

Requires Python 3.12+.

```bash
git clone <this repo> && cd amazing-marvin-complete-mcp
python -m venv .venv && .venv/bin/pip install .
```

### Local (stdio) — Claude Desktop, Claude Code, any MCP client

The default transport is stdio, so the client starts the server itself:

```json
{
  "mcpServers": {
    "amazing-marvin": {
      "command": "/path/to/.venv/bin/marvin-mcp",
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
/path/to/.venv/bin/marvin-mcp`.)

### Remote (Streamable HTTP)

```bash
MCP_TRANSPORT=http PORT=8787 MCP_AUTH_TOKEN_FILE=/path/to/token \
MARVIN_API_TOKEN_FILE=/path/to/api-token .venv/bin/marvin-mcp
```

The MCP endpoint is `/mcp`. The built-in bearer check (`MCP_AUTH_TOKEN`)
protects every path but is an internal barrier, not a complete auth story:
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

Marvin's documented limits — 1 write/second, 1 read/3 seconds, 1440
calls/day — are enforced by a single process-global queue shared by all
tools and sessions, with margin (1.1 s / 3.1 s). The daily counter persists
across restarts (`STATE_DIR`) and rolls over at midnight in the configured
timezone. `get_rate_limit_status` shows today's usage.

## Marvin API quirks & findings

Everything below was verified against the live API on 2026-08-19. This is
the half of the repo you can use without running it.

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
- `/doc/create` does not echo back a server-generated `_id` — supply your
  own if you need to reference the document afterwards.
- Deletion via `/doc/delete` is permanent; Marvin's trash is client-side.

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

**Reminders**
- A *task* reminder in Marvin is two writes that only the app keeps in
  sync: reminder fields on the task document (`taskTime`, `reminderTime`,
  `reminderOffset`, `snooze`, `autoSnooze`) **and** a server-side entry via
  `/reminder/set`. Writing only one side (all the API lets you do
  comfortably) produces entries the app UI won't show on the task, or
  server-side orphans. Standalone reminders (type `M`) are the safe use of
  the API. (Risk first documented by
  [Recon2026/marvin-mcp](https://github.com/Recon2026/marvin-mcp);
  confirmed by the official wiki's own warning.)

**Time & planning**
- `/todayTimeBlocks` omits the block↔category link (issue #65); this
  server recovers the mapping from the `strategySettings.plannerSmartLists`
  profile document.
- Stopping time tracking via the API does not update the task's own
  `times`/`duration` fields; `/tracks` is the source of truth.
- Calendar events created via `/addEvent` sync onwards only while the
  Marvin app is running somewhere (client-side calendar sync).

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
