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

- **Nothing is deleted.** Existing code paths stay until a later cleanup.
- **qq first, with a fallback.** Where a qq command takes over a job, the server runs the command
  first. Only when qq cannot run at all (not installed, timed out, no JSON answer) does it fall back
  to the old in-process code, and the server log says why. There is no setting to choose a path.
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
| Sharing: share, revoke, apply, members | The project sharing relay loop (see below) |
| Backup, restore (xo-projects-sync) | |
| Periodic batches, run as scheduler jobs: daily usage upload, GitHub issue mirror, connections collectors | |

## How the server calls qq

One helper in the server, `services/qq_runner.py`: `run_qq(args, timeout)` runs `qq <args> --json`
from the checkout through `utils.commands` (the executor every external command uses, so the run is
logged and redacted) and returns the exit code and the JSON object. When there is no JSON object it
raises one of two errors:

- `QQNotRun`: the operation never started (qq missing, bad usage, no Python: exit 2, 125, 126, 127).
  The route runs its in-process code instead.
- `QQNoAnswer`: it started but gave no answer (timed out, crashed). The operation may be half done,
  so a route whose operation is not safe to repeat (clone, remove, backup, restore, share) answers
  502 and does not run it again. Update is safe to repeat (fast-forward only) and still falls back.

A real answer from qq, a refusal included, is final. Routes become thin: guard and validate the
request, call qq, map the object to HTTP the way the old code did.

## Periodic work: the watcher's command scheduler

`services/cowork_agent/visualizer/watcher.py` ticks every second (`QUIRQ_WATCHER_INTERVAL_SECONDS`).
Step 7 of each tick calls `utils/commands/scheduler.tick()`, which launches due jobs (an argv plus a
mandatory timeout), never waits for them, never overlaps a job with itself, caps concurrency
(default 4), and keeps a log and run history per job. `run_now(job_id)` starts a job immediately.

So the watcher is already xo-space's clock and its scheduler already runs commands. Periodic work
becomes **built-in scheduler jobs that run qq commands**, registered by the server at start-up:
the watcher keeps time, the scheduler handles overlap and logging, qq does the work.

## Project sharing stays in the server

The relay keeps its own asyncio loop in the server (`project_sharing/poller.py`), exactly as before:
poll, fetch, auto-clone and publish every 60 s ±20 %, 5 s drain ticks while there is a backlog,
woken early by a nudge (share/revoke/apply/"Check now") or by a local push. It is a frequent,
stateful loop that must react to nudges at once, so a process per tick (status on disk, lock and
nudge files, scheduler round trips) would add cost and moving parts for nothing. There is no
`qq sharing tick` command. The sharing operations people run (share, revoke, apply, members,
status) are qq commands.

## Plan

1. **Contract and helper.** `run_qq` in the server; `--json` and the exit codes on the existing commands.
2. **Pilot: update.** Move `self_update`'s logic behind `qq update-check` / `qq update`; make
   `/space/update/*` call them, with the in-process fallback. Measure the latency.
3. **Lifecycle.** `qq start --background`, `qq restart`; `cowork-api.sh` becomes a wrapper.
4. **The periodic batches and operations.** Usage upload, GitHub mirror, connections,
   doctor (waits until the doctor is finished).

Done after the pilot: sharing operations, project add/remove, backup/restore (see below).

## Decisions (2026-10-09)

1. **Project sharing stays in the server.** Its relay loop runs as before, independent of the
   watcher; no `qq sharing tick` command (a tried scheduler-job version was reverted).
2. **The UI-read boundary.** Confirmed: constantly polled reads and chat streaming stay in-process.
3. **Pilot.** `update` first.

## Pilot: update

- `qq update-check --json` / `qq update --json` print the same objects the routes return today.
  For now the commands call `services.cowork_agent.self_update`, so the logic is shared; where the
  code lives is part of the later cleanup. What changes now is who calls whom.
- `/space/update/status` and `/space/update/apply` call `run_qq` first and return the command's
  object unchanged, so the Setup tab does not notice. When qq cannot run they fall back to the
  in-process `self_update` functions.
- Tests: `run_qq` (JSON, exit codes, timeout, qq missing) and both routes: answered by qq, qq's
  errors mapped to HTTP, and the fallback when qq cannot run.

## Batch 1: sharing operations, project add/remove, backup/restore

Each route runs its qq command first through `qq_first()` in `routers/qq_ops.py`; its old body is
now `<route>_in_process`, the fallback and also what the command runs (`python -m routers.qq_ops`),
so the logic and the HTTP errors are the same on both paths.

| Route | qq command |
| --- | --- |
| `POST /api/xo-projects/{id}/share`, `/revoke`, `/apply` | `qq share`, `qq revoke`, `qq apply` |
| `POST /api/xo-projects`, `DELETE /api/xo-projects/{id}` | `qq projects add`, `qq projects remove` |
| `POST /api/xo-projects-sync/projects/{id}`, `/all` | `qq backup PROJECT`, `qq backup --all` |
| `POST /api/xo-projects-sync/projects/{id}/restore`, `/all/restore` | `qq restore PROJECT`, `qq restore --all` |

What the move had to keep:

- **The relay's nudge.** It wakes the loop in the server's memory, so the route nudges after qq answers.
- **One backup at a time.** The services' asyncio locks cover one process only; the command takes a
  file lock (`xo-projects-sync.lock` in the state locks folder) around every backup and restore.
- **The request guards** (JSON, same origin, workspace id) run in the route, before qq.
- **Values stay values.** The route passes every value as `--name=VALUE`, so a project called
  `--all` is never read as a flag.
- **Tests.** `tests/__init__.py` makes qq "not installed" in tests, so route tests keep testing the
  in-process code with their mocks; `tests/test_qq_ops.py` covers the bridge.

Stays in the server: backup `/setup` (it writes the passphrase into the server's environment, and a
passphrase must not travel on a command line), the reads (status, members, commits, removal check,
backup list) and "Check now" (only a nudge).
