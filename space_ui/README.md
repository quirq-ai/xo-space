# Space UI

An explorable map of `~/xo-projects`. Four primary sections share the same
navigation structure: **Projects**, **Agents**, **Work**, and **Setup**.
Primary links open a section's default page; the secondary links open its
pages and can be copied, opened in another tab, or revisited with Back/Forward.

| Section | Default route | Pages |
|---------|---------------|-------|
| Projects (`1`) | `#/projects/overview` | Overview, Data (List, Graph, Tree), Timeline, Manage |
| Agents (`2`) | `#/agents/overview` | Overview, Sessions, Trends, Configure |
| Work (`3`) | `#/inbox/items` | Inbox, Jobs, Activity, Sharing |
| Setup (`4`) | `#/setup/workspace` | Workspace, Intelligence layer, Connections, Secrets, Jobs, Server |

Space starts at Projects Overview. **Data** contains the existing List, Graph
and Tree views at `#/projects/data/list`, `#/projects/data/graph` and
`#/projects/data/tree`. The List / Graph / Tree links sit in each view’s local
toolbar. The Data section link remembers the last view used; a direct
`#/projects/data` link opens List. Clicking Projects or pressing `1` always
opens Overview. The section roots
`#/projects`, `#/agents`, `#/inbox`, and `#/setup` normalize to their defaults.
Legacy `#/dashboard`, `#/graph`, `#/tree`, and `#/time` links open
the corresponding Projects page. `#/sharing` and `#/projects/sharing` now open
Work Sharing at `#/inbox/sharing`. The previous `#/projects/list`,
`#/projects/graph` and `#/projects/tree` links also remain valid and normalize
to the corresponding Data route. The former `#/projects/files`,
`#/projects/files/list`, `#/projects/files/graph` and `#/projects/files/tree` addresses
remain aliases for Data. Secrets keeps its alias `#/secrets`; `#/connectors`,
`#/setup/connectors` and `#/inbox/connections` open Setup Connections at
`#/setup/connections`. Technical details is a child of Setup Server at
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
query, selected root and existing project drawers. Work Sharing keeps **Share a project**,
**Check now**, and **Refresh** beside its own navigation.
Each Manage project card has **Share**, which opens a Space ID form
in that card. Cancel keeps you on the page; submitting grants access to that
Space ID. Drafts stay with their project while you filter, refresh or navigate.
Choosing a node from Data List, Data Tree, Manage or
Timeline opens Data Graph rooted on that node. The secondary navigation does
not repeat primary section labels. Projects page descriptions are removed to leave more room
for graphs and content; List keeps its counts and actions in a compact row. List, Tree, Timeline, Setup, Setup Connections, the Work Inbox, Activity, and
the Agents session list have their own search; typing there keeps you on that page.
Wiki, Sharing, Quirq, Work Jobs, and the Agents charts/detail have no
page-search field; they still show the Cmd+K trigger.

| Page | Search scope |
|------|--------------|
| Projects List | All search words match across project name, folder ID and description. Combines with the Filter menu’s All projects, Live or Pinned options beside Sort by. |
| Tree | Folder and file names, keeping the ancestors of matches visible. |
| Timeline | Project names; the selected timeline mode and date range still apply. |
| Setup | Setting names and topics. Choose a result to open its section; searches never read field values or credentials, and all unfinished forms stay mounted. |
| Setup → Connections | Workspace integrations and account apps by name, identifier, description, and connected account label. Filtering preserves open controls and unsaved edits; the polled-apps list above them is not filtered. |
| Work → Inbox | Title, entity, state, outcome kind, project and runtime of the rows loaded for the current tab and state pill. The matching count shows this scope. |
| Work → Activity | Loaded workspace event labels, details, project names/IDs, runtime and session ID, intersected with the project selector. Load older adds more events to this search. |
| Sessions list | Project, path, source, model, and session ID in the loaded sessions, intersected with the selected sources. Matching counts distinguish loaded rows from the total. |

