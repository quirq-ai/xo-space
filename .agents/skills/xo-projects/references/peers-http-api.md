# Peers HTTP API

The agent-facing reference for `<project>/.xo/peers.json` — the roster of
humans a project is shared with. Sibling of `todos-http-api.md` and
`workitems-http-api.md`; all three share a dialect, so if you can drive
either of those you can drive this one.

Every example below is a **real captured response**, not an illustration.

```
http://${HOST:-localhost}:${PORT:-5002}
```

---

## 1. What the roster is, and what it is not

`peers.json` answers exactly one question: **who is on this project now.**
The schema says so in as many words — *"Roster of human collaborators. Empty
list = solo project."*

| it is | it is not |
|---|---|
| the list of people this project is shared with | an access-control list — nothing here grants or denies anything |
| the source of `user_id` values you can assign work to | a record of who *used* to be on the project |
| a synced document that travels with the project | a sync log; whether anything was actually exchanged is the sync API's business |

**An empty roster is a real answer**, and the common one: a solo project has
`"peers": []` and that is correct, not missing data.

**`role` is descriptive.** `owner`, `collaborator`, `viewer` — nothing in this
system reads the value to make a permission decision. Do not treat a `viewer`
as blocked from anything, and do not invent a rule that says otherwise.

---

## 2. Endpoints

```
GET    /api/xo-projects/{project_id}/peers            ?role=
POST   /api/xo-projects/{project_id}/peers            { user_id, role, label?, endpoint? }
GET    /api/xo-projects/{project_id}/peers/{user_id}
PATCH  /api/xo-projects/{project_id}/peers/{user_id}  { role?, label?, endpoint? }
DELETE /api/xo-projects/{project_id}/peers/{user_id}
```

`{project_id}` is the folder name under the projects root
(`GET /api/config/workspace`). `{user_id}` is the peer's identity — there is
no separate id.

**There is no `runtime` field on this surface.** Todos and workitems require
one because their records have somewhere to record it; a peer record does not,
so sending one is a `422`.

---

## 3. The roster of a solo project

```
GET /api/xo-projects/demo/peers
```
```json
200
{
  "project_id": "demo",
  "updated_at": null,
  "peers": []
}
```

That is what a project with no `peers.json` yet, and a project with the
shipped stub, both look like. `updated_at` is the document's own stamp — when
the roster last **changed**, not when it was last read. It stays `null` until
somebody is added.

---

## 4. Add a peer

```json
POST /api/xo-projects/demo/peers
{
  "user_id": "ankitdwivedi",       // required — the identity
  "role": "owner",                 // required — owner | collaborator | viewer
  "label": "Ankit Dwivedi"         // optional — display name
}
```
```json
201
{
  "user_id": "ankitdwivedi",
  "role": "owner",
  "added_at": "2026-09-08T11:13:07Z",
  "endpoint": null,
  "label": "Ankit Dwivedi"
}
```

With a sync endpoint:

```json
POST /api/xo-projects/demo/peers
{
  "user_id": "ada",
  "role": "collaborator",
  "label": "Ada Lovelace",
  "endpoint": "https://space.example/xo/sync"
}
```
```json
201
{
  "user_id": "ada",
  "role": "collaborator",
  "added_at": "2026-09-08T11:13:08Z",
  "endpoint": "https://space.example/xo/sync",
  "label": "Ada Lovelace"
}
```

**`added_at` is server-set.** It records when *this Space* learned of the peer.
Sending it is a `422`, not an override.

### `user_id` is the same identity a workitem assignee is

This is the point of keeping a roster. A `user_id` that this API accepts is
always a `user_id` the workitems API accepts as an `assignee` — the two are
validated against one charset, `[A-Za-z0-9_:\-\.]`, 1–200 characters.

```json
POST /api/xo-projects/demo/workitems
{ "runtime": "claude_code", "title": "Rate-limit the poller", "assignee": "ada" }
```
```json
201
{
  "id": "021f8dcc-e6ef-4f04-9462-de83a81d83ee",
  "title": "Rate-limit the poller",
  "body": null,
  "labels": [],
  "status": "open",
  "state_reason": null,
  "source": { "kind": "local", "github": null },
  "origin": "space",
  "assignee": "ada",
  "assigned": true,
  "assignees": ["ada"],
  "github_assignees": [],
  "stale": false,
  "in_progress": false,
  "links": { "todo_ids": [], "session_ids": [] },
  "created_at": "2026-09-08T11:13:10Z",
  "updated_at": "2026-09-08T11:13:10Z",
  "created_by": "claude_code",
  "deleted_at": null,
  "deleted_by": null
}
```

(That is the workitems surface, unchanged — see `workitems-http-api.md`. It is
here only to show the identity crossing intact.)

The reverse is **not** enforced: assigning somebody who is not on the roster
works fine. The roster tells you who the people are; it does not gate who may
be given work.

---

## 5. Reading the roster

