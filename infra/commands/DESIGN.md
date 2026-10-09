# qq as xo-space's operational interface: design note

Status: proposal for review, 2026-10-09. Branch `qq-commands`. Nothing here is built yet beyond the
commands listed in `README.md`, which today call the server's HTTP API.

## Goal

xo-space's operations become `qq` commands, and everything else calls them: the Space UI (through
the server), the server's lifespan and periodic work, cloud scripts, people and agents. One way to do
each operation, usable from anywhere.

```
UI button ─► server route ─────────┐
watcher's command scheduler ───────┼─►  qq <operation> --json  ─►  the logic
terminal, agent, cloud script ─────┘
```

## Ground rules

- **Nothing is deleted.** Existing code paths stay until a later cleanup. Where a qq command takes
  over a job, a setting chooses between the old path and the new one, defaulting to the old.
- **Not everything becomes a command.** See "What becomes a qq command" below.
- The commands invert gradually: today `infra/commands/*.sh` call the server; each migrated
  operation moves its logic into the command, and the server calls the command instead.

## The command contract

Code will depend on these rules, so every command follows them:

| Rule | Detail |
| --- | --- |
| Output | `--json`: one JSON object on stdout. Without it, short human text. Errors and progress on stderr. |
| Exit codes | 0 ok · 1 failed or refused · 2 bad usage · 3 pending/in progress. qq's own errors are 125. |
| Non-interactive | Never prompts. Destructive commands need an explicit flag (`--yes`, `--force`). |
| Bounded | Every command has a timeout; the caller sets one too. |
| Idempotent | Running it twice is safe. Where two runs could collide, a lock file under `<state>/.locks/`. |
| State on disk | A command is a fresh process: anything another run or the server needs is written under `<state>/`, never kept only in memory. |
| Environment | Reads the same `.env`, `QUIRQ_STATE_ROOT`, `XO_PROJECTS_ROOT` and runtime settings as the server, so both act on the same install. |

## What becomes a qq command

Test: an operation with a clear start and end, useful outside the server, that tolerates roughly
0.5 s of process start-up (`qq --version` measured 0.2 s on a warm launcher, plus Python start-up).

| Becomes a qq command | Stays in the server |
| --- | --- |
| Lifecycle: start, stop, restart, background mode for cloud (one model for `install.sh` and `cowork-api.sh`) | The **watcher** and the **command scheduler**: the clock must be long-lived |
| install, deps, uninstall, update-check, update | Telemetry ingestion (every 1 s) |
| doctor and its move-aside fix | Chat and streaming (`/api/chat/*`): a live connection |
| checks, unit | UI reads polled constantly: project list, inbox list, sessions, usage summaries, file trees, status. qq may offer the same views for terminals, but the UI keeps reading in-process |
| Project add, clone, remove | Request guards, auth checks, sign-in flows |
| Sharing: share, revoke, apply, members, **tick** | |
| Backup, restore (xo-projects-sync) | |
| Periodic batches, run as scheduler jobs: daily usage upload, GitHub issue mirror, connections collectors, sharing tick | |

## How the server calls qq

One helper in the server, `run_qq(args, timeout) -> dict`: run `qq <args> --json` as a subprocess
from the checkout, with this install's environment; parse stdout; map exit codes to HTTP (1 → 409
with the command's message, 2 → 422, timeout → 504). Routes become thin: validate the request, call
`run_qq`, return its JSON. The old in-process function stays behind the setting.

## Periodic work: the watcher's command scheduler

`services/cowork_agent/visualizer/watcher.py` ticks every second (`QUIRQ_WATCHER_INTERVAL_SECONDS`).
Step 7 of each tick calls `utils/commands/scheduler.tick()`, which launches due jobs (an argv plus a
mandatory timeout), never waits for them, never overlaps a job with itself, caps concurrency
(default 4), and keeps a log and run history per job. `run_now(job_id)` starts a job immediately.

So the watcher is already xo-space's clock and its scheduler already runs commands. Periodic work
becomes **built-in scheduler jobs that run qq commands**, registered by the server at start-up:
the watcher keeps time, the scheduler handles overlap and logging, qq does the work.

## Project sharing on the scheduler

Today the relay is its own asyncio loop (`project_sharing/poller.py`): poll, fetch, auto-clone and
publish every 60 s ±20 %, 5 s drain ticks while there is a backlog, woken early by a nudge
(share/revoke/apply/"Check now") or by a local push (a cheap 5 s scan of `origin/<branch>` refs).

Proposed: a built-in job running `qq sharing tick --json` every 60 s (timeout about 10 minutes).
Running the relay's tick inside the watcher's own step instead was considered and rejected: a tick
does network calls and `git fetch`/`clone` (seconds to minutes), and the watcher's tick is
synchronous with a ~1 s budget, so ingestion would stall.

What has to change first:

| Today | With the scheduler |
| --- | --- |
| Status in memory (`project_sharing/status.py`), read by `/api/project-sharing/status`, the Inbox `sharing` feeder (`services/inbox/feeders.py:204`) and doctor (`services/doctor/liveness.py:577`) | Each tick writes its status, including the recent transitions, to `<state>/sharing/status.json`; those readers read the file. Cursors and bookmarks are already on disk. |
| A nudge wakes the loop | share/revoke/apply/check call `scheduler.run_now("sharing-tick")` |
| A local push is noticed within ~5 s | The watcher's tick runs the same cheap ref scan and calls `run_now` when it changes |
| 5 s drain ticks | The command keeps ticking while the swarm says `has_more` (bounded), then exits |
| ±20 % jitter spreads a fleet | Lost unless the job adds a random delay; acceptable for now |

A setting selects the driver, `PROJECT_SHARING_DRIVER=loop|watcher`, default `loop` until the
watcher driver is proven. The old loop is not deleted.

## Plan

1. **Contract and helper.** `run_qq` in the server; `--json` and the exit codes on the existing commands.
2. **Pilot: update.** Move `self_update`'s logic behind `qq update-check` / `qq update`; make
   `/space/update/*` call them. Measure the latency. Old code stays behind a setting.
3. **Sharing tick on the scheduler.** Status on disk, `qq sharing tick`, the built-in job, run_now
   for nudges, the watcher's local-change check, the driver setting.
4. **Lifecycle.** `qq start --background`, `qq restart`; `cowork-api.sh` becomes a wrapper.
5. **The other periodic batches and operations.** Usage upload, GitHub mirror, connections,
   doctor, backup/restore, project add/remove.

## Decisions (2026-10-09)

1. **Sharing depends on the watcher.** Accepted: when the watcher is off, sharing is off, and the
   Space UI says so in text (the reason it is off), instead of looking broken.
2. **The UI-read boundary.** Confirmed: constantly polled reads and chat streaming stay in-process.
3. **Pilot order.** `update` first, then the sharing tick.

## Pilot: update

- Switch: `QUIRQ_QQ_OPERATIONS`, a comma-separated list of operations the server runs through qq
  (`update` for the pilot). Unset means the old in-process path, for every operation.
- `qq update-check --json` / `qq update --json` print the same objects the routes return today.
  For now the commands call `services.cowork_agent.self_update`, so the logic is shared; where the
  code lives is part of the later cleanup. What changes now is who calls whom.
- `/space/update/status` and `/space/update/apply` call `run_qq` when `update` is switched on, and
  return the command's object unchanged, so the Setup tab does not notice.
- Tests: `run_qq` (JSON, exit codes, timeout, qq missing) and both routes in both modes.
