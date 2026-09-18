# Space UI

An explorable map of `~/xo-projects`. Four primary sections share the same
navigation structure: **Projects**, **Agents**, **Inbox**, and **Setup**.
Primary links open a section's default page; the secondary links open its
pages and can be copied, opened in another tab, or revisited with Back/Forward.

| Section | Default route | Pages |
|---------|---------------|-------|
| Projects (`1`) | `#/projects/overview` | Overview, Data (List, Graph, Tree), Timeline, Manage |
| Agents (`2`) | `#/agents/overview` | Overview, Sessions, Trends, Configure |
| Inbox (`3`) | `#/inbox/items` | Items, Connections, Jobs, Activity, Sharing activity, Sharing |
| Setup (`4`) | `#/setup/workspace` | Workspace, Intelligence layer, Connectors, Secrets, Jobs, Server |

Space starts at Projects Overview. **Data** contains the existing List, Graph
and Tree views at `#/projects/data/list`, `#/projects/data/graph` and
`#/projects/data/tree`. The List / Graph / Tree links sit in each view’s local
toolbar. The Data section link remembers the last view used; a direct
`#/projects/data` link opens List. Clicking Projects or pressing `1` always
opens Overview. The section roots
`#/projects`, `#/agents`, `#/inbox`, and `#/setup` normalize to their defaults.
Legacy `#/dashboard`, `#/graph`, `#/tree`, and `#/time` links open
the corresponding Projects page. `#/sharing` and `#/projects/sharing` now open
Inbox Sharing at `#/inbox/sharing`. The previous `#/projects/list`,
`#/projects/graph` and `#/projects/tree` links also remain valid and normalize
to the corresponding Data route. The former `#/projects/files`,
`#/projects/files/list`, `#/projects/files/graph` and `#/projects/files/tree` addresses
remain aliases for Data. Connectors and Secrets keep their aliases
`#/connectors` and `#/secrets`. Technical details is a child of Setup Server at
`#/setup/server/details`; `#/quirq` remains an alias. Stored Inbox links using
`view: "projects"` continue to open List; that API value is independent of the
Projects section URL.

