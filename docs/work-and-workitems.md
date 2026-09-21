# The Work section and work items

Status: design draft, 2026-09-16. Replaces the Inbox tab (issue #132, part of #129).
The pages exist on sample data (PR 1) and the read model is under way (PR 2).
Sections 13 and 14 list the order of work and the
decisions still open.

## 1. Why this matters

XO's promise is that one person supervises many agents across many projects
without reading transcripts. The Work is where that promise is kept or broken.

A person opens the Space with three questions:

| Question | Surface | What answers it |
|---|---|---|
| What needs me? | Inbox, tab badge | Open work assigned to me, unassigned work, blocked todos, a mail or mention that expects a reply, a source that is failing |
| What is happening now? | Live | The calendar beside a live stream of every line the logs report |
| What happened? | History | Every event over a window, with charts, split Space and Projects, with project sharing inside |

What the business needs from this surface, in order:

1. **An accurate badge.** The number on the tab must mean "decide something".
   A badge that counts noise is switched off in a week and trust goes with it.
2. **Work items that cannot be lost or duplicated.** A work item is the unit
   of delegation and accountability: one owner, one lifecycle, linked to the
   sessions and todos that did the work, adoptable from a GitHub issue so an
   external tracker stays the source of truth where one exists. Without it,
   "done" is a chat message nobody can check.
3. **One action from arrival to delegation.** A mail, an issue, a blocked
   todo, an agent's finding: each becomes tracked, assigned work in one click.
   That is the loop XO sells: arrive, triage, delegate, verify.
4. **A complete stream.** Every state change is already a timeline event.
   The Work reads that log rather than keeping a second copy, so what a person
   sees and what an auditor would read are the same facts.

Polish comes after these four. A compact page with the right four numbers beats
a rich page with a wrong badge.

## 2. The model in one picture

```
   facts (append-only logs, one per source)          person decisions (tiny)
   ─────────────────────────────────────────         ───────────────────────
   ~/.quirq/projects/timeline.jsonl   sessions,      ~/.quirq/work/{inbox,live,history}/
                                      todos, files,    watermark   (seen up to ts)
                                      work items       dismissed   (attention keys)
   ~/.quirq/connections/<tk>/events.jsonl  mail,       promoted    (entry -> work item)
                                      calendar, slack  pinned
   ~/.quirq/scheduler/runs/<id>.jsonl job runs         posts       (what agents POST)
   sharing relay `recent`             share events     sources     (what to show)
                    │                                        │
                    ▼                                        ▼
         ┌──────────────────────┐   read-time merge   ┌──────────────┐
         │  GET /api/feed       │◄─────────────────── │  feed state  │
         │  GET /api/work/      │                     └──────────────┘
         │      attention       │◄──── derived from current state, not stored
         └──────────────────────┘
                    │ promote
                    ▼
   owned records (mutable, one owner, one lifecycle)
   ───────────────────────────────────────────────
   <project>/.xo/workitems.json          the work item        (committed, shared)
   ~/.quirq/projects/<pid>/workitems/claims.json   who is on it now  (machine-local)
```

Three kinds of thing, kept apart on purpose:

- **Events** are facts. They are never edited and never copied. The Work is a
  view over them.
- **Attention items** are a query over current state: "is this todo blocked
  right now", "is this work item open and mine". When the condition clears the
  item leaves on its own. Nothing to auto-close, nothing to reopen.
- **Work items** are the only mutable records. They already exist
  (`services/cowork_agent/visualizer/workitems_store.py`) with adoption, claims,
  assignment, links and lifecycle events. They have no UI today. The Work
  gives them one.

The person's own decisions (what I have seen, what I dismissed, what I
promoted, what I pinned) are the only state the Work writes, and they are small.

## 3. Vocabulary

| Word | Meaning | Lives in |
|---|---|---|
| event | one line in a source log | the source's file |
| entry | an event as the Work shows it (one shape for every source) | computed |
| attention item | a condition that expects a person, with a `since` | computed |
| work item | a unit of work with an owner and a lifecycle | `.xo/workitems.json` |
| claim | a live session saying it is working a work item; `in_progress` is derived from it | `claims.json` |
| todo | one step inside a session; linked to a work item, never the same thing | `.xo/todos.json` |
| post | a note an agent sent to the person through the API | `work.json` |

## 4. What exists, what changes

| Today (Inbox) | Work | Why |
|---|---|---|
| Five feeders copy events into `inbox.json` on every read (5 s throttle) | Readers merge the source logs at read time; nothing is copied | one fact, one file; no ingest, no throttle, no retention conflicts |
| Per-item `new / seen / done`, plus `auto_closed` so feeders can reopen | A watermark for seen; attention items keyed on `key@since`, dismissable | per-item state does not scale to a stream; conditions replace reopen logic |
| Badge = unseen items | Badge = attention count | "decide something", not "something happened" |
| Six pages: Items, Connections, Jobs, Activity, Sharing activity, Sharing | Three: Inbox, Live, History | one page per question: what needs me, what is happening now, what happened |
| Work items have an API and no UI | The Inbox's Work list, and Track on any History entry | the delegation loop needs a surface |
| Agents `POST /api/inbox` | Agents `POST /api/feed`; `/api/inbox` stays as an alias | the skill reference names the old path |
| `~/.quirq/inbox/inbox.json` | `~/.quirq/work/{inbox,live,history}/`, one folder per page | the state root has one folder per subject, and the Work one per page |

Unchanged: `.xo/workitems.json` and its schema, `claims.json`, every
`/api/xo-projects/{id}/workitems*` route, the rollup, the timeline sink and its
`workitem.*` vocabulary, the connections poller and its `events.jsonl`.

## 5. The data tree: collected, stored, shown

Every fact the Work shows is collected by one producer, stored in one file
and read by one page. Nothing is copied on the way. Paths are under
`~/.quirq/` unless they start with `<project>/`.

```
Work
├── Inbox (#/work)                       everything that needs the person, in four groups
│   ├── Needs a decision  (derived on every read; only dismissals are stored)
│   │   ├── assigned to me, unassigned .... <project>/.xo/workitems.json            <- work item API (people, agents, Track)
│   │   ├── blocked ....................... <project>/.xo/todos.json                <- todo API, driven by agent sessions
│   │   ├── issue for me, not tracked ..... projects/<pid>/github/issues.json       <- GitHub poller
│   │   ├── needs a reply ................. connections/<toolkit>/events.jsonl      <- connections poller (Composio)
│   │   ├── shared with you, behind ....... sharing/<repo>-<hash>.json, relay status   <- sharing relay
│   │   └── source error .................. connections/<toolkit>/state.json,
│   │                                       scheduler/state.json                    <- each poller's own last_error
│   ├── Coming up  (meetings from now to the end of tomorrow)
│   │   └── meetings ...................... connections/googlecalendar/events.jsonl <- calendar collector
│   ├── Completed, for a look  (the last day; acknowledgements are stored)
│   │   ├── jobs that ran (one row per job)  scheduler/runs/<id>.jsonl               <- scheduler
│   │   ├── work items an agent closed .... <project>/.xo/workitems.json            <- work item API
│   │   └── todos an agent completed ...... projects/timeline.jsonl   todo.completed   <- watcher
│   ├── Open work
│   │   ├── the record .................... <project>/.xo/workitems.json            <- work item API
│   │   ├── in progress ................... projects/<pid>/workitems/claims.json    <- claims API, live sessions
│   │   └── the issue behind it ........... projects/<pid>/github/issues.json       <- GitHub poller (status, assignees)
│   └── the person's marks ................ work/inbox/inbox.json  dismissed, acked, promoted   <- this page
├── Live (#/work/live)                   what is happening now
│   ├── the calendar
│   │   ├── meetings, past and upcoming ... connections/googlecalendar/events.jsonl <- calendar collector
│   │   └── next runs ..................... scheduler/state.json                    <- scheduler
│   ├── running now (the badge row)
│   │   ├── open sessions ................. cache/activity/                          <- watcher presence
│   │   ├── running jobs .................. scheduler/state.json                    <- scheduler
│   │   ├── pollers ....................... connections/<toolkit>/state.json        <- connections poller
│   │   └── watcher tick .................. cache/heartbeat.json                     <- watcher
│   └── the stream  (a server-sent tail over the same logs, nothing stored for it)
│       ├── Watcher ....................... cache/heartbeat.json ticks, projects/<pid>/timeline.jsonl appends
│       ├── Agents ........................ projects/<pid>/timeline.jsonl   session.*, todo.*, file.*   <- watcher
│       ├── Jobs .......................... scheduler/runs/<id>.jsonl, logs/scheduler/<id>.log   <- scheduler
│       └── Pollers ....................... connections/<toolkit>/state.json, events.jsonl; relay `recent`
├── History (#/work/history)            what happened, over a window (today, 7 days, 30 days, all)
│   ├── the figures and the five charts ... counted over the loaded events in the window, nothing stored
│   ├── Space  (everything not tied to one project)
│   │   ├── mail, calendar, mentions ...... connections/<toolkit>/events.jsonl      <- connections poller
│   │   ├── jobs ran, failed .............. scheduler/runs/<id>.jsonl               <- scheduler
│   │   ├── shared with you, relay errors . relay `recent` (memory today; sharing/events.jsonl proposed)   <- sharing relay
│   │   └── source errors ................. connections/<toolkit>/state.json, scheduler/state.json
│   ├── Projects  (one project or all)
│   │   ├── work items .................... projects/timeline.jsonl   workitem.*     <- work item API, claims API
│   │   ├── sessions, todos, files ........ projects/timeline.jsonl   session.*, todo.status_changed, file.* (bursts collapsed)   <- watcher
│   │   ├── project, peers ................ projects/timeline.jsonl   project.created, peer.sync.*
│   │   ├── issues ........................ projects/<pid>/github/issues.json       <- GitHub poller
│   │   ├── commits fetched, applied ...... relay `recent`                          <- sharing relay
│   │   ├── agent notes ................... work/history/history.json  posts[]      <- POST /api/feed
│   │   └── Sharing card .................. sharing/<repo>-<hash>.json, relay status,   <- project sharing
│   │       (state, Apply, Share, members)  <project>/.xo/peers.json                <- peers API
│   └── the person's marks ................ work/history/history.json  watermark, pinned   <- this page
```

