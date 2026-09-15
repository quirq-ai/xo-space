# Todo HTTP API

This file is the endpoint reference for recording todos. **It is the only write path, for every runtime.** These endpoints are the sole writer of `<project>/.xo/todos.json`, and the sole source of the todo events that reach the project timeline and the per-session task counters — so the same sequence of calls produces the same files whichever backend is active.

**If your runtime has a native todo tool, it is not a substitute.** There was once a watcher sink that tailed one runtime's session log and mirrored its native todos into `todos.json`. It is gone: it worked for exactly one backend out of five, minted a second colliding id space, and made the file two-writer. Removing it did not stop a native `TaskCreate` from firing — it stopped anyone **seeing** the result. A todo that lives only in a native tool is invisible to the UI, to `GET …/todos`, to `timeline.jsonl` and to the next agent, and nothing errors or warns to tell you. Use the native tool for your own thinking if you like; the step is not recorded until one of these calls returns.

The todo lifecycle (list at boot, one `in_progress` at a time, `cancelled` vs `blocked`, finished work → `PROGRESS.md`) lives in SKILL.md Part 3. This file covers only the HTTP schemas.

## Contents

- Endpoints
- Create
- Update (the common case: status transitions)
- List
- Delete
- Todo fields

---

## Endpoints

Same base URL as the rest of cowork-api (`http://${HOST:-localhost}:${PORT:-5002}`). This API is the file's only writer; the advisory lock it takes is against *itself* — two concurrent requests are still two read-modify-writes — so interleaved calls from several agents don't tear each other's edits.

```
GET    /api/xo-projects/{project_id}/todos
POST   /api/xo-projects/{project_id}/todos
GET    /api/xo-projects/{project_id}/todos/{todo_id}
PATCH  /api/xo-projects/{project_id}/todos/{todo_id}
DELETE /api/xo-projects/{project_id}/todos/{todo_id}
```

`{project_id}` is the folder name under `<projects_root>` (from `GET /api/config/workspace`).

## Create

```json
POST /api/xo-projects/{project_id}/todos
{
  "runtime": "<your-runtime-identifier>",   // required; see "Choosing `runtime`" below
  "content": "Implement /metrics endpoint",
  "description": "...",                     // optional, ≤4000 chars
  "active_form": "Implementing the /metrics endpoint",  // optional, shown while in_progress
  "session_id": "<your-session-id>",        // optional; defaults to "_project"
  "status": "pending"                       // optional; defaults to "pending"
}
→ 201 { "id": "a1b2c3d4", "content": "...", "status": "pending", "description": "...",
        "active_form": "...", "created_at": "2026-05-14T...Z", "updated_at": "2026-05-14T...Z",
        "deleted_at": null, "deleted_by": null }
→ 400 invalid_runtime | invalid_session_id | invalid_value | invalid_status
```

**Choosing `runtime`.** It's a stable string that identifies the agent or runtime writing the todo, so the UI and the next agent can tell whose todo is whose. The agent picks the value. Both `runtime` and `session_id` must match the regex `[A-Za-z0-9_:\-\.]{1,200}` — alphanumeric plus `_ : - .` as separators (so compound identifiers like `<runtime>:<profile>:<instance>` are fine). The codebase already uses these values, which are safe defaults if your agent has no preference:

| Runtime | Conventional value | Native todo tool? |
|---|---|---|
| Claude Code | `claude_code` | yes — still call this API |
| OpenClaw | `openclaw` | no |
| Hermes | `hermes` | no |
| Codex | `codex` | no |
| Antigravity | `antigravity` | no |
| Cursor | `cursor` | no |
| Aider | `aider` | no |