**Wiki** and **GitHub** stay at the top right across views. Wiki opens the local
`#/wiki` overview in the same tab; GitHub opens in a new tab. Wiki has no numbered
shortcut. Its three-step quickstart and topic cards link to the
[full Space guides](https://docs.quirq.ai/docs/space), which open online in a new
tab. Existing first-run and storage-help actions focus the matching overview
section. The overview itself works offline.

The toolbar adapts to the active page. The Cmd+K search trigger sits with
**Wiki** and **GitHub** in the top-right cluster on every page, including Overview and Graph. Every Projects page keeps **Graph root** and
**Refresh** together in the section bar. **Manage** is a Projects page at
`#/projects/manage`; its **Add project** button opens the clone form. The old
`#/setup/projects` link opens Manage. Cards start collapsed; one card opens at a time to show
metadata and Issues, while inline sharing drafts stay mounted. Copy icons
beside recorded metadata copy its exact value; tooltips and keyboard focus identify
each action. **View activity**, **Share**, **Pin**, **Copy GitHub URL** and **Remove**
are grouped in each card header and work while collapsed. Pins keep their existing
browser storage and feed Data List’s **Pinned** filter, including across open tabs. Refresh rereads the active page’s data without reloading the app, retaining its
query, selected root and existing project drawers. Inbox Sharing keeps **Share a project**,
**Check now**, and **Refresh** beside its own navigation.
Each Manage project card has **Share**, which opens a Space ID form
in that card. Cancel keeps you on the page; submitting grants access to that
Space ID. Drafts stay with their project while you filter, refresh or navigate.
Choosing a node from Data List, Data Tree, Manage or
Timeline opens Data Graph rooted on that node. The secondary navigation does
not repeat primary section labels. Projects page descriptions are removed to leave more room
for graphs and content; List keeps its counts and actions in a compact row. List, Tree, Timeline, Setup, Inbox Items, both activity pages, and
the Agents session list have their own search; typing there keeps you on that page.
Wiki, Sharing, Quirq, Inbox Connections/Jobs, and the Agents charts/detail have no
page-search field; they still show the Cmd+K trigger.

| Page | Search scope |
|------|--------------|
| Projects List | All search words match across project name, folder ID and description. Combines with the Filter menu’s All projects, Live or Pinned options beside Sort by. |
| Tree | Folder and file names, keeping the ancestors of matches visible. |
| Timeline | Project names; the selected timeline mode and date range still apply. |
| Setup | Setting names and topics. Choose a result to open its section; searches never read field values or credentials, and all unfinished forms stay mounted. |
| Setup → Connectors | Workspace integrations and account apps by name, identifier, description, and connected account label. Filtering preserves open controls and unsaved edits. |
| Inbox → Items | Title, body, kind, source, and project in the loaded status page, intersected with the source filter. The matching count shows this scope. |
| Inbox → Activity | Loaded workspace event labels, details, project names/IDs, runtime and session ID, intersected with the project selector. Load older adds more events to this search. |
| Inbox → Sharing activity | Loaded relay event labels, details and repository names, intersected with the repository selector. |
| Sessions list | Project, path, source, model, and session ID in the loaded sessions, intersected with the selected sources. Matching counts distinguish loaded rows from the total. |

Each page remembers its query while you navigate within the app; a full
reload resets it. Press `/` outside an editable control to open the command
palette on Graph and on pages that have search. In a page search, `Escape` clears the query; pressing it again removes
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
| `fonts/inter/` | Inter variable font (upright and italic) with its SIL OFL license. `css/base.css` loads it as `--sans`; `--mono` stays the system monospace and is only for code, commands, logs and IDs. |
| `js/app.js` | Entry point. Registers views; **adding a view = one new file in `js/views/` + one import line here.** |
| `js/core/registry.js` | View registry: primary section links, `1..n` hotkeys (ignored while editing), canonical hash routes and aliases, history, lazy mounts, per-view refresh and failure isolation. Primary sections are configured independently of their pages. |
| `js/core/navigation.js` | Primary sections and their page definitions, canonical routes, labels and stable view IDs. |
| `js/core/project-actions.js` | The Add handoff opens Manage’s clone form only after current navigation completes. The legacy Setup-project event opens Manage without opening Add. |
| `js/core/project-share.js` | Reusable inline Space ID form for Manage project cards, with draft retention, pending-state protection, and the existing share endpoint. |
| `js/core/timeline-summary.js` | Counts mapped files or loaded commits in the selected project lanes and date window; playback and trace dimming do not alter those totals. |
| `js/core/project-pins.js` | Browser-local project pins shared by Manage actions and Data filtering, with storage-event synchronization and an in-memory fallback. |
| `js/core/data-views.js` | Native List, Graph and Tree links shared by the local Data toolbars. |
| `js/core/section-nav.js` | Shared secondary navigation and a slot for stable view-owned actions; native links mark the active page. |
| `js/core/project-root.js` | Root picker shared by all Projects pages. Reads node metadata independently of the canvas; a selection opens the appropriate graph, while stale reads cannot reopen the picker after navigation. |
| `js/core/toolbar.js` | Shared toolbar: Cmd+K trigger in the navbar search slot, active-filter page search, and the `/` shortcut that opens the palette. |
| `js/core/api.js` | The one fetch layer: `API_BASE`, query-string auth forwarding, offline / HTTP-error / 501 classification, single-flight GETs, and `failText(res)`, the one wording for a failed result ("xo-space is unreachable", "not available for the active agent", or the HTTP error) that every tab shows. |
| `js/core/store.js` | Idempotency helpers: single-flight promises, slotted (non-stacking) intervals. |
| `js/core/ui.js` | Shared UI helpers: `toast`, `esc` (HTML escaping for every interpolated value), `rel` (relative time; empty for a missing stamp), `pills` (a filter strip of `data-<attr>` buttons with `is-on` / `aria-pressed`). |
| `js/core/connections.js` | Pure formatters over one `GET /api/connections` entry: `every` (cadence), `collectorLabels`, `pollLine` (last poll or the error). Shared by the Inbox's Connections section and Setup Connectors so both read the same. |
| `js/core/server-widget.js` | Footer server pill (status poll + terminal start hint). |
| `js/core/preview.js` | File previewer drawer. Any view opens it with a `space:preview-file` event; markdown renders through `markdown.js`, HTML renders in an empty-`sandbox` iframe, everything else as escaped source. |
| `js/views/atlas.js` | Projects Overview, Graph and Timeline. Changing projections rebuilds only the atlas engine, disposes its listeners and frames, and ignores superseded reads; the document and other mounted pages remain intact. |
| `js/views/sessions.js` | Five Agents routes under `#/agents/`, sharing one mounted telemetry view: session telemetry from `/xo/sessions.json`, contributed by whichever backends implement the `session_telemetry` capability. The module file keeps its `sessions.js` name; the data file `sessions.json` and the internal Sessions sub-view are session telemetry, not the tab. |
| `js/views/inbox.js` | Three Inbox routes (`items`, `connections`, `jobs`) share a mounted controller. Items shows what arrived in the workspace (new sessions, blocked todos, shares, anything POSTed to `/api/inbox`) as new / seen / done rows, plus the unread badge on the primary link (`initInboxBadge`). Styled by `css/inbox.css`, its own `.inb-*` classes. |
| `js/views/inbox-activity.js` | Independent workspace Activity and Sharing activity pages. Workspace events, live sessions and project names come from their existing read APIs; Sharing activity reads the relay’s recent-event buffer. |
| `js/views/sharing.js` | Inbox Sharing management: shared repositories, incoming clones, commits, Apply, members, grants and revocations. Existing Sharing links normalize to `#/inbox/sharing`. |
| `js/views/projects.js` | Data List: searchable catalog, Pinned and Live filters and a file browser in each expanded row. Catalog and optional telemetry load independently. Stable rows retain focus, folders and scroll across sorting and navigation; request generations reject stale file replies. Refresh files rereads the current folder. Each file and folder row has a **Copy path** button (also on right-click) offering the path relative to the project root or the full path, built from `roots.applied.xo_projects_root` in `GET /api/runtime-config` plus the project id. Registers `project-list` at `#/projects/data/list`. |
| `js/core/workspace.js` | Indexed project counts from `/xo/space.json`. Prefers hub `index_counts` captured before graph display limits; marks incomplete scans with `+` and treats missing counts as unknown. Older graphs use conservative lower bounds when their display limits were reached. |
| `js/views/tree.js` | The Projects Tree page: horizontal hierarchy over the same `/xo/space.json` dataset as Graph: folders as columns, files stacked beside their parent. Deep-link `#/projects/data/tree`. |
| `js/views/chat.js` | The Chat view: Plane-B chat (`/api/chat/prompt` → SSE stream → transcript refetch) with session sidebar, project binding for new sessions, and mini-markdown rendering. Works across claude_code / hermes / openclaw. Deliberately unregistered: no tab. |
| `js/views/wiki.js` | The compact Wiki overview: local quickstart/view actions and links to detailed online guides. Opens from the header resource link (`nav:false`, `#/wiki`), with no primary tab. Legacy `space:wiki-page` requests focus the matching topic without replacing the overview. |
| `js/views/quirq.js` | The Quirq view: machine-local `.quirq` state (watcher infrastructure and the derived runtime tier) beside the durable project `.xo` output. Its file rows come from `services/cowork_agent/quirq_catalog.py`, which is data-driven: a file that moves root without a catalog entry to match renders as `0 present`. No tab of its own: `nav:false, parent:'setup'`, opened from **Setup → Server → Technical details** (`#/setup/server/details`). |
| `js/views/project-manage.js` | Persistent Projects Manage page. Owns the project-management controller, catalog refresh, Add handoff and form retention across navigation. |
| `js/views/project-management.js` | Clone, collapsible project cards, pins, GitHub URL copying, inline sharing and removal/access-review controls, styled by `css/project-management.css`. Details load Issues when expanded; View activity opens the selected project in Inbox. |
| `js/core/project-issues.js` | Reusable GitHub issue mirror: local Open/Closed/All filters and search, retained controls and explicit polling through Refresh. Styled by `css/project-management.css`. |
| `js/views/setup.js` | The guided Setup controller: `createSetupViews` registers Workspace and Intelligence layer, then Connectors, Secrets, Jobs and Server management under `#/setup/<section>`. Every route shares one mounted shell, so forms keep drafts across sections and status refreshes. |
| `js/views/setup-shell.js` | Setup layout and stable form controls. Workspace shows Space ID and verified account status; Secrets uses the existing masked-list and single-key environment APIs. |
| `js/core/setup-sections.js` | Setup section IDs, labels, canonical routes and compatibility mappings for old section handoffs. |
| `js/views/setup-search.js` | Searchable setting names and topics; opens the existing controls without reading their values or rebuilding forms. |
| `js/views/setup-identity.js` | Read-only Workspace metadata, verified XO user ID and GitHub account from `/space/setup/status`; no tokens or browser session minting. |
| `js/core/setup-state.js` | Factual Setup summaries and the next action from runtime configuration; no authentication or ingestion readiness claims. |
| `js/views/setup-commands.js` | Setup Jobs card: scheduled/manual kind choice, plain-language schedule and time-limit form, Run now, live results and history drawer over `/api/schedules`. |
| `js/core/jobs.js` | Job vocabulary shared by Setup and Inbox Jobs, with no DOM or network: schedule presets ↔ `every_seconds`/`first_run_at`, upcoming runs and runs per day for the editor's preview, schedule and status wording, duration units. |
| `js/core/command-results.js` | Shared job results drawer used by Setup and Inbox Jobs, including output, status, working directory and log path. |
| `js/views/connectors.js` | The persistent Connectors controller inside Setup: Composio toolkits, connect / disconnect, the Actions drawer and the Polling drawer (`PUT /api/connections/{toolkit}`). The Polling drawer keeps unsaved edits across the repaints Refresh, the Actions drawer and a connect landing cause; Save repaints from the server's copy, and closing the drawer (Hide, opening another toolkit's drawer, turning the toolkit off, disconnect) discards them. Lazily authenticates on first selection (`js/core/session.js`); opens at `#/setup/connectors`, with `#/connectors` retained as an alias. |
| `js/views/native-connectors.js` | GitHub, MagicPath, Vercel, Google Drive and OneDrive connection controls using their existing `/api/connectors/` routes. Status reads run independently of XO sign-in; credential fields and pending authorization stay mounted across filtering, refresh and navigation. |

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
`~/.quirq/cache/graph.json`. The graph is derived state, so it lives in
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