Retention belongs to each file, so the Work prunes nothing of its own:

| File | Written by | Kept | Rebuildable |
|---|---|---|---|
| `projects/<pid>/timeline.jsonl` | watcher; the todo, work item and claim stores | rotates at 8 MB, five segments | no |
| `projects/timeline.jsonl` | the same lines, tagged with the project | no rotation today; section 8 adds it | no |
| `connections/<toolkit>/events.jsonl` | connections poller | per toolkit, rotated by the poller | no |
| `scheduler/runs/<id>.jsonl` | scheduler | append-only, newest last | no |
| `projects/<pid>/github/issues.json` | GitHub poller | current state, rewritten each poll | yes |
| `cache/activity/`, `cache/heartbeat.json` | watcher | live, rebuilt every tick | yes |
| `<project>/.xo/workitems.json`, `todos.json`, `peers.json` | their APIs | committed with the project | through git |
| `work/inbox/inbox.json`, `live/live.json`, `history/history.json` | the Work pages, `POST /api/feed` | posts 500 items or 30 days; dismissals and acknowledgements 30 days | no |

The Live stream keeps nothing: it is a tail over the files above, served as
they grow, and a browser holds at most 300 lines of it.

## 6. The Work section

Three pages in the section bar, one per question. Tab id `work`, label
**Work**, alias `inbox` so every old link lands.

### 6.1 Inbox (`#/work`): everything that needs me

Four group cards across the top (Decisions, Calendar, Completed, Work),
each with its count, an icon and one line saying what it holds; pressing a
card narrows the list to that group, pressing again shows everything. Under
them one list in the same four groups, a project select, and "+ Work item"
in the section bar. Every row has one primary action, a line saying why it
is here, and a way to put it away.

```
[All | Decisions | Calendar | Completed | Work]                             [All projects ▾]
NEEDS A DECISION · 7
  ●  Fix parser timeout on large transcripts   assigned to you   xo-space      2h ago   [Claim]        [Dismiss]
  ●  Invoice question from Sagar               needs a reply     gmail         35m ago  [Track]        [Dismiss]
  ●  quirq-ai/xo-docs shared with this Space   shared with you   sharing       1d ago   [Clone]        [Dismiss]
  ●  Gmail: connection expired                 source error      gmail         21h ago  [Reconnect]    [Dismiss]
COMING UP · 3
  03:00 PM  ○  Design sync with Sagar   in 4 h      googlecalendar   [Open]  [Dismiss]
COMPLETED, FOR A LOOK · 4
  ○  Job   nightly backup finished   4.1 s · 212 files          1h ago   [Output] [Acknowledge]
  ○  Job   telemetry rebuild · 3 runs   all ok                  1h ago   [Output] [Acknowledge]
  ○  Work item   Agents page: hero stats   closed   xo-space   Claude Code   23h ago   [Accept] [Reopen] [Dismiss]
  ○  Todo  Read the Inbox implementation   todo done   xo-space   Claude Code   41m ago   [Acknowledge]
OPEN WORK · 6
  ●  Space UI redesign: Work page   xo-space   #132   in progress                you        12m ago
```

- **Needs a decision**: the attention items (section 9), one primary
  action each and a plain-language reason under the title (who assigned
  it, what blocks it, what the mail asks). Track turns a mail, mention or issue into a work item;
  Clone and Apply are the two sharing actions only a person can take.
- **Coming up**: meetings from the calendar connection, now to the end of
  tomorrow, with the time and "in 4 h"; a meeting under way reads "now"
  and offers Join.
- **Completed, for a look**: what finished in the last day. Jobs, one row
  per job however often it ran (Output, Acknowledge); work items an agent
  closed (Accept, Reopen, Dismiss); todos an agent completed
  (Acknowledge). Acknowledging is stored so a row leaves for good.
- **Open work**: the work items. A row expands to the body or the issue
  link, who is on it, an assignee select, Close (Change on GitHub for an
  adopted item), Open project, Delete.
- The **badge** on the tab counts decisions and completed rows: what still
  awaits a person.

### 6.2 Live (`#/work/live`): what is happening now

A hero row (sessions open with agents active; jobs running with the next
run; pollers with how many fail), then two columns. Left, the
**calendar**: a shadcn Calendar with a dot on every day that carries
something, the selected day's agenda under it (meetings, each job's next
run, Run now), the next four upcoming entries, and a Today link when
another day is selected. Right, the **stream**: one line per thing the
logs report, as it happens, reading as a console: a level dot, the time,
the source as a tag (the vendor color for an agent), the line; a new line
flashes in briefly. A chip row over it names what runs now (each open
session and its project, each running job, each poller), with a pulse
sparkline of lines per twenty seconds over the last five minutes beside
it; clicking a chip or a line's tag narrows the stream to that source, and
the header says so. A source toggle (All, Agents, Jobs, Pollers, Watcher) and Pause sit in
the header; the stream follows the newest line unless the person scrolls
up, when "Jump to latest" appears. Nothing here is configured; Setup keeps
that.

### 6.3 History (`#/work/history`): what happened

A window toggle (Today, 7 days, 30 days, All) scopes everything on the
page together: the hero row, five charts and the timeline, split Space |
Projects. The charts, all drawn by the chart kit from the loaded events:
events per day (area), the split by source (donut), the busiest hours
(Space) or events per day by agent (Projects), and a 16-week heatmap of
daily activity. Then the timeline with sticky day headers.

- **Space** is everything not tied to one project: mail, calendar,
  mentions, jobs ran or failed, the sharing relay's own events, source
  errors. Its figures: arrived (per day), jobs ran (failed), sharing events.
- **Projects** is the rest, for one project or all: work items, sessions,
  todo status changes, files (bursts from one session collapsed to one
  row that opens to the paths), issues, peer syncs, commits fetched, agent
  notes. Its figures: events (per day), files touched (sessions), work
  closed (started). Above the timeline sits
  the **Sharing** card, folded in from the former Sharing page: one row
  per shared project with its repo, branch and sync state, Apply when
  behind, Open to narrow to that project; with one project selected the
  row opens to its members (owner first, Revoke, Copy invite) and "+ Share
  a project" in the section bar opens the composer.

Every row is a timeline item: the time on the left, a dot on the rail
(accent for attention, red for an error), the kind badge, the title, a
one-line detail, then the project chip, the agent tag and the age on the
right. The kind badge, the project chip and the agent tag are clickable:
each narrows the timeline, and the narrowing shows as removable chips in
the toolbar. The title opens the row to its full detail and Open, Open
project, Track (or Open work item, which lands on the Inbox with the
record open), Pin. The timeline shows sixty rows at a time with Show more.
Manage's "View activity" lands here with the project selected. Seen is a
watermark moved as the top of the timeline scrolls into view.

### 6.4 Retired routes

`#/inbox/items` aliases to `#/work`; `#/inbox/jobs` and `#/inbox/connections`
to `#/work/live`; `#/inbox/activity`, `#/inbox/sharing-activity`,
`#/inbox/sharing`, `#/sharing`, `#/projects/sharing` and the interim
`#/work/activity` to `#/work/history`.
Connections keep their configuration under Setup; their health shows on
Live's badge row and reaches Needs you as a `source_error`.

## 7. Work-item architecture

### 7.1 The record (exists, unchanged)

