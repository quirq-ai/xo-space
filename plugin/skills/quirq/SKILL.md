---
name: quirq
description: Use when the user mentions Quirq, xo-space, the XO workspace / control plane, or asks whether their local agent server is installed or running. Covers finding the install, checking health, and the strict ask-before-acting rules for install/start.
---

# Quirq — the local control plane

Quirq (repo: `xo-space`) is a FastAPI server the user runs **locally**. It
brokers coding-agent runtimes, owns the `xo-projects` model, and serves its
own web UI at `<base_url>/space/`. Default port 5002 (falls back to 5003).
The server's lifecycle belongs to the **user**, not to you.

## Always start with discovery (read-only)

Before answering anything about Quirq, run:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/discover.sh"
```

It performs no actions — it reads the pointer file
(`~/.config/quirq/install.json`, written by the server on every boot),
probes `/health` on localhost, and checks the cwd. It prints one JSON
object with `state`: `running`, `installed`, or `not_installed`.

## What to do per state

**`running`** — proceed with whatever was asked. Useful endpoints:
- `GET <base_url>/health` — status, stage, auth state, active sessions.
- `GET <base_url>/api/runtime-config` — resolved port, roots, path status.
- The web UI: `<base_url>/space/` — send the user there for anything
  interactive (setup, connectors, secrets).

**`installed`** (checkout at `repo_dir`, no live server) — report it, then
ask: *"Quirq is installed at `<repo_dir>` but not running. Want me to start
it?"* Only on an explicit yes, follow `/quirq:start`.

**`not_installed`** — report it, then ask: *"Quirq isn't installed on this
machine. Want me to install it? Which directory should be the workspace?"*
The directory is **always the user's answer — never pick or suggest a
default yourself**. On an explicit yes + directory, follow `/quirq:install`.

## Hard rules

1. **Discovery is check-only.** Never start, stop, restart, update, or
   install anything as a side effect of checking.
2. **Every state-changing action needs an explicit yes from the user in
   this conversation.** An earlier yes does not carry over to a new action.
3. **Never choose the install directory.** It decides where the workspace
   and all project data live — that is the user's call.
4. **Starting is not updating.** `/quirq:start` runs the existing checkout
   as-is. Updating (re-running `install.sh`, which fast-forwards the
   checkout) only happens if the user explicitly asks to update.
5. **Never touch `/api/secrets*` or connector endpoints.** Credentials and
   OAuth flows belong in the `/space/` UI — point the user there.
6. If the server misbehaves, report what you observed (exact curl output)
   and let the user decide; do not "fix" it by restarting.