## Setup tab

Two setup steps keep one section visible at a time. `core/setup-sections.js` owns section IDs, labels and compatibility aliases; `setup-shell.js` renders the layout and `setup.js` owns behavior, styled by `setup.css`. Legacy agent/activity section events resolve to Intelligence, and project management lives under Projects → Manage:

1. **Workspace** shows the Space ID, configured workspace name/owner, verified XO user ID and GitHub account, then the projects and Space data folders. Applied paths and connection diagnostics are expandable.
2. **Intelligence layer** combines agent connection with activity collection. Choose the chat agent, review installation and credential checks, and select which agents contribute sessions and project history. Agent and activity settings retain independent forms, saves and drafts; other agents and detailed paths are collapsed.

**Next** moves between steps without saving. All forms stay mounted, so section
and app navigation preserve drafts. Refresh and saving a credential also keep
unfinished folder, agent and activity edits. Agent and Activity saves send all
required runtime fields, but use the last saved values for the other form. Intelligence also retains the advanced collection interval and usage-reporting status. Its footer opens Manage, whose project drafts, count and load errors remain independent of runtime settings.
The status strip points to pending changes or a reported folder/installation
issue; it does not infer authenticated access from a saved key or installed CLI.

Every section has a URL that opens it directly:

| Section | URL |
| --- | --- |
| Workspace | `#/setup/workspace` |
| Intelligence layer | `#/setup/intelligence` |
| Connectors | `#/setup/connectors` |
| Secrets | `#/setup/secrets` |
| Jobs | `#/setup/commands` |
| Server | `#/setup/server` |

