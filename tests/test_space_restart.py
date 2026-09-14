"""Restart modes, HTTP controls and the native runner surviving its parent."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from routers import space
from routers.cowork_agent.runtime_config import router as runtime_router
from services.cowork_agent import runtime_config
from utils.commands import CommandResult, run_sync

ROOT = Path(__file__).resolve().parents[1]


def _processes_started_in(directory: Path) -> list[int]:
    """PIDs whose working directory is ``directory``: what a fixture launched there."""
    target = os.path.realpath(directory)
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.readlink(entry / 'cwd') == target:
                found.append(int(entry.name))
        except OSError:
            continue
    return found


class RestartModeTests(unittest.TestCase):
    def test_managed_native_wrapper_foreground_and_stale_pid(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            'QUIRQ_MANAGED_CONTAINER': '0', 'UVICORN_RELOAD': '0', 'QUIRQ_ALLOW_SELF_RESTART': '1'
        }), patch.object(runtime_config, 'REPO_ROOT', Path(tmp)), patch.object(
            runtime_config, 'NATIVE_PID_FILE', Path(tmp) / 'pid'
        ):
            script = Path(tmp) / 'cowork-api.sh'
            script.write_text('#!/bin/sh\n')
            script.chmod(0o700)
            pid_file = runtime_config.NATIVE_PID_FILE
            self.assertEqual(runtime_config.restart_mode(), 'foreground')
            for value in ('invalid', '0', '1', '99999999'):
                pid_file.write_text(value)
                self.assertEqual(runtime_config.restart_mode(), 'foreground')
            for pid in (os.getpid(), os.getppid()):
                if pid <= 1:
                    continue
                pid_file.write_text(str(pid))
                self.assertEqual(runtime_config.restart_mode(), 'native')
            with patch.dict(os.environ, {'UVICORN_RELOAD': '1'}):
                self.assertEqual(runtime_config.restart_mode(), 'foreground')
                with patch.dict(os.environ, {'QUIRQ_MANAGED_CONTAINER': 'true'}):
                    self.assertEqual(runtime_config.restart_mode(), 'foreground')
            script.unlink()
            self.assertEqual(runtime_config.restart_mode(), 'foreground')
            with patch.dict(os.environ, {'QUIRQ_MANAGED_CONTAINER': 'true'}):
                self.assertEqual(runtime_config.restart_mode(), 'managed')

    def test_checked_in_runner_is_executable(self):
        self.assertTrue(os.access(ROOT / 'cowork-api.sh', os.X_OK))


class RestartRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(space.router)
        self.app.include_router(runtime_router)
        self.client = TestClient(self.app, client=('127.0.0.1', 12345))

    def test_status_exposes_mode_and_stable_instance(self):
        with patch.object(runtime_config, 'restart_mode', return_value='native'):
            status = self.client.get('/space/server/status').json()
            self.assertEqual(status['restart_mode'], 'native')
            self.assertEqual(status['instance_id'], self.client.get('/space/server/status').json()['instance_id'])

    def test_native_spawn_foreground_refusal_and_legacy_alias(self):
        for route in ('/space/server/restart', '/api/runtime-config/restart'):
            with self.subTest(route=route), patch.object(runtime_config, 'restart_mode', return_value='native'), patch.object(
                runtime_config, 'native_restart_pid', return_value=12345
            ), patch(
                'utils.commands.spawn_detached', return_value=CommandResult(
                    argv=['./cowork-api.sh', 'restart'], returncode=0, output='', duration_seconds=0)
            ) as spawn:
                res = self.client.post(route)
                self.assertEqual(res.status_code, 200)
                self.assertTrue(res.json()['restarting'])
                self.assertEqual(res.json()['mode'], 'native')
                spawn.assert_called_once_with(
                    ['./cowork-api.sh', 'restart-owned', '12345', str(os.getpid())],
                    cwd=runtime_config.REPO_ROOT,
                )
            with patch.object(runtime_config, 'restart_mode', return_value='foreground'):
                res = self.client.post(route)
                self.assertEqual(res.status_code, 409)
                self.assertIn('Ctrl-C and re-run', res.json()['detail'])

    def test_spawn_failure_is_actionable_and_remote_cannot_restart(self):
        with patch.object(runtime_config, 'restart_mode', return_value='native'), patch.object(
            runtime_config, 'native_restart_pid', return_value=12345
        ), patch(
            'utils.commands.spawn_detached', return_value=CommandResult(
                argv=['./cowork-api.sh'], returncode=-1, output='permission denied', duration_seconds=0, exception='denied')
        ) as spawn:
            res = self.client.post('/space/server/restart')
            self.assertEqual(res.status_code, 503)
            self.assertIn('permission denied', res.json()['detail'])
            spawn.reset_mock()
            remote = TestClient(self.app, client=('192.0.2.10', 12345))
            for route in ('/space/server/restart', '/api/runtime-config/restart'):
                self.assertEqual(remote.post(route, headers={'X-Forwarded-For': '127.0.0.1'}).status_code, 403)
            spawn.assert_not_called()

    def test_changed_native_pid_refuses_instead_of_spawning(self):
        with patch.object(runtime_config, 'restart_mode', return_value='native'), patch.object(
            runtime_config, 'native_restart_pid', return_value=None
        ), patch('utils.commands.spawn_detached') as spawn:
            response = self.client.post('/space/server/restart')
            self.assertEqual(response.status_code, 409)
            spawn.assert_not_called()

    def test_restart_rejects_browser_cross_origin_forms(self):
        client = TestClient(self.app, base_url='http://localhost:5002', client=('127.0.0.1', 12345))
        with patch.object(runtime_config, 'restart_mode', return_value='foreground'):
            for route in ('/space/server/restart', '/api/runtime-config/restart'):
                for origin in ('https://evil.example', 'null', 'http://127.0.0.1:5002',
                               'http://localhost:5003', 'https://localhost:5002',
                               'http://localhost:5002/path', 'http://user@localhost:5002',
                               'http://localhost:invalid', 'http://[broken'):
                    with self.subTest(route=route, origin=origin):
                        response = client.post(route, headers={'Origin': origin}, data={'run': '1'})
                        self.assertEqual(response.status_code, 403)
                self.assertEqual(client.post(route).status_code, 409)
                self.assertEqual(client.post(route, headers={'Origin': 'http://localhost:5002'}).status_code, 409)
        rebinding = TestClient(self.app, base_url='http://attacker.example', client=('127.0.0.1', 12345))
        self.assertEqual(rebinding.post('/space/server/restart', headers={
            'Origin': 'http://attacker.example',
        }).status_code, 403)

    def test_local_origin_uses_effective_port_and_ipv6(self):
        def request(host, origin, scheme='http'):
            return Request({'type': 'http', 'scheme': scheme, 'path': '/',
                            'headers': [(b'host', host.encode()), (b'origin', origin.encode())],
                            'client': ('::1', 1), 'server': ('::1', 80)})
        self.assertTrue(space._is_local_mutation(request('localhost', 'http://localhost:80')))
        self.assertTrue(space._is_local_mutation(request('[::1]:5002', 'http://[::1]:5002')))
        self.assertFalse(space._is_local_mutation(request('localhost', 'http://localhost:0')))


class ManagedRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_termination_is_deferred_until_after_response(self):
        request = Request({'type': 'http', 'client': ('::1', 12345), 'headers': []})
        sent = []
        async def send(message):
            sent.append(message)
        def terminate(*_args):
            self.assertEqual(sent[-1]['type'], 'http.response.body')
            self.assertFalse(sent[-1].get('more_body', False))
        with patch.object(runtime_config, 'restart_mode', return_value='managed'), patch(
            'routers.space.os.kill', side_effect=terminate
        ) as kill, patch(
            'routers.space.asyncio.sleep', new_callable=AsyncMock
        ) as sleep:
            response = await space.space_server_restart(request)
            self.assertEqual(json.loads(response.body)['mode'], 'managed')
            self.assertTrue(json.loads(response.body)['restarting'])
            kill.assert_not_called()
            await response(request.scope, AsyncMock(), send)
            sleep.assert_awaited_once_with(0.4)
            kill.assert_called_once_with(os.getpid(), space.signal.SIGTERM)


@unittest.skipUnless(os.name == 'posix' and shutil.which('pgrep'), 'native process manager needs POSIX and pgrep')
class NativeRestartIntegrationTests(unittest.TestCase):
    def _stop_everything_started_in(self, root, runner, env):
        # A restart requested through the API runs on its own runner, which
        # holds the process lock until the new server is confirmed up. A stop
        # sent before that is refused, which is how this test used to leave the
        # restarted server running. Retry until nothing launched from the
        # fixture is left, then kill whatever still is.
        deadline = time.monotonic() + 30
        while True:
            run_sync([str(runner), 'stop'], cwd=root, env=env, timeout=20)
            if not _processes_started_in(root) or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        for pid in _processes_started_in(root):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_native_route_restarts_with_a_new_process(self):
        # A miniature install with private pid/lock/log files. Intercept the
        # broad CLI sweep for safety and assert API restart never calls it.
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            script = (ROOT / 'cowork-api.sh').read_text().replace('/tmp/xo-space', str(root / 'xo-space'))
            dispatch = 'case "${1:-restart}" in'
            sweep_file = root / 'sweeps'
            overrides = ('kill_hindering_processes() { printf "sweep\\n" >> '
                         + shlex.quote(str(sweep_file)) + '; }\n'
                         'resolve_python_cmd() { printf "%s\\n" '+shlex.quote(sys.executable)+'; }\n')
            script = script.replace(dispatch, overrides + dispatch)
            runner = root / 'cowork-api.sh'
            runner.write_text(script)
            runner.chmod(0o700)
            (root / 'server.py').write_text(
                'import sys\nfrom pathlib import Path\n'
                f'sys.path.insert(0, {str(ROOT)!r})\n'
                'from fastapi import FastAPI\nimport uvicorn\n'
                'from routers.space import router\nfrom services.cowork_agent import runtime_config\n'
                f'runtime_config.REPO_ROOT = Path({tmp!r})\n'
                f'runtime_config.NATIVE_PID_FILE = Path({str(root / "xo-space.pid")!r})\n'
                'app = FastAPI()\napp.include_router(router)\n'
                f'uvicorn.run(app, host="127.0.0.1", port={port}, log_level="error")\n'
            )
            # Private roots: the fixture server must never read or write the
            # developer's real state root or projects.
            env = {**os.environ, 'PORT': str(port), 'HOST': '127.0.0.1',
                   'QUIRQ_MANAGED_CONTAINER': '0', 'UVICORN_RELOAD': '0',
                   'QUIRQ_STATE_ROOT': str(root / 'state'), 'XO_PROJECTS_ROOT': str(root / 'projects')}
            def status():
                try:
                    return httpx.get(f'http://127.0.0.1:{port}/space/server/status', timeout=0.5).json()
                except (httpx.HTTPError, ValueError):
                    return None
            try:
                start = run_sync([str(runner), 'start'], cwd=root, env=env, timeout=15)
                self.assertTrue(start.ok, start.output)
                deadline = time.monotonic() + 15
                before = status()
                while before is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                    before = status()
                self.assertIsNotNone(before, (root / 'xo-space.log').read_text())
                self.assertEqual(before['restart_mode'], 'native')
                sweeps = sweep_file.read_text()
                # Simulate ownership changing after the API reads its PID.
                # Both supplied PIDs still belong only to this fixture.
                pid_file = root / 'xo-space.pid'
                managed_pid = pid_file.read_text().strip()
                pid_file.write_text('1')
                try:
                    stale = run_sync([str(runner), 'restart-owned', managed_pid, str(before['pid'])],
                                     cwd=root, env=env, timeout=5)
                    self.assertFalse(stale.ok)
                    self.assertIn('runner changed', stale.output)
                    self.assertEqual(status()['instance_id'], before['instance_id'])
                finally:
                    pid_file.write_text(managed_pid)
                response = httpx.post(f'http://127.0.0.1:{port}/space/server/restart', timeout=5)
                self.assertEqual(response.status_code, 200, response.text)
                deadline = time.monotonic() + 30
                after = None
                while time.monotonic() < deadline:
                    after = status()
                    if after and after['instance_id'] != before['instance_id']:
                        break
                    time.sleep(0.2)
                self.assertIsNotNone(after, (root / 'xo-space.log').read_text())
                self.assertNotEqual(after['instance_id'], before['instance_id'])
                self.assertNotEqual(after['pid'], before['pid'])
                self.assertEqual(after['restart_mode'], 'native')
                self.assertEqual(sweep_file.read_text(), sweeps, 'API restart entered the broad CLI sweep')
            finally:
                self._stop_everything_started_in(root, runner, env)
            self.assertEqual(_processes_started_in(root), [], 'a server started by this test is still running')
