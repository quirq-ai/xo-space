# AGENTS.md — operating contract for this folder

> You are an agent (Claude, Codex, Cursor, Aider, or other) working inside a well harness engineered folder. This file is the contract every agent reads first. It is short on purpose.
>
> Ignore it and you will duplicate work, lose context, and corrupt the human's externalized memory. Don't.

---

## 1. What this folder is

A working folder shared between a human and any number of AI agents. It contains the actual project work, plus two persistence layers:

- **`memory/`** — shared cognition. Committed to git. Distilled facts, past episodes, reusable procedures. Visible to every teammate and every agent.
- **`.xo/`** — portable project metadata: identity, todos, and the sharing roster. Small, durable, and the half of the state a copy of this folder would want.
- **`~/.quirq/`** — machine-local service state: session indexes, statistics, the event timeline, sync progress, live presence and watcher cursors. All of it is re-derivable from this machine's runtime logs, so none of it travels with the project.

### Who writes what

|                                                          | Agent writes? | Notes |
|----------------------------------------------------------|:-:|---|
| `PROJECT.md`, `OBJECTIVES.md`, `PLAN.md`, `PROGRESS.md` | yes | Co-edited with the human. |
| `memory/{semantic,episodic,procedural,working}/`        | yes | The agent's externalized cognition. |
| `.xo/**` — **everything** under `.xo/`                  | **no** | Quirq services own this directory. The watcher fills identity; the todo API owns `todos.json`; collaboration flows own the peer roster. Agents only **read** `.xo/`. Never write — your edits will be overwritten and may corrupt coordinated state. |
| `.xo/todos.json`                                        | **no**, but you **drive** it | You never edit the file, and you never rely on a native todo tool to reach it. Record every todo through `POST/PATCH/DELETE /api/xo-projects/<project-id>/todos` (§5). The API is the file's only writer, for every runtime. |
| `~/.quirq/**`                                          | **no** | Machine-local service state: this project's session index, stats, timeline and sync progress, plus watcher cursors and live-presence snapshots. Read it through the cowork API; never edit these files directly. |

If the agent needs something not listed as agent-writable, it almost certainly needs a different tool (a tool call that mutates state) — not a direct edit.

Every agent that works here is expected to leave the folder in a **better state than it found it**: more accurate memory, cleaner plan, honest progress log.

---

## 2. File map (this folder *is* the spec — don't invent new top-level files)

```
<project>/
├── AGENTS.md           ← this file. The contract. Read first.
├── CLAUDE.md           ← imports AGENTS.md for Claude Code (`@AGENTS.md`).
├── PROJECT.md          ← what this is for. Stable.
├── OBJECTIVES.md       ← OKRs / north-star outcomes. Stable, weeks.
├── PLAN.md             ← current plan. Agent-maintained, days.
├── PROGRESS.md         ← running narrative of work done. Append-only.
│
├── memory/                  ← shared cognition. Committed.
│   ├── semantic/            distilled facts (preferences, project-facts, constraints)
│   ├── episodic/            what happened, with context (one file per episode)
│   ├── procedural/          how to do recurring things (validated twice)
│   └── working/             session-scoped scratch (wiped at close)
│
├── .xo/                     ← portable project metadata. Gitignored.
│   ├── project.json         identity: pid, name, owner_user_id, created_at
│   ├── agent.json           optional backend-specific agent attachment
│   ├── todos.json           work items, written only by the todo API (§5)
│   └── peers.json           who this folder is shared with
│
└── ... (the actual project work files)
```

**Everything else the services keep about this project lives outside the
folder**, under the machine-local state root, keyed by the `pid` in
`.xo/project.json`:

```
~/.quirq/projects/<pid>/
├── stats.json               rolling 7d/30d: tokens, models, files, sessions, time
├── timeline.jsonl           append-only event log (sessions, todos, edits)
├── sync.json                last-sync state per peer
└── sessions/
    ├── sessionslist.d/      the session index, one shard file per session
    └── sessions-augment.json   watcher counters and timing joined at read time

~/.quirq/watcher/activity/projects/<pid>.json   live "who is here now"
```