Opening the Setup tab starts at Workspace. Legacy `#/setup`, `#/connectors` and `#/secrets` links resolve to the corresponding canonical URLs. The **Manage** group opens **Connectors**, **Secrets**, **Jobs** or **Server** directly. Secrets lists configured keys with fixed masks and uses `PATCH /api/secrets/{key}` and `DELETE /api/secrets/{key}` to edit the existing environment store. Workspace identity uses the read-only `GET /space/setup/status`; unavailable checks are distinct from missing or rejected credentials. Inbox Jobs' **Open Setup** button opens `#/setup/commands`; Sharing's **Clone project** opens the Add form in `#/projects/manage`. **Server → Technical details** opens Quirq's state browser, whose Setup button returns to `#/setup/server`.

The topbar search stays visible throughout Setup. Search setting names such as
“folders”, “secrets” or “restart”, then choose a result to open its control.
Inside Connectors, the same input filters the app cards instead.

Connectors groups **Workspace integrations** (GitHub, MagicPath, Vercel,
Google Drive and OneDrive) above **Account apps** from Composio. GitHub supports
a personal access token or device sign-in; Vercel supports an API token or
browser sign-in with a pasted redirect URL when needed. Disconnect an existing
Vercel connection before starting another browser sign-in. MagicPath has an
explicit skill/CLI installation action and authorization-code sign-in. Drive
accounts use the existing rclone add, authorization, cancel and remove routes;
“Configured” means a complete stored remote, not a live account check.
Removing a remote does not delete cloud files. Account apps retain their
workspace toggles, account labels, action permissions and Inbox polling.
No installation or sign-in starts from a native status refresh.

