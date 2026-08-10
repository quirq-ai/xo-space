# quirq (Codex plugin)

Codex plugin for [Quirq / xo-space](https://github.com/quirq-ai/xo-space),
the local control plane for AI coding agents.

The plugin never manages the server's lifecycle on its own: discovery is
read-only, and installing or starting Quirq only happens after you say yes.

## Install

```
codex plugin marketplace add quirq-ai/xo-space
```

then install **quirq** from the `/plugins` list in Codex.

## Skills

| Skill | What it does |
|---|---|
| `quirq` | Knowledge of the local control plane and its discovery contract |
| `quirq-status` | Check whether Quirq is installed and running (check-only) |
| `quirq-install` | One-time guided install into a directory you choose |
| `quirq-start` | Start an already-installed server, without updating it |

## How discovery works

`skills/quirq/scripts/discover.sh` reads `~/.config/quirq/install.json`
(written by the server on every boot), probes `/health` on localhost
(pointer port first, then 5002/5003), and falls back to checking the
current directory. It performs no actions and reports one of: `running`,
`installed`, `not_installed`.

This bundle is the Codex counterpart of the Claude Code plugin in
`plugin/` at the repo root; `discover.sh` is byte-identical between the
two (checked by `scripts/check_plugin_sync.sh`).