```
GET /api/xo-projects/demo/peers
```
```json
200
{
  "project_id": "demo",
  "updated_at": "2026-09-08T11:13:09Z",
  "peers": [
    {
      "user_id": "ankitdwivedi",
      "role": "owner",
      "added_at": "2026-09-08T11:13:07Z",
      "endpoint": null,
      "label": "Ankit Dwivedi"
    },
    {
      "user_id": "ada",
      "role": "collaborator",
      "added_at": "2026-09-08T11:13:08Z",
      "endpoint": "https://space.example/xo/sync",
      "label": "Ada Lovelace"
    },
    {
      "user_id": "grace",
      "role": "viewer",
      "added_at": "2026-09-08T11:13:09Z",
      "endpoint": null,
      "label": null
    }
  ]
}
```

Oldest first — the order people were added, which is `added_at` order.

`?role=` narrows to one role:

```
GET /api/xo-projects/demo/peers?role=viewer
```
```json
200
{
  "project_id": "demo",
  "updated_at": "2026-09-08T11:13:09Z",
  "peers": [
    {
      "user_id": "grace",
      "role": "viewer",
      "added_at": "2026-09-08T11:13:09Z",
      "endpoint": null,
      "label": null
    }
  ]
}
```

A typo in the filter is a `400 invalid_role`, never a confident empty list.
`updated_at` is the whole document's stamp and does not change with the filter.

One peer:

```
GET /api/xo-projects/demo/peers/ada
```
```json
200
{
  "user_id": "ada",
  "role": "collaborator",
  "added_at": "2026-09-08T11:13:08Z",
  "endpoint": "https://space.example/xo/sync",
  "label": "Ada Lovelace"
}
```

---

## 6. POST is a create, never an upsert

Adding somebody who is already listed is a **409**, and nothing is written:

```json
POST /api/xo-projects/demo/peers
{ "user_id": "ada", "role": "owner" }
```
```json
409
{
  "detail": {
    "code": "peer_exists",
    "message": "ada is already on this project's roster. The roster is a set keyed by user_id, so a create cannot also be an edit: PATCH the peer to change their role, label or endpoint."
  }
}
```

The roster is a set keyed by identity, so a POST of an existing `user_id` has
two readings and they disagree about something that matters:

- As an upsert it would rewrite `role` — quietly turning a `viewer` into an
  `owner` because somebody re-ran a create. A privilege change must not be the
  side effect of an insert the caller believed was new.
- It would also have to decide what `added_at` means. Resetting it loses when
  the person actually joined; keeping it makes the `201 Created` a lie about a
  record that predates the request.

So it conflicts, and the message names the call that does what you wanted.
**If you need "ensure listed": GET first, or POST and treat `409 peer_exists`
as success.** That is safe precisely because the 409 changed nothing.

---

## 7. PATCH — what can change and what cannot

```json
PATCH /api/xo-projects/demo/peers/grace
{ "role": "collaborator" }
```
```json
200
{
  "user_id": "grace",
  "role": "collaborator",
  "added_at": "2026-09-08T11:13:09Z",
  "endpoint": null,
  "label": null
}
```

`label` and `endpoint` are nullable, so **omitting a key and sending it as
`null` mean different things**:

| you send | result |
|---|---|
| `{"role": "viewer"}` | role changes; label and endpoint **untouched** |
| `{"label": null}` | label **cleared** |
| `{}` | nothing changes, and nothing is written |

Clearing a label, with the endpoint left alone:

```json
PATCH /api/xo-projects/demo/peers/ada
{ "label": null }
```
```json
200
{
  "user_id": "ada",
  "role": "collaborator",
  "added_at": "2026-09-08T11:13:08Z",
  "endpoint": "https://space.example/xo/sync",
  "label": null
}
```

An empty PATCH is genuinely idempotent — it returns the record and does not
advance the document's `updated_at`:

```json
PATCH /api/xo-projects/demo/peers/ada
{}
```
```json
200
{
  "user_id": "ada",
  "role": "collaborator",
  "added_at": "2026-09-08T11:13:08Z",
  "endpoint": "https://space.example/xo/sync",
  "label": null
}
```

**`user_id` and `added_at` are not patchable** (`422` if you send them).
`user_id` is the identity — re-keying a record would hand whatever it meant to
a different person — so a rename is a DELETE plus a POST, deliberately two
calls. `added_at` is this Space's own observation, not yours to revise.

An unknown peer is a 404:

```json
PATCH /api/xo-projects/demo/peers/nobody
{ "role": "owner" }
```
```json
404
{ "detail": { "code": "peer_not_found", "message": "Peer not found." } }
```

---

## 8. DELETE is a hard delete — no tombstone

```
DELETE /api/xo-projects/demo/peers/grace
```
```json
200
{ "project_id": "demo", "user_id": "grace", "deleted": true }
```

Idempotent, exactly like the other two DELETEs — a second call is `deleted:
false`, never a 404:

