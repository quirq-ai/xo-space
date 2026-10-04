# Codex remote control

The Codex adapter exposes these endpoints only when `AGENT_NAME=codex`:

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/codex/remote-control/status` | `agent`, `supported`, `running`, `state`, `login_present`, `name` |
| POST | `/api/codex/remote-control/start` | Start/enable Codex's managed daemon; return status |
| POST | `/api/codex/remote-control/stop` | Stop the managed daemon; return stopped status |
| POST | `/api/codex/remote-control/pair` | Short-lived manual `code` and `expires_at` (Unix seconds) |

This follows Claude's adapter-owned lifecycle, with a Codex-specific namespace
so a client cannot accidentally operate another agent. Mutations require a local
TCP peer and an allowed origin, including the authenticated local Coder proxy.
All responses are `no-store`. Errors use non-2xx HTTP status and an `error` string.

Install a recent Codex CLI with `remote-control start`, `stop` and `pair` support
(contract verified with 0.153.4). Connect ChatGPT through `/connect/codex` first.
The native login in `$CODEX_HOME/auth.json` is shared with the daemon; an API key
alone is not a ChatGPT subscription login. `CODEX_HOME` defaults to the Codex
manifest home. The feature runs on Linux/macOS, like the Space backend.

Start/stop/pair use the official CLI with JSON output. The CLI owns daemon
persistence and process cleanup, so the session survives API restarts. Status
performs the native initialize/initialized handshake and calls
`remoteControl/status/read` over the private Unix WebSocket at
`$CODEX_HOME/app-server-control/app-server-control.sock`. It never enables remote
control as a side effect. Missing/stale sockets are stopped; protocol/network
errors are errors, not stopped or connected. `running` means enabled, including
the `connecting` and `errored` states; only `connected` permits pairing.

Pairing codes are generated on demand, never persisted or logged, and returned
without the CLI's machine pairing token. The UI must hide expired codes and
request a new one explicitly. The pairing exchange still requires approval in
the user's supported Codex client. No sandbox or approval defaults are changed.

CLI reference: https://learn.chatgpt.com/docs/developer-commands
Protocol source: https://github.com/openai/codex/tree/rust-v0.153.4/codex-rs/app-server-protocol/src/protocol/v2

Validation: `python -m pytest -q tests/test_codex_remote_control.py
tests/test_command_executor.py` and `python scripts/check_route_parity.py`.
The tests use isolated auth fixtures, a real Unix WebSocket peer, and mocked CLI
results; they do not pair a real ChatGPT account or operate a user's daemon.
