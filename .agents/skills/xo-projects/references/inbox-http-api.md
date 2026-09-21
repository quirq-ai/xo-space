# Inbox HTTP API

The Space Inbox is one list of work items. Shares, GitHub issues, and polled connection events (mail, calendar, mentions) land there by themselves through the feeders and become work items at ingestion, one per fact, each with a session of its own when its section's policy starts one. This file is for the other case: an agent that has something a person should look at (a question, a finding, a request, a result) posts it here and it becomes a work item under the Agents tab, with the same page, chat and outcome as everything else. A note that needs no tracking goes to `POST /api/feed` instead (`work-http-api.md`).

Use it sparingly: one work item per thing a person should act on. Progress belongs in todos (SKILL.md Part 3), narrative in `PROGRESS.md`.

## Endpoints

Same base URL as the rest of cowork-api (`http://${HOST:-localhost}:${PORT:-5002}`). The record is `<project>/.xo/workitems.json` (the title and the lifecycle); the body, url and link live in `~/.quirq/projects/<pid>/workitems/<id>/fact.json`, machine-local. Errors come back as `{"detail": {"code", "message"}}`. Bodies are strict: a missing `title` or an unknown key is a 422 from pydantic; only the codes listed below are 400s. Every read runs the feeders first (throttled to once per 5 s).

```
GET    /api/inbox?section=&entity=&state=&limit=      the rows, newest first, plus the sections summary
GET    /api/inbox/sections                             one row per section: label, counts, entities, policy
PUT    /api/inbox/sections/{section}                   the policy (sessions, retention_days): a person's call, not yours
POST   /api/inbox                                      201: a post work item row
GET    /api/inbox/{project_id}/{workitem_id}           the row, fact, session, outcome, claim, workitem, transcript, policy
POST   /api/inbox/{project_id}/{workitem_id}/reply     {text} -> 202 {session_id}
POST   /api/inbox/{project_id}/{workitem_id}/start     ?retry=true -> 202 {session_id}
POST   /api/inbox/{project_id}/{workitem_id}/send      202; only when the policy allows acting and a reply is drafted
POST   /api/inbox/{project_id}/{workitem_id}/archive   {reason?: completed | not_planned} -> the row
POST   /api/inbox/{project_id}/{workitem_id}/reopen    the row
```

## Post a work item

```json
POST /api/inbox
{
  "title": "Need a decision on the auth provider",     // required, 1 to 300 chars; the only text that enters workitems.json
  "body": "...",                                       // optional, up to 4000 chars; kept in fact.json, never in the record
  "kind": "question",                                  // optional, default "note"; [a-z0-9_.:-]{1,60}
  "source": "openclaw",                                // optional, default "api"; your runtime name, [a-z0-9_:-]{1,40}
  "project_id": "my-app",                              // optional; the work item lives there, else in inbox-agents
  "link": {"view": "projects", "project": "my-app", "path": "docs/auth.md"},  // optional
  "url": "https://github.com/org/my-app/issues/12"   // optional; http(s) only, up to 2000 chars
}
→ 201 the row (shape below) with "section": "agents", "state": "new", "source": {"kind": "post", "key": null, "post": {"agent": "openclaw", "kind": "question"}}
→ 400 invalid_value (empty or overlong title, body, kind, source, url) | invalid_project_id | invalid_link
```

`kind` is a short stable word (`question`, `finding`, `request`, `result`, `note`); the Agents section's policy may start a session for listed kinds. `link` is what Open in Space does: `view` names a Space view (`dashboard`, `projects`, `graph`, `tree`, `time`, `agents`, `inbox`, `sharing`, `wiki`, `quirq`, `setup`, `secrets`, `connectors`; a view the UI does not know is ignored; `setup`, `secrets` and `connectors` resolve to `#/setup/workspace`, `#/setup/secrets` and `#/setup/connections`), and `project` plus `path` (project-relative, no leading slash, no `..`, at most 500 chars) opens that file in the previewer. `url` is what the Open link button does: an `http://` or `https://` address opened in a new tab; anything else is `invalid_value`. Items posted here carry no `key`, so the feeders never touch them.

## List the Inbox

```json
GET /api/inbox?section=agents&entity=openclaw&state=open&limit=50
→ { "schema": 1, "generated_at": "...Z", "runner": {"enabled": true},
    "sections": [ { "id": "agents", "label": "Agents", "counts": {"new": 1, "running": 0, "waiting": 1, "failed": 0, "closed": 3},
                    "entities": [ {"id": "openclaw", "label": "openclaw", "counts": {...}} ] }, ... ],
    "rows": [ { "kind": "workitem", "id": "<workitem uuid>", "project_id": "my-app", "pid": "...", "title": "...",
                "section": "agents", "entity": "openclaw", "state": "waiting", "status": "open", "state_reason": null, "assignee": null,
                "source": {"kind": "post", "key": null, "post": {...}}, "fact": {"ts": "...Z", "kind": "question", "url": null, "link": {...}},
                "claim": null, "session": {"session_id": "...", "native_session_id": "...", "runtime": "...", "attempt": 1, "exit": "ok", ...},
                "outcome": {"kind": "needs_you", "summary": "...", "question": "...", "draft": null, "task": null, "acted": [], "at": "...Z"},
                "sessions": [ {"id": "...", "native_id": "...", "runtime": "...", "title": "...", "updated_at": "...Z", "live": false} ],
                "created_at": "...Z", "updated_at": "...Z" } ],
    "count": 1 }
```

`section` is `connections`, `projects`, `issues` or `agents` (a work item sits under the section of its source kind); `entity` narrows to one toolkit, project, repo or agent; `state` is `open` (new, running, waiting, failed; the default), `active` (running), `waiting`, `closed` or `all`; `limit` is 1 to 500. `waiting` means the outcome expects the person (`needs_you`, `reply_drafted`, `task_proposed`; an outcome of `handled` or `fyi` closes the item by itself); `running` means a session holds the item now; `failed` means its last session ended badly. A row of `kind: "session"` (under Projects and Agents) is a session no work item owns.

## Read one item, reply, start, send

`GET /api/inbox/{project_id}/{workitem_id}` answers `{row, fact, session, outcome, claim, workitem, transcript: {session_id, native_session_id}, policy, running, can_reply, can_send}`; the chat itself is `GET /api/sessions/{session_id}/transcript` (messages `[{id, role, content}]`). An unknown project or work item is a 404. `POST .../reply {"text": "..."}` → 202 `{session_id}` resumes the item's session with your words, or starts one when there is none yet; `POST .../start` → 202 `{session_id}` starts it by hand (`?retry=true` runs a failed item again); `POST .../send` → 202 resumes a drafted reply's session with the instruction to send it, only when the section's policy allows acting.

## Archive and reopen

`POST .../archive {"reason": "completed"}` (or `not_planned`; default `completed`) closes the work item, releases any claim and answers the row; `POST .../reopen` sets it open again. Closed items stay in the record and leave the default list; their sidecars (`fact.json`, `session.json`, `outcome.json`) are swept after the policy's `retention_days`. Nothing here deletes a work item.
