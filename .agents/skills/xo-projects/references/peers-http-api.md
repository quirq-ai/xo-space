# Peers HTTP API

The peers roster is the humans a project is shared with, kept in
`<project>/.xo/peers.json` (committed with the project). An empty roster means
the project is solo: say so rather than guessing.

This file is the endpoint reference. Routes live in
`routers/cowork_agent/bff/visualizer.py`; the store is
`services/cowork_agent/visualizer/peers_store.py`.

A workitem `assignee` uses the same `user_id` charset, so a listed peer can
always be assigned work. Removing a peer is a **hard delete**: unlike a todo or
a workitem, nothing is left behind.

## Endpoints

Same base URL as the rest of cowork-api (`http://${HOST:-localhost}:${PORT:-5002}`).
`{project_id}` is the folder name under the projects root. Bodies are strict
(`ForbidExtra`). Errors come back as `{"detail": {"code", "message"}}`.

```
GET    /api/xo-projects/{project_id}/peers
POST   /api/xo-projects/{project_id}/peers
GET    /api/xo-projects/{project_id}/peers/{user_id}
PATCH  /api/xo-projects/{project_id}/peers/{user_id}
DELETE /api/xo-projects/{project_id}/peers/{user_id}
```

## List

```
GET /api/xo-projects/{project_id}/peers?role=owner|collaborator|viewer
→ { "project_id": "my-app", "updated_at": "...Z", "peers": [ ... ] }
```

Oldest first. Omit `role` for the whole roster. An empty `peers` array is the
solo project, not an error. A corrupt or newer-schema `.xo/peers.json` is 409
(`corrupt_document` / `unsupported_schema`), never overwritten.

## Create

```json
POST /api/xo-projects/{project_id}/peers
{
  "user_id": "alice",
  "role": "collaborator",
  "label": "Alice",
  "endpoint": "https://alice.example"
}
→ 201 { "user_id": "alice", "role": "collaborator", "added_at": "...Z",
        "label": "Alice", "endpoint": "https://alice.example" }
→ 400 invalid_user_id | invalid_role | invalid_label | invalid_endpoint | invalid_value
→ 409 peer_exists
```

`user_id` is required and must match `[A-Za-z0-9_:\-.]{1,200}` (the workitem
assignee charset). `role` is required: `owner`, `collaborator`, or `viewer`.
`label` is optional display name (1 to 200 printable chars, or omit).
`endpoint` is optional `http(s)://` URL, at most 2000 chars, no whitespace.

The roster is a set keyed by `user_id`. Creating a peer who is already listed
is 409 `peer_exists` (PATCH them instead). Ceiling is 1000 peers
(`invalid_value` past that).

## Get and update

```
GET /api/xo-projects/{project_id}/peers/{user_id}
→ 200 the peer
→ 404 peer_not_found
```

```json
PATCH /api/xo-projects/{project_id}/peers/{user_id}
{ "role": "viewer", "label": null }
→ 200 the peer
```

Only supplied fields change. `role` cannot be cleared. `label` and `endpoint`
treat JSON `null` as a clear; omit the key to leave them. A no-op PATCH is a
no-op (no write).

A stored role that is not in the vocabulary is served as `viewer` (least
privilege) rather than failing the whole roster.

## Delete

```
DELETE /api/xo-projects/{project_id}/peers/{user_id}
→ 200 { "project_id": "my-app", "user_id": "alice", "deleted": true }
```

Hard delete: the record is removed from the file. Idempotent (`deleted: false`
when they were not on the roster). This does not unassign their workitems and
does not revoke project-sharing on the swarm; those are separate surfaces.

## Pitfalls

- Empty roster = solo. Do not invent an owner from git or GitHub.
- Delete is permanent. There is no `include_deleted` and no tombstone.
- `user_id` is an identity string, not an email. Use the same value you would
  put on `PUT .../workitems/{id}/assignee`.
