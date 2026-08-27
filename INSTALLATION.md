# Install and run Quirq

Pick a directory to keep Quirq in, then run:

```bash
curl -fsSL https://quirq.ai/install | sh
```

Open <http://localhost:5002/space/>.

Docker is not required. The command:

1. installs [uv](https://docs.astral.sh/uv/) if it is missing;
2. clones Quirq into `./xo-space`, named after the repository;
3. creates `./xo-space/venv` with Python 3.12, downloading an interpreter if
   the host has none, and installs `requirements.txt`;
4. creates `./.quirq`, and treats the current directory as your projects root;
5. reports which optional tools are present; and
6. starts the server in the foreground and prints the URL.

Press Ctrl-C to stop it. Run the same command again to update and restart.

## Your first run

What you see first depends on where you ran the installer. Quirq does not
create anything for you: it watches the directory you installed in — your
workspace, the *XO root* — and shows what is there.

- **An empty directory:** Files says **No projects in this workspace yet**,
  and Dashboard, Graph, Tree and Timeline draw the same empty map. Setup and
  Wiki work fully; Sessions shows *no data* until a runtime has run a session
  on this machine.
- **A directory that already holds folders:** every non-hidden folder is
  listed as a project on the spot — your existing repos, scratch folders, and
  the `xo-space` checkout the installer just made — each marked
  *unscaffolded*. Big repos count toward the map's file cap (400 per project,
  1,500 across the workspace), so a very full directory shows a trimmed
  picture. If that is not the collection you meant, point the XO root at a
  narrower directory from the Setup tab; it changes which folders are listed
  and never moves files.

**A project is a direct child folder of the workspace.** The folder name is
the project id, and the watcher lists every non-hidden folder it finds on its
next tick. A plain folder shows up as *unscaffolded* (browsable, no `.xo/`
metadata yet); a project created through the API is *scaffolded* — it gets
`.xo/project.json` and the template docs an agent works from (`AGENTS.md`,
`PROJECT.md`, `OBJECTIVES.md`, `PLAN.md`, `PROGRESS.md`, `memory/`).

Three ways to get a project in:

1. **Drop a folder in** — `git clone <repo> <workspace>/my-app`, `mkdir`, or
   copy. It appears unscaffolded within a tick.
2. **Ask your coding agent** — Claude Code or Codex running in the workspace
   carries the `xo-projects` skill (`.agents/skills/xo-projects/` in the
   checkout, and the Claude Code plugin under `plugin/`). "Create an
   xo-project called my-app" makes it call the API and scaffold the folder.
3. **Call the API yourself:**

   ```bash
   curl -X POST http://localhost:5002/api/files/mkdir \
     -H 'Content-Type: application/json' \
     -d '{"path": "<XO root>/my-app", "scaffold": true, "display_name": "My App"}'
   ```

   The path must be a direct child of the XO root (400 otherwise, 409 if the
   folder exists). The root is printed by the installer (`XO projects:`),
   shown in the Setup tab, and returned by `GET /api/config/workspace`.

Tabs fill in stages: any folder lights up Files (List, Graph, Tree) and a
Dashboard node; a scaffolded project adds identity and todos; a `.git` inside
the project adds a Timeline lane and file dates; an agent session adds live
badges, drawer events and Sessions telemetry; credentials (Setup or `.env`)
enable chat, connectors and backup. Nothing is required just to browse.

Before the first chat, check three things in Setup: the roots are the ones you
meant, the agent CLI is on the server's PATH (`claude` or `codex` — the
installer does not install it; `npm install -g @anthropic-ai/claude-code`),
and a credential is saved (Setup values are write-only and outrank `.env`).

## Stopping and starting again

Ctrl-C stops the server; nothing supervises it, so nothing restarts it. The
startup banner prints both ways back, from the directory you installed in:

```bash
./xo-space/install.sh                      # start again, no update
curl -fsSL https://quirq.ai/install | sh   # update to the latest main, then start
```

Running the one-liner from *inside* `./xo-space` is fine: the installer
notices it is standing in a checkout and uses that one, with the directory
above as the workspace, rather than cloning a second copy into it. It also
leaves a checkout alone when it has local changes, or when it is on a branch
other than the one it tracks (`main`, or `QUIRQ_SOURCE_REF`), so a
development clone is never silently reset.

The short URL serves a small POSIX-sh bootstrap that downloads `install.sh`
to a temporary file and runs it under `bash`, which is why `| sh` works. If
you fetch `install.sh` itself, pipe it to `bash`, not `sh` — the script uses
`BASH_SOURCE` and `set -o pipefail`, neither of which exists in POSIX `sh`.