Project management uses `POST /api/xo-projects` with `repository_url` and
`project_id`, `GET /api/xo-projects/{id}/removal`, and `DELETE /api/xo-projects/{id}`
with `confirm_project_id`. Existing folders are never overwritten. Removal
checks both workspace sharing grants and the local collaborator roster; each
grant must be revoked individually, and each other collaborator removed.
Unavailable or malformed access checks block removal. The server repeats its
checks when Delete is pressed, regardless of the earlier preview. Deletion
removes local files; it does not delete the remote repository or backups.
Automatic sharing clones remember the local removal so they do not recreate
the folder. Explicitly cloning it again restores it. Projects List, Tree and
Sharing refresh on return. Project changes invalidate cached map data; a map
still showing earlier data offers **Refresh map**. Changing projections or
refreshing the map preserves the document and unfinished Setup forms.

For never-shared Git repositories, XO Swarm must support `GET /commits/members`
returning `200` with `members: []` when its sharing ledger has no rows. Older
versions return the same `403` as an inaccessible shared repository; Space
keeps removal blocked in that case. Deploy the companion `members_for_user`
change before enabling removal of never-shared repositories. Existing shared
repositories still require an active, bound caller and individual revocation.

**Restart server**, **Apply & restart** and the update action appear in Server,
with only the relevant restart button visible. They use `/space/server/restart`. Restart takes
a few seconds; the footer pill may go offline before it returns. The page reloads
when a new server instance responds, so every tab loads the updated code.

| `restart_mode` | How it works |
|---|---|
| `managed` | SIGTERM lets the container supervisor restart the server. |
| `native` | The pid file belongs to `./cowork-api.sh start`; a detached `cowork-api.sh restart-owned` helper checks ownership and restarts only that installation. |
| `foreground` | No supervisor or matching pid file: the button is disabled with “Ctrl-C and re-run”. The route returns 409. |

The footer still has no process start control: its Start hint copies a terminal
command. Process restart belongs on Setup.

**Jobs** starts empty. A job is a saved command. **New job** and **Edit** open a
separate **New job** card above the **Your jobs** list (✕ or Cancel closes it).
The card asks which kind the job is before anything else: **Repeating** (runs
again and again) or **One time** (runs once on a day and time, or whenever
someone clicks **Run now**). **When should it run?** follows for both, with a
shadcn Calendar (`calendar()`/`wireCalendar()` in `js/core/shadcn.js`, styled in
`css/shadcn.css`; past days disabled). For Repeating the calendar picks an
optional start date, and days in the shown weeks that get a run are dotted;
four preset tiles (Every… N seconds/minutes/hours/days, Hourly at a minute,
Daily at a time, Weekly on a day at a time) show only the chosen tile's inputs,
and a custom interval with a start date also asks for the first run's time.
For One time the calendar picks the day and a time input the time; with no day
it waits for Run now, and a time already past is refused (except the job's own
saved time). A live preview states the schedule and first run, plus the next
three runs for Repeating. **What should it run?** follows: a name, optional
description, command line or argv JSON, optional folder and a time limit (“Stop
it if a run takes longer than” N seconds, minutes or hours; 5 minutes for a new
job). `js/core/jobs.js` translates the form into the scheduler's own fields, so
the API has no presets: Repeating is `every_seconds` plus a `first_run_at`
anchor at the first matching local time on or after the start (a custom
interval without a start sends no anchor); One time is a null `every_seconds`
with `first_run_at` at its time, or none. Runs keep a fixed interval, so a daily
time can move by an hour across a daylight-saving change; the form says so.
**Edit** reopens a job as the choice that produced it (an interval that matches
no preset opens as a custom interval and keeps its existing anchor; a first run
still ahead shows as its start date), can switch its kind (the id and history
stay), and preserves existing environment, project and enabled settings. A
command line is split without a shell; validation errors appear in the card.
Rows carry a Repeating/One time badge with the schedule in words and the next
run in local time, or “Runs once on …”, “Ran once on …” or “Runs when you click
Run now”; a one-time job stays listed after it runs.

