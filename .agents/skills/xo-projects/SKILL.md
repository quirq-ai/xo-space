---
name: xo-projects
description: Use whenever an agent creates, operates inside, or manages an xo-project. Covers scaffolding a new xo-project via the xo-cowork-api; the boot ritual, canonical file map, and closing conventions for a project folder (AGENTS.md is the operating contract); recording and updating todos across a session; and backing up, restoring, syncing, snapshotting, or migrating xo-projects through the GitHub-backed sync API. Trigger this for any mention of xo-projects, .xo/, xo-cowork-api, or AGENTS.md, and for requests to back up / restore / sync / snapshot / migrate project state — even when the user doesn't name the skill explicitly. Four hard constraints — new xo-projects are created only via the xo-cowork-api; the agent never writes to .xo/ (Quirq services own that directory, and todos go through the todo HTTP API — a native todo tool does not reach it); backups and restores happen only through the sync API (never via local tar, zip, cp, rsync, or pushing to an external git remote); and when this skill covers a task, the skill is the way to do it — no improvising with shell commands or ad-hoc copies because the proper tool feels heavy.
---

# xo-projects

This skill covers the whole lifecycle of an xo-project. It does three things:

1. **Four hard constraints** (enforced outside the agent's judgment):
   - New xo-projects are created via the xo-cowork-api only.
   - The agent does not write to `.xo/` — Quirq services own the entire directory. Todos are the one thing an agent *drives*, and they go through the todo HTTP API (Part 3), for every runtime; a native todo tool does not reach `.xo/todos.json`.
   - **Backups and restores happen only through `/api/xo-projects-sync/*`.** Never `tar`, `zip`, `cp`, `rsync`, or `git push` the project to a local archive or external remote — even when the user just says "back this up" or "save this somewhere safe." Local copies miss the encryption, the manifest, and the secret excludes the API enforces, and they can't be discovered or restored by `GET /projects`.
   - **When this skill covers a task, the skill is the way to do it.** Creating a project, recording todos, backing up, restoring — use the documented endpoints and native tools. Don't substitute shell commands, ad-hoc file copies, or local approximations because the proper tool feels heavy. If a documented tool fails, surface the failure to the human; don't silently roll your own.
2. **A guide to every file in an xo-project:** what it's for, how it lives across the session, and the reasoning behind the conventions — so the agent can apply judgment when an edge case appears.
3. **Pointers to two reference files** for situational, API-heavy detail — the todo HTTP API (**every** runtime records todos through it) and the backup/restore API — so they load only when the task actually calls for them.

## Base URL

```
http://${HOST:-localhost}:${PORT:-5002}
```

## How this skill is organized

Read this file top to bottom on first contact — the four parts below are all here in full, including todo discipline, which every session needs. Two reference files hold detail you only reach for situationally:

- **`references/todos-http-api.md`** — the todo HTTP endpoint schemas. Every runtime needs this, including runtimes with a native todo tool of their own; read it the first time you record a todo in a session (Part 3).
- **`references/backup-restore.md`** — the GitHub-backed backup/restore/sync API. Read it when the user asks to back up, save, snapshot, sync, push, restore, pull, download, recover, or migrate projects.

Keeping these out of the main file means a routine session doesn't drag endpoint schemas or the entire backup API into context.

---

## You are a coworker, not a one-shot tool

You're rarely alone in an xo-project. The human watches the same project folder in a UI. Other agents may read it before starting their own work, or pick up after you tomorrow. You yourself may come back to it next session. The conventions in this skill exist because that shared context only works if everyone leaves a clean, accurate trail.

Two things matter most:

1. **Show your work in real time, not in retrospect.** Todos are the public log of what you're doing right now — what you've decided, what's in flight, what's done. Update them as state changes (Part 3 has the mechanics). Don't batch updates at the end; by then the human and other agents have already had to guess.
2. **Be honest about state.** Don't mark a todo `completed` if it's only partially done. Don't leave abandoned `in_progress` items dangling. If you change your mind, mark it `cancelled` with the reason. The next person — human or agent — trusts the trail you left.

Everything else is in the hard constraints above and the parts that follow: workspace hygiene in Part 2, the todo lifecycle in Part 3.

---

## Part 1 — Creating a project (hard constraint)

```
GET /api/config/workspace
→ {"roots": {"openclaw": "/home/coder/xo-projects"}, "default": "openclaw"}
```
Always call this first. Never hard-code `~/xo-projects`.

```
POST /api/files/mkdir
{ "path": "<projects_root>/<id>",
  "scaffold": true,
  "display_name": "My App",     // optional → seeds template
  "description": "..." }        // optional → seeds template
→ 200 {"path": "...", "name": "<id>", "copied": []}
→ 400 if path.parent ≠ projects_root
→ 409 if folder already exists
```

The backend scaffolds the canonical tree (top-level docs + `memory/` + `.xo/`) from a template. Top-level docs (`PROJECT.md`, `OBJECTIVES.md`, `PLAN.md`, `PROGRESS.md`) start with `[TEMPLATE]` markers — the agent fills those in on first boot (ask the user when scope is unclear). `.xo/project.json` starts with `_template: true`; the watcher service clears that flag once identity is resolved — the agent never touches it. Hand-rolling the layout means missing files or drift from the structure the backend expects.

---

## Part 2 — Once inside an xo-project

`AGENTS.md` (or `CLAUDE.md`, which is just `@AGENTS.md`) is the operating contract for every xo-project folder, and the first thing the agent reads every session. **Read it before doing anything else.** It covers:

- **§2** — the canonical file map (top-level docs + `memory/` + `.xo/`)
- **§3** — first-boot behaviour: replacing `[TEMPLATE]` markers in `PROJECT.md` / `OBJECTIVES.md` / `PLAN.md` / `PROGRESS.md`
- **§4** — the boot ritual (what to read, in what order, before answering)
- **§5** — during-work conventions (and why there is no project-level `TASKS.json`)
- **§6** — the six-step closing ritual
- **§7** — `PROGRESS.md` format and the three logs
- **§8** — memory discipline (semantic / episodic / procedural / working)
- **§9** — hard rules
- **§10** — the index → filter pattern for looking up past sessions

The skill defers to AGENTS.md for all of these — read the section there rather than paraphrasing here.

Two guardrails to internalize **before** opening AGENTS.md, since they apply from the moment the folder exists:

1. **Never write to `.xo/` — record todos through the HTTP API instead.** A background watcher service tails the runtime's native session logs (Claude Code's `~/.claude/projects/…`, OpenClaw's `~/.openclaw/agents/…`, etc.) and writes session events, timeline entries, stats, and activity heartbeats on your behalf. It does **not** read your todos from anywhere — `.xo/todos.json` has exactly one writer, the todo HTTP API in Part 3. Any agent write to `.xo/` conflicts with a service writer, gets overwritten, or corrupts sync state. This applies even to `.xo/project.json` on first boot — the watcher clears its `_template: true` flag itself.
2. **Work lives in the project folder.** Outputs scattered into `~/`, `/tmp/`, or sibling projects become orphaned from the project's memory, plan, and history — they don't get narrated in `PROGRESS.md`, don't get committed, and the next agent can't find them. If something genuinely must live outside, surface that to the user instead of doing it silently.

---

## Filesystem fallbacks (no HTTP endpoint yet)

- **List existing projects** — enumerate directories under the root from `/api/config/workspace`.
- **Read project metadata** — read `<project>/.xo/project.json`.
- **Rename or delete a project** — no API; do it via filesystem.

---

## Part 3 — Recording todos throughout the session

This is the mechanics behind the coworking discipline at the top of this file.

**`<project>/.xo/todos.json` is the live, cross-session view of what's in flight** — it's what the human sees in the UI, what other agents read before they start, and what you yourself rely on when you come back to the project tomorrow. The frontend and peers read from it, so **every agent keeps it accurate continuously** — not just once at boot. Every concrete step becomes a todo before you take it; flip status as work moves; close each one deliberately.

**One write path — the HTTP API — whatever your runtime is:**

```
GET    /api/xo-projects/{project_id}/todos
POST   /api/xo-projects/{project_id}/todos
PATCH  /api/xo-projects/{project_id}/todos/{todo_id}
DELETE /api/xo-projects/{project_id}/todos/{todo_id}
```

See **`references/todos-http-api.md`** for the schemas, the `runtime` / `session_id` conventions, and the conventional runtime values. Read it the first time you record a todo.

**If your runtime has a native todo tool, it is not enough — call the API as well.** There used to be a watcher sink that tailed one runtime's session log and mirrored its native todos into `.xo/todos.json`. It is gone. It only ever worked for a single backend, it minted a second, colliding id space, and it gave `todos.json` two writers. Removing it did **not** stop a native `TaskCreate` from firing — your runtime's own todo list still works exactly as before. What it stopped is anyone **seeing** the result: a todo that only exists in the native tool is invisible to the human's UI, to the project timeline, to the per-session task counters, and to the next agent. Nothing errors and nothing warns; the todo simply never leaves your process.

So: use the native tool if it helps you think, but a step is not recorded until the API call returns. Practically, treat the API call as the todo and the native entry as a private note.

**Lifecycle:**

- **At session start** — list todos. Open work from previous sessions is the backlog you inherit.
- **As you plan** — one todo per concrete step; keep the content line short.
- **Starting work** — flip the next step to `in_progress`. Only one `in_progress` per agent at a time; the UI assumes that discipline.
- **Finishing** — mark it `completed` before moving on.
- **Stopping early** — `cancelled` (decided not to) or `blocked` (waiting on something). Keep the record; don't delete.

The todo list is the **live** view of in-flight work. Past, finished work belongs in `PROGRESS.md` at session close, not as `completed`-but-still-listed todos — see AGENTS.md §6.

---

## Part 4 — Backup & restore (GitHub-backed)

**This is a hard constraint, not a recommendation.** When the user asks to back up, save, snapshot, sync, push, upload, restore, pull, download, recover, or migrate projects — or says they're moving to a new workspace — the only correct action is to call the `/api/xo-projects-sync/*` endpoints. Stop and read `references/backup-restore.md` before taking any action; do not improvise.

**Forbidden local alternatives — never do any of these in response to a "back up" or "save" request:**

- `tar czf project.tar.gz <project>` or any local archive — not encrypted, no manifest, won't be discoverable by `GET /projects`, can't be restored by the API, and includes secrets the API would exclude.
- `cp -r <project> <somewhere>` or copying to `~/Downloads`, `/tmp`, a USB mount, another folder — same problems, plus carries `.env` files.
- `git push <some-other-remote>` — bypasses the encryption layer and pushes secrets directly to a third remote.
- Manually creating a GitHub repo and pushing the project to it — the API does this *for* you (per-project, on first backup), and your manual repo won't match the `xo-project-<id>` naming the API needs for discovery.

If the user explicitly asks for a local tarball or a manual copy (not a backup), confirm that's what they want, do it as a one-off, and tell them it is **not** a backup and won't be available for restore.

**The architecture this constraint protects:** xo-cowork-api ships endpoints under `/api/xo-projects-sync/` that encrypt the project with `gpg`, generate a manifest with a sha256, split it into chunks under GitHub's 100 MB limit, and push to a private GitHub repo. Each xo-project lives in its own private repo named `xo-project-<project_id>`, lazily created on first backup. The user never opens GitHub manually — the backend creates and manages each repo.

**Read `references/backup-restore.md`** for the first-run setup flow (passphrase only), token resolution, per-project and bulk backup/restore, listing snapshots, what gets backed up, the staging model, and the error table.