`<project>/.xo/workitems.json`, committed with the project, written only by the
workitems API. Per item: `id` (uuid4), `title`, `body`, `labels`, `status`
(`open | closed`, GitHub's words), `state_reason`, `source` (`local`, or
`github` with `{repo, number, node_id, url}`), `assignee`, `links.todo_ids`,
`links.session_ids`, timestamps, soft-delete tombstone. For an adopted item
GitHub owns `status`, `state_reason` and `body`; the read joins the issue
mirror and flags `stale` when the issue is gone. `in_progress` is never
stored: it is derived from a claim whose session is live or younger than the
grace window.

Every transition is a `workitem.*` timeline event (`created, adopted, assigned,
claimed, released, closed, reopened, deleted`), written to the project's
timeline and the Space timeline. That is why the Work can show work-item
history without asking the store.

### 7.2 What the Work adds

Nothing to the record. The join from a Work entry to the work item it became
lives in the feed state (`promoted`), because it is a machine-local fact about
this person's triage, and `.xo/` is a committed file whose schema change would
need a migration in every project.

`POST /api/work/promote {key, project_id, title?, assignee?}`:

1. Finds the entry by key across the readers (a 404 if it is older than the
   logs keep).
2. Builds the work item: title from the entry unless given; body = the
   entry's detail plus its url; labels `["work", "<source>"]`.
3. An entry that is itself a GitHub issue (`issue.*` from the mirror) adopts
   the issue instead of creating a local item, through the existing adopt path.
4. Writes `promoted[key] = {project_id, workitem_id}` and answers the work
   item. Idempotent: a second promote of the same key answers the same item.

The Work row then shows "tracked" with a link to the item, and the attention
strip drops the entry if it was there.

### 7.3 Who assigns, who works

- **A person assigns** from the Work page or at promotion: `me`, an agent
  name, or a login. The assignee picker lists the agents from `GET /api/agents`
  through the shell, never by name in core code (the modularity invariant).
- **An agent finds its work** with `GET /api/workspace/workitems?assignee=<runtime>&status=open`,
  claims it (`POST .../claim`), records todos under it, and closes it. This
  is the boot-ritual step to add to the project template and the xo-projects
  skill; the endpoints exist.
- **`me`** resolves as today: the GitHub login, else the Coder owner, else
  `local`.

### 7.4 GitHub

An open issue in a project's mirror shows in the Work as an `issue.*` entry.
Track adopts it. Adopted items read status from the mirror, so closing on
GitHub closes the work item on the next poll and the Work shows
`workitem.closed` only if the store emits it; today it does not, since the
mirror is a read-time join. A follow-up: the poller emits `workitem.closed`
and `workitem.reopened` when a mirrored issue changes state, so the stream
carries the transition. Until then the Work page is right and the stream is
one event short.

## 8. The Work read model

`GET /api/feed?limit=100&before=<ts>&sources=a,b&kinds=x,y&project=<id>`

Readers, one per source, each answering newest-first up to `limit` before
the cursor. All of them exist as functions today:

| Source | Reader | Entry kinds |
|---|---|---|
| `timeline` | `WorkspaceVisualizerScope.read_timeline(limit, before, types)` | `session.*`, `todo.*`, `file.*`, `workitem.*`, `project.created`, `peer.sync.*`, `plan.written`, `episode.written` |
| `issues` | the per-project mirror rows, newest `updated_at` first | `issue.opened`, `issue.updated`, `issue.closed` |
| `connections` | `connections.store.read_events(toolkit, limit)` per configured toolkit | `<toolkit>.<type>` |
| `sharing` | `sharing_status.snapshot()["recent"]` | `sharing.<kind>` |
| `jobs` | `scheduler.list_runs(job_id, limit)` per job | `job.finished`, `job.failed` |
| `posts` | `work.json["posts"]` | whatever the agent wrote |

The service merges by `ts`, takes `limit`, and answers `next_cursor` as the
last `ts` when the page is full. One entry shape for every source (section 10).
Each reader is wrapped: a failing reader contributes nothing and a
`sources[name].error` line, never a failed page.

Cost per request: bounded by `sources × limit` small reads, no network. The
timeline reader reads the file tail; the connections reader reads one tail per
toolkit; sharing is in memory; jobs is one tail per job; posts is one small
JSON. A 30 s poll from one browser is the load.

Two things the readers need that do not exist yet:

- The Space timeline (`~/.quirq/projects/timeline.jsonl`) does not rotate.
  Add the per-project rotation (8 MB, keep 5) to `workspace/timeline.apply`
  before the Work depends on it.
- Sharing `recent` is in memory and lost on restart. Append the same lines to
  `~/.quirq/sharing/events.jsonl` so the stream survives a restart. Not
  blocking: the stream tolerates an empty source.

## 9. Attention

`GET /api/work/attention` answers the strip and the badge. Each item is a
condition over current state with a `since`:

| Reason | Condition | Since | Primary action |
|---|---|---|---|
| `assigned_to_me` | work item open, assignee is me, not in progress | `updated_at` | Claim (opens the project) |
| `share_pending` | a repo shared with this Space is not cloned here | the share's `at` | Clone |
| `commits_behind` | a shared project's branch is behind origin | the fetch's `at` | Apply |
| `unassigned` | work item open, no assignee | `created_at` | Assign |
| `todo_blocked` | a todo is `blocked` in any project | the `todo.status_changed` event ts | Open |
| `issue_mine` | open issue assigned to me on GitHub, not adopted | issue `updated_at` | Track |
| `connection` | a connection event whose kind is listed in `sources.connections.attention` | event ts | Track / Open |
| `source_error` | a connection or job whose last run failed | first failure ts | Reconnect / Open |

Dismissal is `POST /api/work/dismiss {key, since}`. The pair is what is hidden,
so the same todo blocked again next week (a new `since`) comes back on its
own. Dismissed keys are pruned when older than 30 days.

The badge counts what still awaits a person: the decisions and the Completed
rows not yet acknowledged (section 6.1). `GET /api/work/summary`
answers `{attention, unseen, in_progress, errors}` in one small call; the
shell polls it every 60 s while another tab is shown, as the badge does today.

## 10. Shapes

One entry, every source:

```jsonc
{
  "key": "timeline:workitem.closed:xo-space:8f2c...",   // stable, from the source; dedup and promote by it
  "ts": "2026-09-16T09:12:03Z",
  "source": "timeline",                                  // timeline | issues | connections | sharing | jobs | posts
  "kind": "workitem.closed",                             // the source's own type
  "title": "Closed: Fix parser timeout",
  "detail": "completed",                                 // one line, may be empty
  "project_id": "xo-space", "pid": "7deb...",            // null when the source has none
  "actor": {"runtime": "claude_code", "session_id": "..."},   // null for a person or a poller
  "ref": {"workitem_id": "...", "issue": {"repo": "...", "number": 1},
          "todo_id": null, "path": null, "url": null},   // only the keys that apply
  "tone": "info",                                        // info | attention | error
  "tracked": {"project_id": "xo-space", "workitem_id": "..."}   // from promoted, else null
}
```

The state, one folder per page under `~/.quirq/work/` (split on 2026-09-18 from
one `work.json`): `inbox/inbox.json` holds `attention`, `dismissed`, `acked` and
`promoted`; `live/live.json` holds `stream` (which groups the Live stream shows);
`history/history.json` holds `sources`, `watermark`, `pinned` and `posts`. In
memory the three read as this one document:

```jsonc
{
  "schema": 3,
  "updated_at": "...",
  "watermark": "2026-09-16T09:00:00Z",                  // seen up to and including
  "sources": {
    "timeline":    {"enabled": true},
    "issues":      {"enabled": true},
    "connections": {"enabled": true, "attention": ["gmail.message", "slack.mention"]},
    "sharing":     {"enabled": true},
    "jobs":        {"enabled": true},
    "posts":       {"enabled": true}
  },
  "dismissed": {"todo:xo-swarm:12@2026-09-15T18:00:00Z": "2026-09-16T08:10:00Z"},
  "promoted":  {"connection:gmail:message:18c...": {"project_id": "xo-space", "workitem_id": "..."}},
  "pinned":    ["timeline:session.started:..."],
  "posts": [ {"id": "a1b2c3d4", "ts": "...", "source": "api", "kind": "note",
              "title": "...", "body": "...", "project_id": null, "pid": null,
              "link": null, "url": null} ]
}
```

Same rules as the Inbox file: normalised on read, unknown keys survive,
hand-editable, one locked read-modify-write per change, `posts` keeps the
500-item and 30-day retention. Schema 1 (the Inbox file) is read once on
first use: its `items` with `source: "api"` become `posts`; everything else
was a copy of a log and is dropped.

## 11. API