## Prerequisites

`git` is required: Quirq uses it to download itself, and at runtime for project
sync and history.

Everything else is optional. The script prints a readiness table at startup
naming what each missing tool costs you:

| Tool | Used for |
|---|---|
| `node`, `npm` | installing the agent CLI |
| `gh` | xo-projects-sync backup repositories |
| `rclone` | Google Drive and OneDrive connectors |
| `gpg` | encrypted backup and restore |

Nothing here is fatal. A missing tool disables its feature and nothing else.

## The agent CLI

Quirq does not install anything beyond `requirements.txt`. It runs on your own
machine, so it will not `apt-get`, pipe an installer to your shell, or
`npm install -g` behind your back.

That means you install the agent CLI yourself, once:

```bash
npm install -g @anthropic-ai/claude-code
```

To opt back into the automatic bootstrap — apt packages, Node via nvm, and the
CLI — start with `QUIRQ_SKIP_BOOT_INSTALL=0`.

## Local data

Everything lives under the directory you launched from, so an install is
self-contained and you can move or delete it as one folder.

| Path | Purpose |
|---|---|
| `.` | Your projects root — each project is a subdirectory with its own `.xo` |
| `./xo-space` | The Quirq source checkout |
| `./xo-space/venv` | Python environment, made by uv — it has no `pip`. To add packages (e.g. the test suite): `~/.local/bin/uv pip install --python ./xo-space/venv/bin/python -r <file>`; uv itself lives in `~/.local/bin`, which the installer does not add to your shell's PATH |
| `./.quirq` | Runtime configuration, saved credentials, watcher activity, cursors, locks, and other machine-local state |

Open the **Setup** tab after installation. It shows the paths in use, CLI
readiness, native session file counts, the active chat backend, and the watcher
source mode and tick interval. It also lets you configure a different XO
projects root and `.quirq` state root.

The Setup tab cannot restart the server for you — nothing supervises the
process. When it reports that a restart is required, press Ctrl-C and run the
command again.

## Running from a clone

If you already have a checkout, run the script from inside it:

```bash
git clone https://github.com/quirq-ai/xo-space
cd xo-space
./install.sh
```

It detects the checkout, uses that working tree in place, and never runs a git
command against it — your local edits are left alone. In this mode
the repository itself becomes the projects root and `./.quirq` is created
inside it. Both are already in `.gitignore`.

## Configuration

Every value is overridable from the environment:

| Variable | Default |
|---|---|
| `PORT` | `5002` |
| `HOST` | `127.0.0.1` |
| `XO_PROJECTS_ROOT` | the launch directory |
| `QUIRQ_STATE_ROOT` | `./.quirq` |
| `QUIRQ_APP_DIR` | `./xo-space` |
| `QUIRQ_SOURCE_REF` | `main` |
| `AGENT_NAME` | `claude_code` |
| `QUIRQ_SKIP_BOOT_INSTALL` | `1` |

For example:

```bash
curl -fsSL https://quirq.ai/install | PORT=8080 XO_PROJECTS_ROOT=/absolute/path sh
```

On first run the installer writes `./xo-space/.env` (the checkout's `.env`) recording exactly what it used,
then never rewrites it — it is yours to edit. Change a value there and re-run
`./install.sh` to apply it. Credentials are written commented out; uncomment
the ones you need, or configure them through the Setup tab instead.

Precedence, highest first:

1. variables exported in your shell — `PORT=8080 ./install.sh`
2. `./xo-space/.env`
3. the defaults above

The Setup tab's `runtime.env` and `secrets.env` are loaded with `override=True`
and beat all three, so the tab stays authoritative for whatever it manages.

Roots are also read from `roots.env` in the state root, which the Setup tab
writes. Explicit environment variables take precedence over it.

Quirq refuses to start if the projects root and state root are nested inside
one another, or if the checkout sits inside the state root — relocating the
state root copies it wholesale, and it must not drag a checkout or your
projects along.

The one exception is the default layout: the state root may be a *hidden*
directory directly inside the projects root, as `./.quirq` is. Project
enumeration skips dot-prefixed entries, so it can never be mistaken for a
project. A visible `./quirq` state root, or one nested deeper, is still
rejected.

## Windows

Not supported. The server's boot hooks are bash scripts, and while they fail
non-fatally, no equivalent installer exists. Use WSL.
