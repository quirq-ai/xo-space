# xo-space qq commands

Each `infra/commands/NAME.sh` here is a `qq NAME` command for this repo. They all use one
Python environment, `.qq/venv`, built by `qq build server` on the Python `infra/repo.toml` pins. Run them from the
checkout with [qq](https://github.com/quirq-ai/qq)'s `qq` on `PATH`; `qq --help` lists
them. Commands that talk to the server find this checkout's port from
`~/.config/quirq/install.json` (only when it names this checkout), else `$PORT`, else 5002
(`infra/qq-lib.sh`).

## Tier 1: lifecycle

| Command | What it does |
|---|---|
| `qq start` | Builds or updates `.qq/venv` (`qq build server`), then starts xo-space in the foreground through `install.sh` on that venv. |
| `qq stop` | Stops this checkout's server through its localhost-only stop route, and nothing else. |
| `qq health` | Checks the server is up and prints its `/health` report. |
| `qq info` | Shows the running server's port, PID, instance and restart mode. |
| `qq logs` | Follows the server log in `<state>/logs/quirq.log`. |
| `qq open` | Prints the Space UI address. |

## Tier 2: maintenance

| Command | What it does |
|---|---|
| `qq update-check` | Says whether a newer commit is on the branch, without changing anything. |
| `qq update` | Fast-forwards the checkout to the newest commit, like the Setup tab's Update button. |
| `qq deps` | Rebuilds `.qq/venv`: the pinned Python from `infra/repo.toml` plus `requirements.txt`. |
| `qq doctor` | Shows the server's state health report. |
| `qq uninstall` | Shows what uninstall would remove. Only `qq uninstall --yes` removes it. |

## Tier 3: development

| Command | What it does |
|---|---|
| `qq checks` | Runs every CI check except the test suite, keeps going after a failure, and summarises. |
| `qq unit [ARGS]` | Runs pytest in qq's test venv; with no arguments, every test in `tests/`. |

## Tier 4: data

| Command | What it does |
|---|---|
| `qq projects` | Lists the projects in this Space. |
| `qq projects add NAME URL` | Clones a repository into this Space as a project. |
| `qq projects remove NAME` | Says whether the project can be removed. Only `--yes` deletes this Space's copy. |
| `qq backup PROJECT` | Backs up a project to its encrypted GitHub repo. `--all` backs up every project, `--list` shows the backups. |
| `qq restore PROJECT` | Restores a project from its newest backup. `--snapshot ID` picks one, `--force` overwrites, `--all` restores every project. |
| `qq inbox [open\|done\|all]` | Shows inbox items with their counts; open items by default. |
| `qq usage [DAYS]` | Shows cost, tokens and messages per day; 7 days by default. |
| `qq sessions [COUNT]` | Lists agent sessions like the Agents → Sessions page; newest 20 by default. |

## Tier 5: sharing and agent

| Command | What it does |
|---|---|
| `qq sharing [check]` | Shows the project-sharing relay status; `check` makes it poll now. |
| `qq members PROJECT` | Lists the Spaces a project is shared with. |
| `qq commits PROJECT [COUNT]` | Shows the project's shared branch and which commits are new. |
| `qq share PROJECT SPACE_ID` | Shares a project with another Space. |
| `qq revoke PROJECT SPACE_ID` | Stops sharing a project with a Space; their copy stays. |
| `qq apply PROJECT` | Fast-forwards a project to the commits fetched from its sharers. |
| `qq ask "QUESTION"` | Sends a prompt to the active agent and streams its answer. `QQ_SESSION=ID` continues a conversation, `QQ_PROJECT=NAME` works in a project. |
| `qq agent` | Shows the active agent runtime and every runtime this Space supports, read only. |

The server runs `share`, `revoke`, `apply`, `projects add|remove`, `backup` and `restore` itself
(and `update-check`, `update`): its routes call these commands first. See `DESIGN.md`.