**Do not go looking for those paths.** They are machine-local, they are keyed
by an id you would have to resolve yourself, and the session index is sharded.
Read them through the cowork API instead — it does the resolution and the
merge for you:

| You want | Ask |
|---|---|
| open todos | `GET /api/xo-projects/<project-id>/todos` |
| recent sessions | `GET /api/xo-projects/<project-id>/usage/sessions` |
| the event log | `GET /api/xo-projects/<project-id>/timeline?limit=100` |
| who is working here now | `GET /api/xo-projects/<project-id>/activity` |

`<project-id>` is this folder's name. The base URL is
`http://${HOST:-localhost}:${PORT:-5002}`.

---

## 3. First-boot behaviour (template detection)

This folder ships as a **template**. On the very first session, before any real work, look for `[TEMPLATE]` markers in `PROJECT.md`, `OBJECTIVES.md`, `PLAN.md`, and `PROGRESS.md`. If any are present, the folder is fresh — the human has not yet defined scope or objectives. **Ask them** to clarify before doing real work, then replace the markers with their answers.

`.xo/project.json` (identity: pid, name, owner, created_at) is initialised **by the watcher service**, not by you. The watcher detects the `_template: true` flag, generates a UUID, fills in identity from the harness, and removes the flag. By the time you boot, `.xo/project.json` is either still a template (watcher hasn't run yet — wait or read identity from the harness env) or fully populated. Either way, **don't edit it**.

After scope is clarified and template markers are gone, jump to §4.

---

## 4. Boot ritual — every session

Read these in order, **before answering**:

1. `AGENTS.md` (this file)
2. `PROJECT.md` — what we're building
3. `OBJECTIVES.md` — why
4. `PLAN.md` — current plan
5. `memory/semantic/*.md` — distilled facts (3 short files)
6. `PROGRESS.md` — **last ~30 lines only**
7. `GET /api/xo-projects/<project-id>/todos` — open todos across active sessions. This is the API, not the file: `.xo/todos.json` is readable, but the endpoint is what hides deleted rows and is the same surface you write through.
8. `GET /api/xo-projects/<project-id>/usage/sessions` — **the 3 most recent only**, to know what was worked on last. The session index itself is machine-local and sharded now (§2); this endpoint merges it for you.
9. `GET /api/xo-projects/<project-id>/activity` — is anyone else working here right now?

You don't need to "announce yourself." The runtime adapter registers the
session, while the watcher observes supported native events and writes the
corresponding `session.started` timeline event, session augmentation, and
machine-local activity heartbeat.

**Do not read** `memory/episodic/`, `memory/procedural/`, the full session
list, or the full timeline from the main thread. They grow without bound. To
inspect past session history, follow the rule in §10.

---

## 5. During work

Keep these living:

- **`PLAN.md`** — when the plan changes, edit it. A stale plan misleads the next agent.
- **`memory/working/`** — scratchpad. Whatever you'd write on a whiteboard. Wiped at close.

**Do not** edit `PROGRESS.md` mid-work — it is written once at session close.

**Record todos through the HTTP API — every runtime, no exceptions:**

```
GET    /api/xo-projects/<project-id>/todos                # list (add ?include_deleted=true for history)
POST   /api/xo-projects/<project-id>/todos                # {"runtime": "...", "content": "..."}
PATCH  /api/xo-projects/<project-id>/todos/<todo-id>      # {"status": "in_progress"}
DELETE /api/xo-projects/<project-id>/todos/<todo-id>      # tombstone; prefer status "cancelled"
```

Statuses: `pending | in_progress | completed | cancelled | blocked`. One
`in_progress` per agent at a time. There is no project-level `TASKS.json`;
project todos and session todos are the same list, and this API is its only
writer.

**If your runtime has a native todo tool, it is not enough.** There used to be
a watcher sink that tailed one runtime's session log and mirrored its native
todos into `.xo/todos.json`. It is gone — it only ever worked for that one
backend. Removing it did **not** stop a native `TaskCreate` from firing; your
runtime's own list still works. What it stopped is anyone **seeing** the
result. A todo that exists only in a native tool is invisible to the human's
UI, to the timeline, to the task counters and to the next agent, and nothing
errors to warn you. Use the native tool to think with if you like; a step is
not recorded until the API call returns.

---

## 6. Closing ritual — when the user signals done

When the human says "done", "wrap up", "good for today", or you detect a natural close, do these in order:

1. **`memory/episodic/{YYYY-MM-DD}-{slug}.md`** — write **only if** the session contained a non-trivial decision, a hard problem solved, an unexpected failure, or strong user feedback. Routine work does not deserve an episode. Format: see §8.
2. **`PROGRESS.md`** — append one paragraph (newest at the bottom). See format in §7.
3. **`memory/semantic/*.md`** — distill any new facts that meet both criteria: (a) observed twice or explicitly stated by the user, (b) true regardless of context. One claim per line. No narrative.
4. **`memory/procedural/{slug}.md`** — write **only if** a workflow has now succeeded ≥2 times. One success is not a pattern. Format: see §8.
5. **`PLAN.md`** — if scope shifted, update. Move the superseded plan to "Recently superseded" as a one-liner.
6. **`memory/working/`** — wipe (`rm -f memory/working/*` except `.gitkeep`).

Do all six. Skipping for "the session was short" is how folders rot.

Everything in `.xo/` is service-owned: the watcher fills identity, the todo
API owns `todos.json`, and collaboration flows maintain peer state.
**Do not write to those files** — your edits will conflict with coordinated
writers and may be overwritten. Close your todos through the API (§5), not
by editing the file. Session indexes, statistics, the timeline and live
presence are machine-local under `~/.quirq/`; reach all of them through the
cowork API.

---

## 7. The three logs (don't mix them up)

| Log                              | Format                          | Purpose                                            | Read by                              |
|----------------------------------|---------------------------------|----------------------------------------------------|--------------------------------------|
| `PROGRESS.md`                    | append-only paragraphs          | human-readable progress, scrolled by humans        | every agent at boot (last ~30 lines) |
| `GET …/usage/sessions`           | merged JSON rows                | one row per session — the **index** of history      | every agent at boot (3 newest rows) |
| `GET …/timeline`                 | one JSON event per record       | machine-readable firehose (audit, sync, dashboards)| services write; agents read only via §10 |
| `memory/episodic/*.md`           | one file per noteworthy episode | distilled context for future recall                | memory subagent (never main thread)  |

**`PROGRESS.md` paragraph format:**
```
## YYYY-MM-DD — [outcome] one-line headline
agent: <model id>

3–6 sentences: what was attempted, what shipped, what's blocked, what's next.
```
`[outcome]` ∈ `shipped | progress | blocked | pivoted | cleanup | research`.

**Timeline event shape** (one record per line in the machine-local
`timeline.jsonl`; read it with `GET /api/xo-projects/<project-id>/timeline`):
```json
{"ts":"2026-05-09T14:33:00Z","type":"session.started","session_id":"ses_abc123","runtime":"<runtime>"}
```
Current types: `session.started`, `todo.added`, `todo.completed`,
`file.created`, and `file.edited`. The `todo.*` lines come from the todo API
(§5), so they appear for every runtime — which is exactly why a native-only
todo leaves no trace here. The schema also reserves project and peer sync
event families for their owning services.

---

## 8. Memory discipline

`memory/` has four flavours. The discipline of *which* is the difference between a useful folder and a cluttered one.

**Semantic — `memory/semantic/`** — distilled facts. One claim per line. No narrative. No timestamps. Only update when a fact is observed twice or stated by the user. Three files only: `preferences.md`, `project-facts.md`, `constraints.md`. Do not add new files in this directory.

**Episodic — `memory/episodic/`** — append-only. One file per episode, named `YYYY-MM-DD-{slug}.md`:
```markdown
---
date: YYYY-MM-DD
tags: [tag1, tag2]
outcome: success | failure | partial | abandoned
---

## What
One sentence.

## Why it mattered
One or two sentences.

## How it went
Raw narrative. Do not summarise at write time — summarisation destroys episodic signal.
```
Never edit an episode after writing. If context changed, write a new episode that references the old one by filename.

**Procedural — `memory/procedural/`** — only after a workflow has succeeded ≥2 times:
```markdown
---
name: skill-name
trigger_when: human-readable trigger condition
---

## Steps
1. ...
2. ...

## Gotchas
- ...

## Last validated
YYYY-MM-DD
```
Procedural memory is the highest-leverage kind — it converts experience into reusable capability. It is also the most dangerous to fabricate. Never write a procedural skill from a single success.

**Working — `memory/working/`** — live scratchpad for the current session. Wiped at close. Use it for mid-session reasoning you want to preserve across tool calls.

---

## 9. Hard rules

- **Never write to `.xo/`.** No exceptions — not even `.xo/project.json` on first boot, and not `todos.json` (drive it through the API in §5). The services own the entire directory; your edits will conflict with them, be overwritten, or corrupt sync state.
- **Never treat a native todo tool as the record.** It is invisible to everyone but you. The API call in §5 is what makes a todo real.
- **Never delete** anything in `memory/` outside the rules in §8 (and even then, only `working/` gets wiped). Memory loss is irreversible.
- **Never edit** an episodic memory file after it is written. Append-only.
- **Never** write narrative text to `memory/semantic/*`. That folder is for distilled claims only.
- **Never** dump tool output, full file contents, or raw logs into any memory file. Memory is *distilled*; raw logs live in the machine-local timeline (§7).
- **Never claim work as done** without verifying it (run the test, open the page, read the diff).
- **Never put secrets** in `memory/` (it is committed) or `.xo/` (it may be synced to peers).
- **Never invent** peer/sync state. If `.xo/peers.json` is empty, you are working solo.
- **Stop and ask** if `PLAN.md` and the user's request disagree. Don't silently re-plan.

---

## 10. Looking up past sessions (read-only)

The session index and the timeline are **read-only for agents** — the services
maintain them, and they live outside this folder under `~/.quirq/` (§2). You
consult them through the API; you never edit them.

When the user references prior work ("continue the auth thing", "the bug from yesterday", "what we discussed"), or whenever you need history older than the last 3 sessions:

1. **Start at the index, not the log.** `GET /api/xo-projects/<project-id>/usage/sessions` and find the relevant session by its last activity. This is a small merged list — scanning it is cheap.
2. **Pull only that session's events.** `GET /api/xo-projects/<project-id>/timeline?limit=100` returns newest-first and accepts a `types=` filter; page with `before=<ts>`. Don't try to read the raw log — it is machine-local, rotated, and keyed by an id you would have to resolve yourself.
3. **For narrative detail**, go to `memory/episodic/` — the episode files are named `YYYY-MM-DD-{slug}.md`, so the session's date is the index. Have a subagent read them; never main-thread.
4. **For raw artefact recovery**, the session row's `sessionFile` names the runtime's native log. The API returns the file name only, never its absolute path — the transcript is machine-local and the service will not hand out a path into it.

If the question is open-ended ("what have we been working on lately?"), read the last 5–10 rows of the session list and summarise — do not load the whole timeline.

> **Why two surfaces?** The session list is the human/agent-readable index; the timeline is the firehose. They are joined on `session_id`. Most lookups need only the index.

---

## 11. If you're a new agent and lost

Run §3 (if there are `[TEMPLATE]` markers anywhere) or §4 (otherwise). By the time you finish you'll know:

- What this project is (`PROJECT.md`)
- What success looks like (`OBJECTIVES.md`)
- The current plan (`PLAN.md`)
- What has already been done (`PROGRESS.md` last 30 lines)
- What facts are settled (`memory/semantic/`)
- What's in flight (`GET /api/xo-projects/<project-id>/todos`)

That is enough to be useful. Ask the human if anything contradicts.
