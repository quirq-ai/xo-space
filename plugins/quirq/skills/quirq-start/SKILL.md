---
name: quirq-start
description: Start an existing XO Space installation and open its UI in Codex, preserving its backend and workspace without fetching updates.
---

# Start XO Space

Resolve `<plugin-root>` as two directories above this skill directory.

1. Run `bash "<plugin-root>/scripts/discover.sh"`. If `running`, open its
   `<base_url>/space/`. If `not_installed`, follow `quirq-install` only when the
   user asked to open/run/install Space; a status request must not install it.
2. For `installed`, inspect its saved backend. If it uses Codex, verify a working
   CLI (`codex --version` or the user's `CODEX_CLI_PATH`). If PATH is absent/broken
   and this desktop environment exposes a bundled CLI, pass its absolute path as
   `CODEX_CLI_PATH` to the launcher, just as on first install. That override is
   session-local and may need to be supplied again. Do not install a global CLI
   or change an existing backend automatically.
   Launch the returned `repo_dir` in a persistent foreground
   terminal/background task owned by this Codex session:

   ```bash
   bash "<plugin-root>/scripts/space.sh" start "<absolute-repo-dir>"
   ```

   The helper uses the existing venv, `.env` and saved configuration, writes to
   the Space log, and runs the existing checkout without fetching or installing.
   An explicit start/open request is sufficient authorization. Missing dependencies
   produce a recovery message instead of triggering a hidden reinstall.
3. Inspect process output and poll bundled discovery every five seconds for up
   to sixty seconds. Verify `/health` and `/api/runtime-config`, the actual port
   (including a custom port from the pointer). Require discovery's `repo_dir` to
   match the requested checkout and runtime `paths.projects.container_path` /
   `paths.state.container_path` to match its effective roots. The runtime API
   does not expose repo_dir. If another server answers or this task exits, report
   that instead of success.
4. Open `<base_url>/space/` with the Codex browser tool when available, otherwise
   provide the link. Report the log path and task/terminal to keep alive. Starting
   preserves the active backend; change it through Space Setup only when asked.

To stop a server this session launched, stop its owning task/terminal when the
user requests it. Never kill unrelated listeners or processes to reclaim a port.
