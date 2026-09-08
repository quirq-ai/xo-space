# Workitems HTTP API

The agent-facing reference for `<project>/.xo/workitems.json` and the GitHub
issues attached to it. Sibling of `todos-http-api.md`; the two surfaces
deliberately share a dialect, so if you can drive todos you can drive these.

Every example below is a **real captured response**, not an illustration.

```
http://${HOST:-localhost}:${PORT:-5002}
```

---

## 1. What a workitem is, and how it differs from a todo

| | workitem | todo |
|---|---|---|
| scale | a unit of work with an owner and a lifecycle | one step inside a session |
| audience | the team; visible in the UI and to peers | mostly the agent doing the work |
| lifetime | outlives the session | usually dies with it |
| may mirror | a GitHub issue | nothing |

An agent working a workitem **creates todos under it** and links them with
`links.todo_ids`. Don't model steps as workitems, and don't model deliverables
as todos.

**Two kinds of workitem**, and the difference governs almost everything below:

- **local** (`source.kind: "local"`) — authored here. You own every field.
- **adopted** (`source.kind: "github"`) — a mirror of a GitHub issue. **GitHub
  is authoritative** for title, state and assignee; the file keeps only a
  snapshot taken at adoption. Writing those fields returns `400
  github_authoritative`.

---

## 2. Endpoints

```
GET    /api/xo-projects/{project_id}/workitems        ?status=&assignee=&kind=&include_deleted=
POST   /api/xo-projects/{project_id}/workitems
GET    /api/xo-projects/{project_id}/workitems/{id}
PATCH  /api/xo-projects/{project_id}/workitems/{id}
DELETE /api/xo-projects/{project_id}/workitems/{id}   ?runtime=

POST   /api/xo-projects/{project_id}/workitems/{id}/claim      { session_id, runtime }
DELETE /api/xo-projects/{project_id}/workitems/{id}/claim
PUT    /api/xo-projects/{project_id}/workitems/{id}/assignee   { assignee }
DELETE /api/xo-projects/{project_id}/workitems/{id}/adoption

GET    /api/xo-projects/{project_id}/github/issues
POST   /api/xo-projects/{project_id}/github/issues/{number}/adopt

GET    /api/workspace/workitems                        ?assignee=&status=&limit=
```

`{project_id}` is the folder name under the projects root
(`GET /api/config/workspace`).

---

## 3. Create

```json
POST /api/xo-projects/demo/workitems
{
  "runtime": "claude_code",           // required, same vocabulary as todos
  "title": "Rate-limit the poller",
  "body": "Budget is in points.",     // optional
  "labels": ["infra"],                // optional
  "assignee": "ankitdwivedi",         // optional
  "status": "open"                    // optional; "open" | "closed"
}
```
```json
201
{
  "id": "a3af03b3-c3b2-40c9-83ec-1ad800aa79a9",
  "title": "Rate-limit the poller",
  "body": "Budget is in points.",
  "labels": ["infra"],
  "status": "open",
  "state_reason": null,
  "source": { "kind": "local", "github": null },
  "assignee": "ankitdwivedi",
  "assignees": ["ankitdwivedi"],
  "stale": false,
  "in_progress": false,
  "links": { "todo_ids": [], "session_ids": [] },
  "created_at": "2026-09-08T08:57:08Z",
  "updated_at": "2026-09-08T08:57:08Z",
  "created_by": "claude_code",
  "deleted_at": null,
  "deleted_by": null
}
```

`id` is a UUID4 and is unique across every project — the workspace rollup
(§8) is a cross-project query, so ids must be unique by construction, not by
convention.

**`source` is not accepted here.** Adoption is deliberate and goes through
`POST /github/issues/{n}/adopt` (§6). Generic CRUD cannot fabricate one.

---

## 4. Status is only ever `open` or `closed`

There is no `in_progress` or `blocked` status, and this is deliberate.

- `status` mirrors GitHub's two states exactly, so the two can never disagree.
- "cancelled" is `status: "closed"` with `state_reason: "not_planned"` —
  GitHub's own vocabulary, versus `"completed"`. Nothing invented.
- **`in_progress` is derived, never stored** (§5).

