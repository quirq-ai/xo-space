# Connect an MCP client to Space

Space can expose its projects, planning documents, todos, and saved Inbox through
an MCP server. It is off by default. The initial tool set is read-only and uses
Streamable HTTP at `/mcp` on the same address and port as Space.

For terminal commands and shell scripts, use the separate [Space CLI](CLI.md).
Its enablement and token are independent of the MCP server.

## Enable and connect

1. Open Space's **Setup → Server** page from the local installation.
2. Turn on **Enable MCP server**.
3. Copy the **Server URL** and **Access token**, or select **Copy client config**.
4. Add the connection in an MCP client that supports Streamable HTTP and custom
   authorization headers. Refresh that client's tools if needed.

Copy the token when it is issued. Space stores only its hash; reloading the page
hides the token and it cannot be retrieved later. A client already configured
with it continues to work. If you lose it, select **Generate new token** and
update every client using the old token.

The copied configuration uses this generic shape:

```json
{
  "mcpServers": {
    "space": {
      "type": "http",
      "url": "http://127.0.0.1:5002/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_ACCESS_TOKEN"
      }
    }
  }
}
```

Replace the example URL with the URL shown in Setup and the placeholder with
your issued token. Client configuration schemas vary: if the client does not
accept this JSON, enter the URL manually, choose **Streamable HTTP**, and set
the `Authorization` header to `Bearer ` followed by the token. There is no OAuth
sign-in flow or stdio transport. Clients that require either are not supported.

The token grants access to all data exposed by these four tools. There are no
per-client or per-project access scopes. The tools do not execute commands,
edit projects, or expose arbitrary files or Space's credential settings.

## Local and remote connections

`127.0.0.1` reaches Space only from the same machine. For another machine or a
hosted MCP client, use a reachable HTTPS endpoint behind your existing
authenticated proxy or private access perimeter, and retain MCP bearer-token
authentication. Confirm the client can also satisfy that perimeter's access
requirements. HTTPS alone does not provide access control.

The MCP token protects `/mcp`; it does **not** secure the rest of Space's API.
Do not expose the whole Space server publicly just to connect an MCP client.
Keep settings and token management restricted to the local installation or its
existing trusted proxy. Enabling MCP does not open a port, change the bind
address, configure TLS, or establish a tunnel.

## Available tools

| Tool | Arguments | Result and limits |
| --- | --- | --- |
| `space_list_projects` | `limit` = `50` (integer `1`–`100`); `offset` = `0` (integer ≥ `0`) | Project IDs, display names, descriptions, total, and `has_more`; ordered by folder name. Increase `offset` for subsequent pages. |
| `space_read_project_document` | Required `project_id` and `document` | UTF-8 document content, limited to 64 KiB, with a `truncated` flag. |
| `space_list_todos` | Required `project_id`; `limit` = `50` (integer `1`–`100`) | Todos with session IDs, descriptions, status, and timestamps; deleted items omitted. Returns total and `has_more`. |
| `space_list_inbox` | `status` = `"open"` (`"open"`, `"done"`, or `"all"`); `limit` = `50` (integer `1`–`100`) | Saved items, newest first, plus counts, total, and `has_more`. `open` includes new and seen items. |

Use a `project_id` returned by `space_list_projects`. A project's folder must
contain `.xo/project.json` to appear. Hidden folders and symbolic links are not
exposed. Todo and Inbox lists currently have no offset parameter.

`document` must be one of these filenames at the project root:
`README.md`, `PROJECT.md`, `OBJECTIVES.md`, `PLAN.md`, `PROGRESS.md`, or `AGENTS.md`.
Nested paths and other filenames are rejected. Oversized JSON data files are
not served. The tools read saved state without ingesting feeds, marking Inbox
items seen, or triggering file migrations.

Example requests to a connected assistant:

- “List my Space projects and describe each one.”
- “Read the PLAN.md for the project I selected and summarize the next steps.”
- “Show that project's todos and group them by status.”
- “Summarize my open Space Inbox items.”

## Verify the connection with curl

Set these environment variables in your shell; the values below are placeholders.
Use your actual reachable URL and newly copied token locally.

```sh
export SPACE_MCP_URL='http://127.0.0.1:5002/mcp'
export SPACE_MCP_TOKEN='YOUR_ACCESS_TOKEN'

space_mcp_rpc() {
  curl --silent --show-error --fail-with-body "$SPACE_MCP_URL" \
    -H "Authorization: Bearer $SPACE_MCP_TOKEN" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -H 'MCP-Protocol-Version: 2025-03-26' \
    --data "$1"
}

space_mcp_rpc '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"space-curl-check","version":"1.0"}}}'
space_mcp_rpc '{"jsonrpc":"2.0","method":"notifications/initialized"}'
space_mcp_rpc '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
space_mcp_rpc '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"space_list_projects","arguments":{"limit":10,"offset":0}}}'

unset SPACE_MCP_TOKEN
```

Initialization should identify the server as `space`; discovery should return
the four tools above. The initialized notification returns HTTP `202` with no
body. This example uses the supported `2025-03-26` protocol; normal MCP clients
negotiate their protocol version. The transport is stateless, so this flow does
not require a session ID. Tool failures may arrive in a successful HTTP response
with `result.isError: true`; inspect the response body as well as the status.

## Disable or replace access

Turning the switch off blocks subsequent MCP requests with `404` and removes
the saved token hash. Enabling it again issues a new token. **Generate new token**
keeps the server enabled but makes the previous token return `401` on subsequent
requests. Update all clients after either action. Requests that have already
passed authentication may finish; these actions do not cancel in-flight work.

## Dependencies and troubleshooting

This feature requires the Python MCP SDK `mcp>=2.2,<3`. After updating an existing
checkout, install the current requirements into its existing virtual environment
and restart Space using your usual launch method:

```sh
uv pip install --python venv/bin/python -r requirements.txt
```

The requirements upgrade an older MCP SDK to the supported range. Installing
into a different Python environment will not update the running Space server.
If your environment includes pip, `venv/bin/python -m pip install -r requirements.txt`
is also supported; the default uv-managed environment may not include pip.

| Symptom | What to check |
| --- | --- |
| `/mcp` returns `404` | Enable the server in Setup and use the exact `/mcp` URL. |
| `/mcp` returns `401` | Send `Authorization: Bearer YOUR_ACCESS_TOKEN`; replace missing, incorrect, or revoked tokens. |
| Settings return `403` | Open Setup through the local installation or its trusted proxy; management requires a loopback connection and an allowed browser origin. |
| `/mcp` returns `403` | Browser origin checks rejected the request; use a supported MCP client and the intended Space origin. |
| Settings or MCP return `503` | Check server startup and local data-folder permissions. Missing transport initialization or unreadable/invalid saved MCP settings fails closed. The settings file is `~/.quirq/settings/mcp-server.json`, or under `QUIRQ_STATE_ROOT` when configured. |
| No projects appear | Confirm `XO_PROJECTS_ROOT` points to the intended projects and each project contains `.xo/project.json`; symlinked or hidden projects are excluded. |
| A document is unavailable | Check the project ID, fixed filename list, file presence, UTF-8 encoding, and absence of symlinks. |
| Inbox looks stale | MCP reads the saved Inbox without polling connections or refreshing feeders. Refresh through Space's normal Inbox/connections workflow, then read again. |
| Server URL cannot be reached | Check the actual Space port and the client's network access. A hosted client cannot reach your machine through its own `127.0.0.1`. |
