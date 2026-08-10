# quirq-plugin

Claude Code plugin for [Quirq / xo-space](https://github.com/quirq-ai/xo-space),
the local control plane for AI coding agents.

The plugin never manages the server's lifecycle on its own: discovery is
read-only, and installing or starting Quirq only happens after you say yes.

## Install

```
/plugin marketplace add quirq-ai/xo-space
/plugin install quirq@quirq-ai
```

## Commands

| Command | What it does |
|---|---|
| `/quirq:status` | Check whether Quirq is installed and running (check-only) |
| `/quirq:install <dir>` | One-time guided install into a directory you choose |
| `/quirq:start` | Start an already-installed server, without updating it |

The bundled `quirq` skill also lets Claude answer questions about your local
Quirq install in plain conversation.

## How discovery works

`scripts/discover.sh` reads `~/.config/quirq/install.json` (written by the
server on every boot), probes `/health` on localhost (pointer port first,
then 5002/5003), and falls back to checking the current directory. It
performs no actions and reports one of: `running`, `installed`,
`not_installed`.
