# XO Space for Codex

Install this plugin, then ask Codex **“Open XO Space.”** Codex installs the local
server on first use, starts it and opens the Space UI in its browser when
available. Projects, agent activity, todos and usage stay in your local workspace.
The stable plugin identifier is `quirq@quirq-ai` (the name existing installs use).

## Click install

Add this repository as a marketplace once:

```bash
codex plugin marketplace add quirq-ai/xo-space
```

In Codex, open **Plugins** (or `/plugins`), choose the **Quirq** marketplace,
select **XO Space**, and click **Install**. Start a new task and say:

> Open XO Space

Or give an existing projects directory:

> Install XO Space in ~/work

For a terminal-only install, after adding the marketplace:

```bash
codex plugin add quirq@quirq-ai
```

This is a repository marketplace, not a claim of publication in OpenAI's public
plugin directory. The repository URL can be shared with other users; they do not
need to clone the backend or copy skills themselves.

**Before these changes reach `main`:** add the marketplace with
`--ref development` after the PR merges (or `--ref <PR-branch>` for review).
The plugin always installs the reviewed backend `main` by default. To explicitly
test another backend branch, set `QUIRQ_SOURCE_REF=development` when launching it.

## What first use does

- Uses the requested directory, or `~/xo-workspace` if none was supplied.
- Clones `xo-space` into that directory, prepares its Python environment with the
  existing installer, and selects the **Codex** backend for this fresh install.
- Keeps `.env`, the venv and `.quirq` state outside the plugin cache. Updating or
  uninstalling the plugin does not delete your projects or server installation.
- Starts a foreground process in a Codex-owned task/terminal and verifies health.
  Keep that task/terminal alive. Ask to open Space again after it stops.
- Reuses an existing Space installation and its saved backend/roots without
  fetching updates, replacing configuration or launching a duplicate server.

Requirements: a local macOS, Linux or WSL environment, Git, network access for
first install, and a working signed-in Codex CLI. `CODEX_CLI_PATH` may point to a
CLI bundled with the desktop app when it is not on PATH. Existing Codex login and
`CODEX_HOME` are reused; the plugin never copies credentials or installs global
agent CLIs. Check `codex login status` and use `codex login` if needed. No XO
account is required for local browsing. Remote tasks need their host's supported
port forwarding to expose the UI.

The plugin installation itself only loads its skills and assets. Downloading the
server and dependencies happens when you ask Codex to open/install Space. Runtime
sandbox or network permission prompts may still apply.

Space chat launches a separate Codex CLI process with Space's configured runtime;
it does not continue the current Codex task or inherit its approval settings.

## Skills

| Skill | Purpose |
|---|---|
| `quirq` | Open Space; discover, install or start as needed |
| `quirq-install` | Install once in a durable workspace with Codex selected |
| `quirq-start` | Start an existing checkout without updating it |
| `quirq-status` | Read-only health, URL and workspace inspection |

The bundled `scripts/space.sh` exposes `install <absolute-workspace>` and
`start <absolute-checkout>`. Both run in the foreground. Discovery is read-only:
`scripts/discover.sh` uses the server's XDG install pointer and local probes,
then checks the current directory for a checkout. `QUIRQ_DISCOVER_PORTS` supplies
additional/custom probe ports. `PORT` selects a port for a fresh installation.

## Update and remove

Refresh the marketplace with `codex plugin marketplace upgrade quirq-ai`, then
reinstall/update XO Space from Plugins and start a new task to load its skills.
Server updates are separate: explicitly ask to update Space or follow the root
[installation guide](../../INSTALLATION.md). Starting Space never updates it.

`codex plugin remove quirq@quirq-ai` removes the plugin. To remove the server,
stop its owning task and run `<workspace>/xo-space/uninstall.sh`; that workflow
preserves projects by default. See the installation guide before removing data.

## Test a checkout

```bash
codex plugin marketplace add /absolute/path/to/xo-space
codex plugin add quirq@quirq-ai
```

Start a new task and ask for Space status, then install/start in a disposable
workspace. Confirm the actual URL, Codex backend and workspace in Setup.

Maintainer checks from the repository root:

```bash
venv/bin/python -m unittest tests.test_codex_plugin_package tests.test_codex_plugin_runtime tests.test_plugin_discovery
bash scripts/check_plugin_sync.sh
venv/bin/python scripts/check_route_parity.py
```

Packaging follows the [official OpenAI plugin guide](https://developers.openai.com/plugins/build/plugins).
The Claude Code package remains in `plugin/`; only read-only discovery is shared.
