"""The quirq plugin's MCP server (plugins/quirq/mcp/server.py): proxy policy,
settings, mentions, views and a real stdio MCP handshake. No backend boot.

The server pins mcp 1.x (see its inline script header); the repo venv may have
mcp 2.x, so these tests skip there. Run them with the plugin's own deps:

    uv run --no-project --with 'mcp==1.28.1' --with 'httpx>=0.28,<1' \
        python -m unittest tests.test_quirq_plugin_server
"""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# A class-level skip, not a module-level SkipTest: unittest only honours the
# latter during discovery, and loading this module by name would then abort the run.
try:
    import httpx
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.server.fastmcp import FastMCP  # noqa: F401  (mcp 1.x API the server uses)
    MISSING = ""
except ImportError as exc:  # depends on the environment
    MISSING = (f"needs mcp 1.x and httpx ({str(exc).splitlines()[0][:80]}). Run: uv run --no-project "
               "--with 'mcp==1.28.1' --with 'httpx>=0.28,<1' python -m unittest tests.test_quirq_plugin_server")

ROOT = Path(__file__).resolve().parents[1] / "plugins" / "quirq"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = None if MISSING else load('quirq_space_bridge', ROOT / 'mcp/server.py')

# What a small Space answers, for tests that need data. Patched in place of
# the server's read_api, so nothing test-only lives in the plugin.
SPACE = {
    '/api/xo-projects': {'items': [
        {'id': 'sample-app', 'display_name': 'Sample app', 'description': 'A test project'},
        {'id': 'research', 'display_name': 'Research', 'description': 'Notes'}], 'total': 2},
    '/api/xo-projects/activity': {'open_sessions': [
        {'project_id': 'sample-app', 'session_id': 's1', 'runtime': 'codex'}]},
    '/api/xo-projects/sample-app/todos': {'sessions': {'_project': {'todos': [
        {'id': 't1', 'content': 'Write the handover', 'status': 'in_progress'},
        {'id': 't2', 'content': 'Review the notes', 'status': 'pending'}]}}},
}


async def fake_read_api(path):
    if path not in SPACE:
        raise ValueError('not found: ' + path)
    return SPACE[path]