| Route | Body | Answers |
|---|---|---|
| `GET /api/feed` | `limit, before, sources, kinds, project` | `{entries, next_cursor, watermark, sources: {name: {ok, error}}}` |
| `GET /api/work/attention` | | `{items, counts}` |
| `GET /api/work/summary` | | `{attention, unseen, in_progress, errors}` |
| `PUT /api/work/watermark` | `{ts}` | `{watermark}`; only moves forward |
| `POST /api/work/dismiss` | `{key, since}` | 204 |
| `DELETE /api/work/dismiss/{key}` | | 204, idempotent |
| `POST /api/work/promote` | `{key, project_id, title?, assignee?}` | the work item, 201 or 200 when already promoted |
| `POST /api/feed` | as `POST /api/inbox` today | 201, the post |
| `POST /api/inbox` | `{title, body?, kind?, source?, project_id?, link?, url?}` | 201, a `post` work item row (section 18) |
| `PATCH /api/work/pins` | `{key, pinned}` | 204 |

Work page and actions use the existing `GET /api/workspace/workitems` and the
per-project `workitems` routes. No new work-item route.

The Inbox's own routes (`GET /api/inbox`, `/api/inbox/sections` and the
per-item routes under `/api/inbox/{project_id}/{workitem_id}`) are section
18's; the old rows API (`PATCH` and `DELETE /api/inbox`) and section 17's
`/api/work/inbox/sections` and `/api/work/inbox/items/...` are gone. Two
skill references: `work-http-api.md` for `POST /api/feed` and the loop,
`inbox-http-api.md` for the Inbox.

Errors follow the BFF pattern: `WorkError(ServiceError)` mapped by
`bff/errors.http_error`; strict bodies (`ForbidExtra`).

## 12. Code placement

- `services/work/` (top level, beside `inbox` today; the Work is a property
  of the Space): `store.py` (work.json), `readers.py` (one function per
  source, replacing `feeders.py`), `attention.py`, `service.py`.
- `routers/cowork_agent/bff/feed.py`.
- `space_ui/js/views/work.js` (Inbox), `work-live.js` (Live),
  `work-history.js` (History, with the sharing management the former
  `sharing.js` page carried; that page and `inbox.js` go in PR 1),
  `work-sample.js` (fixtures until the API lands); `css/work.css`. The kit
  gained `nativeSelect`, `calendar`, `timeline` and seven lucide icons.
- `navigation.js`: `WORK_PAGES` with the old routes as aliases; `PRIMARY_TABS`
  entry `{id: 'work', label: 'Work', aliases: ['inbox']}`.
- `services/storage/layout.py`: `feed_dir()` and a `Move` from `inbox/`.
- Core names no agent. The assignee picker gets agents from the API.

## 13. Order of work

Four PRs, each shippable on its own.

1. **Rename and restructure (UI only).** Work tab with the `inbox` alias, four
   pages on sample data, old routes aliased, badge moved. Built 2026-09-16 for
   the brainstorm; lands once the look is agreed, with `views/inbox.js`,
   `inbox-activity.js` and their tests removed. Docs: `space_ui/README.md`.
2. **Work read model (backend).** `services/work/`, `/api/feed*`, `work.json`
   with the layout move and the schema 1 import, Space timeline rotation,
   tests. `services/inbox/` and its feeders are removed in the same PR;
   `POST /api/inbox` stays as the alias. Docs: `DEVELOPING.md` section 11,
   the skill reference, `tests/fixtures/quirq-state/`.
3. **The pages on the new API.** Inbox on `/api/work/attention`, the work
   item rollup, the calendar events and the scheduler's runs; History on
   `/api/feed` with a `since` for the window, watermark, dismiss, promote,
   grouping, the summary badge, and the sharing card on the existing
   `sharing_data.js` seam; Live on the activity, connections, schedules and
   calendar-event routes plus a server-sent `GET /api/work/live` that tails
   the logs (section 5).
4. **Work item actions and the agent loop.** Create, assign (me, agent,
   login), close, reopen, adopt from an issue; the boot-ritual step in the
   project template and the skill; the poller's `workitem.closed` emission
   for mirrored issues (section 7.4).

PR 1 can land this week and already answers #132's first two checkboxes.

## 14. Decisions to make

1. **A promoted work item needs a project.** Recommended: yes. Agents run
   inside a project and the record is committed with it. The picker defaults
   to the project the entry names, else the last one used. The alternative,
   a Space-level work item file under `~/.quirq/`, would be machine-local and
   invisible to peers, which contradicts what a work item is for.
2. **The badge counts attention only, not unseen activity.** Recommended:
   yes. Unseen is shown as a muted count inside the page.
3. **`services/inbox` is renamed in PR 2, not shimmed.** Recommended: yes.
   The five inbox test files move with it; a shim would keep two names for
   one thing.

## 15. Assumptions and risks

- The Work is machine-local, like the Inbox and the timeline. Two machines
  on the same projects have two Works. Work items are shared through the
  project; that is the sync boundary, as today.
- `file.*` volume can dominate the Space timeline. Density and grouping
  handle the page; rotation (section 8) handles the file.
- The Space timeline has no `project.created` for projects cloned by hand
  before the watcher saw them; the stream starts where the log starts.
- A connection event's "needs a reply" is a collector judgement. Until
  collectors mark it, `sources.connections.attention` is a kind list a person
  edits, and the default is empty.

## 16. The inbox loop

Added 2026-09-18. Sections 1 to 15 describe the pages and the read model.
This section is the loop those pages serve: how one piece of information
enters the Space, becomes tracked work, gets done, and leaves, with nothing
lost and nothing counted twice. It is the contract for `services/work/`.

### 16.1 The five stages

```
   arrive            surface              decide                work                 verify
   ──────            ───────              ──────                ────                 ──────
   a fact lands  ->  the Inbox shows  ->  a person picks   ->  an agent claims  ->  the person
   in a log          it as a decision     one action           the work item,       accepts or
   (no copy)         with a reason        (Dismiss, Track,     records todos,       reopens; the
                                          Assign, Open)        closes it            row leaves
```

| Stage | Who acts | What changes on disk | What the Inbox shows |
|---|---|---|---|
| arrive | a poller, the watcher, an agent, the relay | one appended line in the source's own log | nothing yet |
| surface | nobody: derived on read | nothing | a row under Needs a decision, with a reason and one primary action; the badge counts it |
| decide | the person | `work.json`: `dismissed[key@since]`, or `promoted[key]` plus a new record in `<project>/.xo/workitems.json` | the row leaves; a tracked entry shows under Open work |
| work | the agent | `claims.json` (claimed), `todos.json` (steps), `workitems.json` (closed); each writes a `workitem.*` or `todo.*` timeline event | Open work shows "in progress" and who; History shows every step |
| verify | the person | `work.json`: `acked[key]`, or `workitems.json` reopened (`workitem.reopened` event) | the row leaves Completed for good, or the item is back in Open work and, if assigned to an agent, in that agent's queue |

Two rules hold the loop together:

1. **Facts are never copied.** Every stage reads the source log or the
   record. `work.json` holds only the person's decisions, keyed by the fact
   they were made about, so a decision cannot outlive its fact by mistake
   and a fact cannot appear twice because it was ingested twice.
2. **Every leaving is a stored decision or a cleared condition.** A row
   leaves Needs a decision when its condition clears (the todo is unblocked,
   the item is assigned) or when the person dismisses that condition with
   its `since`. A row leaves Completed when the person acknowledges it.
   Nothing expires on its own, so a badge of 0 always means "you decided".

### 16.2 Every kind of information, through the loop

| Arrives as | Surfaces as (reason) | Decide | Work | Verify |
|---|---|---|---|---|
| a work item assigned to me (API, an agent, Track) | `assigned_to_me` | Claim opens the project; Assign to an agent hands it over | the agent claims and closes | Completed: Accept, Reopen |
| a work item with no owner | `unassigned` | Assign to me or an agent | as above | as above |
| a todo an agent marked blocked | `todo_blocked` | Open project (unblock in the session); Dismiss | the agent moves the todo on | leaves when the todo is no longer blocked |
| an open GitHub issue assigned to me, not tracked | `issue_mine` | Track adopts it as a work item (GitHub keeps status) | the agent claims it; closing on GitHub closes it | Completed on the next poll |
| a mail, a mention, a page (a connection event whose kind is listed in `sources.connections.attention`) | `connection` | Track makes a work item in a project; Open; Dismiss | as a local work item | Completed: Accept, Reopen |
| a repo shared with this Space, not cloned | `share_pending` | Clone; Dismiss | the relay clones and fetches | leaves when cloned |
| a shared project behind origin | `commits_behind` | Apply; Dismiss | the relay fast-forwards | leaves when in sync |
| a connection or a job that keeps failing | `source_error` | Reconnect (Setup) or Open (Live); Dismiss | the poller recovers | leaves on the next good run |
| a meeting from the calendar connection | Coming up | Open or Join; Dismiss | none | leaves when it ends |
| a job run, a work item an agent closed, a todo an agent completed | Completed, for a look | Output, Accept, Reopen, Acknowledge | none | `acked[key]` |
| an agent's note (`POST /api/feed`) | a History entry; Needs a decision when `kind` is `question` or `request` | Track, Pin, Dismiss | the agent continues when answered | none |
| a session, a file, a plan, a peer sync | History only | Pin | none | none |

