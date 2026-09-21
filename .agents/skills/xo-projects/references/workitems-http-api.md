# Workitem HTTP API

A workitem is a unit of work with an owner and a lifecycle. A todo is one step
inside a session. They are linked (`links.todo_ids`), not interchangeable.

This file is the endpoint reference. The store is `<project>/.xo/workitems.json`
(committed with the project). Claims that make `in_progress` true live in
`~/.quirq/projects/<pid>/workitems/claims.json` (machine-local). Routes live in
`routers/cowork_agent/bff/visualizer.py` (project tier) and
`routers/cowork_agent/bff/workspace_visualizer.py` (the rollup).

**GitHub is read-only.** Adoption copies an issue into `.xo/workitems.json`.
Assignment writes a local annotation. Closing, retitling, or commenting on the
issue happens on GitHub, not through these routes.

## Contents

- Endpoints
- Create (local)
- List, get, update, delete
- Claims (`in_progress`)
- Assignment
- GitHub issue mirror and adoption
- Workspace rollup
- Fields and errors

---

## Endpoints

Same base URL as the rest of cowork-api (`http://${HOST:-localhost}:${PORT:-5002}`).
`{project_id}` is the folder name under the projects root
(`GET /api/config/workspace`). Bodies are strict (`ForbidExtra`). Errors come back
as `{"detail": {"code", "message"}}`.

```
GET    /api/xo-projects/{project_id}/workitems
POST   /api/xo-projects/{project_id}/workitems
GET    /api/xo-projects/{project_id}/workitems/{workitem_id}
PATCH  /api/xo-projects/{project_id}/workitems/{workitem_id}
DELETE /api/xo-projects/{project_id}/workitems/{workitem_id}
POST   /api/xo-projects/{project_id}/workitems/{workitem_id}/claim
DELETE /api/xo-projects/{project_id}/workitems/{workitem_id}/claim
PUT    /api/xo-projects/{project_id}/workitems/{workitem_id}/assignee
DELETE /api/xo-projects/{project_id}/workitems/{workitem_id}/adoption

GET    /api/xo-projects/{project_id}/github/issues
POST   /api/xo-projects/{project_id}/github/issues/{issue_number}/adopt

GET    /api/workspace/workitems
```

## Create (local)

```json
POST /api/xo-projects/{project_id}/workitems
{
  "runtime": "claude_code",
  "title": "Ship the metrics endpoint",
  "body": "Optional, up to 16000 chars",
  "labels": ["backend"],
  "status": "open",
  "state_reason": null,
  "assignee": "alice",
  "todo_ids": ["a1b2c3d4"],
  "session_ids": ["sess-1"]
}
→ 201 { "id": "<uuid4>", "title": "...", "status": "open", "source": {"kind": "local"},
        "origin": "space", "assignee": "...", "assigned": true, "in_progress": false, ... }
→ 400 invalid_runtime | invalid_value | invalid_status | invalid_state_reason | invalid_assignee | invalid_todo_id | invalid_session_id
```

`runtime` is required (same charset as todos: `[A-Za-z0-9_:\-.]{1,200}`). `title` is
required, 1 to 1000 chars. `id` is a server-minted UUID4, not the 8-hex todo id.
`status` is `open` or `closed` (default `open`). There is no stored `in_progress`:
that flag is derived from a live claim. `state_reason` is `null` or one of
`completed`, `not_planned`, `reopened`. At most 50 labels (each 1 to 100 printable
chars) and 500 todo/session links.

Create always makes a **local** workitem. To track a GitHub issue, adopt it
(below). Do not POST a `source` block.

## List, get, update, delete

```
GET /api/xo-projects/{project_id}/workitems
    ?status=open|closed
    &assignee=<login>
    &kind=local|github
    &include_deleted=true
```

Oldest first. Default list hides tombstones. `kind=github` is adopted items.

```json
PATCH /api/xo-projects/{project_id}/workitems/{workitem_id}
{ "title": "...", "status": "closed", "assignee": null }
→ 200 the workitem
→ 400 github_authoritative   // adopted item: status, state_reason, body are GitHub's
→ 404 workitem_not_found
```

Only supplied fields change. `body`, `state_reason`, and `assignee` treat JSON
`null` as a clear; omit the key to leave them. Closing a workitem also releases
its claim.

For an **adopted** workitem, `status`, `state_reason`, and `body` are not stored
here (`GITHUB_OWNED_FIELDS`). Change them on the issue. Title, labels, assignee,
and links remain local.

```
DELETE /api/xo-projects/{project_id}/workitems/{workitem_id}?runtime=<runtime>
→ 200 { "project_id", "workitem_id", "deleted": true|false }
```

A tombstone, like todos: `deleted_at` / `deleted_by` are set, the record stays,
and it cannot come back. Idempotent (`deleted: false` when already gone). Prefer
`status: "closed"` for finished work; delete for a duplicate or a create that
should not have existed. A tombstone also releases any claim. `GET .../{id}` after
delete is 404; `?include_deleted=true` returns it.

A corrupt or newer-schema `workitems.json` is 409 (`corrupt_document` /
`unsupported_schema`), never overwritten.

## Claims (`in_progress`)

`in_progress` is **never stored** on the workitem. An agent working the item
records a claim. The flag is true while that session is still open, or for a
grace window of at least 60 s (and at least two watcher ticks) after `started_at`
if the session list has not caught up.

