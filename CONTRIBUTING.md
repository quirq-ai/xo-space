# Contributing to xo-space

Thanks for helping. `xo-space` is the open-source local control plane for AI
coding agents — Claude Code, Codex, OpenClaw, Hermes, Antigravity, and Cursor
for telemetry — plus the Space UI that shows what those agents did to your
projects. This guide is the short path from "I found something" to "it's
merged". The engineering detail lives in [DEVELOPING.md](DEVELOPING.md); this
file tells you how to work with us.

## Contents

**Getting involved**

1. [What we're looking for](#what-were-looking-for) — what helps most, and what we'll push back on
2. [Before you start](#before-you-start) — search first; when to open an issue before a PR
3. [Reporting a bug](#reporting-a-bug) — what to include so it can be reproduced
4. [Security](#security) — how to report a vulnerability without publishing it

**Working on the code**

5. [Set up a dev environment](#set-up-a-dev-environment) — clone, two ways to run, WSL on Windows
6. [Ground rules](#ground-rules) — the seven invariants every change must keep
7. [Where things live](#where-things-live) — the map, adding a runtime, changing the Space UI
8. [Testing and validation](#testing-and-validation) — the commands to run before a PR
9. [Docs move with code](#docs-move-with-code) — which docs change with which code

**Getting it merged**

10. [Branches, commits, pull requests](#branches-commits-pull-requests) — `development` vs `main`, what a PR must say
11. [Review](#review) — what we look at, in what order, and how fast
12. [Windows](#windows) — what works natively and what needs WSL
13. [Community and licence](#community-and-licence) — where to ask, MIT

---

## What we're looking for

Welcome, in rough order of how much they help:

- **Bug reports with a reproduction.** Even without a fix. See
  [Reporting a bug](#reporting-a-bug).
- **New runtimes.** Adding a coding agent is designed to be "drop two folders"
  with zero core edits (below, and DEVELOPING.md §4).
- **Fixes and small improvements** to the server, the watcher, the Space UI,
  the installer, and the docs. Anything labelled
  [`good first issue`](https://github.com/quirq-ai/xo-space/labels/good%20first%20issue)
  or [`help wanted`](https://github.com/quirq-ai/xo-space/labels/help%20wanted)
  is a safe place to start.
- **Docs and wiki corrections** where a page says something the code no
  longer does.
- **Connectors** (Google Drive, OneDrive, GitHub, Vercel, Manus live in
  `routers/cowork_agent/connectors/` + `services/cowork_agent/connectors/`).

Things we will push back on, so you don't spend time on them first:

- Code that names a specific agent outside the agent-owned trees (the
  modularity invariant, below). This is the one rule that gets a PR sent back
  on sight.
- Writing anything into a user's project folder or `.xo/` from outside the
  watcher.
- A build step, framework, or bundled dependency for `space_ui/` — it is
  deliberately plain ES modules served as files.
- Changing an existing endpoint's path, request, or response shape without an
  issue agreeing to it first. Other clients depend on them.
- Cloud-only behaviour. The same tree runs on a laptop and in a hosted
  workspace (DEVELOPING.md §9); a change must make sense for both.

## Before you start

1. **Search [the issues](https://github.com/quirq-ai/xo-space/issues)** —
   open and closed. Add to an existing thread rather than opening a twin.
2. **Typos, broken links, obvious one-line fixes:** just open the PR.
3. **Anything larger — a feature, a new endpoint, a change to what `.xo/`
   contains, a new runtime — open an issue first** and say what you plan to
   do. A five-minute conversation beats a rewritten PR. Say if you intend to
   implement it yourself so nobody duplicates the work.
4. Comment on the issue when you start, so it is visibly taken.

## Reporting a bug

Open an issue with:

- **How you installed:** the one-liner (`curl -fsSL https://quirq.ai/install | sh`),
  `./install.sh` from a clone, `./cowork-api.sh dev`, `./quirq` (compose), or
  a hosted workspace.
- **Where it runs:** OS and version (Windows means WSL — see
  [Windows](#windows)), Python version (`./venv/bin/python --version`), and
  the checkout's commit (`git rev-parse --short HEAD`, or the *Installed*
  line on the Setup tab).
- **Which runtime:** `AGENT_NAME` (and, for chat bugs, whether the CLI works
  on its own in a terminal).
- **What you did, what you expected, what happened.** Exact commands and the
  exact text of any error.
- **Evidence:** `curl -s localhost:5002/health`, and the tail of the server
  log — `<state root>/quirq.log` for the installer path, `/tmp/xo-space.log`
  for `cowork-api.sh`. **Redact tokens and keys before pasting** (`/health`
  reports presence, not values, on purpose; logs may not).
- For UI bugs: the browser, a screenshot, and the Wiki tab's page for that view
  if it contradicts what you saw.

If you are not sure it is a bug, open the issue anyway and say so.

## Security

Do **not** describe a vulnerability in a public issue — credentials, path
escapes, anything that would let one user reach another's data. Instead, open
an issue titled **"Security: request for a private channel"** with no details
in it, and a maintainer will reply with a way to send the report privately.
Give us a reasonable window to fix it before disclosing. Credit goes to the
reporter in the fix's notes unless you prefer otherwise.

## Set up a dev environment

**Linux or macOS. On Windows, use WSL** (Ubuntu is fine) and do everything
below — clone, run, test — inside it; the server does not run on native
Windows (see [Windows](#windows)). Prerequisites: `git`, Python 3.12+ (the
installer downloads one via uv if you have none), and — only for chat — an
agent CLI such as `npm install -g @anthropic-ai/claude-code`.

```bash
git clone https://github.com/quirq-ai/xo-space.git
cd xo-space
git checkout development          # where work lands; see Branches below
```

Two equivalent ways to run it; pick one:

```bash
# A. Contributor mode: system python3 (3.12+), venv/ + pip, auto-reload.
./cowork-api.sh install           # venv + requirements.txt
./cowork-api.sh dev               # http://localhost:5002/space/ (5003 if busy)

# B. Exactly what users run: uv-managed Python 3.12, foreground server.
./install.sh                      # in-place mode — never runs git on your clone
```

Both create `./venv`. The uv-made one (B) has **no `pip`**; add packages with
`~/.local/bin/uv pip install --python ./venv/bin/python …`. In-place mode makes
the clone itself your workspace and puts state in `./.quirq` — both are
gitignored. To boot a specific backend:

```bash
AGENT_NAME=claude_code venv/bin/python server.py   # or codex, openclaw, hermes, antigravity
```

The server runs its boot hooks (`config/agents/<name>/setup.sh`,
`scripts/install_shared_deps.sh`) on every start; set
`QUIRQ_SKIP_BOOT_INSTALL=1` if you do not want them installing anything on a
dev box (the installer sets it by default).

## Ground rules

These are the invariants the architecture depends on. They are enforced in
review, and some by tests.

1. **The modularity invariant.** Core code never names a specific agent
   (`claude_code`, `codex`, `openclaw`, `hermes`, `antigravity`). Agent-specific
   code lives in exactly three trees: `services/cowork_agent/adapters/<name>/`,
   `config/agents/<name>/`, and `config/models/<name>/`. Everything else
   resolves the active agent from `AGENT_NAME` through one seam,
   `services/cowork_agent/adapters/loader.py`. The only sanctioned core
   literal is the `openclaw` safe-boot default in `registry/agent_registry.py`
   (plus the short frozen allowlist in DEVELOPING.md §6).
2. **Thin routers, logic in services.** Endpoints in `routers/` are HTTP
   handlers; behaviour lives in `services/`. Routers import services, never
   the reverse. BFF route modules do no path work at all.
3. **The project folder is sacred.** Nothing goes into `<XO root>/<project>/`
   that would not survive a `git push`: no transcripts, prompts, credentials.
   Conversations stay in each runtime's own home (`~/.claude/`, `~/.codex/`,
   `~/.openclaw/`, `~/.hermes/`, …).
4. **`.xo/` belongs to the watcher.** Only
   `services/cowork_agent/visualizer/{sinks,workspace}/` write it; everything
   else — endpoints, the UI, agents — reads. Anything else written there is
   overwritten on the next tick.
5. **Backward compatibility.** Endpoint paths, request schemas, and response
   shapes are contracts. A missing capability module degrades to an empty/501
   shape; an import error inside an existing module fails loudly.
6. **Never log secrets.** No tokens, keys, cookies, or prompt text in logs or
   error messages. `/health` reports whether a credential is present, never
   its value; keep it that way.
7. **Async** for network and subprocess work; actionable error messages;
   clear code over clever code.

## Where things live

```
server.py                      app wiring, lifespan, both planes
routers/                       HTTP only — cowork_agent/ (Plane B /api/*), auth/, status/, space.py, xo_data.py
services/cowork_agent/         the broker: engine, adapters/, registry/, connectors/, visualizer/, xo_projects_sync/
config/agents/<name>/          per-runtime manifest, capabilities, settings, setup.sh
space_ui/                      the Space UI — plain ES modules, no build; js/views/wiki.js is the in-app manual
install.sh · cowork-api.sh · quirq   the three ways to run it
tests/                         xo-space's own unittest suite
plugin/ · .agents/             the Claude Code / Codex plugin bundles (kept in sync by a script)
```

DEVELOPING.md §2 has the full map; the README's "Project structure" section
has the annotated tree.

### Adding a runtime ("drop two folders")

No core edits. `config/agents/<name>/manifest.json` + `capabilities.json`, and
`services/cowork_agent/adapters/<name>/adapter.py` with a `BaseAgentAdapter`
subclass implementing `run`, `stream`, `adapter_name`. Add only the optional
capability modules you need (`usage.py`, `models.py`, `sessions.py`,
`routes.py`); the rest degrade to empty/501. Boot it with
`AGENT_NAME=<name>` and run the validation below. Walkthrough: DEVELOPING.md §4.

### Changing the Space UI

- One file per view under `space_ui/js/views/`; views never import each
  other — cross-view jumps go through `ctx.switchTo()`. All backend calls go
  through `js/core/api.js`.
- Bump the `?v=` cache stamp of every module you touch in `js/app.js` (and
  the `app.js` stamp in `index.html` if a view stamp moves; CSS stamps live in
  `index.html`). Otherwise browsers keep the old file.
- Update the view's tab guide in `js/views/wiki.js` in the same PR. The wiki
  is the manual for the exact build the user runs; stale pages are bugs.
- `node --check` each module you edit.

## Testing and validation

Run these before opening a PR. They need Linux/macOS (the watcher uses
`fcntl`); on Windows only the text-level tests run.

```bash
# 1. xo-space's own suite (prints its own count — do not quote a number in docs)
venv/bin/python -m unittest discover -s tests -t .

# 2. Import gate: the app must build under every runtime, and route counts
#    differ by design — read them, don't assert them.
for a in claude_code codex openclaw hermes antigravity; do
  AGENT_NAME=$a venv/bin/python -c "import server; print('$a', len(server.app.openapi()['paths']))"
done

# 3. Modularity invariant — grep core for an agent name you may have leaked
#    (DEVELOPING.md §6 lists the frozen exceptions).

# 4. If you touched install.sh: the harness runs its real functions in /tmp,
#    no network, no venv, no server.
bash tests/install_sh_harness.sh

# 5. If you touched plugin/ or .agents/: the two bundles must match.
./scripts/check_plugin_sync.sh
```

Add a test with every behaviour change. `tests/` is a flat `unittest` suite,
one `TestCase` per module, hermetic (temp dirs; never a real `~/.quirq` or
`.env`). Docs and wiki text are pinned by `tests/test_space_wiki.py` — when a
rename breaks a pin, update the pin to the new name rather than reinstating the
old one.

## Docs move with code

The docs are part of the product, and several are pinned by tests. When you
change… update in the same PR:

| Change | Also update |
|---|---|
| the adapter contract, session model, `.xo` layout, `/xo/*.json` views | `DEVELOPING.md` and the relevant page of `space_ui/js/views/wiki.js` |
| any view's behaviour | its tab guide in `wiki.js` |
| `install.sh`, roots, `.env` handling | `INSTALLATION.md` and the wiki's *Install & run locally* / *Your first run* pages |
| what a first-time user sees | the three places must agree: `INSTALLATION.md` "Your first run", the README quick start, the wiki `first-run` page |
| what leaves the machine | README "What leaves your machine" — keep the heading verbatim; `install.sh` prints a pointer to it |

The README only references files that exist on `main`; it states the repo's
contract, not observations from one machine, and re-measures any number it
quotes (route counts, test counts).

## Branches, commits, pull requests

**Branch from `development`, target `development`.** `main` is the release
branch: it is what the public one-liner installs, what the container image is
built from (`.github/workflows/publish-container.yml` runs on push to `main`),
and what `install.sh` fast-forwards a managed checkout to. `development` is
merged into `main` by the maintainers in "Merge Dev to Main" pull requests;
nothing lands on `main` directly.

Name branches by intent: `fix/…`, `feat/…`, `docs/…`, `chore/…`.

**Commits** — one concern per commit; validate before each. The first line is
`type(scope): what changed` in the imperative (`fix(install.sh): keep the exec
in start_server`, `docs: explain the first run`); the body says *why* and what
you verified. Commit messages are read by people debugging a year from now —
write them for that reader.

**Pull requests** — the description should let a reviewer understand the
change without reading the diff first:

- what it does and why (link the issue: `Fixes #N` — note that a PR into
  `development` does not auto-close the issue; a maintainer closes it after
  the merge);
- how you verified it, precisely — which commands, on which platform. Say
  plainly what you did *not* run;
- any behaviour change a user could notice, and any contract you touched;
- screenshots for UI changes.

Keep PRs focused: a rename, a fix, and a feature are three PRs. Allow edits
from maintainers so small review fixes don't need a round trip. Keep your
branch rebased on `development` if it falls behind; we merge with a merge
commit, so no need to squash yourself.

Contributions do not need a CLA. By submitting a PR you agree your change is
licensed under the repository's [MIT licence](LICENSE), like the rest of the
code.

## Review

A maintainer reviews every PR. We look at, in this order: does it break an
invariant; is it verified the way it claims; does it change a contract; are
the docs and tests in the same PR; then style. Expect questions rather than
silent edits. We try to respond within a few days; nudge the thread if a week
passes. Approval plus green checks is the bar to merge — there is no CI test
run yet, so "how you verified it" in the description carries real weight.

## Windows

**Use WSL.** The server does not run on native Windows: the watcher imports
`fcntl`, the boot hooks are bash, and the installer needs bash. Clone and work
inside a WSL distribution (Ubuntu), and run the validation there. From a
Windows-side checkout you can still edit and do static checks
(`python -m compileall`, `node --check`, the text-level tests). Two things bite:
`core.autocrlf` makes working copies CRLF, so judge a shell script's line
endings on the blob (`git ls-files --eol <file>` must say `i/lf`) and strip
`\r` before running a working-copy script under WSL; and Git-Bash's `grep`
hides CRs, so don't trust it for that check.

## Community and licence

- **Issues** — bugs, features, questions:
  <https://github.com/quirq-ai/xo-space/issues>.
- **The in-app Wiki** — `http://localhost:5002/space/` → Wiki. Sixteen
  version-matched pages; the maintained manual (the GitHub wiki is empty on
  purpose).
- **Docs site** — <https://docs.quirq.ai/docs/space/>.
- **Licence** — MIT, see [LICENSE](LICENSE).

Be kind and specific. Assume the other person is doing their best with the
information they have, and give them more of it.
