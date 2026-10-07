---
name: quirq-start
description: Start an existing XO Space installation and open its UI in Codex, preserving its backend and workspace without fetching updates.
---

# Start XO Space

Resolve `<plugin-root>` as two directories above this skill directory.

1. Run `bash "<plugin-root>/scripts/discover.sh"`. If `running`, open its
   `<base_url>/space/`. If `not_installed`, follow `quirq-install` only when the
   user asked to open/run/install Space; a status request must not install it.
2. For `installed`, launch the returned `repo_dir` in a persistent foreground
   terminal/background task owned by this Codex session:

   ```bash
   bash "<plugin-root>/scripts/space.sh" start "<absolute-repo-dir>"
   ```

   The helper uses the existing venv, `.env` and saved configuration, checks
   the Codex CLI itself when the saved backend is Codex, writes to the Space
   log, and runs the existing checkout without fetching or installing. An
   explicit start/open request is sufficient authorization. Missing
   dependencies produce a recovery message instead of a hidden reinstall.
   If it stops with a Codex CLI error and this desktop environment exposes a
   bundled CLI, rerun with that absolute path as `CODEX_CLI_PATH`
   (session-local). Do not install a global CLI or change the backend.
3. Wait for the line `XO_SPACE_START {...}` in the task output (up to about 90
   seconds; do not poll /health or runtime-config yourself). The helper prints it
   once, after checking that this checkout answers health checks:
   - `"state":"running"`: Space is up; use its `base_url`, `repo_dir`,
     `projects_root` and `state_root`.
   - `"state":"exited"` or `"timeout"`: Space did not come up. Read the end of the
     log named on the `Logs:` line and report the error instead of success.
4. Show Space: when the Space MCP tools are available, call `space_open` (the
   desktop app displays Space in the conversation; a terminal such as Codex CLI
   displays nothing). Always also give the link `<base_url>/space/`; without the
   MCP tools, open it with the Codex browser tool when available. Report the log
   path and task/terminal to keep alive. Starting preserves the active backend;
   change it through Space Setup only when asked.
5. Tell the user in one sentence: Space keeps running while this task is open;
   if XO Space later says it is unreachable, ask "start XO Space" to start it
   again.

To stop a server this session launched, stop its owning task/terminal when the
user requests it. Never kill unrelated listeners or processes to reclaim a port.
