---
name: quirq-install
description: Install and launch XO Space from Codex in a durable local workspace with the Codex backend. Use when asked to install Space or open it for the first time.
---

# Install XO Space

Resolve `<plugin-root>` as two directories above this skill directory.

1. Run `bash "<plugin-root>/scripts/discover.sh"` first. If running, open the
   returned UI. If installed, follow `quirq-start`; do not reinstall or update it.
2. Expand the user's workspace path to an absolute path. If no path was given,
   use `~/xo-workspace`. State the path and that first use downloads XO Space,
   uv/Python when needed, and Python dependencies, then starts a local server.
   An explicit install/open request authorizes these steps and directory creation.
3. Check that a working Codex CLI is available (`codex --version`), or use the
   user's `CODEX_CLI_PATH`. A desktop app may bundle a CLI even when `codex` is
   absent/broken on PATH; if the local environment exposes its location, pass that
   absolute executable as `CODEX_CLI_PATH`. Never replace a broken global install
   automatically. Ask the user to install/sign in to Codex if none is available.
   Use the CLI's login status command to check auth; never read or copy auth files.
4. Launch this command in a persistent foreground terminal/background task owned
   by this Codex session (the script stays running):

   ```bash
   bash "<plugin-root>/scripts/space.sh" install "<absolute-workspace>"
   ```

   Fresh installs use `AGENT_NAME=codex`, a loopback host and skip boot-time global
   tool installs. The script clones the reviewed `main` branch to
   `<workspace>/xo-space` and invokes its `install.sh` with Bash. It never runs
   a server from the plugin cache. `QUIRQ_SOURCE_REF` can select a branch for an
   explicit development test; `QUIRQ_SOURCE_REPO` can select a test source.
5. While the task runs, inspect its output and poll the bundled discovery script
   about every five seconds for up to five minutes. Verify `/health` and
   `/api/runtime-config` on the returned URL. Require the launch task to still be
   running, discovery's `repo_dir` to match the intended checkout, and runtime
   `paths.projects.container_path` / `paths.state.container_path` to match the
   chosen workspace and state directory. The API itself does not expose repo_dir.
   Honor a custom `PORT` by passing
   it through `QUIRQ_DISCOVER_PORTS`. Do not mistake another running Space for
   this install. If the process exits or times out, report its error and a short
   redacted log excerpt; do not repeatedly reinstall.
6. Open `<base_url>/space/` in Codex's browser when available. Report
   `<workspace>/xo-space/.env`, the runtime's state/log path, and the task/terminal
   to keep alive. Existing Codex login is reused; no XO account is required.

If a partial checkout already exists, the helper refuses to overwrite it. Inspect
it and explain recovery: run its `install.sh` with `AGENT_NAME=codex` only when the
user requests completing setup, then repeat readiness verification.
