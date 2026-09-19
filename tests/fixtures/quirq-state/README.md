# Sample state root: the folders of `~/.quirq/`

This folder is the sample of what XO Space keeps on one machine, outside every
project. `tests/test_quirq_state_layout.py` holds the code to it: every store
writes inside one of these folders, `services/storage/layout.py` names exactly
these folders, and an install from before the state root had folders is moved
into them.

```
~/.quirq/
├── projects/      one folder per project, named by pid, plus the Space log (events with no pid)
├── inbox/         the Inbox
├── connections/   one folder per connection
├── jobs/          saved commands and their run history (modules/jobs)
├── sharing/       shared repositories this machine has already seen
├── usage/         how far usage has been reported to XO
├── settings/      Space-wide choices
├── secrets/       credentials (owner-only)
├── cache/         safe to delete: rebuilt automatically
├── logs/          safe to delete
└── .locks/        internal
```

## What each folder holds

| Folder | Files | Written by | Delete it and you lose |
|---|---|---|---|
| `projects/` | `<pid>/timeline.jsonl`, `<pid>/stats.json`, `<pid>/sessions/`, `<pid>/github/issues.json`, `<pid>/workitems/claims.json`; `timeline.jsonl`, the Space log (events with no pid; every line is written once); `offsets.json` and `<source>-offsets.json`, where the watcher stopped reading | `modules/telemetry/` (the watcher), `modules/projects/`, `modules/sessions/`, `modules/timeline/` | history nothing can rebuild |
| `inbox/` | `inbox.json` | `services/inbox/` | Inbox items and what you marked done |
| `connections/` | `accounts.json`; `<toolkit>/config.json`, `state.json`, `events.jsonl` | `modules/connections/` | what each connection collected |
| `jobs/` | `jobs.json`, `state.json`, `runs/<id>.jsonl` | `modules/jobs/` | saved commands and their run history |
| `sharing/` | `state.json`, `events.jsonl`, `<repo>-<hash>.json`, `removed/` | `modules/sharing/` | the relay's last snapshot and event history, where sharing stopped reading, and removal decisions |
| `usage/` | `<agent>.json` | `modules/telemetry/` | how far usage was reported, so it would be sent again |
| `settings/` | `roots.env`, `runtime.env`, `onboarding.json`, `modules.json` | `modules/settings/`, the kernel (`modules.json`) | choices you would enter again, and the module switches |
| `secrets/` | `secrets.env`, `token.json` | the Setup tab, the GitHub and Vercel connectors | credentials; uninstall keeps this folder |
| `cache/` | `graph.json`, `dashboard.json`, `sessions.json`, `stats.json`, `sessions/`, `heartbeat.json`, `activity/` | `modules/telemetry/` (the watcher), `modules/projects/` (the graphs) | nothing: rebuilt automatically |
| `logs/` | `quirq.log`, `commands.log`, `jobs/<id>.log` | `install.sh`, `utils/commands/` | diagnostics only |
| `.locks/` | lock sentinels | `services/storage/flock.py` | nothing |

A cursor lives next to the data it advances, so a reset wipes both or neither:
deleting `projects/` also deletes `offsets.json`, and the watcher starts over
instead of replaying sessions onto surviving totals.

## The rules for what goes inside a file

1. Project data is keyed by `pid`; a folder name is only a label.
2. Times are ISO-8601 UTC ending in `Z` (milliseconds on event lines).
3. Every event line starts with `ts` and `type`.
4. Every data file carries a `schema` number. Exempt: rebuilt views in
   `cache/`, and files keyed by name, where an extra key would read as an entry
   (`secrets/token.json` by provider, session-index shards by session).

## Adding a store

1. Put its files in its subject's folder. A new data source copies
   `connections/<toolkit>/`: `config.json` (what to collect), `state.json`
   (where it stopped), `events.jsonl` (what arrived).
2. A new module gets a folder: name it in its `module.json` (`folder`), declare its
   files in `store.py` (`FILES`) and add examples here. `tests/test_modules.py` and
   `tests/test_quirq_state_layout.py` fail until the three agree; the table below is
   generated from `FILES` by `scripts/write_layout_docs.py`.
3. If existing files move, add a `Move` to `layout.MOVES`.

## Kept outside the state root on purpose

- `~/.config/composio/`: the Composio stores, where `COMPOSIO_STORE_DIR` and the
  compose file expect them.
- `~/.config/rclone/rclone.conf` and `~/.config/gh/`: those tools' own config.
- `BACKUP_PASSWORD` in the checkout's `.env`.
- `/tmp/xo-space.{pid,lock,log}`: the `cowork-api.sh` daemon's process files.

## The example files

Every folder holds example files for one project, `sample-project`, with the
same pid (`00000000-0000-4000-8000-000000000000`) as the `.xo/` sample in
`tests/fixtures/xo-project/`. Values are placeholders: paths start at
`/home/you`, credentials read `replace-with-...`, and the agent is
`sample_agent`. Names the code derives (session-index shards, sharing bookmarks
and removal markers, lock sentinels) are the names it would give.

Three kinds of file have no example, on purpose:

- the UI's views in `cache/` (`graph.json`, `dashboard.json`, `sessions.json`),
  whose shape belongs to their builders;
- rotated segments (`timeline.<stamp>.jsonl`, `events.<stamp>.jsonl`,
  `commands.log.1`), older copies of the files shown.

`tests/test_quirq_state_layout.py` checks that the examples follow the four
rules, match their JSON schemas, and read back through the stores that own
them.

## Tracking this sample in git