If your agent is something else, pick a short stable identifier (your agent's name, or `<agent-name>:<instance>` if you run multiple instances). Whatever you pick, use the **same** value for every todo your agent writes in the project — switching mid-session makes the UI show two separate sources for one logical agent.

`session_id` is your runtime's session/conversation id when you have one; omit the field (or send `null`) to use the `"_project"` pseudo-session bucket. `content` is required and ≤1000 chars. `id` is 8 hex chars, server-generated.

## Update (the common case: status transitions)

```json
PATCH /api/xo-projects/{project_id}/todos/{todo_id}
{ "status": "in_progress" }      // any field may be sent; only those provided are touched
→ 200 { "id": "a1b2c3d4", "content": "...", "status": "in_progress", ... }
→ 404 todo_not_found
```

Valid statuses: `pending | in_progress | completed | cancelled | blocked`. Only one `in_progress` per agent at a time — the UI assumes that discipline. A `PATCH` that changes nothing (re-sending the status a todo already has) is a no-op: no write, no timeline line, no counter move.

## List

```json
GET /api/xo-projects/{project_id}/todos
→ {
    "project_id": "<id>",
    "updated_at": "2026-05-14T...Z",
    "sessions": {
      "<your-session-id>": {
        "runtime": "<your-runtime-identifier>",
        "source_file": null,
        "session_started_at": "2026-05-14T...Z",
        "todos": [ { "id": "a1b2c3d4", "content": "...", "status": "in_progress" }, ... ]
      },
      "_project": { ... }
    }
  }
```

List at boot to inherit open work from previous sessions; list again whenever you need a cross-session view.

```
GET /api/xo-projects/{project_id}/todos?include_deleted=true
```
adds the tombstoned records back in, each with `deleted_at` / `deleted_by` set. Default is `false`: the plain list is the *work*, the switch gets you the *history*. `source_file` is always `null` on the wire — the server never echoes an absolute path back.

## Todo fields

| Field | Written by | Notes |
|---|---|---|
| `id` | server | 8 hex chars, server-generated; unique within the project |
| `content` | you | required, ≤1000 chars |
| `status` | you | one of the five values above; defaults to `pending` |
| `description` | you | optional, ≤4000 chars |
| `active_form` | you | optional, ≤1000 chars; what the UI shows while the todo is `in_progress` |
| `created_at` | server | ISO-8601 UTC, stamped on create |
| `updated_at` | server | ISO-8601 UTC, restamped only on a call that actually changes something |
| `deleted_at` | server | `null` until `DELETE`; then the tombstone timestamp |
| `deleted_by` | server | the `runtime` passed to `DELETE`, or `null` when the caller passed none |

## Delete

```json
DELETE /api/xo-projects/{project_id}/todos/{todo_id}?runtime=<your-runtime-identifier>
→ 200 { "project_id": "<id>", "todo_id": "<id>", "deleted": true }
→ 400 invalid_runtime
```

`runtime` is **optional** and records who tombstoned the todo, in `deleted_by`.
Pass the same identifier you use on create. Omit it and the delete still
succeeds, unattributed — but pass it when you can: a tombstone whose author is
unknown cannot be told apart from one nobody remembers making. Same charset as
create (`[A-Za-z0-9_:\-\.]`, 1..200 chars); a value outside it is a `400` and
writes nothing.

**Delete is a tombstone, not an erasure.** The record stays in the file with `deleted_at` / `deleted_by` set and its `status` untouched; it simply stops being returned. That is what makes a deleted todo unable to come back, and it keeps "everything ever closed" answerable.

Idempotent — returns `deleted: false` (not 404) when the todo wasn't present, or was already tombstoned. After a delete, `GET …/todos/{id}` is `404` and the todo is gone from the list; `GET …/todos?include_deleted=true` returns it with its tombstone fields. A tombstoned todo is not editable — `PATCH` answers `404 todo_not_found`.

`status` and deletion are different axes, so pick deliberately: `cancelled` means "we decided not to do this" — a real outcome that stays visible in the list. Delete means "this should not have existed" (a typo, a duplicate). Prefer `cancelled` whenever the todo was real.