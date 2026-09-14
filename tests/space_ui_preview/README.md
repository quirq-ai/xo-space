# Space UI browser review

The server serves the **actual `space_ui/` assets** from this checkout and
supplies deterministic, fictional API data. It does not start an agent,
read private workspace files, call upstream services, or support writes.
The ten projects and all activity, people, paths and repository names are
invented review fixtures.

Start the server (Python 3.9+; standard library only):

```sh
python3 tests/space_ui_preview/server.py --port 5100
```

Open `http://127.0.0.1:5100/space/` to inspect it manually. Stop with Ctrl-C.

With Node.js and Playwright (including its Chromium browser) installed, run:

```sh
node tests/space_ui_preview/capture.mjs /tmp/space-ui-issue-100
```

For an existing Playwright installation, set `PLAYWRIGHT_MODULE` to its
`index.mjs` file. `PLAYWRIGHT_CHROMIUM_EXECUTABLE` optionally selects an existing
Chromium executable. Set `SPACE_PREVIEW_URL` to use another local server port.
The output directory receives 1440 × 1000 Projects Overview, List and
expanded project screenshots, 320px and 375px screenshots, and `report.json`.
`--screenshots-only` skips the issue #100 navigation assertions for comparing
the old interface. The browser fixes relative timestamps and seeds graph
layout randomness; images are unmodified captures of the rendered app.

The full check verifies the four primary sections, Projects Overview default,
Overview / Data / Timeline / Manage, the Data List / Graph / Tree modes and
canonical routes, native secondary links, historical
file previews across projection changes, closing the preview when leaving
Projects, the local Wiki resource, number keys 1–4, and responsive navigation.

`projects-root.mjs` checks direct List, Tree and Manage loads, root search
without booting a hidden graph, selection into Data Graph, browser history,
Timeline root selection, and leaving Projects during a pending metadata read. It blocks service writes
and external requests; run it with the same environment variables as `capture.mjs`.
It fails on console errors, uncaught page errors and unsuccessful HTTP responses.

For the complete section and route contract, run:

```sh
node tests/space_ui_preview/section-navigation.mjs /tmp/space-section-navigation
node tests/space_ui_preview/projects-experience.mjs /tmp/space-projects-experience
```

The section check covers canonical `projects/data/{list,graph,tree}` URLs and legacy
`projects/files` aliases, section defaults
versus List, Back/Forward, native links, toolbar ownership, List and Setup state
across map changes, Inbox Activity and Sharing activity ordering, and Sharing
legacy aliases resolving to Inbox. It captures all Projects pages
and representative Agents, Inbox and Setup pages at 1440px, 390px and 320px,
including content clearance below secondary navigation. The Projects check
covers catalog availability, filtering, Manage pins reflected in the Data Pinned
filter, the absence of management actions in Data rows, file browsing, retained
file drawers, and out-of-order folder responses. All service writes stay blocked or inside
explicit browser-owned fixtures.

For inline project sharing and the separate activity feeds:

```sh
node tests/space_ui_preview/inline-sharing.mjs /tmp/space-inline-sharing
node tests/space_ui_preview/inbox-activity.mjs /tmp/space-inbox-activity
node tests/space_ui_preview/project-actions.mjs /tmp/space-project-actions
```

The inline check exercises Manage: Space ID validation,
cancellation, retained drafts, unchanged routes, mocked error/success responses,
and duplicate submission protection after leaving and returning. The activity check covers
workspace history, scoped todos and session details, pagination, repository events, independent
search/selection, escaped payloads, partial errors and late reads. Both capture
1440px, 390px and 320px layouts. The actions check verifies per-page data refresh,
retained filters/root/drawers, and the clone form inside Manage. Every write is
blocked or handled by an explicit in-memory fixture.

For contextual toolbar coverage, use the same server and Playwright settings:

```sh
node tests/space_ui_preview/contextual-toolbar.mjs /tmp/space-contextual-toolbar
node tests/space_ui_preview/search-sessions-inbox.mjs
```

These checks cover page-specific controls, query restoration, clear and keyboard
behavior, responsive headers, and filtering without graph navigation. The toolbar
check supplies synthetic Connector/account and Quirq responses and blocks external
requests and service writes. The Sessions/Inbox check supplies synthetic telemetry
and Inbox responses, including
an in-memory bulk action, to verify source filters, pagination, loaded counts,
and hiding search on session charts/detail. No real Inbox data is modified.

