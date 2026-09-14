# Space: the workspace knowledge graph UI

An explorable map of `~/xo-projects`. Five top-level tabs: **Projects**
(Dashboard | List | Graph | Tree | Sharing | Timeline lenses under one tab),
**Agents**, **Inbox**, **Setup**, and **Connectors**, plus the
**Quirq** state view, which has no tab of its own and opens from Setup's header.

Space opens on Dashboard (`#/dashboard`) with Projects highlighted. Clicking
the Projects tab or pressing `1` opens List (`#/projects`); existing deep links
to `#/graph`, `#/tree`, `#/sharing`, and `#/time` (Timeline) keep their
meanings. The numbered shortcuts follow the top bar: Projects `1`, Agents
`2`, Inbox `3`, Setup `4`, Connectors `5`. Timeline is a lens of the Projects
tab, reached from the Projects lens switch rather than a numbered shortcut.

**Wiki** and **GitHub** stay at the top right across views. Wiki opens the local
`#/wiki` overview in the same tab; GitHub opens in a new tab. Wiki has no numbered
shortcut. Its three-step quickstart and topic cards link to the
[full Space guides](https://docs.quirq.ai/docs/space), which open online in a new
tab. Existing first-run and storage-help actions focus the matching overview
section. The overview itself works offline.

The toolbar adapts to the active page. Dashboard and Graph keep the root
picker and map autocomplete. List, Tree, Timeline, Connectors, Inbox, and
the Sessions list have their own search; typing there keeps you on that page.
Setup, Wiki, Sharing, Quirq, and the Sessions charts/detail have no search
toolbar. On phones these pages also give back the empty toolbar row.

| Page | Search scope |
|------|--------------|
| Projects List | Project names in the loaded catalog. |
| Tree | Folder and file names, keeping the ancestors of matches visible. |
| Timeline | Project names; the selected timeline mode and date range still apply. |
| Connectors | Toolkit name, identifier, description, and resolved connected account label. Filtering preserves open controls and unsaved polling edits. |
| Inbox | Title, body, kind, source, and project in the loaded status page, intersected with the source filter. The matching count shows this scope. |
| Sessions list | Project, path, source, model, and session ID in the loaded sessions, intersected with the selected sources. Matching counts distinguish loaded rows from the total. |

Each page remembers its query while you navigate within the app; a full
reload resets it. Press `/` outside an editable control to focus the visible
search. In a page search, `Escape` clears the query; pressing it again removes
focus. The clear button does the same reset. When Inbox is narrowed, **Mark all
loaded seen** explicitly includes loaded new items hidden by search or source
filters.

This folder is a bundled snapshot of the xo-atlas UI (originally a standalone
folder with no remote), trimmed to the single endpoint-driven page and served
by this API, so every workspace that runs xo-space gets the graph with
zero configuration.

## Files

Build-free ES modules (no bundler, no dependencies); the browser loads them
directly. Descended from the single-file xo-atlas `v3.html`.

| Path | What it is |
|------|------------|
| `index.html` | Thin shell: markup + stylesheet links + an import map + the `js/app.js` entry. The import map is where `core/api.js`, `core/ui.js` and `core/connections.js` get their cache stamp: views import those three bare, the map rewrites every such import to one `?v=` URL (one module instance, fetched fresh after a bump); every other core module keeps the stamp on its import line. |
| `css/` | The original stylesheet split at its section banners, loaded in original order (cascade unchanged). |
| `js/app.js` | Entry point. Registers views; **adding a view = one new file in `js/views/` + one import line here.** |
| `js/core/registry.js` | View registry: tab nav, `1..n` hotkeys (ignored while an input, textarea or select has focus), `#/<id>` hash routing, lazy mount, per-view failure isolation. |
| `js/core/toolbar.js` | Shared toolbar: renders the active view's controls, closes hidden map menus, restores page queries, and owns the `/` focus shortcut. |
| `js/core/api.js` | The one fetch layer: `API_BASE`, query-string auth forwarding, offline / HTTP-error / 501 classification, single-flight GETs, and `failText(res)`, the one wording for a failed result ("xo-space is unreachable", "not available for the active agent", or the HTTP error) that every tab shows. |
| `js/core/store.js` | Idempotency helpers: single-flight promises, slotted (non-stacking) intervals. |
| `js/core/ui.js` | Shared UI helpers: `toast`, `esc` (HTML escaping for every interpolated value), `rel` (relative time; empty for a missing stamp), `pills` (a filter strip of `data-<attr>` buttons with `is-on` / `aria-pressed`). |
| `js/core/connections.js` | Pure formatters over one `GET /api/connections` entry: `every` (cadence), `collectorLabels`, `pollLine` (last poll or the error). Shared by the Inbox's Connections section and the Connectors tab so both read the same. |
| `js/core/server-widget.js` | Footer server pill (status poll + terminal start hint). |

| `js/core/preview.js` | File previewer drawer. Any view opens it with a `space:preview-file` event; markdown renders through `markdown.js`, HTML renders in an empty-`sandbox` iframe, everything else as escaped source. |
| `js/views/atlas.js` | Dashboard + Graph + Timeline: three lenses over one dataset, one shared closure, three exported views. |
| `js/views/sessions.js` | The Agents view (tab id `agents`, route `#/agents`): session telemetry from `/xo/sessions.json`, contributed by whichever backends implement the `session_telemetry` capability. The module file keeps its `sessions.js` name; the data file `sessions.json` and the internal Sessions sub-view are session telemetry, not the tab. |
| `js/views/inbox.js` | The Inbox view: what arrived in the workspace (new sessions, blocked todos, shares, anything POSTed to `/api/inbox`) as new / seen / done rows, plus the unread badge on the tab button (`initInboxBadge`). Styled by `css/inbox.css`, its own `.inb-*` classes. |
| `js/views/projects.js` | The Projects List lens: project list with per-project drawers (folder browser via `/tree`, todos, open sessions, recent events, and the project's GitHub issues via `/github/issues`). Todos are read *and written* through `/api/xo-projects/{id}/todos`, the only write path for any runtime. Owns the `Projects` tab; Dashboard, Graph, Tree, and Sharing are sibling lenses (`nav:false`, `parent:'projects'`). |
| `js/views/tree.js` | The Projects Tree lens: horizontal hierarchy over the same `/xo/space.json` dataset as Graph: folders as columns, files stacked beside their parent. Deep-link `#/tree`. |
| `js/views/chat.js` | The Chat view: Plane-B chat (`/api/chat/prompt` → SSE stream → transcript refetch) with session sidebar, project binding for new sessions, and mini-markdown rendering. Works across claude_code / hermes / openclaw. Deliberately unregistered: no tab. |
| `js/views/wiki.js` | The compact Wiki overview: local quickstart/view actions and links to detailed online guides. Opens from the header resource link (`nav:false`, `#/wiki`), with no primary tab. Legacy `space:wiki-page` requests focus the matching topic without replacing the overview. |
| `js/views/quirq.js` | The Quirq view: machine-local `.quirq` state (watcher infrastructure and the derived runtime tier) beside the durable project `.xo` output. Its file rows come from `services/cowork_agent/quirq_catalog.py`, which is data-driven: a file that moves root without a catalog entry to match renders as `0 present`. No tab of its own: `nav:false, parent:'secrets'`, opened from Setup's header button (`#/quirq`). |
| `js/views/secrets.js` | The Setup view: storage roots, agent runtime, watcher coverage, write-only credentials, git self-update, server restart and saved commands. |
| `js/views/setup-commands.js` | Setup Commands card: definition form, run controls, live results and history drawer over `/api/schedules`. |
| `js/core/command-results.js` | Shared command Inbox/results drawer used by Setup and Inbox Jobs, including output, status, working directory and log path. |
| `js/views/connectors.js` | The Connectors view: Composio toolkits, connect / disconnect, the Actions drawer and the Polling drawer (`PUT /api/connections/{toolkit}`). The Polling drawer keeps unsaved edits across the repaints Refresh, the Actions drawer and a connect landing cause; Save repaints from the server's copy, and closing the drawer (Hide, opening another toolkit's drawer, turning the toolkit off, disconnect) discards them. The only view that authenticates (`js/core/session.js`). |

| `js/core/markdown.js` | Escape-first mini-markdown (fences, inline code, bold/italic, links, headings, lists). |

The view contract (`id`/`label`/`order`/`nav`/`parent`/`section`/`toolbar`, mount/show/hide)
is documented in the header comment of `js/core/registry.js`; repo-wide working
rules are in the root `AGENTS.md`.

`toolbar` is an object or function returning `{graph: true}` for the map
controls, `{search: {placeholder, getValue, setValue}}` for page search, or
`null` for no controls. A descriptor can set `disabled` while loading; a search
can supply an accessible `label`. Views own query state and filtering, and
call `ctx.refreshToolbar()` when a subview or load changes the available
controls. The registry ignores refreshes from inactive views and waits for
mount to finish before showing the latest requested view.

## How it's served

`routers/space.py` mounts this folder read-only at `/space`, so the app is at
`http://localhost:5002/space/`. The three datasets the page fetches are
**files on disk**, served by `routers/xo_data.py` at `/xo/space.json`,
`/xo/dashboard.json` and `/xo/sessions.json`. The watcher materialises them
from a walk of `~/xo-projects`; a request rebuilds one on demand when it is
missing or older than `XO_VIEW_MAX_AGE_S`, so the page still works with the
watcher switched off. If a build fails the route answers 503 and the app
shows its "no data source" panel: a truthful error panel beats a
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
to 60 units: generated data can put 100+ leaves in one cluster, whose summed
spring stiffness makes the original explicit-Euler sim diverge (positions hit
1e20 and the canvas goes blank).

## Setup tab: restart and commands

**Restart server** lives in the hero beside **Refresh status**. **Apply & restart**
and the self-update card use the same `/space/server/restart` route. Restart takes
a few seconds; the footer pill may go offline before it returns. The page reloads
when a new server instance responds, so every tab loads the updated code.

| `restart_mode` | How it works |
|---|---|
| `managed` | SIGTERM lets the container supervisor restart the server. |
| `native` | The pid file belongs to `./cowork-api.sh start`; a detached `cowork-api.sh restart-owned` helper checks ownership and restarts only that installation. |
| `foreground` | No supervisor or matching pid file: the button is disabled with “Ctrl-C and re-run”. The route returns 409. |

The footer still has no process start control: its Start hint copies a terminal
command. Process restart belongs on Setup.

**Commands** starts empty. Use **Add command** to save a name, optional description,
command line or argv JSON, optional working directory, required timeout and optional
interval. Leave the interval blank for manual-only execution. A command line is
split without a shell; validation errors appear in the card. Interval jobs show a
“Runs every N” chip and use the watcher. **Edit** preserves existing environment,
project and enabled settings.

**Run** executes through the command utility and disables while running. The card
polls the job every three seconds until the status and duration appear. The row
shows its configured working directory and a preview of the latest result.
**Inbox** opens the latest 20 results with escaped output, exit codes, timing,
and a copyable full-log path. The drawer updates while a command runs and also
offers Refresh. A concurrent run or a full shared execution limit returns 409.
Restart, command writes and runs require a local client; browser requests must come from the same loopback origin. Remote requests receive 403.

Definitions and every result stay under `<quirq state>/scheduler/`:

```text
scheduler/
├── jobs.json          # saved commands, intervals and descriptions
├── state.json         # next run, running since, last result
├── runs/<id>.jsonl    # append-only history, 2000-character output tails
└── logs/<id>.log      # full output from every run
```

Deleting a command keeps its history and logs on disk and does not cancel an active process. Commands run locally with
the server's environment, including when the watcher is disabled for manual runs.

## Agents tab

The second topbar tab (`Projects | Agents | Inbox | Setup |
Connectors`) is a session-telemetry dashboard: per-session stats rendered as cards,
tables, and hand-drawn canvas charts (no dependencies), re-skinned to the
Space theme. The payload is assembled from every backend that implements the
`session_telemetry` capability, so a runtime that reports nothing shows as
"not available" rather than as a zero. It lives in its own module
(`js/views/sessions.js`), independent of the atlas's `boot()`; either can
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
- No alerts and no prompts by design: those tables are never read, so raw
  prompt text never enters the payload.

## Inbox tab

The fourth topbar tab is where information arriving in the workspace is seen,
tracked, and acted on. One human-readable JSON file is the source of truth for its items, a
small service feeds and edits it, five HTTP routes serve it, and one view
module (`js/views/inbox.js`, styled by `css/inbox.css`) renders it. The tab
button carries an unread badge (`counts.new`: polled every 60 s while another
tab is shown; while Inbox is open the view's own 30 s read feeds it).

**Jobs** sits immediately below Connections and reads `/api/schedules`
independently. It lists every command with an interval, including disabled jobs,
with its cadence, enabled state, next due time and latest/running status.
**Results** opens the same command Inbox used by Setup; **Open Setup** returns
to command management. Manual-only commands remain in Setup. Jobs refresh on
entry, through either Refresh button, and every 30 seconds while visible
(every three seconds while a listed job is running). This section neither runs
commands nor creates Inbox items, and item search, filters and unread counts
retain their existing scope.

- Data: `GET /api/inbox?status=open|done|all&limit=N` (defaults `open`, 200;
  `limit` 1 to 500). The reply is `{schema, updated_at, counts: {new, seen,
  done}, items: [...]}`: counts always cover the whole file, items are newest
  first. Every read runs the feeders first (throttled to once per 5 s per
  process; the throttle is stamped whether or not a feeder fails, so a broken
  feeder is retried once per 5 s, not on every read) and a feeder failure
  never fails the read. The view polls every 30 s while shown (skipping the
  repaint when nothing changed, restoring focus when it did) and re-fetches
  after every write.
- Writes: `POST /api/inbox` `{title, body?, kind?, source?, project_id?,
  link?, url?}` answers 201 with the item; `PATCH /api/inbox/{id}` `{status}`
  answers the item; `PATCH /api/inbox` `{ids, status}` (1 to 500 ids) sets
  many in one locked write and answers `{updated, missing}` (`updated` counts
  items whose status changed, `missing` lists malformed or absent ids in
  request order; idempotent), which is what Mark all seen sends, once per
  page; `DELETE /api/inbox/{id}` answers `{item_id, deleted}` and is
  idempotent. Ids are 8 lowercase hex; a malformed id is a 404
  `item_not_found`. Bodies are strict: a missing `title`, an unknown key,
  `ids` that is not a list of strings, or `limit` outside 1..500 is a 422
  (pydantic). The service's own failures are 400 with `{code, message}`:
  `invalid_value` (an empty or overlong title, body, kind, source, url; an
  empty or oversized `ids`), `invalid_project_id`, `invalid_link`,
  `invalid_status`. A status set through either PATCH is a person's: it drops
  `auto_closed` (below), so the feeders never undo it.
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
- Open follows `link`: `{project, path}` switches to Projects and opens the file
  previewer; `{view}` switches to that tab; `{project}` alone switches to
  Projects.

### The file: `~/.quirq/inbox.json`

Machine-local, under the Quirq state root (`QUIRQ_STATE_ROOT`), next to the
polled connections: what a person has seen or done is this install's state,
not something a project folder should carry into git or a sync. Written only
by `services/inbox/store.py` (atomic write under `services/storage/flock.locked`,
the same mechanism the todo API uses) and read with
`services/storage/reader.read_json`. It appears on the first
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
      "link": {"view": "agents"},                // optional: view, project, path
      "url": null,                               // optional: http(s) address, up to 2000 chars, else null
      "status": "new",                           // new | seen | done
      "auto_closed": true,                       // optional, only ever true, only with status done: a feeder closed it (see Hand-editing)
      "key": "timeline:session.started:<session_id>" }   // dedup identity; API items carry none
  ]
}
```

Items are kept newest-first. Retention runs on every write (constants in
`store.py`): `DONE_TTL_DAYS = 30` prunes done items older than that, then
`MAX_ITEMS = 500` drops the oldest done items first, then the oldest of the
rest.

### Feeders (`services/inbox/feeders.py`)

Best-effort and idempotent. An item whose `key` already exists is updated in
place (title, body, link, url; status is never reset) and never duplicated. The
one exception to "never reset": an item a feeder itself set to done
(`auto_closed: true`, from the todos and issues feeders' auto-close) goes back
to `new` with the reported `ts` when its key is reported again, so a reopened
issue or a re-blocked todo resurfaces at the top; a done a person set carries
no flag and stays done. A feeder that throws is logged and skipped for that
run while the others still run. A source with `enabled: false` is never read.

| Feeder | Reads | Default | Cursor | Produces |
|---|---|---|---|---|
| `timeline` | `~/.quirq/workspace/timeline.jsonl` (the runtime-tier workspace timeline), the newest 500 events of the enabled types | `types: ["session.started", "todo.added"]`; `todo.completed`, `file.created`, `file.edited` can be added | `cursors.timeline`, the newest event timestamp seen; with no cursor only the last 24 hours are taken | `Session started in <project> (<runtime>)` linking to Agents; `Todo added in <project>: <content>` linking to Projects |
| `todos` | every `<project>/.xo/todos.json` | `statuses: ["blocked"]` | none | `Todo blocked in <project>: <content>` (kind `todo.blocked`, linking to Projects); the item is set to done by itself (flagged `auto_closed`) once the todo leaves the watched status or disappears, and comes back as new if the todo is blocked again |
| `sharing` | the in-memory relay status (the `recent` list of `GET /api/project-sharing/status`) | on | `cursors.sharing` | `Repo shared with this workspace: <repo>`, `New commits fetched: <repo>`, `Sharing error: <repo>`, `Sharing access revoked: <repo>`, with the relay detail as body, linking to Projects |
| `issues` | every project's GitHub issue mirror, `~/.quirq/projects/<pid>/github/issues.json` (written by the GitHub issue poller) | `states: ["open"]`; `closed` can be added | `cursors.issues`, the newest `updated_at` seen across every readable mirror; with no cursor only the last 7 days are taken | `Issue #<number> in <project>: <title>` (kind `issue.<state>`, key `issue:<project>:<number>`, labels and assignees as body, the issue URL as `url`, linking to Projects); the item is set to done by itself (flagged `auto_closed`) once the issue leaves a watched state, but only on a run where every mirror was readable, so a transient read failure never closes real issues; a reopened issue comes back as new once its `updated_at` passes the cursor |
| `connections` | the newest 200 lines of `~/.quirq/connections/<toolkit>/events.jsonl` for every polled toolkit (see Connections polling below) | on | `cursors.connections`, one cursor across every toolkit, the newest event `ts` seen; with no cursor only the last 24 hours are taken | one item per event: the event title, body, and `url`, kind `<toolkit>.<collector>`, key `connection:<toolkit>:<collector>:<id>`, linking to Connectors |

