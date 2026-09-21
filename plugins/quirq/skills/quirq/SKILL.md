---
name: quirq
description: Open, install or inspect XO Space (Quirq), the local workspace for coding agents. Use when the user asks to open Space, run XO Space inside Codex, browse their Space projects or check the local Space server.
---

# XO Space in Codex

XO Space is a local server with a browser UI. Resolve this installed skill's
location first: the plugin root is two directories above this file's directory.
All scripts below are bundled under that root; never assume the user's current
working directory is the plugin checkout.

1. Run `bash "<plugin-root>/scripts/discover.sh"` and parse its JSON.
2. For a status/question-only request, follow the sibling `quirq-status` skill.
   Discovery must remain read-only.
3. For an explicit request to open, run or install Space:
   - `running`: use the returned URL; do not launch another server.
   - `installed`: follow `quirq-start` with `repo_dir`.
   - `not_installed`: follow `quirq-install`. Use the workspace the user named;
     otherwise use `~/xo-workspace` and tell them the resolved path before starting.
     This dedicated directory keeps Space out of the plugin cache and current repo.
   The request authorizes the necessary install/start steps; do not ask for the
   same permission again. If a runtime permission gate blocks an operation,
   explain that specific gate and request the needed access.
4. After health and runtime-config verification, open `<base_url>/space/` with
   Codex's browser/open-in-app tool when available. Otherwise provide a clickable
   URL. Report workspace, log location and the foreground task/terminal used.

Space runs as a process owned by the local task/terminal. It is not a system
service. Keep that task alive; stopping it or closing its environment stops Space.
Use `quirq-start` to reopen an existing installation later.

The plugin works in local macOS/Linux tasks and WSL. A remote/cloud task serves
Space on that host's loopback; use the environment's supported port forwarding
instead of claiming its localhost URL opens on the user's computer.

Space can inspect projects through `/api/runtime-config` and its UI. Do not read
credential files, print secrets or configure third-party accounts unless asked.
Space chat starts a separate Codex CLI process using Space's configured runtime;
it does not continue this task or inherit this task's approval settings.
