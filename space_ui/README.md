# Space — the workspace knowledge graph UI

An explorable map of `~/xo-projects`. Eight top-level tabs: **Dashboard**,
**Files** (List | Graph | Tree lenses under one tab), **Timeline**,
**Sessions**, **Inbox**, **Wiki**, **Setup**, and **Connectors**, plus the
**Quirq** state view, which has no tab of its own and opens from Setup's header.

This folder is a bundled snapshot of the xo-atlas UI (originally a standalone
folder with no remote), trimmed to the single endpoint-driven page and served
by this API — so every workspace that runs xo-space gets the graph with
zero configuration.

## Files

Build-free ES modules (no bundler, no dependencies); the browser loads them
directly. Descended from the single-file xo-atlas `v3.html`.

| Path | What it is |
|------|------------|
| `index.html` | Thin shell: markup + stylesheet links + `js/app.js` entry. |
| `css/` | The original stylesheet split at its section banners, loaded in original order (cascade unchanged). |
| `js/app.js` | Entry point. Registers views; **adding a view = one new file in `js/views/` + one import line here.** |
| `js/core/registry.js` | View registry: tab nav, `1..n` hotkeys, `#/<id>` hash routing, lazy mount, per-view failure isolation. |
| `js/core/api.js` | The one fetch layer: `API_BASE`, query-string auth forwarding, offline / HTTP-error / 501 classification, single-flight GETs. |
| `js/core/store.js` | Idempotency helpers: single-flight promises, slotted (non-stacking) intervals. |
| `js/core/ui.js` | Shared UI helpers (toast). |
| `js/core/server-widget.js` | Footer server pill (status poll + stop). |
| `js/core/preview.js` | File previewer drawer. Any view opens it with a `space:preview-file` event; markdown renders through `markdown.js`, HTML renders in an empty-`sandbox` iframe, everything else as escaped source. |
| `js/views/atlas.js` | Dashboard + Graph + Timeline — three lenses over one dataset, one shared closure, three exported views. |
| `js/views/sessions.js` | The Sessions view: session telemetry from `/xo/sessions.json`, contributed by whichever backends implement the `session_telemetry` capability. |
| `js/views/inbox.js` | The Inbox view: what arrived in the workspace (new sessions, blocked todos, shares, anything POSTed to `/api/inbox`) as new / seen / done rows, plus the unread badge on the tab button (`initInboxBadge`). Styled by `css/inbox.css`, its own `.inb-*` classes. |
| `js/views/projects.js` | The Files List lens: project list with per-project drawers (folder browser via `/tree`, todos, open sessions, recent events, and the project's GitHub issues via `/github/issues`). Todos are read *and written* through `/api/xo-projects/{id}/todos` — the only write path for any runtime. Owns the `Files` tab; Graph and Tree are sibling lenses (`nav:false`, `parent:'projects'`). |
| `js/views/tree.js` | The Files Tree lens: horizontal hierarchy over the same `/xo/space.json` dataset as Graph — folders as columns, files stacked beside their parent. Deep-link `#/tree`. |
| `js/views/chat.js` | The Chat view: Plane-B chat (`/api/chat/prompt` → SSE stream → transcript refetch) with session sidebar, project binding for new sessions, and mini-markdown rendering. Works across claude_code / hermes / openclaw. Deliberately unregistered — no tab. |
| `js/views/wiki.js` | The Wiki view: bundled, version-matched operating documentation. It includes storage architecture, watcher internals, complete `.xo` / `.quirq` data catalogs (durable project tier, machine-local runtime tier, workspace tier), and flow-building recipes. |
| `js/views/quirq.js` | The Quirq view: machine-local `.quirq` state — watcher infrastructure and the derived runtime tier — beside the durable project `.xo` output. Its file rows come from `services/cowork_agent/quirq_catalog.py`, which is data-driven: a file that moves root without a catalog entry to match renders as `0 present`. No tab of its own — `nav:false, parent:'secrets'`, opened from Setup's header button (`#/quirq`). |
| `js/views/secrets.js` | The Setup view: storage roots, agent runtime, watcher coverage, write-only credentials, git self-update, managed restart. |
| `js/core/markdown.js` | Escape-first mini-markdown (fences, inline code, bold/italic, links, headings, lists). |

The view contract (`id`/`label`/`order`/`nav`/`parent`/`section`, mount/show/hide)
is documented in the header comment of `js/core/registry.js`; repo-wide working
rules are in the root `AGENTS.md`.

## How it's served

`routers/space.py` mounts this folder read-only at `/space`, so the app is at
`http://localhost:5002/space/`. The three datasets the page fetches are
**files on disk**, served by `routers/xo_data.py` at `/xo/space.json`,
`/xo/dashboard.json` and `/xo/sessions.json`. The watcher materialises them
from a walk of `~/xo-projects`; a request rebuilds one on demand when it is
missing or older than `XO_VIEW_MAX_AGE_S`, so the page still works with the
watcher switched off. If a build fails the route answers 503 and the app
shows its "no data source" panel — a truthful error panel beats a
wrong-looking demo map.

The URL is a name, not a path: `/xo/space.json` serves
`~/.quirq/workspace/graph.json`. The graph is derived state, so it lives in
the machine-local runtime tier with the other rollups, while
`<XO root>/.xo/space.json` is the durable Space *record*. The older
`/space/data/` routes for these three are gone; only
`GET /space/data/session_prompts.json` remains, because it is a per-session
lookup rather than a workspace file.

- Override the folder with the `SPACE_DIR` env var (e.g. to point at a live
  xo-atlas checkout during UI development).
- The footer server pill polls `GET /space/server/status`. (The backend also
  exposes `POST /space/server/stop`, localhost-only, but the UI deliberately
  carries no stop control.)

Local change vs upstream xo-atlas: `simTick()` clamps per-tick node velocity
to 60 units — generated data can put 100+ leaves in one cluster, whose summed
spring stiffness makes the original explicit-Euler sim diverge (positions hit
1e20 and the canvas goes blank).

## Sessions tab

The fourth topbar tab (`Dashboard | Files | Timeline | Sessions | Inbox |
Wiki | Setup`) is a session-telemetry dashboard: per-session stats rendered as cards,
tables, and hand-drawn canvas charts (no dependencies), re-skinned to the
Space theme. The payload is assembled from every backend that implements the
`session_telemetry` capability, so a runtime that reports nothing shows as
"not available" rather than as a zero. It lives in its own module
(`js/views/sessions.js`), independent of the atlas's `boot()` — either can
fail without taking the other down, and the registry keeps the tabs
switchable regardless.

- Data: `GET /xo/sessions.json`, one pre-aggregated payload built from the
  session telemetry every runtime that reports it contributes. Fetched
  lazily on first open; the Refresh button re-fetches (the file is rebuilt
  at most every `XO_VIEWS_REFRESH_S`, default 30 s).
- Sub-views: Overview · Sessions (list → detail with sub-agents and
  per-session tools) · Tools · Models · Trends. The `Today/7d/30d/All`
  window selector filters client-side over per-day rollups shipped in the
  payload.
- No alerts and no prompts by design — those tables are never read, so raw
  prompt text never enters the payload.

## Inbox tab

The fifth topbar tab is where information arriving in the workspace is seen,
tracked, and acted on. One human-readable JSON file is the source of truth, a
small service feeds and edits it, four HTTP routes serve it, and one view
module (`js/views/inbox.js`, styled by `css/inbox.css`) renders it. The tab
button carries an unread badge (`counts.new`, refreshed every 60 s).

- Data: `GET /api/inbox?status=open|done|all&limit=N` (defaults `open`, 200;
  `limit` 1 to 500). The reply is `{schema, updated_at, counts: {new, seen,
  done}, items: [...]}`: counts always cover the whole file, items are newest
  first. Every read runs the feeders first (throttled to once per 5 s per
  process) and a feeder failure never fails the read. The view polls every
  30 s while shown and re-fetches after every write.
- Writes: `POST /api/inbox` `{title, body?, kind?, source?, project_id?,
  link?, url?}` answers 201 with the item; `PATCH /api/inbox/{id}` `{status}`
  answers the item; `DELETE /api/inbox/{id}` answers `{item_id, deleted}` and
  is idempotent. Ids are 8 lowercase hex; a malformed id is a 404
  `item_not_found`. Validation failures are 400 with `{code, message}`:
  `invalid_value` (title, body, kind, source, url), `invalid_project_id`,
  `invalid_link`, `invalid_status`.
- Rows: status dot (accent while new), kind chip, title, project chip,
  relative time. Clicking a row expands the body and marks a new item seen.
  Actions: Open (only when the item carries a link), Open link (only when
  `url` is an http or https address, checked in JS before it reaches an
  href; a new tab with `rel="noopener noreferrer"`), Done or Reopen,
  Delete. Filter pills Open (new plus seen) | Done | All; source pills
  All | Issues | Connections | Workspace | Sharing | Agents narrow the
  loaded page on the client and never fetch (Workspace is `timeline` plus
  `todos`, Agents is every source that is not a feeder); "Mark all seen"
  shows only while there are new items; Refresh re-fetches.
- Connections section: between the header strip and the rows, one line per
  toolkit from `GET /api/connections` that is configured for polling or
  connected here: display name, collector labels (or "no collectors"),
  cadence ("every N min" or "every N h"), last poll relative or the error
  in the error style, plus Poll now (`POST /api/connections/{toolkit}/poll`,
  then the section and the list both reload) and Configure (switches to
  Connectors). Collapsible: open by default when an entry carries an error,
  else collapsed to its count. Loaded on mount and on every show with its
  own request, so a failed load renders one muted line and never blocks
  the rows. With nothing configured or connected it reads "No connections
  polled yet. Connect a toolkit on the Connectors tab and turn on polling."
- Open follows `link`: `{project, path}` switches to Files and opens the file
  previewer; `{view}` switches to that tab; `{project}` alone switches to
  Files.

### The file: `<XO root>/.xo/inbox.json`

Written only by `services/cowork_agent/inbox/store.py` (atomic write under
the visualizer's `flock.locked`, the same mechanism the todo API uses) and
read with the visualizer's `reader.read_json`. It appears on the first
ingest that finds something or on the first `POST`; a read-only `GET` on a
fresh workspace creates nothing.

```jsonc
{
  "schema": 1,
  "updated_at": "2026-09-10T12:00:00Z",
  "sources": {                                   // optional; absent = these defaults
    "timeline": {"enabled": true, "types": ["session.started", "todo.added"]},
    "todos":    {"enabled": true, "statuses": ["blocked"]},
    "sharing":  {"enabled": true},
    "issues":   {"enabled": true, "states": ["open"]},
    "connections": {"enabled": true}
  },
  "cursors": {"timeline": "<ts>", "sharing": "<ts>",  // optional; absent = no cursor
              "issues": "<ts>", "connections": "<ts>"},
  "items": [
    { "id": "a1b2c3d4",                          // 8 lowercase hex, unique in the file
      "ts": "2026-09-10T11:59:30Z",              // when it arrived (ISO-8601 UTC)
      "source": "timeline",                      // [a-z0-9_:-]{1,40}
      "kind": "session.started",                 // [a-z0-9_.:-]{1,60}
      "title": "Session started in xo-space (claude_code)",   // 1 to 300 chars
      "body": "",                                // up to 4000 chars
      "project_id": "xo-space",                  // optional project folder name
      "link": {"view": "sessions"},              // optional: view, project, path
      "url": null,                               // optional: http(s) address, up to 2000 chars, else null
      "status": "new",                           // new | seen | done
      "key": "timeline:session.started:<session_id>" }   // dedup identity; API items carry none
  ]
}
```

Items are kept newest-first. Retention runs on every write (constants in
`store.py`): `DONE_TTL_DAYS = 30` prunes done items older than that, then
`MAX_ITEMS = 500` drops the oldest done items first, then the oldest of the
rest.

### Feeders (`services/cowork_agent/inbox/feeders.py`)

Best-effort and idempotent. An item whose `key` already exists is updated in
place (title, body, link, url; status is never reset) and never duplicated. A
feeder that throws is logged and skipped for that run while the others still
run. A source with `enabled: false` is never read.

| Feeder | Reads | Default | Cursor | Produces |
|---|---|---|---|---|
| `timeline` | `~/.quirq/workspace/timeline.jsonl` (the runtime-tier workspace timeline), the newest 500 events of the enabled types | `types: ["session.started", "todo.added"]`; `todo.completed`, `file.created`, `file.edited` can be added | `cursors.timeline`, the newest event timestamp seen; with no cursor only the last 24 hours are taken | `Session started in <project> (<runtime>)` linking to Sessions; `Todo added in <project>: <content>` linking to Files |
| `todos` | every `<project>/.xo/todos.json` | `statuses: ["blocked"]` | none | `Todo blocked in <project>: <content>` (kind `todo.blocked`, linking to Files); the item is set to done by itself once the todo leaves the watched status or disappears |
| `sharing` | the in-memory relay status (the `recent` list of `GET /api/project-sharing/status`) | on | `cursors.sharing` | `Repo shared with this workspace: <repo>`, `New commits fetched: <repo>`, `Sharing error: <repo>`, `Sharing access revoked: <repo>`, with the relay detail as body, linking to Files |
| `issues` | every project's GitHub issue mirror, `~/.quirq/projects/<pid>/github/issues.json` (written by the GitHub issue poller) | `states: ["open"]`; `closed` can be added | `cursors.issues`, the newest `updated_at` seen across every readable mirror; with no cursor only the last 7 days are taken | `Issue #<number> in <project>: <title>` (kind `issue.<state>`, key `issue:<project>:<number>`, labels and assignees as body, the issue URL as `url`, linking to Files); the item is set to done by itself once the issue leaves a watched state, but only on a run where every mirror was readable, so a transient read failure never closes real issues |
| `connections` | the newest 200 lines of `~/.quirq/connections/<toolkit>/events.jsonl` for every polled toolkit (see Connections polling below) | on | `cursors.connections`, one cursor across every toolkit, the newest event `ts` seen; with no cursor only the last 24 hours are taken | one item per event: the event title, body, and `url`, kind `<toolkit>.<collector>`, key `connection:<toolkit>:<collector>:<id>`, linking to Connectors |

The relay list restarts empty with the server, so a persisted sharing cursor
never re-ingests old events. The connections cursor is shared across
toolkits: a toolkit polled for the first time whose events are all older
than the cursor surfaces nothing until it collects something newer (delete
`cursors.connections` to take the last 24 hours of every toolkit again).

### Connections polling

The `connections` feeder reads what a background poller collected from the
Composio connections (Gmail, Google Calendar, Notion) over the same MCP
upstream the agent proxy uses. Everything lives in
`services/cowork_agent/connections/` (store, collectors, mcp_client, poller,
service) and in one folder per toolkit, hand-maintainable in the same spirit
as `inbox.json`:

Each poll lists the session's tools once. A slug the session exposes is called
directly; Composio's tool-router session lists only its meta tools, so the
collector runs through `COMPOSIO_MULTI_EXECUTE_TOOL` and the per-tool result is
unwrapped to the same shape. A 404 on `initialize` means the session behind the
cached MCP url is gone upstream: the poller invalidates it and retries once with
a fresh one. "Poll now" waits briefly for a running tick instead of reporting busy.

```
~/.quirq/connections/<toolkit>/      # gmail, googlecalendar, notion
  config.json     # what to collect and how often; hand-editable
  state.json      # last_poll_at, last_ok_at, last_error, seen keys per collector, events_total
  events.jsonl    # one line per collected item, append-only; rotated at 2 MB, three rotations kept
```

```jsonc
{
  "schema": 1,
  "toolkit": "gmail",
  "enabled": true,             // false keeps the folder but stops polling
  "interval_s": 900,           // 60 to 86400; the drawer offers 5 min to 24 h
  "collectors": ["unread"],    // catalog ids for that toolkit; unknown ids are dropped on read
  "updated_at": "2026-09-11T12:00:00Z"
}
```

Collectors are read-only tools from the catalog in `collectors.py`: `gmail`
`unread` (default) and `inbox`, `googlecalendar` `upcoming` (default),
`notion` `recent_pages` (default); every other toolkit has none yet and the
drawer says so. The poller only ever polls a toolkit that has a
`config.json`, dedups by the seen keys in `state.json` (the newest 500 per
collector), and records failures in `last_error` instead of raising: not
signed in to XO, the toolkit not turned on in this workspace, or one
collector the upstream rejected while the others still run. `events.jsonl`
keeps everything the poller ever collected; the Inbox surfaces only events
newer than its 24 hour bootstrap floor and never reads the rotated files.

Routes (`routers/cowork_agent/bff/connections.py`, no session header):

- `GET /api/connections` answers `{signed_in, poller_enabled, connections:
  [...]}`, one entry per known toolkit with `configured`, `enabled`,
  `interval_s`, `collectors`, `available_collectors`, `connected_here`,
  `last_poll_at`, `last_ok_at`, `last_error`, `events_total`.
- `GET /api/connections/{toolkit}` answers that entry; `PUT` with
  `{enabled?, interval_s?, collectors?}` creates the folder on first save and
  merges the given fields (400 `invalid_interval`, `invalid_collector`);
  `DELETE` removes the folder and answers `{toolkit, removed}`.
- `POST /api/connections/{toolkit}/poll` runs the collectors at once and
  answers `{toolkit, polled, new_events, error, skipped}`.
- `GET /api/connections/{toolkit}/events?limit=1..500` answers the newest
  events first as `{toolkit, events}`.

An unknown toolkit is a 404 `unknown_toolkit`. The Connectors tab drives
these from its Polling drawer, which opens by itself after a connect.

Environment: `XO_CONNECTIONS_POLL_ENABLED` (default `true`, the hard off
switch for the background loop; Poll now still works) and
`XO_CONNECTIONS_POLL_TICK_S` (default 30, minimum 5: how often the loop looks
for connections whose interval has elapsed).

### Hand-editing

The store tolerates edits: missing keys get defaults, unknown top-level and
per-item keys survive a rewrite, items without a title or a valid id are
dropped with a warning, an invalid status becomes `new`.

- Disable a source: set `sources.<name>.enabled` to `false`.
- Re-read a source: delete `cursors.<name>`. The timeline and connections
  feeders then take the last 24 hours again and issues the last 7 days;
  sharing re-reads whatever the relay still holds, deduped by key.
- Watch more: add types to `sources.timeline.types`, statuses to
  `sources.todos.statuses`, or states to `sources.issues.states`.
- Remove items: delete them from `items`, or mark them `done` and let
  retention prune them. A feeder item you delete comes back while its source
  still reports it.

## Data format (`/xo/space.json`)

```jsonc
{
  "meta":       { "title", "tagline", "mappedOn", "workspace" },
  "categories": { "p_<project>": {"name": "...", "color": "#a2b56b"}, ... },
  "hubAngles":  { "p_<project>": -1.57, ... },      // radians, one region per project
  "timeline":   { "start": "2026-01-27", "end": "2026-07-20" },
  "root":       { "id": "xo", "label", "blurb" },
  "hubs":       [ { "id", "cat", "label", "blurb" } ],          // one per project
  "groups":     [ { "id", "cat", "label", "blurb" } ],          // one per top-level dir
  "leaves":     [ { "id", "group", "shape", "tag", "label",
                    "date", "blurb", "path" } ],                // one per file
  "ties":       [ { "s", "t", "label" } ],      // derived cross-links (see below)
  "milestones": [ { "d": "YYYY-MM-DD", "t": "caption" } ],      // first commits
  "gitHistory": { "p_<project>": [ { "d": "YYYY-MM-DD", "n": 3,
                    "s": ["subject", "…"] } ] }  // commits/day per project (optional)
}
```

`gitHistory` feeds the Timeline's **By project** mode: one lane per project,
one dot per commit day (`n` commits, up to 3 sampled subjects in `s`). The
mode toggle only renders when at least one project carries history; the
Dashboard projection and non-git projects have none.

Shapes are semantic: `disc` = code, `ring` = document, `diamond` = everything
else. Leaf `date` is the git first-added date, or `null` when git does not
know the file (untracked, or a non-git project); undated leaves appear on the
graph but sit out the timeline. Tree edges (leaf → cluster → project → root)
are derived by the UI; only cross-ties are listed.

Ties are derived facts, never editorial: files that repeatedly share commits
("changed together ×N", from the same git log that dates the leaves), docs
whose text names another file's relative path ("references"), and
`test_x` ↔ `x` filename pairs ("tests"). Strongest first, capped at 60.