The relay list restarts empty with the server, so a persisted sharing cursor
never re-ingests old events. The connections cursor is shared across
toolkits: a toolkit polled for the first time whose events are all older
than the cursor surfaces nothing until it collects something newer (delete
`cursors.connections` to take the last 24 hours of every toolkit again).

### Connections polling

The `connections` feeder reads what a background poller collected from the
Composio connections (Gmail, Google Calendar, Notion, Slack, Telegram) over the same MCP
upstream the agent proxy uses. Everything lives in
`services/connections/` (store, collectors, mcp_client, poller,
service) and in one folder per toolkit, hand-maintainable in the same spirit
as `inbox.json`:

Each poll opens one MCP session (the handshake once), lists its tools once,
runs every collector's call on it and closes it. A slug the session exposes is
called directly; Composio's tool-router session lists only its meta tools, so
the collector runs through `COMPOSIO_MULTI_EXECUTE_TOOL` and the per-tool
result is unwrapped to the same shape. A 404 on `initialize` means the session
behind the cached MCP url is gone upstream: the poller invalidates it and
retries once with a fresh one. A session that dies mid-poll (a collector's call
answers 404) ends that poll: the collectors after it are recorded as "not
attempted, the MCP session died mid-poll" and the next poll starts afresh.
"Poll now" waits briefly for a running tick instead of reporting busy.

```
~/.quirq/connections/<toolkit>/      # gmail, googlecalendar, notion, slack, telegram
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
`unread` (default) and `inbox`, `googlecalendar` `upcoming` (default; every calendar in the account's list, not `primary` alone),
`notion` `recent_pages` (default), `slack` `recent` (messages from the last
day, default), `telegram` `updates` (new messages to the bot, default; no
links, since private chats have no permalink); every other toolkit has none
yet and the drawer says so. The poller only ever polls a toolkit that has a
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
- Make a done stick: an item carrying `"auto_closed": true` was closed by its
  feeder (the todo unblocked, the issue closed) and is reopened, with the
  reported `ts`, when its key returns. Delete the key, or set the status
  yourself through PATCH (which drops it), and the done stays. Any value other
  than `true` is dropped on read.

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
