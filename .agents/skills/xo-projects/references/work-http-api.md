# Work HTTP API

The Work tab is where a person sees everything that needs them across the Space: work items assigned to them or to nobody, blocked todos, issues for them, mail and mentions of a listed kind, failing sources, and what finished. Those arrive by themselves from the logs. This file is for the other case: an agent that has something a person should look at (a question, a finding, a request, a result) posts it here instead of burying it in a transcript. `POST /api/inbox` is the tracked form of the same thing: a work item under the Inbox's Agents tab, with a page, a chat and an outcome (`inbox-http-api.md`).

Use it sparingly: one post per thing a person should act on. Progress belongs in todos (SKILL.md Part 3), narrative in `PROGRESS.md`. A post of kind `question` or `request` is a decision row on the Inbox page until the person acts on it; any other kind is a History entry.

## The loop you are part of

1. On boot, `GET /api/workspace/workitems?assignee=<runtime>&status=open` is your queue.
2. Claim the one you take: `POST /api/xo-projects/{project_id}/workitems/{id}/claim {session_id, runtime}`. The Inbox shows it as in progress while your session is live.
3. Record steps as todos under it (`links.todo_ids`). A `blocked` todo surfaces for the person by itself.
4. When you need the person, post a `question` here with the work item in `ref`.
5. Close it: `PATCH .../workitems/{id} {"status": "closed", "state_reason": "completed"}`. The person accepts or reopens it from the Inbox; a reopened item is back in your queue on your next boot.

## Post

Same base URL as the rest of cowork-api (`http://${HOST:-localhost}:${PORT:-5002}`). Errors come back as `{"detail": {"code", "message"}}`. Bodies are strict: a missing `title` or an unknown key is a 422.

```json
POST /api/feed
{
  "title": "Cap transcripts at 40 MB or stream them?",       // required, 1 to 300 chars
  "body": "Capping is a 20-line change; streaming touches the watcher loop.",   // optional, up to 4000 chars
  "kind": "question",                                        // optional, default "note"; question | request | finding | result | note
  "source": "openclaw",                                      // optional, default "api"; your runtime name, [a-z0-9_:-]{1,40}
  "project_id": "my-app",                                    // optional; folder name under the projects root
  "ref": {"workitem_id": "...", "todo_id": "...", "session_id": "...", "issue": {"repo": "o/r", "number": 12}},  // optional; what this is about
  "link": {"view": "projects", "project": "my-app", "path": "docs/auth.md"},   // optional; what Open does in the Space UI
  "url": "https://github.com/org/my-app/issues/12"          // optional; http(s) only
}
→ 201 { "id": "a1b2c3d4", "ts": "...Z", "source": "openclaw", "kind": "question", "title": "...", "body": "...",
        "project_id": "my-app", "pid": "<the project's pid>", "ref": {...}, "link": {...}, "url": "..." }
→ 400 invalid_value | invalid_project_id | invalid_link
```

`ref.workitem_id` is what ties the question to the work you are on: the Inbox shows the work item's name with it and Track lands on the record. Posts are kept for 30 days or 500 items, whichever comes first.

## Items with a session of their own

What the feeders find (a share, an issue, a polled connection event) and what
agents post become work items at ingestion, and each may get a session of its
own under its section's policy, running the `inbox-item` skill. If you are
that session, the prompt already tells you everything; end with the outcome
block. Listing those items, reading one, starting or continuing its session
and archiving it are `inbox-http-api.md`.

## Read

`GET /api/feed?limit=100&sources=posts&project=my-app` answers your own posts among everything else (`{entries, next_cursor, watermark, sources}`), newest first; `GET /api/work/attention` answers what the person still has to decide, with a `reason` per item. You do not need either to do your work; they exist so you can check whether a question was already answered (it leaves the attention list when the person dismisses it or turns it into a work item).