```json
200
{ "project_id": "demo", "user_id": "grace", "deleted": false }
```

**This is where peers diverge from todos and workitems, on purpose.** Those
tombstone; this removes. Two reasons:

- `peers.schema.json` is `additionalProperties: false` and declares no
  `deleted_at` / `deleted_by`. There is nowhere to tombstone into without
  changing a document that already ships in the project template.
- `peers.json` is in the **synced** tier. A removed collaborator kept as a
  tombstone would travel to every Space this project ever reaches, carrying
  "this person used to have access" forever. That is a privacy problem, not a
  history feature.

The cost, stated plainly: after a delete, the fact that they were ever listed
is **gone**. There is no `?include_deleted=` here because there is nothing to
read back. Adding them again is a plain create with a fresh `added_at` — which
is the truth: that is when they joined this time.

After the delete above:

```json
200
{
  "project_id": "demo",
  "updated_at": "2026-09-08T11:13:10Z",
  "peers": [
    {
      "user_id": "ankitdwivedi",
      "role": "owner",
      "added_at": "2026-09-08T11:13:07Z",
      "endpoint": null,
      "label": "Ankit Dwivedi"
    },
    {
      "user_id": "ada",
      "role": "collaborator",
      "added_at": "2026-09-08T11:13:08Z",
      "endpoint": "https://space.example/xo/sync",
      "label": null
    }
  ]
}
```

---

## 9. What lands on disk

`<project>/.xo/peers.json`, after the calls above — the API is this file's only
writer, and no other process touches it:

```json
{
  "$schema": "xo/peers.schema.json",
  "schema": 1,
  "updated_at": "2026-09-08T11:13:10Z",
  "peers": [
    {
      "user_id": "ankitdwivedi",
      "role": "owner",
      "added_at": "2026-09-08T11:13:07Z",
      "endpoint": null,
      "label": "Ankit Dwivedi"
    },
    {
      "user_id": "ada",
      "role": "collaborator",
      "added_at": "2026-09-08T11:13:08Z",
      "endpoint": "https://space.example/xo/sync",
      "label": null
    }
  ]
}
```

Read it through the API rather than off disk. Write it **only** through the
API — an agent editing `.xo/` by hand is how a roster ends up with the same
person listed twice, which the store then refuses to read at all.

---

## 10. Errors

| code | HTTP | meaning |
|---|---|---|
| `project_not_found` | 404 | no such project folder |
| `peer_not_found` | 404 | that `user_id` is not on the roster |
| `invalid_user_id`, `invalid_role`, `invalid_label`, `invalid_endpoint`, `invalid_value` | 400 | your request |
| `peer_exists` | **409** | that `user_id` is already listed — PATCH instead; nothing was written |
| `corrupt_document`, `unsupported_schema` | **409** | the file on disk is unreadable or from a newer version — **repair it and retry the identical request** |
| `scope_unavailable` | 500 | read/write failed |

```json
400
{ "detail": { "code": "invalid_user_id",
  "message": "user_id must match [A-Za-z0-9_:\\-\\.] (1..200 chars) — the same charset a workitem assignee must satisfy, so a listed peer can always be assigned work." } }
```
```json
400
{ "detail": { "code": "invalid_role",
  "message": "role must be one of ['collaborator', 'owner', 'viewer']." } }
```
```json
400
{ "detail": { "code": "invalid_endpoint",
  "message": "endpoint must be null or an http(s) URL of at most 2000 characters with no whitespace." } }
```
```json
404
{ "detail": { "code": "project_not_found", "message": "Project not found." } }
```

### A roster that cannot be read is a 409, never an empty list

This is the rule that matters most on this surface. If `peers.json` is
truncated, half-merged, or written by a newer Space, **every** endpoint refuses
and the bytes on disk are left exactly as they were:

```json
409
{
  "detail": {
    "code": "corrupt_document",
    "message": "peers.json is not a readable document of its kind. It is refused rather than read as empty or overwritten, so nothing it holds is discarded. Repair or move the file."
  }
}
```

A roster silently read as empty is a project that has forgotten everyone it is
shared with, and nothing about it looks wrong — so the API would rather stop.
**Do not "work around" a 409 by re-creating the peers you remember.** Tell the
user the document needs repairing, and say which project.

409 is deliberate for both codes: nothing about the request is wrong, so 400
misleads; and retrying cannot help until a human repairs the document, so
500/503 would invite a pointless loop.

---

## 11. The loop, end to end

```
1. GET  …/peers                         -> who is on this project (empty = solo)
2. POST …/peers { user_id, role }       -> add someone; 409 means already listed
3. PATCH …/peers/{user_id} { role }     -> change a role, label or endpoint
4. DELETE …/peers/{user_id}             -> remove them; the record is gone
```

Use step 1 before telling a user who they are working with — do not infer a
collaborator from a workitem assignee or a session. If the roster is empty, the
project is solo; say that rather than guessing.