The information tooltip beside **Your jobs** explains **Copy agent prompt**.
The card shows the full `POST /api/schedules` creation URL. The button copies a
short skill (`xo-space-jobs`, SKILL.md format) for the agent: use `/api/schedules`
on the machine running Space, never edit its files; jobs run on the server
without a shell, so give an absolute cwd, an argv list and a timeout in seconds,
and no secrets; `every_seconds` and `first_run_at` (the browser's UTC offset is
filled in) to repeat, a null `every_seconds` with `first_run_at` to run once at
a time, and neither unless asked for a time or schedule; avoid duplicates, remember edits replace the whole definition, and
run nothing unasked. If clipboard access is unavailable, a selectable copy
appears without changing a job draft.

**Run now** executes through the command utility and disables while running. The card
polls the job every three seconds until the status and duration appear. The row
shows its folder and a preview of the latest result, with the status in words
(Succeeded, Failed, Timed out, Command not found, …).
**Results** opens the latest 20 results with escaped output, exit codes, timing,
and a copyable full-log path. The drawer updates while a job runs and also
offers Refresh. A concurrent run or a full shared execution limit returns 409.
Restart, command writes and runs require a local client; browser requests must come from the same loopback origin. Remote requests receive 403.

Definitions and every result stay under `<quirq state>/scheduler/`, and the full output of every run under `<quirq state>/logs/scheduler/<id>.log`:

```text
scheduler/
├── jobs.json          # saved commands, intervals and descriptions
├── state.json         # next run, running since, last result
└── runs/<id>.jsonl    # append-only history: one line per run, starting with ts and type ("job.run"), 2000-character output tails
```

Deleting a command keeps its history and logs on disk and does not cancel an active process. Commands run locally with
the server's environment, including when the watcher is disabled for manual runs.

## Agents tab

The second topbar tab (`Projects | Agents | Inbox | Setup`)
is a session-telemetry dashboard: per-session stats rendered as shadcn/ui
components ported to Space (cards, tables, badges, pagination in
`js/core/shadcn.js` + `css/shadcn.css`) with SVG charts drawn by
`js/core/chart.js` in shadcn's Chart markup (no dependencies). The payload is assembled from every backend that implements the
`session_telemetry` capability, so a runtime that reports nothing shows as
"not available" rather than as a zero. It lives in its own module
(`js/views/sessions.js`), independent of the atlas's `boot()`; either can
fail without taking the other down, and the registry keeps the tabs
switchable regardless.

- Data: `GET /xo/sessions.json`, one pre-aggregated payload built from the
  session telemetry every runtime that reports it contributes. Fetched
  lazily on first open; the section's Refresh button (shell chrome, shared
  by every page) re-fetches (the file is rebuilt at most every
  `XO_VIEWS_REFRESH_S`, default 30 s).
- Sub-views: Overview · Sessions (list → detail with sub-agents and
  per-session tools) · Trends (charts only: weekly volume stacked by model
  and by project, share donuts for models and projects, tool and MCP
  server usage; nothing the Overview shows repeats here, and each card's
  Export CSV action downloads the full rows behind it. The old
  `#/agents/tools` and `#/agents/models` links land here) · Configure
  (data collection: one card per telemetry source with its vendor tag,
  collection status, usage and a 30-day sparkline; edit the data location
  the provider reads, or switch collection off. Backed by
  `/api/telemetry/sources`; a save rebuilds `sessions.json` in the
  background. The chat agent and activity watcher stay in Setup's
  Intelligence layer). The `Today/7d/30d/All` window selector filters
  client-side over per-day rollups shipped in the payload.
- No alerts and no prompts by design: those tables are never read, so raw
  prompt text never enters the payload.

## Work tab