Each page remembers its query while you navigate within the app; a full
reload resets it. Press `/` outside an editable control to open the command
palette on Graph and on pages that have search. In a page search, `Escape` clears the query; pressing it again removes
focus. The clear button does the same reset.

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
| `js/core/registry.js` | View registry: primary section links, `1..n` hotkeys (ignored while editing), canonical hash routes and aliases, history, lazy mounts, per-view refresh and failure isolation. Primary sections are configured independently of their pages. A route may carry a `?query` (`#/inbox/item?p=...&id=...`): the view is matched on the path, the query stays in the URL for the view to read on show. |
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
| `js/core/connections.js` | Pure formatters over one `GET /api/connections` entry: `every` (cadence), `collectorLabels`, `pollLine` (last poll or the error). Shared by Setup Connections' polled-apps list and the Connectors controller it embeds, so both read the same. |
| `js/core/server-widget.js` | Footer server pill (status poll + terminal start hint). |
| `js/core/preview.js` | File previewer drawer. Any view opens it with a `space:preview-file` event; markdown renders through `markdown.js`, HTML renders in an empty-`sandbox` iframe, everything else as escaped source. |
| `js/views/atlas.js` | Projects Overview, Graph and Timeline. Changing projections rebuilds only the atlas engine, disposes its listeners and frames, and ignores superseded reads; the document and other mounted pages remain intact. |
| `js/views/sessions.js` | Five Agents routes under `#/agents/`, sharing one mounted telemetry view: session telemetry from `/xo/sessions.json`, contributed by whichever backends implement the `session_telemetry` capability. The module file keeps its `sessions.js` name; the data file `sessions.json` and the internal Sessions sub-view are session telemetry, not the tab. |
| `js/views/inbox.js` | Two Work routes (`items`, `jobs`) share a mounted controller. The Inbox page shows the workspace's work items joined with their sessions (`GET /api/inbox`): tabs from the answer's sections, entity groups, state pills, rows with a state chip; Open lands on the item page through the hash. Also the badge on the primary link (`initInboxBadge`, waiting plus new). Styled by `css/inbox.css`, its own `.inb-*` classes. |
| `js/views/inbox-activity.js` | The Work Activity page. Workspace events, live sessions and project names come from their existing read APIs. |
| `js/views/work-item.js` | The item page (`#/inbox/item?p=<project_id>&id=<workitem_id>`, or `?s=<session_id>` for a session no work item owns): the fact as it arrived, the transcript of the latest session and the outcome on the left (`GET /api/inbox/{project_id}/{workitem_id}`, `GET /api/sessions/{session_id}/transcript`); the chat and the actions on the right (`POST .../reply`, `start`, `send`, `archive`, `reopen`). Polls every 2 s while a session runs, every 15 s otherwise, only while shown; reads its selection from the hash on every show. Styled by `css/work-item.css`. |
| `js/core/item-links.js` | `openItemLink(switchTo, item)`: what Open in Space does for a fact's `link` (a file previews in the Data list, a sharing item lands on Sharing with its project selected, a view link jumps there); `hasLink` and `safePath`. Used by the item page. |
| `js/views/sharing.js` | Work Sharing management: shared repositories, incoming clones, commits, Apply, members, grants and revocations. Existing Sharing links normalize to `#/inbox/sharing`. |
| `js/views/projects.js` | Data List: searchable catalog, Pinned and Live filters and a file browser in each expanded row. Catalog and optional telemetry load independently. Stable rows retain focus, folders and scroll across sorting and navigation; request generations reject stale file replies. Refresh files rereads the current folder. Each file and folder row has a **Copy path** button (also on right-click) offering the path relative to the project root or the full path, built from `roots.applied.xo_projects_root` in `GET /api/runtime-config` plus the project id. Registers `project-list` at `#/projects/data/list`. |
| `js/core/workspace.js` | Indexed project counts from `/xo/space.json`. Prefers hub `index_counts` captured before graph display limits; marks incomplete scans with `+` and treats missing counts as unknown. Older graphs use conservative lower bounds when their display limits were reached. |
| `js/views/tree.js` | The Projects Tree page: horizontal hierarchy over the same `/xo/space.json` dataset as Graph: folders as columns, files stacked beside their parent. Deep-link `#/projects/data/tree`. |
| `js/views/chat.js` | The Chat view: Plane-B chat (`/api/chat/prompt` → SSE stream → transcript refetch) with session sidebar, project binding for new sessions, and mini-markdown rendering. Works across claude_code / hermes / openclaw. Deliberately unregistered: no tab. |
| `js/views/wiki.js` | The compact Wiki overview: local quickstart/view actions and links to detailed online guides. Opens from the header resource link (`nav:false`, `#/wiki`), with no primary tab. Legacy `space:wiki-page` requests focus the matching topic without replacing the overview. |
| `js/views/quirq.js` | The Quirq view: machine-local `.quirq` state (watcher infrastructure and the derived runtime tier) beside the durable project `.xo` output. Its file rows come from `services/cowork_agent/quirq_catalog.py`, which is data-driven: a file that moves root without a catalog entry to match renders as `0 present`. No tab of its own: `nav:false, parent:'setup'`, opened from **Setup → Server → Technical details** (`#/setup/server/details`). |
| `js/views/project-manage.js` | Persistent Projects Manage page. Owns the project-management controller, catalog refresh, Add handoff and form retention across navigation. |
| `js/views/project-management.js` | Clone, collapsible project cards, pins, GitHub URL copying, inline sharing and removal/access-review controls, styled by `css/project-management.css`. Details load Issues when expanded; View activity opens the selected project in Work Activity. |
| `js/core/project-issues.js` | Reusable GitHub issue mirror: local Open/Closed/All filters and search, retained controls and explicit polling through Refresh. Styled by `css/project-management.css`. |
| `js/views/setup.js` | The guided Setup controller: `createSetupViews` registers Workspace and Intelligence layer, then Connections, Secrets, Jobs and Server management under `#/setup/<section>`. Every route shares one mounted shell, so forms keep drafts across sections and status refreshes. |
| `js/views/setup-shell.js` | Setup layout and stable form controls. Workspace shows Space ID and verified account status; Secrets uses the existing masked-list and single-key environment APIs. |
| `js/core/setup-sections.js` | Setup section IDs, labels, canonical routes and compatibility mappings for old section handoffs. |
| `js/views/setup-search.js` | Searchable setting names and topics; opens the existing controls without reading their values or rebuilding forms. |
| `js/views/setup-identity.js` | Read-only Workspace metadata, verified XO user ID and GitHub account from `/space/setup/status`; no tokens or browser session minting. |
| `js/core/setup-state.js` | Factual Setup summaries and the next action from runtime configuration; no authentication or ingestion readiness claims. |
| `js/views/setup-commands.js` | Setup Jobs card: scheduled/manual kind choice, plain-language schedule and time-limit form, Run now, live results and history drawer over `/api/schedules`. |
| `js/core/jobs.js` | Job vocabulary shared by Setup and Work Jobs, with no DOM or network: schedule presets ↔ `every_seconds`/`first_run_at`, upcoming runs and runs per day for the editor's preview, schedule and status wording, duration units. |
| `js/core/command-results.js` | Shared job results drawer used by Setup and Work Jobs, including output, status, working directory and log path. |
| `js/views/connections.js` | Setup Connections (`#/setup/connections`; `#/connectors`, `#/setup/connectors` and `#/inbox/connections` are aliases): the polled-apps list (`GET /api/connections`, Poll now, Configure opening that app's Polling drawer) above the embedded Connectors controller. Mounted once by Setup on the section's first visit; its 30 s read runs only while the section is shown. |
| `js/views/connectors.js` | The persistent Connectors controller, embedded by Setup Connections: Composio toolkits, connect / disconnect, the Actions drawer and the Polling drawer (`PUT /api/connections/{toolkit}`). The Polling drawer keeps unsaved edits across the repaints Refresh, the Actions drawer and a connect landing cause; Save repaints from the server's copy, and closing the drawer (Hide, opening another toolkit's drawer, turning the toolkit off, disconnect) discards them. |
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

