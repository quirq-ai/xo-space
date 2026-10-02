"""Hermetic bridge validation and real stdio MCP handshake; no backend boot."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load('space_bridge', ROOT / 'mcp/server.py')
packager = load('space_packager', ROOT / 'scripts/package_plugin.py')


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

    async def test_demo_and_error_results(self):
        with patch.dict(os.environ, {'QUIRQ_EXTENSION_DEMO': '1', 'QUIRQ_EXTENSION_BASE_URL': 'http://localhost:5002'}):
            listing = await bridge.space_dashboard()
            self.assertTrue(listing.structuredContent['demo'])
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
        with patch.dict(os.environ, {'QUIRQ_EXTENSION_DEMO': '0', 'QUIRQ_EXTENSION_BASE_URL': 'http://localhost:5002'}), patch.object(bridge.httpx, 'AsyncClient', client):
            result = await bridge.space_list_projects()
        self.assertTrue(result.isError)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, 'GET')
        def large_client(**kwargs):
            return original(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'x' * 50)), **kwargs)
        with patch.dict(os.environ, {'QUIRQ_EXTENSION_DEMO': '0'}), patch.object(bridge, 'MAX_BYTES', 20), patch.object(bridge.httpx, 'AsyncClient', large_client):
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

    async def test_real_stdio_protocol_resource_and_tools(self):
        env = dict(os.environ, QUIRQ_EXTENSION_DEMO='1', QUIRQ_EXTENSION_BASE_URL='http://localhost:5002')
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'mcp/server.py')], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                self.assertTrue(initialized.serverInfo.icons)
                tools = (await session.list_tools()).tools
                self.assertEqual({t.name for t in tools}, {'space_dashboard', 'space_home', 'space_panel', 'space_list_projects', 'space_project_details', 'space_active_sessions'})
                dashboard = next(t for t in tools if t.name == 'space_dashboard')
                self.assertEqual(dashboard.meta['openai/ui']['entrypoints'], [])
                for name, entrypoint in [('space_home', 'global'), ('space_panel', 'thread')]:
                    tool = next(t for t in tools if t.name == name)
                    self.assertEqual(tool.meta['openai/ui']['entrypoints'], [{'type': entrypoint}])
                    self.assertTrue(tool.meta['openai/widgetAccessible'])
                    self.assertTrue(tool.icons)
                self.assertTrue(all(t.annotations.readOnlyHint for t in tools))
                content = await session.read_resource(bridge.UI_URI)
                self.assertEqual(content.contents[0].mimeType, 'text/html;profile=mcp-app')
                self.assertEqual(content.contents[0].meta['openai/ui']['availableDisplayModes'], ['inline', 'fullscreen'])
                self.assertIn('ui/update-model-context', content.contents[0].text)
                result = await session.call_tool('space_dashboard', {})
                self.assertTrue(result.structuredContent['demo'])
                self.assertTrue((await session.call_tool('space_project_details', {'project_id': '..'})).isError)

    def test_archive_is_relocatable_and_excludes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'plugin.zip'
            packager.package(archive)
            with ZipFile(archive) as zipped:
                self.assertIsNone(zipped.testzip())
                names = zipped.namelist()
                self.assertIn('.codex-plugin/plugin.json', names)
                self.assertEqual(zipped.read('plugin.json'), zipped.read('.codex-plugin/plugin.json'))
                self.assertIn('.mcp.json', names)
                self.assertIn('ui/dashboard.html', names)
                self.assertNotIn('.agents/plugins/marketplace.json', names)
                self.assertFalse(any(n.startswith('plugins/') for n in names))
                self.assertFalse(any('.xo/' in n or '__pycache__' in n or n.endswith(('AGENTS.md', '.pyc', '.env')) for n in names))
            packager.package(archive, marketplace=True)
            with ZipFile(archive) as zipped:
                marketplace = json.loads(zipped.read('.agents/plugins/marketplace.json'))
                source = marketplace['plugins'][0]['source']['path'][2:]
                self.assertIn(source + '/.codex-plugin/plugin.json', zipped.namelist())


if __name__ == '__main__':
    unittest.main()