The third topbar tab is **Work** (`#/work`; the old `#/inbox/*`, `#/sharing`
and `#/feed*` routes land on it). Three pages, one per question: **Inbox**
(what needs me), **Live** (what is happening now) and **History** (what
happened). The design and the loop they serve are `docs/work-and-workitems.md`.

**Inbox** (`#/work`, `js/views/work.js`, `css/work.css`) reads one route,
`GET /api/work/inbox`, and paints four groups from it: **Decisions** (the
attention items: a work item assigned to you or to nobody, a blocked todo, an
issue for you, a mail or mention of a kind listed in
`sources.connections.attention`, an agent's question, a share not cloned, a
failing source), **Calendar** (meetings from now to the end of tomorrow),
**Completed** (jobs that ran in the last day, one row per job, work items an
agent closed, todos an agent completed) and **Work** (the open work items,
who owns them and who is on them). A group card narrows the list; the
project select and the toolbar search narrow it further, client-side. Every
row has one primary action and a way to put it away: **Dismiss** stores the
row's `key@since` (the same condition starting again comes back), **Acknowledge**
and **Accept** store the key. **Track** turns a mail, mention or issue into a
work item (`POST /api/work/promote`; an issue is adopted, so GitHub keeps its
status). The work item row opens to its record: body or issue, who is on it,
an assignee select (you, the agents from `/api/telemetry/sources`, any login
already seen), Close, Open project, Delete, all through the existing
`/api/xo-projects/{id}/workitems*` routes. **+ Work item** in the section bar
creates one. The page rereads every 30 s while shown; the tab badge is
`GET /api/work/summary` (`badge` = decisions + completed), polled every 60 s
while another tab is shown. A failed reread keeps the page and says why.

Nothing is copied on the way: the readers under `services/work/` answer from
the Space timeline, the GitHub issue mirrors, `connections/<toolkit>/events.jsonl`,
the sharing relay, `scheduler/runs/` and the agents' posts, and
`~/.quirq/work/` holds only your own state, one folder per page:
`inbox/inbox.json` (`dismissed`, `acked`, `promoted`, and the connection kinds
that count as decisions), `live/live.json` (which stream groups show) and
`history/history.json` (the reader switches, `watermark`, `pinned` and the
posts). Each is hand-editable: unknown keys survive, a bad mark is dropped on
read, and a file that is not valid JSON is served empty and never overwritten. Agents post with `POST /api/feed`
(`.agents/skills/xo-projects/references/work-http-api.md`); `POST /api/inbox`
still lands in the same place.

**Items and their sessions.** A connection can become a folder inside the
Inbox (`~/.quirq/work/inbox/<connection>/`, a policy in `connection.json`,
written today by `PUT /api/work/inbox/connections/{toolkit}`; Setup's drawer
follows). Its listed collectors' events then become items, each a folder,
and an item can own one agent session that handles it and leaves an outcome.
The Inbox shows an item as a decision: arrived (**Start session**, or Track
it as work), a drafted reply (**Open workbench**, **Send** when the policy
allows acting), a proposed task (**Track**), a question (**Open session**,
which lands on the Agents page), or a failed run (**Retry**); an item the
session handled or found merely worth a glance sits under Completed until
you **Acknowledge** it. Dismiss and Acknowledge on an item go through its
own decide route, so the item folder records the decision. The header
counts the sessions running. Opening an item row shows its **thread**: the
mail on top, the agent's turns and your replies below, the outcome, and a
reply box (⌘↩ sends). A reply resumes the item's session, or starts it when
the item has none, and the answer lands on the thread; the page polls the
thread every two seconds while the agent is answering.

**Live** (`#/work/live`) and **History** (`#/work/history`) are built on
sample data (`js/views/work-sample.js`) until their routes land: Live shows
the calendar beside a live stream of the logs, History the events over a
window with charts and a timeline, split Space and Projects, with project
sharing inside.

## Inbox tab

Retired: the Work tab above replaces these pages, which are no longer
registered; this section stays while `services/inbox/` does.