1. **Workspace** includes **Branding** for a custom display name and uploaded logo, followed by the Space ID, configured workspace name/owner, verified XO user ID and GitHub account, then the projects and Space data folders. Applied paths and connection diagnostics are expandable.
2. **Intelligence layer** combines agent connection with activity collection. Choose the chat agent, review installation and credential checks, and select which agents contribute sessions and project history. Agent and activity settings retain independent forms, saves and drafts; other agents and detailed paths are collapsed.

Branding previews changes before saving and updates the header and browser title immediately after a successful save. Names are 1–80 characters; logos accept PNG, JPEG or WebP up to 2 MiB and 4096 × 4096 pixels. Remove the logo or reset to the default Space name and XO mark, then save to apply. `GET`/`PUT /space/branding` persist the display settings together in `settings/branding.json` under the configured state root; `GET /space/branding/logo` serves the validated image. Branding does not change the workspace ID or account identity. Image validation uses the Pillow dependency in `requirements.txt`.

The default storage path is `~/.quirq/settings/branding.json`, outside the application repository. Both the custom name and uploaded image bytes live in that one runtime file; saving never rewrites source files or bundled assets, so code updates preserve branding. If `QUIRQ_STATE_ROOT` points inside the checkout, Git ignores the branding file and its atomic-write temporary file. With no saved customization, the UI shows **Space** and the bundled **XO** logo; a missing uploaded image also falls back to the XO logo in the header and preview.

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
| Connections | `#/setup/connections` |
| Secrets | `#/setup/secrets` |
| Jobs | `#/setup/commands` |
| Server | `#/setup/server` |

