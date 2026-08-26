# AGENTS.md — config/agents

> **DOX branch:** `development` (re-stamped 2026-08-26; originally ported). These AGENTS.md
> files are local-only (not pushed), so a branch switch leaves them in place. If
> the current branch (`git rev-parse --abbrev-ref HEAD`) differs, treat this DOX
> as out-of-branch and re-index before relying on it.

## Purpose

Per-agent declarative configuration (Plane B). Each `config/agents/<name>/` is
auto-discovered at startup by `services/cowork_agent/registry/agent_registry.py`.
This is one of the three sanctioned trees where an agent name may appear.

## Ownership

- Owns each agent's manifest, capability flags, runtime settings, and lifecycle
  scripts for `claude_code/`, `openclaw/`, `hermes/`, `codex/`, `antigravity/`.

## Local Contracts

Per agent folder:

- `manifest.json` — canonical spec: `name`, `binary`, `home_dir`, `env_file`,
  `config_file`, `cli_timeout_seconds`, optional `api` block (gateway url/token/
  model env + defaults, `session_header`), `model_prefix`, `model_capabilities`,
  `commands` (templatable recipes), `providers`, optional `channels`.
  - A `providers.<id>` recipe is either **`.env` + CLI-verb** (`env_key` +
    `commands`) or **settings-env** (`settings_env`: an `env` map, `{api_key}`
    placeholder, merged into the agent's JSON `config_file` — for gateway-style
    providers like OpenRouter that the CLI reads from its own settings file).
- `capabilities.json` — UI feature flags (OAuth/API-keys/connectors/channels)
  consumed by `services/xo_manifest.py`.
- `settings.json` — lightweight runtime hints (`*_env` keys resolved from the
  environment at load time). Optional `startup_skills`: a list of
  `config/skills/catalog.json` names installed in the background at boot by
  `skill_catalog.install_startup_skills()`. Declare names, never commands — the
  command text stays in the catalog.
- `setup.sh` — one-time bootstrap, run from `server.py` lifespan if present.
- `agent.sh` (openclaw, hermes) — gateway/channel lifecycle manager.
- `troubleshoot.py` — diagnostic; exits 0 ok / 1 FAIL / 2 WARN.

## Work Guidance

- Manifest is the single source of truth per agent — do not duplicate its values
  into core or adapter code.
- Command templates must be pre-rendered argv (executed without `shell=True`);
  never interpolate untrusted input into a shell.

## Verification

Per-agent diagnostic: `venv/bin/python config/agents/<name>/troubleshoot.py`.

## Child DOX Index

(No child AGENTS.md. Add `config/agents/<name>/AGENTS.md` if an agent grows
setup rules beyond the shape above.)