The third topbar tab contains Items, Connections, Jobs, Activity, Sharing activity
and Sharing. **Items** tracks information arriving in the workspace. One
human-readable JSON file is its source of truth; a small service feeds and edits
it, five HTTP routes serve it, and `js/views/inbox.js` renders it with
`css/inbox.css`. The tab
button carries an unread badge (`counts.new`: polled every 60 s while another
tab is shown; while Inbox is open the view's own 30 s read feeds it).

**Jobs** follows Connections and reads `/api/schedules`
independently. It lists every job, repeating and one-time, with the same
Repeating/One time badge and plain-language schedule as Setup; repeating jobs
also show their enabled state and next due time, and every job shows its
latest/running status. **Run now** is the section's one write
(`POST /api/schedules/{id}/run`; a 409 re-reads the list). **Results** opens the
same results drawer used by Setup; **Open Setup** returns to job management.
Jobs refresh on entry, through either Refresh button, and every 30 seconds while
visible (every three seconds while a listed job is running). This section
creates no Inbox items, and item search, filters and unread counts retain their
existing scope.

**Activity** (`#/inbox/activity`) follows Jobs and shows recorded workspace
project, session, task and file events. It reads `/api/xo-projects/timeline?limit=200`,
with project names from `/api/xo-projects` and a separate open-session summary
from `/api/xo-projects/activity`. Choosing a project uses its `/timeline`, `/activity`
and `/todos` endpoints. The selected project shows todos in status order and
current sessions with their agent, runtime, session ID, opened time and last
activity. All projects does not request every project’s todos. **View activity**
in Manage selects that project and clears an older event search. **Load older
events** follows `next_cursor` with `before`; search covers loaded events.

**Sharing activity** (`#/inbox/sharing-activity`) follows Activity. It reads
`recent` from `/api/project-sharing/status`: the latest 50 relay events, cleared
when the server restarts. This is distinct from workspace history and the
**Sharing** management page at `#/inbox/sharing`. Each activity page keeps its own
search and selection, refreshes on entry and every 30 seconds while visible,
and has **Refresh** in the section bar. Failed refreshes keep the previous events
visible with an error; malformed records are reported rather than shown as an empty history.

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
  Projects. A sharing item (`view: "sharing"`, or any `sharing.*` kind, which
  covers items stored before the feeder linked there) opens Inbox Sharing with
  its project selected; when commits are waiting, the detail panel says so
  above the commit list and focus lands on **Apply**.

### The file: `~/.quirq/inbox/inbox.json`

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
      "pid": "7deb4a22-0789-497d-9399-a2272579fa06",  // the project's pid, set with project_id; null otherwise
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
| `timeline` | `~/.quirq/projects/timeline.jsonl` (the Space timeline), the newest 500 events of the enabled types | `types: ["session.started", "todo.added"]`; `todo.completed`, `file.created`, `file.edited` can be added | `cursors.timeline`, the newest event timestamp seen; with no cursor only the last 24 hours are taken | `Session started in <project> (<runtime>)` linking to Agents; `Todo added in <project>: <content>` linking to Projects |
| `todos` | every `<project>/.xo/todos.json` | `statuses: ["blocked"]` | none | `Todo blocked in <project>: <content>` (kind `todo.blocked`, linking to Projects); the item is set to done by itself (flagged `auto_closed`) once the todo leaves the watched status or disappears, and comes back as new if the todo is blocked again |
| `sharing` | the in-memory relay status (the `recent` list of `GET /api/project-sharing/status`) | on | `cursors.sharing` | `Repo shared with this workspace: <repo>`, `New commits fetched: <repo>`, `Sharing error: <repo>`, `Sharing access revoked: <repo>`, with the relay detail as body, linking to Inbox Sharing |
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

Timeline uses Data’s centered content width and rectangular controls. Its compact
summary replaces the visible page title with dated-file or commit counts,
mapped project coverage and the current date window. Project filtering, year
selection and vertical zoom/pan update those totals; scrub and playback dim the
same window’s data without changing the totals. Trace details appear separately
from the summary. Empty lanes distinguish missing dated files from missing commit
data in the loaded map; they do not claim the repository has no history. These
are snapshot counts, not repository lifetime totals.

Shapes are semantic: `disc` = code, `ring` = document, `diamond` = everything
else. Leaf `date` is the git first-added date, or `null` when git does not
know the file (untracked, or a non-git project); undated leaves appear on the
graph but sit out the timeline. Tree edges (leaf → cluster → project → root)
are derived by the UI; only cross-ties are listed.

Ties are derived facts, never editorial: files that repeatedly share commits
("changed together ×N", from the same git log that dates the leaves), docs
whose text names another file's relative path ("references"), and
`test_x` ↔ `x` filename pairs ("tests"). Strongest first, capped at 60.
