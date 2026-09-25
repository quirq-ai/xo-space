# Sample state root: the folders of `~/.quirq/`

This folder is the sample of what XO Space keeps on one machine, outside every
project. `tests/test_quirq_state_layout.py` holds the code to it: every store
writes inside one of these folders, `services/storage/layout.py` names exactly
these folders, and an install from before the state root had folders is moved
into them.

```
~/.quirq/
├── projects/      one folder per project, named by pid, plus the Space timeline
├── sessions/      the sessions started with no project (they run in the projects root)
├── inbox/         the Inbox; activity/ holds the command log
├── connections/   one folder per connection
├── scheduler/     saved commands and their run history
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
| `projects/` | `<pid>/timeline.jsonl`, `<pid>/stats.json`, `<pid>/sessions/`, `<pid>/github/issues.json`, `<pid>/workitems/claims.json`; `timeline.jsonl` for the whole Space; `offsets.json` and `<source>-offsets.json`, where the watcher stopped reading | the watcher; the todo, workitem and claim APIs | history nothing can rebuild |
| `sessions/` | `sessionslist.d/<shard>.json`, one row per session started with no project (it belongs to no `projects/<pid>/`, so it lives one level up, where no project key can collide with it) | the chat adapters | those sessions vanish from the session list, messages and transcript |
| `inbox/` | `inbox.json`; `activity/commands.log`, `activity/archive/commands.<stamp>.log` | `services/inbox/`, `utils/commands/` | Inbox items and what you marked done; the record of every command Quirq ran (the archive is every earlier command log) |
| `connections/` | `accounts.json`; `<toolkit>/config.json`, `state.json`, `events.jsonl` | `services/connections/` | what each connection collected |
| `scheduler/` | `jobs.json`, `state.json`, `runs/<id>.jsonl` | `utils/commands/scheduler.py` | saved commands and their run history |
| `sharing/` | `<repo>-<hash>.json`, `removed/` | project sharing | where sharing stopped reading, and removal decisions |
| `usage/` | `<agent>.json` | `services/usage_sync.py` | how far usage was reported, so it would be sent again |
| `settings/` | `roots.env`, `runtime.env`, `onboarding.json` | the Setup tab, onboarding | choices you would enter again |
| `secrets/` | `secrets.env`, `token.json` | the Setup tab, the GitHub and Vercel connectors | credentials; uninstall keeps this folder |
| `cache/` | `graph.json`, `dashboard.json`, `sessions.json`, `stats.json`, `sessions/`, `heartbeat.json`, `activity/` | the watcher | nothing: rebuilt automatically |
| `logs/` | `quirq.log`, `scheduler/<id>.log` | `install.sh`, `utils/commands/` | diagnostics only |
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

1. Put its files under the Space UI section and page that shows them,
   `<section>/<page>/` (sections: `projects`, `agents`, `inbox`, `setup`): the
   command log, shown with the Space's activity, is `inbox/activity/`. The
   older top-level folders keep their names until the whole layout follows
   the UI. A new data source copies
   `connections/<toolkit>/`: `config.json` (what to collect), `state.json`
   (where it stopped), `events.jsonl` (what arrived).
2. A new subject gets a folder: name it once in `services/storage/layout.py`
   and add it to this sample. The test fails until both agree.
3. If existing files move, add a block of `Move`s to `MOVES` in
   `services/storage/migrations.py`. An old copy of a log or other history
   moves into the archive rather than onto the live file, so it can never
   find its new home already taken.

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
- adapter cursor files in `projects/` (`<source>-offsets.json`), whose shape
  belongs to each adapter;
- rotated segments (`timeline.<stamp>.jsonl`, `events.<stamp>.jsonl`,
  `inbox/activity/archive/commands.<stamp>.log`), older copies of the files shown.

`tests/test_quirq_state_layout.py` checks that the examples follow the four
rules, match their JSON schemas, and read back through the stores that own
them.

## Tracking this sample in git

The repository's `.gitignore` hides two parts of this sample: `logs/`
directories and `token.json` files, a guard for real credentials. The examples
here hold none, so add them with
`git add -f tests/fixtures/quirq-state/logs tests/fixtures/quirq-state/secrets/token.json`.
