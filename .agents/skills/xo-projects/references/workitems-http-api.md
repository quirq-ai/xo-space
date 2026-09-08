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

- **from this Space** (`origin: "space"`) — authored here. You own every field.
- **from GitHub** (`origin: "github"`) — a mirror of a GitHub issue. **GitHub
  is authoritative** for `status`, `state_reason` and `body`; the file keeps
  only a `title`/`labels` snapshot taken at adoption. Writing those three
  returns `400 github_authoritative`.

**`origin` and `source.kind` are the same distinction in two vocabularies.**
`origin: "github"` is `source.kind: "github"`, and **`origin: "space"` is
`source.kind: "local"`** — the stored name was not changed, because it is a
synced on-disk format. Read `origin`; expect `source.kind` on disk.

**GitHub is read-only to this system.** Nothing here ever writes to GitHub —
not assignment, not closing, not anything. Issues are polled and adopted;
that is all. See §7.

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
  "id": "c6ddbed6-13df-4e2f-b3ca-3c4249196188",
  "title": "Rate-limit the poller",
  "body": "Budget is in points.",
  "labels": ["infra"],
  "status": "open",
  "state_reason": null,
  "source": { "kind": "local", "github": null },
  "origin": "space",
  "assignee": "ankitdwivedi",
  "assigned": true,
  "assignees": ["ankitdwivedi"],
  "github_assignees": [],
  "stale": false,
  "in_progress": false,
  "links": { "todo_ids": [], "session_ids": [] },
  "created_at": "2026-09-08T10:47:12Z",
  "updated_at": "2026-09-08T10:47:12Z",
  "created_by": "claude_code",
  "deleted_at": null,
  "deleted_by": null
}
```

### The five fields about who owes the work

| field | what it is |
|---|---|
| `assignee` | who **this Space** says owes it. `null` when nobody does. From `.xo/workitems.json`, for both kinds. |
| `assigned` | `assignee != null`, as a boolean. Nothing more. |
| `assignees` | `assignee` as a list, at most one long. The same fact. |
| `github_assignees` | who **GitHub** has on the issue. Information, never an assignment made here. `[]` for a `space` workitem and for one whose issue the mirror cannot speak for. |
| `origin` | `"github"` or `"space"` — where the workitem came from. |

`assigned` is about `assignee` only: an issue with `github_assignees:
["octocat"]` and no local assignee is `assigned: false`. Somebody is on it
upstream; nobody has been given it here.

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
{ "project_id": "demo", "repo": "dwivedi-ai/xo-cowork-api",
  "fetched_at": null, "error": null,
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
item readable when GitHub is unreachable. `status`, `state_reason` and `body`
are *not* stored: GitHub owns them and a stale `closed` is a false statement
about whether the work is done. Absent beats wrong. An `assignee` the workitem
already had is **kept** — it is yours, not GitHub's (§7).

### Reading an adopted item

The read path joins the file with the mirror. When the mirror has the issue you
get live state — note `github_assignees`, which is GitHub's own, and `assignee`,
which is nobody until someone here assigns it:

```json
200
{
  "id": "8ce59779-be62-44d6-a7d7-707dbf6fec43",
  "title": "Rate-limit the poller",
  "body": null,
  "labels": ["infra", "perf"],
  "status": "open",
  "state_reason": null,
  "source": {
    "kind": "github",
    "github": {
      "repo": "dwivedi-ai/xo-cowork-api",
      "number": 42,
      "node_id": "I_kwDOABCD1234",
      "url": "https://github.com/dwivedi-ai/xo-cowork-api/issues/42"
    }
  },
  "origin": "github",
  "assignee": null,
  "assigned": false,
  "assignees": [],
  "github_assignees": ["octocat"],
  "stale": false,
  "in_progress": false,
  "links": { "todo_ids": [], "session_ids": [] },
  "created_at": "2026-09-08T10:47:12Z",
  "updated_at": "2026-09-08T10:47:12Z",
  "created_by": "claude_code",
  "deleted_at": null,
  "deleted_by": null
}
```

When the mirror doesn't have it — deleted, transferred, or the poller has never
run — you still get **200, never 404**. This is the same workitem after the
mirror was removed, and after it had been assigned to `ada` here:

```json
200
{
  "id": "8ce59779-be62-44d6-a7d7-707dbf6fec43",
  "title": "Rate-limit the poller",
  "body": null,
  "labels": ["infra", "perf"],
  "status": null,
  "state_reason": null,
  "source": {
    "kind": "github",
    "github": {
      "repo": "dwivedi-ai/xo-cowork-api",
      "number": 42,
      "node_id": "I_kwDOABCD1234",
      "url": "https://github.com/dwivedi-ai/xo-cowork-api/issues/42"
    }
  },
  "origin": "github",
  "assignee": "ada",
  "assigned": true,
  "assignees": ["ada"],
  "github_assignees": [],
  "stale": true,
  "in_progress": false,
  "links": { "todo_ids": [], "session_ids": [] },
  "created_at": "2026-09-08T10:47:12Z",
  "updated_at": "2026-09-08T10:47:12Z",
  "created_by": "claude_code",
  "deleted_at": null,
  "deleted_by": null
}
```

**`stale: true` means "I could not confirm this against GitHub."** Treat
`status: null` as unknown, never as open, and `github_assignees: []` as "the
mirror has nothing to say", not "nobody is on it upstream". `assignee` is
unaffected by staleness — it never came from GitHub in the first place. A stale
item matches no `?status=` filter, so it appears in an unfiltered listing and
in an `?assignee=` one that matches its local assignee.

---

## 7. Assignment — always local, never GitHub

```json
PUT /api/xo-projects/demo/workitems/{id}/assignee
{ "assignee": "me" }            // or any name; null to un-assign
```

**One path for both kinds.** The assignee is written into
`<project>/.xo/workitems.json`. **No GitHub call is made** — not to write the
assignee, not to resolve `me`, not to check a rate budget. It works offline,
unauthenticated, with `gh` uninstalled.

An adopted workitem, assigned to a peer:

```json
200
{ "project_id": "demo",
  "workitem_id": "8ce59779-be62-44d6-a7d7-707dbf6fec43",
  "kind": "github",
  "assignee": "ada",
  "assignees": ["ada"],
  "pending": false }