The last two rows are the point of the derived design: an agent asking a
question is a decision, an agent editing a file is not, and the difference is
a kind list a person can edit, not a schema change.

### 16.3 Keys and `since`

Every attention item has a stable `key` naming the thing (`workitem:<project>:<id>`,
`todo:<project>:<id>`, `issue:<project>:<number>`, `connection:<toolkit>:<kind>:<id>`,
`share:<repo>`, `behind:<project>`, `source:<name>`, `post:<id>`) and a
`since` naming when the condition started. A dismissal stores the pair, so:

- the same todo blocked again next week (a new `since`) comes back;
- a work item reassigned to me after I dismissed it (a new `updated_at`)
  comes back;
- a mail dismissed once stays dismissed, because a mail has one `since`.

Acknowledgements (Completed) store the key alone: a job that ran again is a
new row with a new key (`job:<id>:<day>`), a work item closed again after a
reopen has a new `updated_at` in its key.

`promoted[key]` is the join from a fact to the work item it became. Promote
is idempotent on the key, and a promoted entry never surfaces as a decision
again: it is tracked, so its work item is what needs deciding now.

### 16.4 The agent's half

An agent joins the loop with the endpoints that exist today:

1. On boot, `GET /api/workspace/workitems?assignee=<runtime>&status=open`
   answers its queue, oldest first.
2. It claims one (`POST .../claim`), which makes the item "in progress" in
   the Inbox while the session is live.
3. It records steps as todos linked to the item (`links.todo_ids`), so a
   blocked step surfaces as `todo_blocked` with the item's context.
4. When it needs the person, it `POST /api/feed {kind: "question"}` with
   the work item in `ref`; that is a decision row until answered.
5. It closes the item (`PATCH {status: "closed"}`), which releases the
   claim and puts the item under Completed, for a look.

A person's Reopen is the only way back from step 5 to step 1, and it is an
ordinary status change the agent sees on its next boot. This is the whole
loop: arrive, triage, delegate, verify, and every arrow is one HTTP call
or one derived read.

### 16.5 What `services/work/` does at each read

`GET /api/work/inbox` composes the four groups in one call (the page paints
once, the badge is the same numbers):

| Group | Reads | Minus |
|---|---|---|
| decisions | the attention derivation (section 9) over the rollup, todos, mirrors, connection events, the sharing snapshot, connection and scheduler state, posts | `dismissed` |
| calendar | `googlecalendar` events with a start from now to the end of tomorrow | `dismissed` |
| completed | scheduler runs of the last day (one row per job), work items closed in the last two days, `todo.completed` timeline events of the last day | `acked` |
| work | the rollup, open items, newest first | nothing |

Cost: one walk of the projects root plus small file reads; no network. A
browser polls it every 30 s on the page and every 60 s for the badge. The
old ingest, throttle, cursors and auto-close of `services/inbox/` have no
counterpart here: a read has nothing to advance.

### 16.6 What is deliberately not in the loop

- **Priority and due dates.** A work item has an owner and a status. If
  ordering matters, labels and the GitHub issue carry it.
- **Comments.** Conversation belongs in the session or the issue. The
  Inbox links to both.
- **Automatic assignment.** Nothing assigns work without a person or an
  agent naming an owner. `unassigned` exists so that gap is visible.
- **Expiry.** A decision row never ages out. If the list grows, the fix is
  a narrower `sources.connections.attention`, not a timer.

## 17. Connections as folders, a session per item

**Superseded on 2026-09-21 by section 18.** The folders under
`~/.quirq/work/inbox/<section>/`, the index, the thread file and the routes
of 17.9 are gone: the Inbox is now the work items (section 7) joined with
the watcher's session data. What survives of this section: the policy shape
(17.4, without the `items` map), the outcome block (17.2's `outcome.json`),
the `inbox-item` skill (17.7), and the runner's start, resume and outcome
parsing (17.6, 17.8). The rest is kept as the record of the first cut.

**Generalised on 2026-09-21.** The folders are now per Inbox *section*, not
per connection: `~/.quirq/work/inbox/<section>/` for `connections`,
`projects`, `issues` and `agents` (the row's source family; the workspace
stream of sessions and todos stays on Activity),
with `policy.json` in place of `connection.json`, an item folder for every
open Inbox row named by the row's Inbox id, and one cursor per section (the
newest Inbox ts the maker read). The policy's `items` collector map is gone:
every open row is tracked. Sessions default to `auto` for every kind
(`sessions.kinds` narrows that); rows older than a day on a section's first
run are tracked as `skipped` and start only by hand. Sessions run in
`~/xo-projects/inbox-<section>/`. The routes take `{section}` where this
section says `{connection}` or `{toolkit}`, and `/api/work/inbox/sections`
replaces `/api/work/inbox/connections`. The item page (`#/inbox/item`) shows
the thread with the chat beside it. The text below is the first cut as
designed; the shapes in 17.2 and 17.4 changed as described here.

Added 2026-09-19; built the same day up to 17.11's step B (folders, policy,
maker, manual Start, the routes, the page rows, the skill, auto mode, caps,
Retry, Send). Step C (the proxy-side allowlist for `act`, Setup's policy
drawer) is open. Section 16 made the Inbox a derived view over the logs
with a person deciding each row. This section adds the next step of the
loop: every connection is a folder inside the Inbox, every collected item
is a folder inside it, and an item can own one agent session that handles
it and leaves an outcome. The person decides on outcomes, not on raw
events. Nothing changes for the other sources (issues, jobs, sharing, the
timeline): they stay derived as in section 16.

### 17.1 The tree

```
~/.quirq/work/inbox/
├── inbox.json                        the person's marks (section 16.3), unchanged;
│                                     an item's marks use the key item:<connection>:<id>
├── gmail/                            one folder per configured connection, made when
│   │                                 the connection is configured
│   ├── connection.json               the policy: which collectors become items, whether
│   │                                 and when a session starts, what it may do (17.4)
│   ├── items.json                    the index: one line per item (status, outcome kind,
│   │                                 decided) and the cursor per collector; rebuildable
│   └── unread-18c2a9f1/              one folder per item, named <collector>-<key>
│       ├── item.json                 the fact as collected, plus status and timestamps
│       ├── session.json              the session bound to it, once one started
│       ├── outcome.json              what the session concluded, once it ended
│       └── run.log                   the run's output tail; safe to delete
├── slack/
└── googlecalendar/

~/xo-projects/inbox-gmail/            the connection's project: where its sessions run
├── AGENTS.md, PLAN.md, memory/       the ordinary project tier (scaffolded once)
├── .xo/                              todos, work items, project.json (the ordinary records)
└── items/unread-18c2a9f1/            the item's workbench: drafts and notes the agent writes
```

Two places per item, on purpose:

- **The item folder under `~/.quirq/work/inbox/<connection>/` is the item.**
  Record, session binding, outcome, log. Written only by `services/work/`
  (the item maker and the runner), never by the agent. Machine-local, like
  every other Work file.
- **The workbench under `~/xo-projects/inbox-<connection>/items/<id>/` is
  where the agent works.** A session has to run inside a project: the
  watcher attributes a transcript to a project by its working directory,
  the session index lives per project, todos and work items are project
  records, and the sync API backs projects up. A session started inside
  `~/.quirq/` would be invisible to Live and History and would vanish on
  uninstall. The project is scaffolded from the template on the first
  item, so it carries `AGENTS.md`, `memory/` and `.xo/` like any other.

### 17.2 The item

`item.json`, written by the item maker from one `events.jsonl` line:

```jsonc
{
  "schema": 1,
  "id": "unread-18c2a9f1",                  // <collector>-<key>, a safe folder name
  "connection": "gmail",
  "collector": "unread",                    // the collector id from config.json
  "key": "18c2a9f1",                        // the producer's own id (the event key)
  "ts": "2026-09-19T08:12:03Z",             // the event's ts
  "kind": "gmail.unread",                   // the entry kind the readers use
  "title": "Invoice question from Sagar",
  "body": "...",                            // the collected body, as is
  "url": "https://mail.google.com/...",
  "status": "new",                          // new | queued | running | done | failed | skipped
  "created_at": "...", "updated_at": "...",
  "decided": null                           // "accepted" | "dismissed" | "tracked", with at
}
```

`session.json`, written by the runner when a session starts and again when
it ends:

```jsonc
{
  "schema": 1,
  "session_id": "5c1e...",                  // the Space's session id (what the chat resumes)
  "session_key": "claude:inbox-gmail:web:9f3a1c2b",
  "native_session_id": "...",               // the runtime's own id
  "runtime": "claude_code",                 // whichever adapter was active
  "project_id": "inbox-gmail",
  "workbench": "items/unread-18c2a9f1",
  "agent_type": "inbox-item",
  "started_at": "...", "ended_at": null,
  "exit": null,                             // "ok" | "timeout" | "error", with the message
  "attempt": 1
}
```

