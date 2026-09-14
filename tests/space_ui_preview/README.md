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
The output directory receives 1440 × 1000 Dashboard, Projects List and
expanded project screenshots, 320px and 375px screenshots, and `report.json`.
`--screenshots-only` skips the issue #100 navigation assertions for comparing
the old interface. The browser fixes relative timestamps and seeds graph
layout randomness; images are unmodified captures of the rendered app.

The full check verifies the six-tab order, Dashboard default, all five
lenses and existing hash routes, stationary lens controls, retention of an
open historical file in source mode across lens switches (including real
Dashboard/Graph dataset reloads), closing the preview when leaving Projects,
the local Wiki resource and its deep link, number keys 1–6, and lens-switch
position relative to the content at 320px and 375px (the contextual toolbar
can change the mobile header height). It fails
on console errors, uncaught page errors and unsuccessful HTTP responses.

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

## Setup Commands and restart

With the read-only fixture above running, exercise Commands, restart UI states,
save/poll races, validation conflicts, and desktop/mobile layouts:

```sh
node tests/space_ui_preview/commands-restart.mjs /tmp/space-commands-review
```

This script intercepts mutations with browser fixtures; it never executes a
command or restarts a process. It checks that all three restart buttons wait for
a changed server instance before reloading.

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
result, output, and retained history with automatic jobs disabled. Set
`SPACE_COMMANDS_URL` to change its URL. Both scripts refuse port 5002.

Stop both servers with Ctrl-C after testing. The temporary command state is
removed on exit. Live-test screenshots contain local test paths and are intended
for private review; use fictional data for published screenshots.