```json
PATCH /api/xo-projects/demo/workitems/{id}
{ "status": "closed", "state_reason": "completed" }
```

### The PATCH rule you must know

`body`, `state_reason` and `assignee` are nullable, so **omitting a key and
sending it as `null` mean different things**:

| you send | result |
|---|---|
| `{"title": "New"}` | title changes; assignee **untouched** |
| `{"assignee": null}` | assignee **cleared** |
| `{}` | nothing changes |

Without that distinction a workitem could never be un-assigned. The other
fields (`title`, `labels`, `status`, `todo_ids`, `session_ids`) are not
nullable, so absent and null collapse there.

---

## 5. Claims — how "in progress" works

A workitem is in progress **iff an agent is currently working it**. That is
computed live from session presence and is *never written down*, so a crashed
agent cannot leave a workitem stuck. There is no cleanup path because there is
nothing to clean up.

```json
POST /api/xo-projects/demo/workitems/{id}/claim
{ "session_id": "sess-1", "runtime": "claude_code" }
```
```json
200
{ "project_id": "demo", "workitem_id": "a3af03b3-…", "session_id": "sess-1",
  "runtime": "claude_code", "started_at": "2026-09-08T08:57:08Z",
  "in_progress": true }
```

- **Claim before you start, release when you stop.** `DELETE …/claim`
  releases; closing or tombstoning the workitem releases implicitly.
- If your session dies, `in_progress` becomes false on its own within a tick.
  The claim record stays on disk untouched — presence is what decides.
- A second claim is an **upsert**: last claimer wins, no conflict error.
- Claims are **machine-local**. Two Spaces working the same GitHub issue each
  see their own agent's progress and neither can overwrite the other's.
- `in_progress` cannot be filtered on (`?status=` filters stored values; this
  one is never stored).

---

## 6. GitHub issues — browsing and adopting

```
GET /api/xo-projects/demo/github/issues
```
```json
200
{ "project_id": "demo", "repo": null, "fetched_at": null, "error": null,
  "issues": [], "untracked": 0, "tracked": 0 }
```

That is the shape before anything has been polled. `issues` is the local
mirror, refreshed about once a minute for repos that are being looked at or
already hold adopted items. **This route never calls GitHub** — it reads the
mirror and marks the project as interesting so the poller starts covering it.

```
POST /api/xo-projects/demo/github/issues/42/adopt      -> 201 (or 200 if already adopted)
DELETE /api/xo-projects/demo/workitems/{id}/adoption   -> un-adopt, keep the workitem
```

Adoption is **explicit**. The mirror holds every open issue; `.xo/` holds only
what someone chose to track. Adopting is idempotent — adopting issue 42 twice
returns the same workitem, not two.

**What adoption copies, and what it does not.** `title` and `labels` are
snapshotted **once** and never refreshed — they are the fallback that keeps the
item readable when GitHub is unreachable. `status`, `state_reason`, `assignee`
and `body` are *not* stored: GitHub owns them and a stale `closed` or a stale
assignee is a false statement about who owes what. Absent beats wrong.

### Reading an adopted item

The read path joins the file with the mirror. When the mirror has the issue you
get live state. When it doesn't — deleted, transferred, or the poller has never
run — you still get **200, never 404**:

```json
{ "title": "snapshotted title",      // from the adoption snapshot
  "status": null,                    // unknown, NOT defaulted to "open"
  "assignee": null,
  "stale": true }
```

**`stale: true` means "I could not confirm this against GitHub."** Treat
`status: null` as unknown, never as open. A stale item matches no `?status=`
or `?assignee=` filter — it appears only in an unfiltered listing.

---

## 7. Assignment

```json
PUT /api/xo-projects/demo/workitems/{id}/assignee
{ "assignee": "me" }            // or a GitHub login
```

- **Adopted item** → written to **GitHub** as an issue assignee, and read back
  by the next poll. Nothing is written locally. Responds `pending: true`.
  GitHub silently ignores a login that cannot be assigned, so the response is
  checked against what was asked and a silent drop becomes
  `400 assignee_not_assignable` rather than a false success.
- **Local item** → assignable **only to yourself, permanently**:

```json
400
{ "detail": { "code": "local_assignee_only",
  "message": "A local workitem is assignable only to yourself, permanently.
    Assignment across Spaces is a GitHub assignee, and a local workitem never
    becomes a GitHub issue … To hand this work to someone else, create it as a
    GitHub issue and adopt that." } }
```

That is the design, not a limitation to work around: coordination lives in
GitHub, which is already a shared, concurrent, conflict-free store. A local
workitem never becomes a GitHub issue — if work needs to reach a peer, create
it as an issue first and adopt it.

---

## 8. The rollup — what an agent polls

```
GET /api/workspace/workitems?assignee=me&status=open&limit=100
```
```json
200
{ "workitems": [ /* each row is a Workitem plus project_id and pid */ ],
  "count": 0, "total": 0, "truncated": false,
  "assignee": "me",
  "identities": ["ankitdwivedi", "ankitdwivedi:collabse_07c611", "dwivedi-ai"],
  "assignee_unresolved": null,
  "projects": 1,
  "skipped": [] }
```

**This is the one to poll.** It is a flat list across every project — no union
key, so no row can be lost to a key collision — filtered on the *projected*
view, so adopted items match on their live GitHub state rather than on what
happens to be stored.

- **`me` is a set, not a name.** It resolves to this Space's local identities
  *and* its GitHub login (see `identities` above), because a local workitem is
  self-assigned to a Coder identity while an adopted one is assigned to a
  GitHub login. Matching either would miss half your work.
- **No GitHub credential** → still 200, still filtered on the local half, with
  `assignee_unresolved: "no_github_credential"`. Never "everything".
- **`skipped`** names any project whose document could not be read, with its
  code. An empty list and a skipped list are different answers — check it
  before concluding you have no work.
- Ordering is newest-first by the record's local `updated_at`, so it is
  "recently changed here", not "recently active on GitHub".
- No network call is made for the workitem data. `?assignee=me` costs one
  identity lookup per request.

---

## 9. Delete is a tombstone

```
DELETE /api/xo-projects/demo/workitems/{id}?runtime=claude_code
-> 200 { "project_id": "demo", "workitem_id": "…", "deleted": true }
```

The record stays with `deleted_at` / `deleted_by` set and its `status`
untouched; it stops being returned. Idempotent — `deleted: false` when it was
already gone, never 404. `?runtime=` is optional and records **who**;
pass it when you can.

`closed` and deleted are different axes. `closed` + `not_planned` means "we
decided not to do this" and stays visible. Delete means "this should not have
existed". Prefer closing.

---

## 10. Errors

| code | HTTP | meaning |
|---|---|---|
| `project_not_found` | 404 | no such project folder |
| `workitem_not_found` | 404 | unknown or tombstoned id |
| `invalid_runtime`, `invalid_assignee`, `invalid_value`, `invalid_status`, `invalid_state_reason`, `invalid_source`, `invalid_todo_id`, `invalid_session_id`, `invalid_node_id` | 400 | your request |
| `github_authoritative` | 400 | you wrote a field GitHub owns on an adopted item |
| `local_assignee_only` | 400 | see §7 |
| `assignee_not_assignable` | 400 | GitHub silently dropped the login |
| `corrupt_document`, `unsupported_schema` | **409** | the file on disk is unreadable or from a newer version — **repair it and retry the identical request**; not your fault and not a retryable server error |
| `not_authenticated` | 503 | this Space has no usable GitHub credential |
| `scope_unavailable` | 500 | read/write failed |

409 is deliberate: nothing about the request is wrong, so 400 misleads; and
retrying cannot help until a human repairs the document, so 500/503 would
invite a pointless loop.

---

## 11. The loop, end to end

```
1. GET /api/workspace/workitems?assignee=me&status=open   -> what is mine
2. POST …/workitems/{id}/claim  { session_id, runtime }   -> mark it live
3. POST …/todos  (one per concrete step)                  -> show the work
4. PATCH …/workitems/{id}  { "status": "closed",
                             "state_reason": "completed" }
   (this releases the claim implicitly)
```

Check `skipped` in step 1. Claim before you start, not after. And if the
rollup is empty but `assignee_unresolved` is set, you are looking at half the
picture — say so rather than reporting no work.