`outcome.json`, the session's last message parsed by the runner (17.6):

```jsonc
{
  "schema": 1,
  "kind": "reply_drafted",     // reply_drafted | task_proposed | needs_you | fyi | handled
  "summary": "Sagar says the June invoice shows 12 seats; we agreed on 10. Draft reply asks finance to reissue.",
  "draft": "items/unread-18c2a9f1/reply.md",      // a path in the workbench, when there is one
  "task": {"title": "Reissue June invoice at 10 seats", "assignee": null},   // for task_proposed
  "question": null,                                // for needs_you: what the agent asks
  "acted": [],                                     // what it did on the connection, when allowed
  "at": "..."
}
```

### 17.3 Lifecycle

```
   events.jsonl line ──► item folder (new) ──► queued ──► running ──► done + outcome
                              │                                 │
                              │ policy: off / manual            └──► failed (retry once)
                              ▼
                        stays new until the person presses Start
```

| Item state | Where the Inbox shows it | The person's actions |
|---|---|---|
| `new`, sessions off or manual | Needs a decision, reason `item_new` | Start session, Track, Open, Dismiss |
| `queued`, `running` | Live, on the running-now row, and Open work as "in progress" when tracked | Open session (the chat, live) |
| `done`, outcome `needs_you` | Needs a decision, reason `item_question`, the agent's question as the detail | Answer (opens the session), Dismiss |
| `done`, outcome `reply_drafted` | Needs a decision, reason `item_draft` | Review draft (opens the workbench file), Send (only when `act` allows), Dismiss |
| `done`, outcome `task_proposed` | Needs a decision, reason `item_task` | Track (promote, with the proposed title and assignee), Dismiss |
| `done`, outcome `fyi` or `handled` | Completed, for a look | Acknowledge |
| `failed` | Needs a decision, reason `source_error` (per item) | Retry, Dismiss |
| decided (`accepted`, `dismissed`, `tracked`) | nowhere; History keeps the events | |

**The thread.** Opening an item shows it as a conversation: the mail (or
mention, or event) on top, then every turn on `thread.jsonl` in the item
folder (the agent's answers without their outcome block, the person's
replies, a system note when a run failed), the outcome, and a reply box.
`POST .../reply {text}` puts the person's words on the thread and resumes
the item's session with them (or starts it, when the item has none yet);
the agent's answer lands on the thread when the turn ends, and a follow-up
answer need not restate the outcome. The page polls the thread every two
seconds while a turn runs. The runtime's own transcript is not copied: the
thread holds only what a person would read.

The marks stay in `inbox.json` keyed `item:<connection>:<id>` with the
item's `updated_at` as `since`, so a session that runs again (Retry) makes
a new pair and the row comes back. `items.json` records `decided` too, so
the folder listing answers without the marks file.

### 17.4 The policy: `connection.json`

```jsonc
{
  "schema": 1,
  "items": {"unread": true},                // which collectors become items; absent = none
  "sessions": {
    "mode": "manual",                       // off | manual | auto
    "kinds": ["unread"],                    // auto: which collectors start a session on arrival
    "agent_type": "inbox-item",             // the skill (17.7); an adapter maps it to its own syntax
    "runtime": null,                        // null = the active agent; a name pins one
    "max_concurrent": 2,
    "max_per_hour": 20,
    "timeout_s": 300,
    "act": false                            // may the session act on the connection (send, label)?
  },
  "retention_days": 30
}
```

Defaults are the safe ones: no collector becomes an item until listed,
sessions are `manual`, and a session may draft but not act. `auto` is an
opt-in per connection. Setup writes this file from the connection's
drawer; a hand edit works too. `XO_INBOX_SESSIONS=off` in the environment
stops every runner regardless of policy.

### 17.5 The item maker

One function, run by the runner's tick and by the connections poller's
new-events listener (so a "Poll now" makes items at once): for each
connection folder whose policy lists collectors, read the tail of
`connections/<toolkit>/events.jsonl` newer than the cursor in
`items.json`, and for each line of a listed collector make the item folder
when it does not exist. Idempotent by folder name. The cursor is the newest
`ts` read, kept beside the items it advanced, like every other cursor in
the state root. A connection with no policy file makes no items: the
folders are explicit.

### 17.6 The session

- **Where.** In `~/xo-projects/inbox-<connection>/`, scaffolded from the
  project template on first use (`project_layout.scaffold_project`), with
  the item's workbench at `items/<id>/` (created empty).
- **How it starts.** Through the same path the chat uses: the runner opens
  `AgentDispatcher(<active agent>).stream(...)` with `agent_id` the
  connection project, `agent_type` `inbox-item`, `is_new_session` true and
  a Space session id it minted, then drains the stream. That path writes
  the session index row before the process spawns, pre-allocates the
  runtime's session id, passes the per-session MCP config, and is what the
  watcher and the chat already understand. The runner never builds a
  command line of its own, so it names no agent.
- **What it is told.** One prompt: the paths (the item record, read-only;
  the workbench, writable), the fact itself inline (title, body, url,
  collected at), the policy (`act` yes or no), and the contract: end with
  one fenced `json` block that is the outcome (17.2). The skill (17.7)
  carries the procedure.
- **What it can do.** The session gets the Composio tools of this Space
  through the MCP proxy when the runner can resolve the Composio identity
  (the same resolution the poller uses); without one it runs with no
  tools and can still draft. `act: false` is stated in the prompt and
  enforced by the skill; a later step adds a proxy-side allowlist per
  session so it is enforced by the server too.
- **How it ends.** The runner parses the last fenced `json` block of the
  final message into `outcome.json`; no block or a block that fails
  validation is `failed` with the reason. The policy's `timeout_s` bounds
  the run; a timeout is `failed` and retried once. The session stays
  resumable: Open session lands in the chat with the same session id.
- **What it writes to the logs.** The runner appends `inbox.item.created`,
  `inbox.item.started`, `inbox.item.finished` (with the outcome kind) and
  `inbox.item.failed` to the connection project's timeline and the Space
  timeline, so History shows the item's life beside the session's own
  events, which the watcher produces as for any session.

### 17.7 The `inbox-item` skill