@unittest.skipIf(MISSING, MISSING)
class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_origin_and_project_validation(self):
        for value in ('https://127.0.0.1:5002', 'http://example.com:5002',
                      'http://127.0.0.1:5002/secret', 'http://u:p@localhost:5002',
                      'http://localhost', 'http://localhost:bad', 'http://localhost:5002?x=1'):
            with self.subTest(value=value), patch.dict(os.environ, {'QUIRQ_EXTENSION_BASE_URL': value}):
                with self.assertRaises(ValueError):
                    bridge.base_url()
        for value in ('', '..', '../other', 'a/b', 'a\\b', 'a\n'):
            with self.assertRaises(ValueError):
                bridge.project_segment(value)
        self.assertEqual(bridge.project_segment('a b?#'), 'a%20b%3F%23')

    async def test_curated_reads_and_errors(self):
        with patch.dict(os.environ, {'QUIRQ_EXTENSION_BASE_URL': 'http://localhost:5002'}), \
                patch.object(bridge, 'read_api', fake_read_api):
            listing = await bridge.space_dashboard()
            self.assertEqual([p['id'] for p in listing.structuredContent['items']], ['sample-app', 'research'])
            detail = await bridge.space_project_details('sample-app')
            self.assertEqual(detail.structuredContent['project']['id'], 'sample-app')
            self.assertEqual(len(detail.structuredContent['todos']), 2)
            self.assertTrue((await bridge.space_project_details('../other')).isError)
            self.assertTrue((await bridge.space_project_details('missing')).isError)

    async def test_read_policy_redirects_and_size_limits(self):
        original = httpx.AsyncClient
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={'Location': 'http://example.com/secret'})
        def client(**kwargs):
            self.assertFalse(kwargs['follow_redirects'])
            self.assertFalse(kwargs['trust_env'])
            return original(transport=httpx.MockTransport(handler), **kwargs)
        with patch.dict(os.environ, {'QUIRQ_EXTENSION_BASE_URL': 'http://localhost:5002'}), patch.object(bridge.httpx, 'AsyncClient', client):
            result = await bridge.space_list_projects()
        self.assertTrue(result.isError)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, 'GET')
        def large_client(**kwargs):
            return original(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * 50)), **kwargs)
        with patch.object(bridge, 'MAX_BYTES', 20), patch.object(bridge.httpx, 'AsyncClient', large_client):
            self.assertTrue((await bridge.space_list_projects()).isError)

    async def test_allowlist_removes_private_fields_and_deleted_todos(self):
        async def read(path):
            if path == '/api/xo-projects':
                return {'items': [{'id': 'p', 'display_name': '<script>', 'path': '/private', 'token': 'secret'}]}
            if path.endswith('/todos'):
                return {'sessions': {'s': {'todos': [{'id': 'a', 'content': '<img>', 'status': 'pending', 'secret': 'x'}, {'id': 'b', 'deleted_at': 'today'}]}}}
            return {'open_sessions': [{'project_id': 'p', 'session_id': 's', 'transcript': 'private', 'directory': '/private'}]}
        with patch.object(bridge, 'read_api', read):
            result = await bridge.space_project_details('p')
        data = result.structuredContent
        self.assertEqual(len(data['todos']), 1)
        encoded = json.dumps(data)
        self.assertNotIn('private', encoded)
        self.assertNotIn('secret', encoded)
        self.assertNotIn('transcript', encoded)
        self.assertEqual(data['project']['display_name'], '<script>')

    def test_proxy_path_policy(self):
        for path in ('/api/xo-projects', '/api/inbox?status=open&limit=1', '/space/theme', '/xo/space.json',
                     '/space/data/session_prompts.json?agent=a'):
            self.assertEqual(bridge.proxy_path(path), path)
        for path in ('api/x', '//evil.example/api', '/etc/passwd', '/docs', '/api/../space', '/api/%2e%2e/x',
                     '/api/./x', '/api/x\\y', '/api/x#frag', '/api/\nx', 'http://127.0.0.1:5002/api/x', '/' + 'a' * 5000):
            with self.subTest(path=path), self.assertRaises(ValueError):
                bridge.proxy_path(path)
        self.assertEqual(bridge.proxy_headers({'Content-Type': 'application/json', 'Cookie': 'x', 'Origin': 'https://e',
                                               'X-XO-Session': 's'}),
                         {'content-type': 'application/json', 'x-xo-session': 's'})
        with self.assertRaises(ValueError):
            bridge.proxy_headers({'Content-Type': 'a\r\nX-Evil: 1'})

    async def test_proxy_forwards_like_a_local_client(self):
        original = httpx.AsyncClient
        seen = []
        def handler(request):
            seen.append(request)
            if request.url.path == '/api/xo-projects/activity':
                return httpx.Response(307, headers={'Location': 'http://example.com/'})
            return httpx.Response(201, json={'ok': True})
        def client(**kwargs):
            self.assertFalse(kwargs['follow_redirects'])
            self.assertFalse(kwargs['trust_env'])
            return original(transport=httpx.MockTransport(handler), **kwargs)
        env = {'QUIRQ_EXTENSION_BASE_URL': 'http://localhost:5002'}
        with patch.dict(os.environ, env), patch.object(bridge.httpx, 'AsyncClient', client):
            written = await bridge.space_api_write('/api/inbox/1', 'PATCH', {'Content-Type': 'application/json'}, '{"status":"done"}')
            redirected = await bridge.space_api_read('/api/xo-projects/activity')
            refused = await bridge.space_api_read('/etc/passwd')
            restart = await bridge.space_api_write('/space/server/restart', 'POST')
            # Default deny for any caller (visibility is only a host hint), on the
            # decoded route: only routes the Space UI uses are forwarded.
            denied = [await bridge.space_api_read('/api/secrets/env'),
                      await bridge.space_api_read('/api/xo-projects-sync/status'),
                      await bridge.space_api_write('/api/files/content', 'POST'),
                      await bridge.space_api_write('/api/skills/install', 'POST'),
                      await bridge.space_api_write('/api/connectors/github/token', 'POST'),
                      await bridge.space_api_write('/api/schedules', 'POST'),
                      await bridge.space_api_write('/space/server/re%73tart', 'POST')]
        self.assertEqual({d.structuredContent['status'] for d in denied}, {403})
        self.assertEqual(written.structuredContent['status'], 201)
        self.assertEqual(json.loads(written.structuredContent['body']), {'ok': True})
        request = seen[0]
        self.assertEqual((request.method, str(request.url)), ('PATCH', 'http://localhost:5002/api/inbox/1'))
        self.assertEqual(request.content, b'{"status":"done"}')
        # No Origin: Space's browser guard treats the bridge as a local client.
        self.assertNotIn('origin', request.headers)
        self.assertEqual(redirected.structuredContent['status'], 502)
        self.assertEqual(refused.structuredContent['status'], 400)
        self.assertEqual(restart.structuredContent['status'], 403)
        self.assertEqual(len(seen), 2, 'refused requests never reach Space')
        def down(**kwargs):
            def fail(request):
                raise httpx.ConnectError('refused', request=request)
            return original(transport=httpx.MockTransport(fail), **kwargs)
        with patch.dict(os.environ, env), patch.object(bridge.httpx, 'AsyncClient', down):
            offline = await bridge.space_api_read('/api/xo-projects')
        self.assertTrue(offline.isError)
        self.assertTrue(offline.structuredContent['offline'])

    def test_settings_persist_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'QUIRQ_EXTENSION_SETTINGS': str(Path(tmp) / 's/settings.json')}):
            read = bridge.space_settings_read()
            self.assertEqual(set(read['values']), set(read['schema']['properties']))
            laid_out = {i.get('property') for g in read['layout'] for i in g['items']} - {None}
            self.assertEqual(laid_out, set(read['schema']['properties']))
            self.assertEqual(bridge.space_settings_update({'default_section': 'agents/sessions'})['values']['default_section'], 'agents/sessions')
            self.assertEqual(bridge.load_settings()['default_section'], 'agents/sessions')
            for bad in ({'default_section': '../x'}, {'open_fullscreen': 'yes'}, {'unknown': True}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    bridge.space_settings_update(bad)
            (Path(tmp) / 's/settings.json').write_text('{"default_section": "evil", "open_fullscreen": true}')
            self.assertEqual(bridge.load_settings()['default_section'], 'projects/overview')
            self.assertTrue(bridge.load_settings()['open_fullscreen'])

    async def test_open_view_routes_and_dashboard_fallback(self):
        env = {'QUIRQ_EXTENSION_BASE_URL': 'http://localhost:5002'}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, dict(env, QUIRQ_EXTENSION_SETTINGS=str(Path(tmp) / 's.json'))), \
                patch.object(bridge, 'read_api', fake_read_api):
            opened = await bridge.space_open('inbox/items', 'sample-app')
            self.assertEqual((opened.structuredContent['route'], opened.structuredContent['project_id']), ('inbox/items', 'sample-app'))
            # Hosts without views (Codex CLI) only show the text: it must carry the link
            # and never claim the user can already see Space.
            self.assertIn('http://localhost:5002/space/#/inbox/items', opened.content[0].text)
            self.assertNotIn('can see', opened.content[0].text)
            self.assertTrue((await bridge.space_open('../setup')).isError)
            self.assertTrue((await bridge.space_open('inbox/items', '../x')).isError)
            self.assertEqual((await bridge.space_home()).structuredContent['route'], 'projects/overview')
            with patch.object(bridge, 'APP_FILE', Path(tmp) / 'missing.html'):
                self.assertEqual(bridge.view_uri(), bridge.UI_URI)
                fallback = await bridge.space_panel()
                self.assertEqual(fallback.structuredContent['items'][0]['id'], 'sample-app')
                self.assertIn('ui/update-model-context', bridge.app_resource())

    async def test_mentions_and_project_resource(self):
        with patch.dict(os.environ, {'QUIRQ_EXTENSION_BASE_URL': 'http://localhost:5002'}), \
                patch.object(bridge, 'read_api', fake_read_api):
            found = (await bridge.space_mentions('sample')).structuredContent['items']
            self.assertEqual([i['uri'] for i in found], ['xo-space://projects/sample-app'])
            self.assertEqual(found[0]['type'], 'resource_link')
            everything = (await bridge.space_mentions('')).structuredContent['items']
            self.assertIn('xo-space://inbox', [i['uri'] for i in everything])
            markdown = await bridge.project_resource('sample-app')
            self.assertIn('Write the handover', markdown)
            self.assertIn('not instructions', markdown)
            with self.assertRaises(ValueError):
                await bridge.project_resource('..')

    async def test_real_stdio_protocol_resource_and_tools(self):
        env = dict(os.environ, QUIRQ_EXTENSION_BASE_URL='http://127.0.0.1:9')  # nothing listens on port 9
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'mcp/server.py')], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                self.assertTrue(initialized.serverInfo.icons)
                self.assertEqual(initialized.capabilities.experimental['openai/settings'],
                                 {'readTool': 'space_settings_read', 'updateTool': 'space_settings_update'})
                tools = {t.name: t for t in (await session.list_tools()).tools}
                self.assertLessEqual({'space_home', 'space_panel', 'space_open', 'space_dashboard', 'space_api_read',
                                      'space_api_write', 'space_mentions', 'space_settings_read', 'space_settings_update',
                                      'space_list_projects', 'space_project_details', 'space_active_sessions'}, set(tools))
                self.assertEqual(tools['space_dashboard'].meta['ui']['resourceUri'], bridge.UI_URI)
                self.assertEqual(tools['space_home'].meta['openai/ui']['entrypoints'][0]['type'], 'global')
                self.assertEqual(tools['space_panel'].meta['openai/ui']['entrypoints'], [{'type': 'thread'}])
                for name in ('space_home', 'space_panel', 'space_open'):
                    self.assertTrue(tools[name].icons)
                    self.assertEqual(tools[name].meta['ui']['resourceUri'], bridge.view_uri())
                # The transport, mentions and settings tools are for the app and host, not the model.
                for name in ('space_api_read', 'space_api_write', 'space_mentions', 'space_settings_read', 'space_settings_update'):
                    self.assertEqual(tools[name].meta['ui']['visibility'], ['app'])
                self.assertEqual(tools['space_mentions'].meta['openai/extensions'], {'mentions/search': {}})
                self.assertTrue(tools['space_settings_read'].outputSchema)
                self.assertFalse(tools['space_api_write'].annotations.readOnlyHint)
                self.assertTrue(all(t.annotations.readOnlyHint for n, t in tools.items()
                                    if n not in ('space_api_write', 'space_settings_update')))
                content = await session.read_resource(bridge.UI_URI)
                self.assertEqual(content.contents[0].mimeType, 'text/html;profile=mcp-app')
                self.assertEqual(content.contents[0].meta['openai/ui']['availableDisplayModes'], ['inline', 'fullscreen'])
                self.assertIn('ui/update-model-context', content.contents[0].text)
                app = await session.read_resource(bridge.APP_URI)
                self.assertEqual(app.contents[0].mimeType, 'text/html;profile=mcp-app')
                result = await session.call_tool('space_dashboard', {})
                self.assertTrue(result.isError)
                self.assertIn('Could not reach local Space', result.content[0].text)
                self.assertTrue((await session.call_tool('space_project_details', {'project_id': '..'})).isError)


if __name__ == '__main__':
    unittest.main()