This is a browser regression check of the frontend and its API contracts.
The fixture server is deliberately not a substitute for backend tests.

## Guided Setup, Commands and restart

With the read-only fixture above running, exercise Commands, restart UI states,
save/poll races, validation conflicts, and desktop/mobile layouts:

```sh
node tests/space_ui_preview/setup-state.mjs
node tests/space_ui_preview/setup-journey.mjs /tmp/space-setup-journey
node tests/space_ui_preview/setup-connectors.mjs /tmp/space-setup-connectors
node tests/space_ui_preview/setup-projects.mjs /tmp/space-setup-projects
node tests/space_ui_preview/manage-refresh-races.mjs /tmp/space-manage-refresh-races
node tests/space_ui_preview/manage-details.mjs /tmp/space-manage-details
node tests/space_ui_preview/native-connectors.mjs /tmp/space-native-connectors
node tests/space_ui_preview/setup-identity.mjs /tmp/space-setup-identity
node tests/space_ui_preview/commands-restart.mjs /tmp/space-commands-review
node tests/space_ui_preview/inbox-jobs.mjs /tmp/space-inbox-jobs-review
node tests/space_ui_preview/command-results-races.mjs
```

The Setup journey check covers section/Next navigation, selected-agent access,
separate Secrets management, masked values, independent Agent/Activity saves, drafts during slow initial
loads and refresh/save races, unavailable status, and desktop/mobile layouts.
All settings and credential writes use fictional browser fixtures. The pure
state check covers pending-change priority and factual summaries without
inferring authentication or live activity from installation checks.

The project check (`setup-projects.mjs`, retained filename) covers the dedicated
Projects Manage page: Git cloning, individual access revocation, local-roster
removal, typed deletion confirmation, stale replies, changed memberships, retained
drafts and refreshed project lists. The read-only preview shows a shared removal
review for Aurora Console; browser tests intercept all project mutations. Backend
tests separately exercise file deletion and clone publication in temporary folders.
The Manage refresh check holds catalog and access reads across a section change,
then verifies re-entry fetches current data and never enables deletion from an old
access response. It only uses fictional GET responses. The Manage details check
covers the single-open accordion, collapsed Activity and pin actions, reload and
cross-tab pin persistence, failed-storage feedback, keyboard copy/tooltips, metadata refresh focus,
lazy Issues, retained issue filters and recorded closed
history, safe GitHub URL copying, independent row actions and the Inbox activity
handoff. Clipboard operations and API responses stay inside the browser fixture. Add
`--screenshots-only` to capture normal collapsed/expanded Manage and selected-project
Activity states without repeating the full behavioral checks. `--focus-only`
isolates a held Issues refresh that removes repository controls, checking focus
restoration and preserving a newer user selection.

The Setup Connectors check covers lazy loading, legacy links, shared navigation,
retained search and polling drafts, authorization during section changes, and
desktop/mobile layouts. Connector requests use browser fixtures. The identity check covers verified, unavailable and unconfigured accounts, independent error states and refresh races.

The Commands/restart script intercepts mutations with browser fixtures; it never executes a
command or restarts a process. It checks that all three restart buttons wait for
a changed server instance before reloading.

The Inbox Jobs check uses synthetic schedule definitions and histories to verify
the section order, interval/disabled status, empty/error recovery, refresh races,
and results access at desktop and mobile widths. It never runs a command or
creates service Inbox items.

The results check covers completion ordering, automatic output updates,
close/reopen races, escaped output, and restoring keyboard focus.

For a real scheduler round trip, start a second server with the project's Python
environment, leaving the read-only fixture on port 5100:

```sh
./venv/bin/python tests/space_ui_preview/commands_server.py --port 5112 --fixture-port 5100
node tests/space_ui_preview/commands-live.mjs /tmp/space-commands-review
```

The second server uses temporary scheduler state, disables automatic jobs, and
exposes no process-control writes. Explicitly submitted commands still execute
with the local user's permissions. The browser check saves and runs only a
Python print command in that temporary directory, then verifies the real API
result, output, and retained history with automatic jobs disabled.
The check then adds an interval to its own test definition, opens that job's
results from Inbox, and removes the definition on completion. Set
`SPACE_COMMANDS_URL` to change its URL. The live server and browser check refuse port 5002.

Stop both servers with Ctrl-C after testing. The temporary command state is
removed on exit. Live-test screenshots contain local test paths and are intended
for private review; use fictional data for published screenshots.