The repository's `.gitignore` hides two parts of this sample: `logs/`
directories and `token.json` files, a guard for real credentials. The examples
here hold none, so add them with
`git add -f tests/fixtures/quirq-state/logs tests/fixtures/quirq-state/secrets/token.json`.

## The files every module declares

<!-- generated: files -->

Generated by `scripts/write_layout_docs.py` from every module's `FILES`; do not edit by hand.

| File | Module | Role | Kept | Delete it and you lose |
|---|---|---|---|---|
| `<project>/.xo/peers.json` | projects | record |  | history nothing rebuilds (who the project is shared with) |
| `<project>/.xo/project.json` | projects | record |  | history nothing rebuilds (the project's identity: pid, name, owner, created_at, display name) |
| `<project>/.xo/todos.json` | projects | record |  | history nothing rebuilds (session-scoped todos, written through the todo API) |
| `<project>/.xo/workitems.json` | projects | record |  | history nothing rebuilds (durable work items, local and adopted from GitHub) |
| `cache/activity/projects/<project>.json` | telemetry | cache |  | nothing; rebuilt automatically (which sessions are open right now in one project) |
| `cache/activity/workspace.json` | telemetry | cache |  | nothing; rebuilt automatically (which sessions are open right now, across every project) |
| `cache/heartbeat.json` | telemetry | cache |  | nothing; rebuilt automatically (the watcher's once-per-tick liveness beat) |
| `cache/sessions/sessions-augment.json` | telemetry | cache |  | nothing; rebuilt automatically (every project's session counts, one map) |
| `cache/sessions/sessionslist.json` | telemetry | cache |  | nothing; rebuilt automatically (every project's session index, one map) |
| `cache/stats.json` | telemetry | cache |  | nothing; rebuilt automatically (the workspace total of every project's stats.json) |
| `connections/<toolkit>/config.json` | connections | decision |  | choices you would enter again (on or off, the interval, which data to collect) |
| `connections/<toolkit>/events.jsonl` | connections | record | 2 MB, keep 3 | history nothing rebuilds (what arrived, newest last) |
| `connections/<toolkit>/state.json` | connections | fact |  | nothing; it is fetched again (cursors and the last result) |
| `connections/accounts.json` | connections | fact |  | nothing; it is fetched again (which account each toolkit's session is bound to) |
| `jobs/jobs.json` | jobs | decision |  | choices you would enter again (saved commands: what runs, how often, with what timeout) |
| `jobs/runs/<id>.jsonl` | jobs | record | 8 MB, keep 3 | history nothing rebuilds (one line per run, newest last) |
| `jobs/state.json` | jobs | fact |  | nothing; it is fetched again (next run, running since, last result) |
| `projects/<pid>/github/issues.json` | projects | fact |  | nothing; it is fetched again (the GitHub issue mirror the poller keeps; fetched again) |
| `projects/<pid>/sessions/sessions-augment.json` | telemetry | cache |  | nothing; rebuilt automatically (one project's message, tool and task counts per session) |
| `projects/<pid>/sessions/sessionslist.d/<shard>.json` | sessions | record |  | history nothing rebuilds (the adapter's index row plus purpose) |
| `projects/<pid>/stats.json` | telemetry | cache |  | nothing; rebuilt automatically (one project's rolling token, tool and model totals, by session and by day) |
| `projects/<pid>/timeline.jsonl` | timeline | record | 8 MB, keep 5 | history nothing rebuilds (what happened in one project, newest last; every line carries the pid) |
| `projects/<pid>/workitems/claims.json` | projects | record |  | history nothing rebuilds (which session is working which workitem; in_progress is derived from it) |
| `projects/<source>-offsets.json` | telemetry | cache |  | nothing; rebuilt automatically (where the watcher stopped reading; a cursor beside the history it counts, one per adapter source that keeps its own) |
| `projects/offsets.json` | telemetry | cache |  | nothing; rebuilt automatically (where the watcher stopped reading; a cursor beside the history it counts) |
| `projects/timeline.jsonl` | timeline | record | 8 MB, keep 5 | history nothing rebuilds (Space-level events, those with no pid) |
| `secrets/secrets.env` | settings | secret |  | credentials (the write-only secret store, one KEY=value per line; the active agent's env) |
| `secrets/token.json` | settings | secret |  | credentials (connector tokens by provider) |
| `settings/modules.json` | settings | decision |  | choices you would enter again (the module switches; written by the kernel) |
| `settings/onboarding.json` | settings | decision |  | choices you would enter again (whether the first-run flow was completed, and when) |
| `settings/roots.env` | settings | decision |  | choices you would enter again (the saved projects root and state root; read at startup) |
| `settings/runtime.env` | settings | decision |  | choices you would enter again (the runtime settings: the agent for new chats and the watcher; read at startup) |
| `sharing/<bookmark>` | sharing | fact |  | nothing; it is fetched again (<repo>-<hash>.json: where the relay stopped reading and reporting, and when it cloned) |
| `sharing/events.jsonl` | sharing | record | 2 MB, keep 3 | history nothing rebuilds (what happened: shared with you, fetched, cloned, revoked, errors) |
| `sharing/removed/<marker>` | sharing | decision |  | choices you would enter again (you removed your local copy; it is never cloned here again by itself) |
| `sharing/state.json` | sharing | fact |  | nothing; it is fetched again (the relay's last snapshot: parked reason, last poll, each repo's status) |
| `usage/<agent>.json` | telemetry | fact |  | nothing; it is fetched again (how far usage was reported to XO, per agent, with the last key probe) |

<!-- /generated: files -->
