# Try the local XO Space extensions

This local prototype adds a read-only MCP App to the existing `quirq` plugin.
Nothing is published. It includes separate `space_home` sidebar and
`space_panel` conversation-panel tools, a searchable project dashboard, project todos, active sessions,
selected-project context sharing, and fullscreen where the host supports it.
It does not implement composer mentions, registered deep links, native plugin
settings, file editors, or rich forms. These can be added separately.

## Upload in the desktop New Plugin dialog

Select `dist/quirq-extensions-local.zip`. This variant puts
`plugin.json`, `.codex-plugin/plugin.json`, `.mcp.json`, `skills/`, `mcp/` and `ui/` directly
at the archive root. Do not upload the marketplace variant below: its nested
plugin and sibling marketplace files do not meet the single-plugin-root layout.
Archive integrity and root layout are verified locally; acceptance by the
desktop importer and its support for bundled stdio MCP remain unverified.
The public submission portal's skills-only path excludes MCP configuration;
do not assume that uploading skills alone enables the dashboard extensions.
If the desktop importer requires a registered MCP App, use its Create MCP App
flow with a supported connection, rather than claiming this ZIP supplies one.

## Install the marketplace ZIP locally

Use `dist/quirq-extensions-marketplace.zip` for the following extraction steps.

1. Extract the ZIP into a durable folder, for example `C:\xo-space-extensions`.
   Keep the hidden `.agents` and `.codex-plugin` folders. The extracted root
   contains `TRY-ME.md`, `.agents/plugins/marketplace.json` and `plugins/quirq/`.
2. Add the extracted folder as a local marketplace:
   `codex plugin marketplace add C:\xo-space-extensions`
   (On Linux/macOS use the corresponding absolute path.)
3. Restart the desktop app, open Plugins, select **XO Space Extensions (local)**,
   and install **XO Space**. Disable the original Quirq plugin while testing
   this copy to avoid duplicate skills/tools. Start a fresh chat and ask:
   **Open the XO Space dashboard using the Space MCP tool.**

`uv` must be on the host's PATH. The MCP process runs through `uv run --script`,
which resolves Python 3.11+ and the script's declared SDK dependencies on first
use. No global Python package installation is needed. The launch configuration
uses `./mcp/server.py` with `cwd: .`, matching the locally working xo-spike
plugin. The host must resolve these relative to the installed plugin root.
Use the explicit launch command below for standalone/manual connections.

For this checkout, the existing repo marketplace already points to the edited
`plugins/quirq/`. Refresh/reinstall its cached plugin and restart the app to
load the local files; installing from the GitHub URL would load the published
version instead of these changes.

## Try without a running Space server

Set `QUIRQ_EXTENSION_DEMO=1` in the environment of the plugin host before
launching it. Alternatively, add `"env": {"QUIRQ_EXTENSION_DEMO": "1"}` inside
the `space` server object in the extracted plugin's `.mcp.json`, then reinstall
or refresh the local cached plugin. The dashboard displays a demo banner.
Remove this setting to switch to real projects.

For a standalone visual preview (no plugin host required):

```powershell
python -m http.server 5014 --bind 127.0.0.1 --directory plugins/quirq/ui
```

Open `http://127.0.0.1:5014/dashboard.html?demo=1`. This previews the UI only;
host entrypoints and conversation context require a compatible MCP Apps host.

## Use real workspace data

Start the existing Space backend on Linux/macOS/WSL as usual. The bridge defaults
to `http://127.0.0.1:5002`. For port 5003 or another loopback port, set
`QUIRQ_EXTENSION_BASE_URL=http://127.0.0.1:5003` in the plugin host environment
or the MCP server's `env` object. The bridge can run on native Windows; the
Space backend still requires Linux/macOS/WSL. Windows-to-WSL localhost forwarding
must work if they run in different environments.

Only loopback HTTP origins with an explicit port are accepted. The UI calls
MCP tools through the host bridge; it never fetches localhost directly. Tools
use GET only, never follow redirects, ignore proxy environment variables, bound
response sizes and omit credentials, absolute paths and session transcripts.
Project descriptions and todo text are returned to the host when read; use demo
mode if you do not want real workspace content sent to your conversation.

Explicit stdio launch for MCP Inspector or another local MCP host:

```powershell
uv run --script C:\xo-space-extensions\plugins\quirq\mcp\server.py
```

For a local Streamable HTTP test, use:

```powershell
$env:QUIRQ_EXTENSION_DEMO = '1'
uv run --script plugins/quirq/mcp/server.py --http --port 5004
```

The endpoint is `http://127.0.0.1:5004/mcp`. It binds to loopback, has no remote
authentication, and is intended only for local testing. Do not expose it via
a tunnel. ChatGPT web cannot use a server on your computer's loopback. A remote
integration needs authenticated hosting and a registered MCP connection; this
prototype does not create one or invent an `.app.json` registration ID.

## Host support and fallback

The server advertises a `global` entrypoint on `space_home` and a `thread`
entrypoint on `space_panel`, using `openai/ui` metadata. `space_dashboard` remains
the inline tool. The package mirrors installed xo-spike 0.3.2: identical root
and compatibility manifests, `Interactive`/`Read` capabilities, icons,
widget-access compatibility metadata, and UI resource display modes. Local
plugins can expose native entrypoints without a remote app registration, as
the user's working xo-spike installation demonstrates. Quirq's host display
still needs verification after reinstall/refresh.

The UI uses MCP Apps initialization, tool calls, and model-context
updates. It requests fullscreen only when the host advertises that display
mode, and reports when project context is unsupported. Legacy `window.openai`
tool calls/widget state are fallback paths. Generic MCP hosts can use all six
tools without rendering HTML. The supplied Space URL opens the existing UI.

These declarations do not guarantee that your Codex build exposes native
sidebar/panel entries for bundled local MCP servers. Test in your host. The
official extension guide currently describes ChatGPT surfaces:
https://developers.openai.com/plugins/build/extensions

## Validate and rebuild

From the repository or extracted marketplace root:

```powershell
uv run --with 'mcp==1.28.1' python -m unittest discover -s plugins/quirq/tests -v
python plugins/quirq/scripts/package_plugin.py dist/quirq-extensions-local.zip
python plugins/quirq/scripts/package_plugin.py dist/quirq-extensions-marketplace.zip --marketplace
```

The upload ZIP contains only the plugin. The marketplace variant adds a local
marketplace wrapper. Both exclude
instruction files, Python caches, backend source, `.xo/`, `.env` and user state.
It is a plugin distribution archive, not a project-state backup.
