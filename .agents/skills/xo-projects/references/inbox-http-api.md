# Inbox HTTP API

The Space Inbox is where information arriving in the workspace is seen, tracked, and acted on. Sessions, blocked todos, shares, GitHub issues, and polled connection events land there by themselves through feeders. This file is for the other case: an agent that has something a person should look at (a question, a finding, a request, a result) posts it here instead of burying it in a transcript.

Use it sparingly: one item per thing a person should act on. Progress belongs in todos (SKILL.md Part 3), narrative in `PROGRESS.md`.

## Endpoints

Same base URL as the rest of cowork-api (`http://${HOST:-localhost}:${PORT:-5002}`). The file behind these routes is `<XO root>/.xo/inbox.json`, written under a flock, so concurrent posts don't tear.

```
GET    /api/inbox?status=open|done|all&limit=N
POST   /api/inbox
PATCH  /api/inbox/{item_id}
DELETE /api/inbox/{item_id}
```
## Create

```json
POST /api/inbox
{
  "title": "Need a decision on the auth provider",     // required, 1 to 300 chars
  "body": "...",                                       // optional, up to 4000 chars
  "kind": "question",                                  // optional, default "note"; [a-z0-9_.:-]{1,60}
  "source": "openclaw",                                // optional, default "api"; [a-z0-9_:-]{1,40}
  "project_id": "my-app",                              // optional; folder name under the projects root
  "link": {"view": "projects", "project": "my-app", "path": "docs/auth.md"},  // optional
  "url": "https://github.com/org/my-app/issues/12"   // optional; http(s) only, up to 2000 chars
}
→ 201 { "id": "a1b2c3d4", "ts": "...Z", "status": "new", "title": "...", "body": "...", "kind": "question", "source": "openclaw", "project_id": "my-app", "link": {...}, "url": "https://..." }
→ 400 invalid_value | invalid_project_id | invalid_link
```

`kind` and `source` are labels the UI shows as chips: use your runtime name for `source` and a short stable word for `kind` (`question`, `finding`, `request`, `result`). `link` is what the Open button does: `view` names a Space tab (`projects`, `sessions`, `time`, `dashboard`; unknown views are ignored), and `project` plus `path` (project-relative, no leading slash, no `..`, at most 500 chars) opens that file in the previewer. Unknown link keys are dropped; an empty link is stored as `null`. `url` is what the Open link button does: an `http://` or `https://` address opened in a new tab; anything else is `invalid_value`. Items created here carry no `key`, so the feeders never touch them.

## Read

```json
GET /api/inbox?status=open&limit=200
→ { "schema": 1, "updated_at": "...Z",
    "counts": { "new": 3, "seen": 2, "done": 12 },
    "items": [ { "id": "a1b2c3d4", "ts": "...", "source": "timeline", "kind": "session.started",
                 "title": "...", "body": "", "project_id": "...", "link": {"view": "sessions"},
                 "url": null, "status": "new", "key": "timeline:session.started:<session_id>" }, ... ] }
→ 400 invalid_status
```

`status` is `open` (new plus seen, the default), `done`, or `all`; `limit` is 1 to 500 (default 200). Counts always cover the whole file; items are newest first. Every read runs the feeders first (throttled to once per 5 s), so the list is current without a separate refresh call.

## Update

```json
PATCH /api/inbox/{item_id}
{ "status": "done" }        // new | seen | done
→ 200 the item
→ 400 invalid_status
→ 404 item_not_found
```

`seen` means a person looked at it, `done` means it was handled; `new` reopens it. Only status can be changed; edit the file for anything else.

## Delete

```json
DELETE /api/inbox/{item_id}
→ 200 { "item_id": "a1b2c3d4", "deleted": true }
→ 404 item_not_found (malformed id only)
```

Idempotent: `deleted: false` (not 404) when the item was already gone. Prefer `done` over delete for anything a person may want to find later; done items are kept for 30 days.