```

`"me"`, on a workitem this Space authored:

```json
200
{ "project_id": "demo",
  "workitem_id": "c6ddbed6-13df-4e2f-b3ca-3c4249196188",
  "kind": "local",
  "assignee": "ankitdwivedi",
  "assignees": ["ankitdwivedi"],
  "pending": false }
```

- `kind` is the workitem's own kind (`source.kind`), **not** where the write
  went — the write always goes to the same place.
- `pending` is always `false`. Nothing is outstanding.
- `"me"` is this Space's own identity. A leading `@` is stripped, so
  `@octocat` and `octocat` are one name. `null` un-assigns.
- **Any name is accepted**, for any workitem. A name that cannot be stored is
  `400 invalid_assignee`, and nothing is written:

```json
400
{ "detail": { "code": "invalid_assignee",
  "message": "assignee must match [A-Za-z0-9_:\\-\\.] (1..200 chars)." } }
```

### What an assignment does *not* do

**It does not reach the person you assigned.** `.xo/` is snapshot
backup/restore, not continuous sync — a restore force-replaces the folder —
so an assignment is visible **only inside the Space that made it**. Assigning
a peer records who this Space thinks owes the work. It does not notify them
and it does not appear on their machine.

To hand work to someone through GitHub, **assign it on the issue**. It shows
up here as `github_assignees`, which this system reads once a minute and never
writes. That is the only assignment two Spaces can both see, and it is not
something this API can make for you.

*(This reverses an earlier design in which assigning an adopted item wrote a
GitHub assignee and a local item was self-assignable only. The
`local_assignee_only` and `assignee_not_assignable` errors are gone with it.)*

---

## 8. The rollup — what an agent polls

```
GET /api/workspace/workitems?assignee=me&status=open&limit=100
```
```json
200
{
  "workitems": [
    {
      "id": "a9649f0c-cb41-4d81-b19f-a29b092bc575",
      "title": "Rate-limit the poller",
      "body": "Budget is in points.",
      "labels": ["infra"],
      "status": "open",
      "state_reason": null,
      "source": { "kind": "local", "github": null },
      "origin": "space",
      "assignee": "ankitdwivedi",
      "assigned": true,
      "assignees": ["ankitdwivedi"],
      "github_assignees": [],
      "stale": false,
      "in_progress": false,
      "links": { "todo_ids": [], "session_ids": [] },
      "created_at": "2026-09-08T10:49:06Z",
      "updated_at": "2026-09-08T10:49:06Z",
      "created_by": "claude_code",
      "deleted_at": null,
      "deleted_by": null,
      "project_id": "demo",
      "pid": "22222222-3333-4444-8555-666666666666"
    },
    {
      "id": "80c2011b-c166-4f4d-849c-e2b3b950979a",
      "title": "Rate-limit the poller",
      "body": null,
      "labels": ["infra", "perf"],
      "status": "open",
      "state_reason": null,
      "source": {
        "kind": "github",
        "github": {
          "repo": "dwivedi-ai/xo-cowork-api",
          "number": 42,
          "node_id": "I_kwDOABCD1234",
          "url": "https://github.com/dwivedi-ai/xo-cowork-api/issues/42"
        }
      },
      "origin": "github",
      "assignee": null,
      "assigned": false,
      "assignees": [],
      "github_assignees": ["dwivedi-ai"],
      "stale": false,
      "in_progress": false,
      "links": { "todo_ids": [], "session_ids": [] },
      "created_at": "2026-09-08T10:49:06Z",
      "updated_at": "2026-09-08T10:49:06Z",
      "created_by": "claude_code",
      "deleted_at": null,
      "deleted_by": null,
      "project_id": "demo",
      "pid": "22222222-3333-4444-8555-666666666666"
    }
  ],
  "count": 2, "total": 2, "truncated": false,
  "assignee": "me",
  "identities": ["ankitdwivedi", "ankitdwivedi:collabse_07c611", "dwivedi-ai"],
  "assignee_unresolved": null,
  "projects": 1,
  "skipped": []
}
```

Both halves of `me` are doing work there. The first row matched because this
Space assigned it to `ankitdwivedi`; the second matched because **GitHub** has
`dwivedi-ai` on the issue — `assigned` is `false` on it, since nobody assigned
it *here*.

Each row is a full workitem — `origin`, `assigned`, `github_assignees` and all
— plus `project_id` and `pid`. The two surfaces answer identically.

**This is the one to poll.** It is a flat list across every project — no union
key, so no row can be lost to a key collision — filtered on the *projected*
view, so adopted items match on their live GitHub state rather than on what
happens to be stored.

- **`?assignee=` matches two things**: the workitem's own `assignee` (what
  someone assigned *here*) **and** its `github_assignees` (who GitHub has on
  the issue). Both, because both are true answers to "who owes this" and
  dropping either hides real work.
- **`me` is a set, not a name.** It resolves to this Space's local identities
  *and* its GitHub login (see `identities` above), because an assignment made
  here names a Coder identity while an issue names a GitHub login. Matching
  either alone would miss half your work.
- **No GitHub credential** → still 200, still filtered on the local half, with
  `assignee_unresolved: "no_github_credential"`. Never "everything".
- **`skipped`** names any project whose document could not be read, with its
  code. An empty list and a skipped list are different answers — check it
  before concluding you have no work.
- Ordering is newest-first by the record's local `updated_at`, so it is
  "recently changed here", not "recently active on GitHub".
- A **stale** adopted item matches no `?status=`, so a filtered rollup omits
  it. It still matches `?assignee=` on its local assignee — that fact does not
  come from GitHub and staleness does not put it in doubt.
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
| `github_authoritative` | 400 | you wrote `status`, `state_reason` or `body` on a `github` workitem |
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
