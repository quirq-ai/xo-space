# Sample state root: the folders of `~/.quirq/`

This folder is the sample of what XO Space keeps on one machine, outside every
project. `tests/test_quirq_state_layout.py` holds the code to it: every store
writes inside one of these folders, `services/storage/layout.py` names exactly
these folders, and an install from before the state root had folders is moved
into them.

```
~/.quirq/
├── projects/      one folder per project, named by pid, plus the Space timeline
├── inbox/         the Inbox
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
| `inbox/` | `inbox.json` | `services/inbox/` | Inbox items and what you marked done |
| `connections/` | `accounts.json`; `<toolkit>/config.json`, `state.json`, `events.jsonl` | `services/connections/` | what each connection collected |
| `scheduler/` | `jobs.json`, `state.json`, `runs/<id>.jsonl` | `utils/commands/scheduler.py` | saved commands and their run history |
| `sharing/` | `<repo>-<hash>.json`, `removed/` | project sharing | where sharing stopped reading, and removal decisions |
| `usage/` | `<agent>.json` | `services/usage_sync.py` | how far usage was reported, so it would be sent again |
| `settings/` | `roots.env`, `runtime.env`, `onboarding.json` | the Setup tab, onboarding | choices you would enter again |
| `secrets/` | `secrets.env`, `token.json` | the Setup tab, the GitHub and Vercel connectors | credentials; uninstall keeps this folder |
| `cache/` | `graph.json`, `dashboard.json`, `sessions.json`, `stats.json`, `sessions/`, `heartbeat.json`, `activity/` | the watcher | nothing: rebuilt automatically |
| `logs/` | `quirq.log`, `commands.log`, `scheduler/<id>.log` | `install.sh`, `utils/commands/` | diagnostics only |
| `.locks/` | lock sentinels | `services/storage/flock.py` | nothing |

A cursor lives next to the data it advances, so a reset wipes both or neither:
deleting `projects/` also deletes `offsets.json`, and the watcher starts over
instead of replaying sessions onto surviving totals.

## The rules for what goes inside a file

1. Project data is keyed by `pid`; a folder name is only a label.
2. Times are ISO-8601 UTC ending in `Z` (milliseconds on event lines).
3. Every event line starts with `ts` and `type`.
4. Every data file carries a `schema` number. Rebuilt views in `cache/` are
   exempt.

## Adding a store

1. Put its files in its subject's folder. A new data source copies
   `connections/<toolkit>/`: `config.json` (what to collect), `state.json`
   (where it stopped), `events.jsonl` (what arrived).
2. A new subject gets a folder: name it once in `services/storage/layout.py`
   and add it to this sample. The test fails until both agree.
3. If existing files move, add a `Move` to `layout.MOVES`.

## Kept outside the state root on purpose

- `~/.config/composio/`: the Composio stores, where `COMPOSIO_STORE_DIR` and the
  compose file expect them.
- `~/.config/rclone/rclone.conf` and `~/.config/gh/`: those tools' own config.
- `BACKUP_PASSWORD` in the checkout's `.env`.
- `/tmp/xo-space.{pid,lock,log}`: the `cowork-api.sh` daemon's process files.

## Tracking this sample in git

Each folder holds a `.gitkeep` so git tracks it. The repository's `.gitignore`
ignores `logs/` directories, which also hides `logs/.gitkeep` here: add it with
`git add -f tests/fixtures/quirq-state/logs/.gitkeep`.
