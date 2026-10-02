# XO Space inside ChatGPT / Codex

The `quirq` plugin shows **the real Space UI** inside the ChatGPT desktop app
(Chat, Work and the Codex tab): in the sidebar, beside a conversation, and
inline whenever the assistant opens a Space page. It is the same `space_ui/`
the browser serves, bundled into one MCP App view; no second UI to maintain.
It runs on the desktop app only: the plugin's server and Space are local.

## What the user gets

| Surface | How it appears | Backed by |
|---|---|---|
| Sidebar app | **XO Space** in the sidebar, opens fullscreen; quick action **Space inbox** | `space_home` (global entrypoint) |
| Conversation panel | **Space projects** tab beside a thread | `space_panel` (thread entrypoint) |
| Inline pages | The assistant opens any Space page: projects, sessions, inbox, setup, wiki | `space_open`, `space_open_inbox`, `space_open_sessions`, `space_open_setup` |
| Deep links | `codex://plugins/quirq@<marketplace>/app/space_home?path=%2Fagents%2Fsessions`; `/project/<id>` focuses a project | `hostContext["openai/deepLink"]` |
| @-mentions | Type **@** to attach a project, the inbox or active sessions | `space_mentions` + `xo-space://` resources |
| Chat context | The open page and project are shared with the assistant (background), or attached explicitly with **+ Chat** | `ui/update-model-context` |
| Settings | Plugin page: start page, fullscreen, context sharing, control bar; buttons to check the connection and open Setup/Inbox | `space_settings_read` / `space_settings_update` |
| Onboarding | After install: check/start Space, open it, explain the sidebar and @ | `skills/quirq-onboarding` |
| Compact dashboard | Read-only project list; works without the full UI build | `space_dashboard` |

Everything in the Space UI is interactive, including changes (todos, inbox,
connectors, secrets, jobs). Stopping, restarting and updating the server are
refused inside ChatGPT; do those from Space in the browser.

## How it works

```
Space UI (unchanged, bundled)          ChatGPT host              MCP server (mcp/server.py)        Space
fetch('/api/…')  ── space-bridge.js ─▶ tools/call space_api_* ─▶ path policy, no Origin ─────────▶ 127.0.0.1:5002
```

- `ui/space-bridge.js` runs before the Space UI. It performs the MCP Apps
  handshake and replaces `fetch` for Space URLs: reads become
  `space_api_read`, changes become `space_api_write`. Space's own `api.js` is
  untouched and still sees normal `Response`s, including "offline" when Space
  is down. Links and popups go through `ui/open-link`; deep links and tool
  results become Space hash routes; a small control bar adds fullscreen,
  **+ Chat** and open-in-browser.
- `scripts/build_space_app.py` inlines `space_ui`'s CSS, fonts and ES modules
  (esbuild) plus the bridge into `ui/space-app.html`. The file is generated but
  **committed**, because the GitHub marketplace installs this folder from git.
  It contains no workspace data; everything is fetched at run time. After any
  change to `space_ui/` or `ui/space-bridge.js`, rerun the build and commit the
  result: `tests/test_quirq_space_view.py` fails while the committed view is stale
  (it compares the fingerprint stamped in the file with the current sources).
  Without the file, every view falls back to the compact dashboard.
- The proxy tools are app-only (`_meta.ui.visibility: ["app"]`): the model
  cannot call them. Paths must be `/api/`, `/space/` or `/xo/`, without `..`,
  `//`, backslashes or fragments; only `Content-Type`, `Accept` and
  `X-XO-Session` are forwarded; redirects are not followed; responses are
  capped at 8 MiB and request bodies at 1 MiB. Requests carry no `Origin`, so
  Space's browser guard treats the bridge like the CLI (a local client); the
  guard still protects Space from other websites.
- Settings are stored in `~/.quirq/setup/extension/settings.json` (or
  `$PLUGIN_DATA/settings.json`, or `QUIRQ_EXTENSION_SETTINGS`).

## Build and install

Needs Node.js (npx) to build, `uv` on the host's PATH to run, and Space running
on Linux/macOS/WSL (default `http://127.0.0.1:5002`).

```powershell
python plugins/quirq/scripts/package_plugin.py dist/quirq-extensions-local.zip
```

This builds `ui/space-app.html`, then writes the upload ZIP (plugin at the
archive root). In the desktop app: **Plugins → New plugin**, select the ZIP,
install it, then start a new chat.

Uploading again over an existing upload of the same plugin name can fail with
only "Couldn't add plugin". The desktop app cannot delete uploaded plugins;
delete the old one from its page on chatgpt.com (**Copy link**), or test under a
temporary manifest `name`. Keep only one XO Space installed so the tools are
not duplicated.

`--marketplace` writes a local-marketplace variant instead; `--no-build`
packages the files already on disk.

To use another port: set `QUIRQ_EXTENSION_BASE_URL=http://127.0.0.1:<port>`
in the `space` server's `env` in `.mcp.json`. Only loopback HTTP origins with
an explicit port are accepted.

## Try without Space

Set `QUIRQ_EXTENSION_DEMO=1` in the `space` server's `env`. Tools and the
compact dashboard return sample data; the full UI shows mostly empty pages,
since demo mode only covers the project, session and inbox endpoints.

## Validate

The tests live in the repository's `tests/`, not in the plugin, so they never
ship to users:

- `tests/test_quirq_space_view.py`: committed view is fresh; upload ZIP
  contents. Standard library only; runs in every test run.
- `tests/test_quirq_bridge.py`: runs `tests/quirq/check_bridge.cjs` against
  `ui/space-bridge.js` (skipped without Node.js).
- `tests/test_quirq_plugin_server.py`: the MCP server. It needs the plugin's
  pinned mcp 1.x, so it skips in a venv with mcp 2.x; run it with:

```powershell
uv run --no-project --with 'mcp==1.28.1' --with 'httpx>=0.28,<1' python -m unittest tests.test_quirq_plugin_server
```

## Known limits

- File uploads from the Space UI (multipart) are refused inside ChatGPT; use
  Space in the browser.
- Images Space links by URL (custom branding logos) do not load in the sandbox.
- The bundle is a snapshot of `space_ui` at build time: rebuild the plugin
  after Space UI changes.
- ChatGPT on the web cannot reach a server on your computer; this is a
  desktop-only, local integration.
