# Use Space from a terminal

The standalone `space` client reads projects, planning documents, todos, and saved
Inbox items through Space's CLI API. It requires **Python 3.10 or later**, uses only
the standard library, and needs no package installation. CLI access has its own
switch and token, independent of MCP server access.

## Enable and configure

1. Open your local Space's **Setup → Server** page.
2. Turn on **Enable CLI access**.
3. Select **Download CLI** and save the file as `space`, or use the `space` script
   at the root of this repository.
4. Copy the CLI token when issued. In the directory containing the script, run
   the setup command shown in Space. For the default local address:

```sh
python3 ./space configure --url http://127.0.0.1:5002
```

Paste the CLI token into the hidden prompt. Configuration first checks
`/api/cli/status`; rejected credentials or disabled access are not saved. Copy
the token before reloading Setup: Space stores its hash and cannot show it again.
If necessary, select **Generate new token**, then configure every client again.
An MCP token will not authenticate CLI commands.

On macOS or Linux, you can make the script executable and put it on your `PATH`:

```sh
chmod +x ./space
mkdir -p "$HOME/.local/bin"
cp ./space "$HOME/.local/bin/space"
```

Add `$HOME/.local/bin` to your shell's `PATH` if needed. The examples below use
`space`; `python3 ./space` works equivalently without installing the script.

## Commands

```sh
space status
space projects
space projects --limit 20 --offset 20
space document my-project PLAN.md
space todos my-project --limit 100
space inbox --status open
space inbox --status done --limit 10 --json
```

Use project IDs returned by `space projects`; quote IDs containing spaces.

| Command | Arguments and behavior |
| --- | --- |
| `configure --url URL` | Verify a CLI token and save the local connection. Prompts for the token unless `SPACE_CLI_TOKEN` is set. |
| `status` | Authenticate and report whether CLI access is enabled and which commands it exposes. |
| `projects` | `--limit` defaults to `50` (range `1`–`100`); `--offset` defaults to `0` (nonnegative). |
| `document PROJECT DOCUMENT` | Read a fixed project-root document, up to 64 KiB. Reports truncation. |
| `todos PROJECT` | `--limit` defaults to `50` (range `1`–`100`); excludes deleted todos. |
| `inbox` | `--status open\|done\|all` defaults to `open`; `--limit` defaults to `50` (range `1`–`100`). Open includes new and seen items. |
| `logout` | Remove this client's saved credentials. Does not contact Space or revoke its token. |

The document filename must be `README.md`, `PROJECT.md`, `OBJECTIVES.md`,
`PLAN.md`, `PROGRESS.md`, or `AGENTS.md`. Arbitrary paths and symbolic links are
not exposed. Projects need `.xo/project.json` to appear in the list.

Add `--json` to `status`, `projects`, `document`, `todos`, or `inbox` for a single
JSON object on standard output. Errors go to standard error with a nonzero exit
code. JSON preserves field names from the API: `projects`, `content`, `todos`,
or `items`, with relevant counts and `has_more`/`truncated` flags. Only projects
support offsets; other lists return at most 100 records. Human-readable output
neutralizes terminal control characters from retrieved content.

## Connection settings and automation

URL precedence is `--url`, then `SPACE_URL`, then the saved URL. `--url` works
before or after the command. Use the **base URL**, without `/mcp`, `/api`, a query,
fragment, or credentials. Token precedence is `SPACE_CLI_TOKEN`, then the saved
token. There is no token command-line argument.

For automation, provide `SPACE_URL` and `SPACE_CLI_TOKEN` through your environment
or secret manager, then run a command without creating a config file:

```sh
# SPACE_CLI_TOKEN should already be supplied by your secret manager.
export SPACE_URL='https://space.example.com'
space projects --json
```

`configure` also accepts `SPACE_URL` and `SPACE_CLI_TOKEN`. If a hidden prompt is
unavailable, supply the token through the environment. A saved token is sent
only to its configured URL; overriding the URL requires configuring that Space
or explicitly providing `SPACE_CLI_TOKEN`. Redirects are refused, including
redirects to another address on the same host.

Credentials are stored as plaintext in an owner-readable/writable (`0600`) file,
created with those permissions before writing and replaced atomically:

- `$XDG_CONFIG_HOME/xo-space/cli.json`, when `XDG_CONFIG_HOME` is set;
- otherwise `~/.config/xo-space/cli.json`;
- `SPACE_CLI_CONFIG` overrides the complete file path for automation or tests.

`logout` removes that file, but does not unset environment variables. To revoke
access, disable CLI access or generate a replacement token in Space. Subsequent
requests with an old token fail; requests already authenticated may finish.
Neither action changes MCP server access.

## Remote access and troubleshooting

HTTP is accepted only for `localhost` and loopback IP addresses. Other hosts
require HTTPS. Keep remote Space behind your existing authenticated proxy or
private access perimeter, and ensure the client can satisfy its access controls.
The CLI token protects CLI data routes; it does not secure the rest of Space's
API. Enabling CLI access does not open a port, configure TLS, or create a tunnel.
Settings, token management, and downloading the client use local management
routes. Download locally before connecting from another machine.

Requests use a 15-second network timeout and a 4 MiB response limit. The CLI
reads saved data without refreshing feeds or changing project files or Inbox
state. For a stale Inbox, refresh through Space's normal Inbox/connections flow.

| Failure | Action |
| --- | --- |
| `401`, token rejected | Configure with the current **CLI** token; MCP tokens and revoked tokens do not work. |
| `404`, disabled or unavailable | Enable CLI access in Setup, and ensure the server version includes this feature. |
| `403`, access denied | Check the origin and your proxy's access requirements. Local management routes must be reached through the local installation or trusted proxy. |
| `503`, unavailable | Check Space's startup, saved access settings, and data-folder permissions. |
| Different Space URL | Configure that URL or provide its token explicitly with `SPACE_CLI_TOKEN`. |
| Redirect refused | Use the final base URL directly. A proxy sign-in redirect requires an access setup this client can use. |
| Cannot reach Space | Verify its port, network access, and TLS certificate. Another machine's `127.0.0.1` does not reach your Space. |
| Empty projects or unavailable document | Check the configured projects root, `.xo/project.json`, document allowlist, and absence of symlinks. |

The client itself has no MCP SDK dependency. The Space backend must have its
current server requirements installed, including `mcp>=2.2,<3` for the separate
MCP server integration. After updating an existing checkout, install requirements
into the existing server virtual environment and restart Space:

```sh
uv pip install --python venv/bin/python -r requirements.txt
```