```json
POST /api/xo-projects/{project_id}/workitems/{workitem_id}/claim
{ "session_id": "<your-session-id>", "runtime": "<your-runtime-identifier>" }
→ 200 { "project_id", "workitem_id", "session_id", "runtime", "started_at", "in_progress": true }

DELETE /api/xo-projects/{project_id}/workitems/{workitem_id}/claim
→ 200 { "project_id", "workitem_id", "released": true|false }
```

Re-claiming replaces the previous claim (a different session, or the same one
restarted). Release is idempotent. Closing or deleting the workitem releases
quietly. Claims are not in `.xo/`; they do not travel with git or backups.

## Assignment

```json
PUT /api/xo-projects/{project_id}/workitems/{workitem_id}/assignee
{ "assignee": "me" }
→ 200 { "project_id", "workitem_id", "kind": "local"|"github", "assignee": "...",
        "assignees": ["..."], "pending": false }
```

Always a local write. `{ "assignee": null }` clears it. `me`, `@me`, and `self`
resolve to this Space's own identity (`coder_identity`, then GitHub login when
available). A leading `@` is stripped. This does **not** assign the GitHub issue.

## GitHub issue mirror and adoption

The poller is the mirror's only writer (`~/.quirq/projects/<pid>/github/issues.json`).
These routes read it.

```
GET /api/xo-projects/{project_id}/github/issues?refresh=true
→ { "project_id", "repo", "fetched_at", "error", "issues": [...],
    "untracked": N, "tracked": N,
    "state": "ok"|"empty"|"never_polled"|"issues_disabled"|"no_remote"|"error" }
```

Each issue row includes `adopted`, `workitem_id` (when tracked), and
`in_progress`. `untracked` counts open issues not yet adopted. `refresh=true`
forces a fetch even when the mirror is warm. A first request may also do a
cold fetch (one page, 8 s budget). If the machine-wide GitHub budget is paused
or spent, interactive GitHub calls answer 503 `github_rate_limited`.

```json
POST /api/xo-projects/{project_id}/github/issues/{issue_number}/adopt
{ "runtime": "claude_code", "workitem_id": null }
→ 201 the new workitem
→ 200 the existing workitem   // already tracked, or this id now mirrors the issue
→ 400 not_a_github_project
```

`workitem_id` is optional: omit it to mint a new workitem, or pass an existing
local id to attach the issue to it. Adoption is explicit: the mirror holds every
issue, `.xo/workitems.json` only what someone chose to track.

```
DELETE /api/xo-projects/{project_id}/workitems/{workitem_id}/adoption
→ 200 the workitem, now local (kept; not deleted)
```

Unadopt keeps the workitem and snapshots the last projected `status` /
`state_reason` so it stays a useful local record.

A project with no `github.com` remote cannot adopt (`400 not_a_github_project`).

## Workspace rollup

```
GET /api/workspace/workitems?assignee=me&status=open&limit=100
→ {
    "workitems": [ { ...Workitem, "project_id": "my-app", "pid": "<uuid>" }, ... ],
    "count": 12, "total": 12, "truncated": false,
    "assignee": "me", "identities": ["local", "alice"],
    "assignee_unresolved": null,
    "projects": 4,
    "skipped": []
  }
```

The endpoint an agent polls for "what is assigned to me" across every project on
this machine. `assignee` is `me` / `@login` / a login (case-insensitive). `me`
matches local identities plus GitHub login; if GitHub cannot be resolved,
`assignee_unresolved` is `no_github_credential` and local identities still
match. `limit` is 1 to 500 (default 100). Newest `updated_at` first.
`truncated` is true when `total > count`. A project whose `workitems.json` is
unreadable is listed in `skipped` (with a path-free message) and does not fail
the rest of the workspace.

## Fields

| Field | Written by | Notes |
|---|---|---|
| `id` | server | UUID4 |
| `title` | you / GitHub snapshot on adopt | <=1000 chars |
| `body` | you, local only | omitted for adopted items |
| `status` | you, local only | `open` or `closed`; GitHub's for adopted items |
| `state_reason` | you, local only | `completed` / `not_planned` / `reopened` / null |
| `source.kind` | server | `local` or `github` |
| `origin` | server | `"space"` iff local, `"github"` iff adopted |
| `assignee` | you | local annotation for both kinds |
| `github_assignees` | mirror | GitHub logins; not writable here |
| `stale` | server | adopted, but the issue is missing from the mirror |
| `in_progress` | derived | live claim; never stored |
| `links.todo_ids` | you | join to `.xo/todos.json` |
| `created_by` | server | the `runtime` on create / adopt |
| `deleted_at` / `deleted_by` | server | tombstone; null until DELETE |

## Pitfalls

- Native todo tools do not create workitems. Record session steps with the todo
  API; create or claim a workitem when the unit of work has an owner.
- Do not PATCH `status` on an adopted item expecting GitHub to change. It 400s
  with `github_authoritative`.
- `in_progress` will drop after the grace window if the session is gone, even
  if nobody called release. Re-claim if you are still working it.
- The rollup filters on the **projected** view (GitHub status for adopted
  items). A locally assigned, GitHub-closed issue is `closed` on the rollup.