A bundled skill (`.agents/skills/inbox-item/SKILL.md`, installed into every
agent's home like `xo-projects`), mapped in each agent's manifest
(`"inbox-item": "/inbox-item"` for Claude Code; the Codex manifest maps the
same name to its own syntax). It says, in order: read the item; decide one
of five outcomes; write a draft or notes to the workbench, never to the
item folder or `.xo/`; act on the connection only when the prompt says
`act: yes`, and record every action under `acted`; keep it short, one
item, no exploring the project; end with the outcome block. It also says
what not to do: no replies to unknown senders when acting, no forwarding,
no deleting.

### 17.8 The runner

`services/work/runner.py`, one background task started with the other
pollers (`periodic.run_forever`, every 15 s, gated by
`XO_INBOX_SESSIONS`), and `services/work/items.py` (the maker, the
folders, the index). A tick: make items (17.5); for each connection in
`auto` mode, queue `new` items of the listed kinds; start queued items
while the connection is under `max_concurrent` and `max_per_hour`; harvest
runs that ended (write `session.json`, `outcome.json`, the index, the
timeline lines); retry a `failed` item once. One item never has two
sessions: the item folder is the lock (`session.json` present means
started), and the runner takes the connection folder's flock for the
index. A manual Start is the same code path called from the route. The
runner logs one line per start and per end, with the item id and the
outcome kind, never the body.

### 17.9 Routes

| Route | Body | Answers |
|---|---|---|
| `GET /api/work/inbox/connections` | | one row per connection folder: policy, counts by status, cursor |
| `PUT /api/work/inbox/connections/{toolkit}` | the policy (17.4), strict | the policy |
| `GET /api/work/inbox/items?connection=&status=&limit=` | | the items, newest first, with the outcome kind |
| `GET /api/work/inbox/items/{toolkit}/{id}` | | the item, its session and its outcome, and the workbench file list |
| `GET /api/work/inbox/items/{toolkit}/{id}/thread` | | the item, its turns, its outcome, `running`, `can_reply`, `can_send` |
| `POST /api/work/inbox/items/{toolkit}/{id}/reply` | `{text}` | 202 `{session_id}`; resumes the session, or starts it with the text |
| `POST /api/work/inbox/items/{toolkit}/{id}/start` | | 202 `{session_id}`; 409 when a session exists |
| `POST /api/work/inbox/items/{toolkit}/{id}/decide` | `{action: accept \| dismiss \| track, project_id?, assignee?}` | the item; `track` promotes through section 7.2 |
| `POST /api/work/inbox/items/{toolkit}/{id}/send` | | 202; only when `act` allows: the runner resumes the session with "send the draft" |

`GET /api/work/inbox` (section 16.5) grows the item rows into its groups
(17.3) and a `connections` summary for the group cards; `GET /api/feed`
gains the `inbox.item.*` kinds from the timeline reader with no new
reader.

### 17.10 Cost, safety, failure

- **Nothing runs unasked.** Items exist only for listed collectors;
  sessions start only in `auto` mode for listed kinds, under two caps, and
  the environment switch stops all of it. The first cut ships `manual`.
- **One session per item, ever, unless a person presses Retry.** The
  folder is the lock; the index is the ledger.
- **Draft, don't act, by default.** `act` is per connection and off; even
  when on, the skill names what is allowed and the outcome lists what was
  done, so the person sees it.
- **A failing connection makes no items.** The maker reads only what the
  poller wrote; a poll that failed wrote nothing.
- **Bodies stay local.** Item bodies are in the item folder and in the
  prompt; they never enter a timeline line or a log. `run.log` keeps the
  runtime's output tail only.
- **Retention.** An item folder is removed 30 days after it was decided
  or failed; `items.json` is pruned with it; at most 500 items per
  connection, oldest decided first. The connection project keeps the
  workbench folders (they are project files, backed up with it) and gets
  the same 30-day sweep of `items/` for decided items.

### 17.11 Code placement and order of work

- `services/work/items.py` (folders, index, policy, the maker),
  `services/work/runner.py` (queue, start, harvest, timeline lines),
  `routers/cowork_agent/bff/work.py` gains the routes of 17.9,
  `.agents/skills/inbox-item/`, a `skills` entry per agent manifest,
  `work-item.schema.json` and `work-connection.schema.json`, the fixture
  `tests/fixtures/quirq-state/work/inbox/gmail/`.
- `attention.py` gains the four item reasons; `inbox.py` puts running items
  on Live's row and `fyi`/`handled` under Completed.
- Order: (A) folders, policy, the maker, manual Start, the routes, the
  page rows (`mode: manual`); (B) the skill, the runner's auto mode, caps,
  Retry, Send; (C) the proxy-side allowlist for `act`, and Setup's policy
  drawer.

### 17.12 Decisions to make

1. **Sessions run in a per-connection project, not inside `~/.quirq/`.**
   Recommended: yes, for the reasons in 17.1. The item folder in the Inbox
   stays the item; the workbench is where the agent writes.
2. **The first cut is `manual`.** Recommended: yes. Items appear as
   decisions with a Start button; `auto` is switched on per connection
   once the outcomes look right.
3. **Draft only by default.** Recommended: yes. `act: true` per connection
   later, with the proxy allowlist.
4. **Only connections get folders.** Recommended: yes for now. GitHub
   issues, jobs and sharing stay derived; a `github/` folder can follow
   the same shape if issues should get sessions too.
5. **One project per connection, named `inbox-<connection>`.**
   Alternative: one shared `inbox` project with `items/<connection>/<id>/`.
   Recommended: per connection, so the project's `AGENTS.md` and memory
   can be about that connection and its sessions are grouped in Agents.

## 18. The Inbox over work items and sessions

Added 2026-09-21; supersedes section 17. Section 17 gave every Inbox row a
folder of its own under `~/.quirq/work/inbox/<section>/` and one session per
folder. Building it showed the folder was a third copy of something the Space
already keeps twice: the work item (`<project>/.xo/workitems.json`, section 7)
and the watcher's session index. This section replaces the Inbox file
(`~/.quirq/inbox/inbox.json`) and the section folders with one record, the
work item, joined at read time with the session data the watcher already has.
Nothing is copied twice. What survives of 17 is named at its top.

### 18.1 Why

- **One record.** A fact that arrives (a mail, a calendar event, a GitHub
  issue, a share event, an agent's post) becomes a work item at ingestion, in
  the store that exists, with the lifecycle, the claims, the links and the
  events that exist. Track, promote and the item folder were three ways of
  making the same record; now there is one, made once, at arrival.
- **Chats created at ingestion.** A session is one attempt at a work item.
  The runner starts it under the section's policy or the person starts it by
  hand; either way it is an ordinary session in an ordinary project, indexed
  by the watcher, and the runtime transcript is the item's chat. There is no
  thread file to keep in step with it.
- **Tabs as facets.** Connections, Projects, Issues and Agents are facets over
  one list of rows, chosen by the work item's source kind, not four stores
  with four cursors. A row is in exactly one tab; a filter is a query
  parameter.

### 18.2 Vocabulary

| Word | Meaning | Lives in |
|---|---|---|
| work item | the durable record; a fact becomes one at ingestion | `<project>/.xo/workitems.json` |
| session | one attempt at a work item; a claim says which session holds it now | the watcher's index; `claims.json` |
| section | a tab of the Inbox: `connections`, `projects`, `issues`, `agents` | derived from `source.kind` |
| entity | the group inside a tab: a toolkit (`gmail`), a project (`xo-space`), a repo (`owner/name`), an agent id from the agents capability or a runtime name seen on sessions | derived |
| row | one line of the Inbox: `kind: "workitem"` (a work item, with or without sessions) or `kind: "session"` (a session no work item owns, under Projects and Agents only) | computed |
| fact | the thing as ingested: title, body, url, link, ts, kind, key, section, entity | `fact.json` |

### 18.3 Source kinds

`source.kind` on a work item is `local` or `github` (both unchanged) or one
of three new kinds:

```jsonc
{"kind": "connection", "key": "connection:gmail:unread:18c2a9f1",
 "connection": {"toolkit": "gmail", "type": "unread", "event": "18c2a9f1"}}
{"kind": "sharing", "key": "sharing:fetched:owner/repo:2026-09-21T08:12:03Z",
 "sharing": {"repo": "owner/repo", "event": "fetched"}}
{"kind": "post", "key": null,
 "post": {"agent": "sample_agent", "kind": "note"}}
```

`key` is the dedup identity: an optional string without whitespace, at most
400 chars; a feeder never creates two work items with the same key in one
project. `github` items keep node-id dedup. The wire model (`WorkitemSource`
in `routers/cowork_agent/bff/_visualizer_models.py`) carries `kind`, `key`,
`github`, `connection`, `sharing` and `post`.

The section that governs a work item's sessions follows its source kind:
`connection` is Connections, `sharing` is Projects, `github` is Issues, `post`
and `local` are Agents.

### 18.4 The tree

```
~/.quirq/inbox/
├── ledger.json                 the feeders' bookkeeping: cursors per feeder, source switches
└── policy/<section>.json       the session policy per section (shape unchanged, 18.6)

~/.quirq/projects/<pid>/workitems/
├── claims.json                 existing: work item id -> {session_id, runtime, started_at}
└── <workitem-id>/              the runner's sidecars for one work item
    ├── fact.json               the fact as ingested: title, body, url, link, ts, kind, key, section, entity
    ├── session.json            the runner's session state: session_id, native_session_id, runtime,
    │                           project_id, agent_type, attempt, started_at, ended_at, exit, manual
    └── outcome.json            kind, summary, draft, task, question, acted, at

<project>/.xo/workitems.json    the record (title, status, assignee, source, links.session_ids, ...)
~/xo-projects/<project>/.xo/    the watcher's session index (read, never copied)
~/xo-projects/inbox-<section>/  the section project: where facts without a project live and run
    items/<workitem-id>/        the workbench (drafts) for such a work item
```

Bodies never enter `workitems.json` (a project file the relay may share): the
work item holds the title only; `fact.json` in the runtime root holds the
body, the url and the link. `run.log` and `thread.jsonl` no longer exist: the
runtime transcript (`GET /api/sessions/{session_id}/transcript`) is the chat.

Gone: `~/.quirq/inbox/inbox.json` and `~/.quirq/work/inbox/<section>/`
(policy, `items.json`, the item folders). `~/.quirq/work/{inbox,live,history}/*.json`
(the person's marks for the Work read model, section 8) stay as they are.

### 18.5 Ingestion: the feeders create work items

`services/inbox/feeders.py` keeps its three feeders (sharing, issues,
connections) and their cursors, now in `ledger.json`. Each fact goes through
`services/inbox/facts.py`:

1. the target project: the fact's project when it names one that exists
   (issues, shares), else `inbox-<section>`, scaffolded from the template on
   first use;
2. dedup by `source.key` within that project (a scan of its work items);
3. an issue becomes an adopted work item (`adopt_workitem`, node-id dedup);
4. the work item is created with `runtime="inbox"`, the `title`, labels
   `["inbox", "<section>"]`, the `source` and `assignee=None`; then
   `fact.json` is written beside the claims file.

`POST /api/inbox` creates a `post` work item the same way (section `agents`).
`GET /api/inbox` runs the feeders first, throttled to once per 5 s, as the old
list route did; the connections poller's new-events listener triggers a forced
run after a "Poll now" that collected something.

### 18.6 The policy

Per section, `~/.quirq/inbox/policy/<section>.json`, the shape of 17.4
without the `items` map:

```jsonc
{"schema": 1,
 "sessions": {"mode": "auto|manual|off", "kinds": [], "agent_type": "inbox-item", "runtime": null,
              "max_concurrent": 2, "max_per_hour": 20, "timeout_s": 300, "act": false},
 "retention_days": 30}
```

Defaults: `connections` is `auto`; `projects`, `issues` and `agents` are
`manual`. `kinds` filters on the fact's kind (`gmail.unread`, `issue.open`,
`sharing.fetched`, `note`). `retention_days` now means: the sidecars
(`fact.json`, `session.json`, `outcome.json`) of closed work items older than
that are removed; the work item record is never deleted by the Inbox.

### 18.7 The runner

`services/work/runner.py`, every 15 s after a 5 s startup delay, gated by
`XO_INBOX_SESSIONS`: refresh the feeders (throttled); for each section in
`auto` mode start sessions for open work items of that section that have no
session yet, oldest first, under `max_concurrent` and `max_per_hour`; mark
items whose session sidecar says running but no task owns as failed (a server
restart); once an hour sweep sidecars past retention.

A session: `session_id = uuid4` (the Space session id); the project is the
work item's project; the workbench `items/<workitem-id>/` is made inside it;
the claim is written (`claim_workitem(session_id, runtime)`), the session id
is appended to `links.session_ids`, `session.json` is written; the stream is
opened through `AgentDispatcher(runtime).stream(prompt, None, agent_type,
our_session_id=session_id, agent_id=project_id, is_new_session=True,
user_id)`; the answer's last fenced `json` block is the outcome (17.2's
`outcome.json`, unchanged).

On finish: `outcome.json`, `session.json` (`ended_at`, exit `ok`), the claim
is released, and the work item's status follows the outcome: `handled` and
`fyi` close it (`state_reason: completed`); `needs_you`, `reply_drafted` and
`task_proposed` leave it open, waiting for the person. On failure (timeout,
error, cancelled, orphaned): the exit in `session.json` records it, the claim
is released, the item stays open.

- **Reply.** `POST .../reply {text}` resumes the session
  (`is_new_session=False`) with the text as the prompt, claims again for the
  turn and releases after; an item with no session yet starts one with the
  text added to the prompt.
- **Send.** Resumes with the instruction to send the draft, only when the
  policy allows `act` and the outcome is `reply_drafted`.
- **Archive.** Closes the work item (`completed` or `not_planned`) and
  releases any claim. **Reopen** sets the status back to open.

Timeline lines `inbox.item.started|finished|failed` go on the work item's
project timeline and the Space timeline as before, with `workitem_id`.

### 18.8 The row and its states

```jsonc
{"kind": "workitem", "id": "<workitem uuid>", "project_id": "inbox-connections", "pid": "<pid>",
 "title": "Invoice question from Sagar", "section": "connections", "entity": "gmail",
 "state": "new | running | waiting | failed | closed",
 "status": "open | closed", "state_reason": null, "assignee": null,
 "source": {"kind": "connection", "key": "...", "connection": {...}},
 "fact": {"ts": "...", "kind": "gmail.unread", "url": "...", "link": {"view": "connectors"}, "toolkit": "gmail"},
 "claim": {"session_id": "...", "runtime": "sample_agent", "started_at": "...", "live": true} | null,
 "session": {"session_id": "...", "native_session_id": "...", "runtime": "...", "attempt": 1,
             "started_at": "...", "ended_at": null, "exit": null} | null,
 "outcome": {"kind": "reply_drafted", "summary": "...", "draft": "reply.md", "task": null, "question": null, "acted": [], "at": "..."} | null,
 "sessions": [{"id": "...", "native_id": "...", "runtime": "...", "title": "...", "updated_at": "...", "live": false}],
 "created_at": "...", "updated_at": "..."}

{"kind": "session", "id": "<session id>", "project_id": "xo-space", "pid": "<pid>", "title": "Fix the flaky test",
 "section": "projects", "entity": "xo-space", "state": "running | closed", "runtime": "sample_agent",
 "native_id": "...", "started_at": "...", "updated_at": "...", "live": true}
```

The state of a work item row, in order: `closed` when the status is closed;
`running` when the runner owns a task for it or its claim is live; `failed`
when the last session sidecar ended with an exit other than `ok` and the item
is open; `waiting` when the outcome kind is `needs_you`, `reply_drafted` or
`task_proposed`; else `new` (no session yet, or a session ended with nothing
to decide). A session row is `running` when live, else `closed`.

### 18.9 Routes

`routers/cowork_agent/bff/inbox.py`, one line per route:

```
GET    /api/inbox?section=&entity=&state=&limit=      the rows, newest first, plus the sections summary
GET    /api/inbox/sections                             one row per section: label, counts, entities, policy; runner {enabled}
PUT    /api/inbox/sections/{section}                   the policy (strict body: sessions{...}, retention_days)
POST   /api/inbox                                      201: {title, body?, kind?, source?, project_id?, link?, url?} -> a post work item row
GET    /api/inbox/{project_id}/{workitem_id}           the row, fact, session, outcome, claim, workitem (projected record),
                                                       transcript {session_id, native_session_id}, policy, running, can_reply, can_send
POST   /api/inbox/{project_id}/{workitem_id}/reply     {text} -> 202 {session_id}
POST   /api/inbox/{project_id}/{workitem_id}/start     ?retry=true -> 202 {session_id}
POST   /api/inbox/{project_id}/{workitem_id}/send      202
POST   /api/inbox/{project_id}/{workitem_id}/archive   {reason?: completed | not_planned} -> the row
POST   /api/inbox/{project_id}/{workitem_id}/reopen    the row
```

`GET /api/inbox` answers:

```jsonc
{"schema": 1, "generated_at": "...", "runner": {"enabled": true},
 "sections": [{"id": "connections", "label": "Connections", "counts": {"new": 2, "running": 1, "waiting": 1, "failed": 0, "closed": 4},
               "entities": [{"id": "gmail", "label": "gmail", "counts": {...}}]}, ...],
 "rows": [...], "count": 12}
```

`state` filter values: `open` (new, running, waiting, failed: the default),
`active` (running), `waiting`, `closed`, `all`. Without `section`, every work
item appears once (under the section of its source kind) plus the orphan
sessions (section `projects`). With `section=projects` every project is an
entity even with no rows; with `section=agents` every agent from the agents
capability is an entity even with no rows.

### 18.10 The page

- `#/inbox/items` (the Work tab's Inbox page): tabs from the `sections` of
  `GET /api/inbox` (Projects, Agents, Connections, Issues; a tab shows its
  entity groups), state pills Open, Active, Waiting, Closed, All; a row shows
  the title, the entity, a state chip, the outcome kind and the live session's
  runtime; Open goes to the item page.
- `#/inbox/item?p=<project_id>&id=<workitem_id>` (the item page): left, the
  fact and the transcript of the latest session
  (`GET /api/sessions/{session_id}/transcript`, messages `[{id, role, content}]`),
  then the outcome; right, the chat (reply), Start, Retry, Send, Archive,
  Reopen. It polls the detail route every 2 s while running, else every 15 s.
- The registry matches routes on the hash path before `?` and leaves the
  query in the URL, so a reload keeps the item.
- The tab badge counts `waiting` plus `new`.

### 18.11 What is removed

- `~/.quirq/inbox/inbox.json`, its `new / seen / done` states, `auto_closed`,
  and the rows API on `/api/inbox` (`PATCH /api/inbox`,
  `PATCH /api/inbox/{id}`, `DELETE /api/inbox/{id}`).
- `~/.quirq/work/inbox/<section>/` (`policy.json`, `items.json`, the item
  folders with `item.json`, `thread.jsonl` and `run.log`), and the whole
  `/api/work/inbox/sections` and `/api/work/inbox/items/...` family of 17.9.
- Kept: `/api/work/inbox` (the composed Work page), `/api/work/attention`,
  `/api/work/summary`, `/api/feed`, the marks and promote (section 11).

### 18.12 Decisions taken

1. **Bodies stay in the runtime root.** `fact.json` under
   `~/.quirq/projects/<pid>/workitems/<workitem-id>/` holds the body, the url
   and the link; `workitems.json` holds the title. A project file the relay
   may share never carries a mail.
2. **The transcript is the chat.** The item page shows the latest session's
   transcript through `GET /api/sessions/{session_id}/transcript`; there is
   no `thread.jsonl` to keep in step with it and no `run.log`.
3. **Sessions of a section run in `inbox-<section>` unless the fact names a
   project.** An issue runs in its project and a share in its project; a mail
   or a post runs in the section project, scaffolded from the template on
   first use, with the workbench at `items/<workitem-id>/`.