Opening the Setup tab starts at Workspace. Legacy `#/setup`, `#/connectors` and `#/secrets` links resolve to the corresponding canonical URLs, as do `#/setup/connectors` and `#/inbox/connections`. The **Manage** group opens **Connections**, **Secrets**, **Jobs** or **Server** directly. Secrets lists configured keys with fixed masks and uses `PATCH /api/secrets/{key}` and `DELETE /api/secrets/{key}` to edit the existing environment store. Workspace identity uses the read-only `GET /space/setup/status`; unavailable checks are distinct from missing or rejected credentials. Work Jobs' **Open Setup** button opens `#/setup/commands`; Sharing's **Clone project** opens the Add form in `#/projects/manage`. **Server → Technical details** opens Quirq's state browser, whose Setup button returns to `#/setup/server`.

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

## Inbox tab

The third topbar tab, **Work**, contains Inbox, Jobs, Activity and Sharing.
**Inbox** is the workspace's work items joined with their sessions: a fact
that arrives (a mail or a calendar event a polled connection collected, a
GitHub issue from a project's mirror, a repo shared with this workspace, a
note an agent posted) becomes a work item at ingestion, a session is one
attempt at it, and the row says whether anyone has dealt with it. One
record, joined at read time; nothing is copied twice. `js/views/inbox.js`
renders it with `css/inbox.css`; the item page is `js/views/work-item.js`
with `css/work-item.css`. The tab button carries a badge, the rows waiting
for a person (`waiting` plus `new`, summed over every section; `GET
/api/inbox?state=open&limit=1`, polled every 60 s while another tab is
shown; while the Inbox is open the view's own 30 s read feeds it).

**Jobs** follows Inbox and reads `/api/schedules`
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

Activity keeps its own search and selection, refreshes on entry and every 30
seconds while visible, and has **Refresh** in the section bar. Failed refreshes
keep the previous events visible with an error; malformed records are reported
rather than shown as an empty history. The relay's recent events are no longer a
page of their own; the **Sharing** management page at `#/inbox/sharing` shows
each repository's sync state.

- Data: `GET /api/inbox?section=<tab>&state=<pill>&limit=200`. The answer is
  `{schema, generated_at, runner: {enabled}, sections: [...], rows: [...],
  count}`. `sections` is the summary of every section whatever the filter
  (`id`, `label`, `counts: {new, running, waiting, failed, closed}`,
  `entities: [{id, label, counts}]`); `rows` are the section's rows in the
  state asked for, newest first. `state` is `open` (new, running, waiting,
  failed; the default), `active` (running), `waiting`, `closed` or `all`.
  Every read runs the feeders first (throttled to once per 5 s). The view
  polls every 30 s while shown (skipping the repaint when nothing changed,
  restoring focus when it did) and re-reads after every action on the item
  page.
- Tabs: the answer's `sections`, shown as Projects, Agents, Connections,
  Issues when present (the page orders them, never maps them: a row's
  section is the server's, from its source kind: connection to Connections,
  sharing to Projects, github to Issues, post and local to Agents). The
  waiting plus new count sits beside each label. A tab shows its entity
  groups (a project, an agent, a toolkit such as `gmail`, a repo) with their
  counts, every entity the answer lists even with no rows, and under each
  its rows. Under Projects every project is an entity; under Agents every
  agent the agents capability knows.
- State pills Open | Active | Waiting | Closed | All fetch; the search box
  filters the loaded page on the client (title, entity, state, outcome kind,
  project, runtime) and never fetches; Refresh re-reads.
- Rows: a dot (accent while new or running, amber while waiting, red when
  failed, muted once closed), the state chip, the title, the entity, the
  outcome kind when a session left one (`reply drafted`, `task proposed`,
  `asks you`, `fyi`, `handled`), the runtime of the live session that holds
  the item, and the relative time. A `kind: "session"` row (a runtime
  session no work item owns, listed under Projects and Agents only) shows
  its runtime and time. Row states: `closed` when the item is closed;
  `running` while the runner owns a task for it or its claim is live;
  `failed` when the last session ended with an exit other than ok and the
  item is open; `waiting` when the outcome is `needs_you`, `reply_drafted`
  or `task_proposed`; else `new` (no session yet, or one that ended with
  nothing to decide). A session row is `running` while live, else `closed`.
- Open: the whole row is the button. A work item row lands on
  `#/inbox/item?p=<project_id>&id=<workitem_id>`; a session row on
  `#/inbox/item?s=<session_id>` (the transcript alone). The selection travels
  in the hash, so a reload or Back keeps the item.
- The item page: left, the fact as it arrived (title, body, when, kind,
  entity, project; **Open link** only when `url` is an http or https address,
  checked in JS before it reaches an href, a new tab with
  `rel="noopener noreferrer"`; **Open in Space** follows `link`, below), the
  transcript of the latest session (`GET /api/sessions/{session_id}/transcript`
  for `transcript.session_id` of the detail; `{title, messages: [{id, role,
  content}]}`, a person's turn and the agent's; a trailing fenced `json`
  outcome block leaves the agent's bubble, the outcome section shows it),
  then the outcome (kind, summary, question, draft, proposed task, what was
  acted). Right, the state line (runtime, attempt, the exit message when
  failed), the chat (a message is `POST /api/inbox/{project_id}/{workitem_id}/reply`
  `{text}`, which resumes the session or starts one; Cmd or Ctrl+Enter
  sends), and the actions: **Start a session** (`POST .../start`, before a
  session exists), **Retry** (`POST .../start?retry=true`, only when failed),
  **Send the draft** (`POST .../send`, only when `can_send`), **Archive**
  (`POST .../archive {}`) or **Reopen** (`POST .../reopen`, once closed).
  Data is `GET /api/inbox/{project_id}/{workitem_id}` (the row, `fact`,
  `session`, `outcome`, `claim`, `workitem`, `transcript`, `policy`,
  `running`, `can_reply`, `can_send`), re-read every 2 s while `running`,
  every 15 s otherwise, only while shown; every action re-reads. When
  `can_reply` is false the chat says sessions are off for the section.
- **Open in Space** follows the fact's `link` (`js/core/item-links.js`):
  `{project, path}` switches to Projects and opens the file previewer;
  `{view}` switches to that tab; `{project}` alone switches to Projects. A
  sharing item (`view: "sharing"`, or any `sharing.*` kind) opens Work
  Sharing with its project selected; when commits are waiting, the detail
  panel says so above the commit list and focus lands on **Apply**.
- `POST /api/inbox` `{title, body?, kind?, source?, project_id?, link?, url?}`
  answers 201 with a `post` work item row (section Agents), which is how an
  agent posts a note. The rest of the writes are the item page's, above;
  `GET /api/inbox/sections` and `PUT /api/inbox/sections/{section}` read and
  write the per-section session policy.

### The files

The Inbox keeps no file of its own rows any more. The record is the work
item in the project's `.xo/workitems.json` (the title, status, assignee,
source and the session ids; never a body, since a project file may be
shared); the runner's sidecars live under the Quirq state root
(`QUIRQ_STATE_ROOT`, machine-local), beside the project's claims file:

```
~/.quirq/inbox/
  ledger.json                 the feeders' bookkeeping: cursors per feeder, source switches
  policy/<section>.json       the session policy per section

~/.quirq/projects/<pid>/workitems/
  claims.json                 work item id -> {session_id, runtime, started_at}
  <workitem-id>/
    fact.json                 the fact as ingested: title, body, url, link, ts, kind, key, section, entity
    session.json              session_id, native_session_id, runtime, project_id, agent_type,
                              attempt, started_at, ended_at, exit, manual
    outcome.json              kind, summary, draft, task, question, acted, at

~/xo-projects/inbox-<section>/  the section project, where facts without a project live and run
  items/<workitem-id>/          the workbench (drafts) for such a work item
```

The runtime's own transcript is the chat; nothing copies it. A policy file:

```jsonc
{"schema": 1,
 "sessions": {"mode": "auto|manual|off", "kinds": [], "agent_type": "inbox-item", "runtime": null,
              "max_concurrent": 2, "max_per_hour": 20, "timeout_s": 300, "act": false},
 "retention_days": 30}
```

`connections` defaults to `auto`; `projects`, `issues` and `agents` to
`manual`. `kinds` narrows which fact kinds start a session (`gmail.unread`,
`issue.open`, `sharing.fetched`, `note`). `retention_days` removes the
sidecars of closed work items older than that; the work item record is
never deleted by the Inbox.

### Feeders (`services/inbox/feeders.py`)

Best-effort and idempotent: a fact whose `source.key` already names a work
item in its target project is never created twice. The target project is
the fact's own when it names one that exists (an issue, a share), else
`inbox-<section>`, scaffolded from the template on first use. A feeder that
throws is logged and skipped for that run while the others still run; a
feeder switched off in `ledger.json` is never read.

| Feeder | Reads | Cursor | Produces |
|---|---|---|---|
| `sharing` | the in-memory relay status (the `recent` list of `GET /api/project-sharing/status`) | `cursors.sharing` | a `sharing` work item per event (`Repo shared with this workspace: <repo>`, `New commits fetched: <repo>`, `Sharing error: <repo>`, `Sharing access revoked: <repo>`), the relay detail as the fact body, linking to Work Sharing |
| `issues` | every project's GitHub issue mirror, `~/.quirq/projects/<pid>/github/issues.json` (written by the GitHub issue poller) | `cursors.issues`, the newest `updated_at` seen across every readable mirror; with no cursor only the last 7 days are taken | an adopted `github` work item per issue (`Issue #<number> in <project>: <title>`, kind `issue.<state>`, labels and assignees as the fact body, the issue URL as `url`), deduped by node id; only on a run where every mirror was readable, so a transient read failure never closes real issues |
| `connections` | the newest 200 lines of `~/.quirq/connections/<toolkit>/events.jsonl` for every polled toolkit (see Connections polling below) | `cursors.connections`, one cursor across every toolkit, the newest event `ts` seen; with no cursor only the last 24 hours are taken | a `connection` work item per event: the event title, body and `url`, kind `<toolkit>.<collector>`, key `connection:<toolkit>:<collector>:<id>`, entity the toolkit, linking to Connectors |

`GET /api/inbox` runs the feeders first, throttled to once per 5 s; the
connections poller's new-events listener forces a run after a Poll now
that collected something. The relay list restarts empty with the server,
so a persisted sharing cursor never re-ingests old events. The connections
cursor is shared across toolkits: a toolkit polled for the first time whose
events are all older than the cursor surfaces nothing until it collects
something newer (delete `cursors.connections` in `ledger.json` to take the
last 24 hours of every toolkit again).

The runner (`services/work/runner.py`, every 15 s, `XO_INBOX_SESSIONS=off`
stops it) starts sessions for the open work items of every section in
`auto` mode, oldest first, under the policy's caps, marks a session the
server restart orphaned as failed, and sweeps sidecars past retention. A
session's answer ends with a fenced `json` outcome block: `handled` and
`fyi` close the item, `needs_you`, `reply_drafted` and `task_proposed`
leave it open, waiting for the person.

### Connections polling

The `connections` feeder reads what a background poller collected from the
Composio connections (Gmail, Google Calendar, Notion, Slack, Telegram) over the same MCP
upstream the agent proxy uses. Everything lives in
`services/connections/` (store, collectors, mcp_client, poller,
service) and in one folder per toolkit, hand-maintainable in the same spirit
as the Inbox's `ledger.json`. Setup Connections lists the polled apps above
the connectors; until an app is connected and polling, that list reads "No
updates yet. Connect an app below and turn on polling.":

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

An unknown toolkit is a 404 `unknown_toolkit`. Setup Connections' Connectors
controller drives these from its Polling drawer, which opens by itself after a connect.

Environment: `XO_CONNECTIONS_POLL_ENABLED` (default `true`, the hard off
switch for the background loop; Poll now still works) and
`XO_CONNECTIONS_POLL_TICK_S` (default 30, minimum 5: how often the loop looks
for connections whose interval has elapsed).

### Hand-editing

The files above tolerate edits; every read validates and drops what it
cannot use with a warning.

- Disable a feeder: set its switch to `false` in `ledger.json`.
- Re-read a feeder: delete its cursor in `ledger.json`. Connections then
  takes the last 24 hours again and issues the last 7 days; sharing re-reads
  whatever the relay still holds. Dedup by `source.key` still holds, so
  nothing comes back twice.
- Change how a section runs: edit `policy/<section>.json` (or `PUT
  /api/inbox/sections/{section}`), for example `"mode": "manual"` to stop
  automatic sessions, `"act": true` to let Send the draft go out.
- Remove a row: archive it from the item page (Reopen brings it back); the
  work item record stays in the project, its sidecars go after
  `retention_days`. A feeder fact you delete from the project's work items
  comes back while its source still reports it and its cursor has not
  passed it.

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
