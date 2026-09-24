"""Setup commands: manual scheduling, persisted results and local-only controls."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.schedules import router
from utils.commands import CommandResult, scheduler


class SpaceCommandsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": self.tmp.name,
                                      "XO_SCHEDULER_ENABLED": "1", "XO_SCHEDULER_MAX_CONCURRENT": "1"})
        env.start()
        self.addCleanup(env.stop)
        scheduler.reset_state()
        self.addCleanup(scheduler.reset_state)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app, base_url="http://127.0.0.1:5002", client=("127.0.0.1", 12345))
        self.remote = TestClient(app, client=("192.0.2.10", 12345))
        self.payload = {"name": "Check checkout", "description": "A saved local command",
                        "command": {"argv": [sys.executable, "-c", "print('result')"], "timeout": 5}}

    def test_manual_never_ticks_and_switching_interval_preserves_results(self):
        now = scheduler.now_utc()
        for interval in ({}, {"every_seconds": None}):
            with self.subTest(interval=interval):
                job = scheduler.create_job({**self.payload, **interval}, now=now)
                self.assertIsNone(job["next_run"])
                self.assertEqual(job["description"], self.payload["description"])
                report = scheduler.tick(now=now + timedelta(days=365))
                self.assertEqual(report.started, [])
                self.assertEqual(report.errors, [])
                self.assertFalse(scheduler.runs_file(job["id"]).exists())
        job_id = job["id"]
        scheduler.run_now(job_id)
        scheduler._running[job_id].thread.join(5)
        result = scheduler.get_job(job_id)["last_result"]
        scheduled = scheduler.update_job(job_id, {**self.payload, "every_seconds": 60}, now=now)
        self.assertEqual(scheduled["next_run"], scheduler.stamp(now + timedelta(seconds=60)))
        manual = scheduler.update_job(job_id, self.payload, now=now)
        self.assertIsNone(manual["next_run"])
        self.assertEqual(manual["last_result"], result)
        self.assertEqual(scheduler.tick(now=now + timedelta(days=1)).started, [])

    def test_every_run_is_kept_and_poll_harvests_with_watcher_disabled(self):
        job = self.client.post('/api/schedules', json=self.payload).json()
        job_id = job['id']
        with patch.dict(os.environ, {"XO_SCHEDULER_ENABLED": "0"}):
            for count in range(1, 4):
                self.assertEqual(self.client.post(f'/api/schedules/{job_id}/run').status_code, 202)
                scheduler._running[job_id].thread.join(5)
                view = self.client.get(f'/api/schedules/{job_id}').json()
                self.assertFalse(view['running'])
                self.assertIsNone(view['next_run'])
                record = view['last_result']
                self.assertEqual(record['status'], 'ok')
                self.assertEqual(record['returncode'], 0)
                self.assertEqual(record['output_tail'], 'result\n')
                self.assertGreaterEqual(record['duration_seconds'], 0)
                self.assertTrue(record['finished_at'])
                self.assertEqual(len(scheduler.runs_file(job_id).read_text().splitlines()), count)
        history = self.client.get(f'/api/schedules/{job_id}/runs?limit=2').json()
        self.assertEqual(len(history['runs']), 2)
        self.assertEqual(history['log_path'], str(scheduler.log_file(job_id)))
        self.assertEqual(scheduler.log_file(job_id).read_text().count('result\n'), 3)
        self.client.delete(f'/api/schedules/{job_id}')
        self.assertTrue(scheduler.runs_file(job_id).is_file())
        self.assertTrue(scheduler.log_file(job_id).is_file())

    def test_single_flight_shared_cap_and_run_now_harvest(self):
        release = threading.Event()
        def blocking_run(spec):
            release.wait(5)
            return CommandResult(argv=spec.argv, returncode=0, output='done', duration_seconds=0.1)
        with patch.object(scheduler, 'run_spec_sync', side_effect=blocking_run):
            first = scheduler.create_job(self.payload)
            second = scheduler.create_job(self.payload)
            try:
                self.assertEqual(self.client.post(f"/api/schedules/{first['id']}/run").status_code, 202)
                conflict = self.client.post(f"/api/schedules/{first['id']}/run")
                self.assertEqual(conflict.status_code, 409)
                self.assertIn('in progress', conflict.json()['detail'])
                capped = self.client.post(f"/api/schedules/{second['id']}/run")
                self.assertEqual(capped.status_code, 409)
                self.assertIn('concurrency limit', capped.json()['detail'])
            finally:
                release.set()
                scheduler._running[first['id']].thread.join(5)
            # No tick or GET between runs: run_now must persist the previous result.
            scheduler.run_now(first['id'])
            scheduler._running[first['id']].thread.join(5)
            self.assertEqual(len(scheduler.list_runs(first['id'])), 2)

    def test_all_writes_and_runs_are_local_only_even_with_forwarded_header(self):
        job = scheduler.create_job(self.payload)
        for method, suffix in [('POST', ''), ('PUT', '/'+job['id']),
                               ('DELETE', '/'+job['id']), ('POST', '/'+job['id']+'/run')]:
            with self.subTest(method=method, suffix=suffix):
                response = self.remote.request(method, '/api/schedules'+suffix, json=self.payload,
                                               headers={'X-Forwarded-For': '127.0.0.1'})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.remote.get('/api/schedules').status_code, 200)
        self.assertFalse(scheduler.runs_file(job['id']).exists())

    def test_validation_uses_the_command_spec_and_leaves_no_definition(self):
        commands = [ {'argv': []}, {'argv': ['-git']}, {'argv': ['git', '\0']},
                     {'command': 'git status | wc'}, {'argv': ['git'], 'timeout': 0} ]
        for command in commands:
            with self.subTest(command=command):
                res = self.client.post('/api/schedules', json={**self.payload, 'command': {'timeout': 5, **command}})
                self.assertEqual(res.status_code, 400, res.text)
                self.assertTrue(res.json()['detail'])
        for bad in ({'description': []}, {'every_seconds': 0}, {'every_seconds': True}):
            self.assertEqual(self.client.post('/api/schedules', json={**self.payload, **bad}).status_code, 400)
        self.assertEqual(scheduler.list_jobs(), [])

    def test_result_statuses_and_output_tail(self):
        results = [
            ('failed', CommandResult(argv=['x'], returncode=2, output='x'*2500, duration_seconds=1)),
            ('timed_out', CommandResult(argv=['x'], returncode=-9, output='', duration_seconds=1, timed_out=True)),
            ('missing_binary', CommandResult(argv=['x'], returncode=-1, output='', duration_seconds=0, binary_missing=True)),
            ('error', CommandResult(argv=['x'], returncode=-1, output='', duration_seconds=0, exception='oops')),
        ]
        job = scheduler.create_job(self.payload)
        for status, result in results:
            with patch.object(scheduler, 'run_spec_sync', return_value=result):
                scheduler.run_now(job['id'])
                scheduler._running[job['id']].thread.join(5)
                record = scheduler.list_jobs()[0]['last_result']
                self.assertEqual(record['status'], status)
                self.assertEqual(record['output_tail'], result.output[-2000:])
        self.assertEqual(len(scheduler.list_runs(job['id'])), 4)

    def test_history_write_failure_keeps_the_completed_result_for_retry(self):
        job = scheduler.create_job(self.payload)
        scheduler.run_now(job['id'])
        scheduler._running[job['id']].thread.join(5)
        # A real filesystem failure: the history directory is occupied by a
        # file. GET reports it and must retain the completed executor result.
        blocked = scheduler.scheduler_dir() / 'runs'
        blocked.write_text('not a directory')
        report = scheduler.tick()
        self.assertEqual(report.started, [])
        self.assertEqual(len(report.errors), 1)
        self.assertIn('could not be appended', report.errors[0])
        response = self.client.get(f"/api/schedules/{job['id']}")
        self.assertEqual(response.status_code, 500)
        self.assertIn('could not be appended', response.json()['detail'])
        self.assertIn(job['id'], scheduler._running)
        blocked.unlink()
        response = self.client.get(f"/api/schedules/{job['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['last_result']['output_tail'], 'result\n')
        self.assertFalse(response.json()['running'])
        self.assertEqual([run['status'] for run in scheduler.list_runs(job['id'])], ['ok'])

    def test_state_write_failure_retries_without_duplicating_history(self):
        job = scheduler.create_job(self.payload)
        scheduler.run_now(job['id'])
        scheduler._running[job['id']].thread.join(5)
        with patch.object(scheduler.os, 'replace', side_effect=OSError('disk unavailable')):
            response = self.client.get(f"/api/schedules/{job['id']}/runs")
        self.assertEqual(response.status_code, 500)
        self.assertIn('state.json', response.json()['detail'])
        self.assertIn(job['id'], scheduler._running)
        self.assertEqual(len(scheduler.runs_file(job['id']).read_text().splitlines()), 1)
        view = self.client.get(f"/api/schedules/{job['id']}").json()
        self.assertEqual(view['last_result']['status'], 'ok')
        self.assertEqual(view['last_result']['output_tail'], 'result\n')
        self.assertFalse(view['running'])
        self.assertEqual(len(scheduler.list_runs(job['id'])), 1)
        scheduler.reset_state()  # emulate the next process reading durable state
        self.assertEqual(scheduler.get_job(job['id'])['last_result'], view['last_result'])

    def test_failed_thread_start_releases_manual_and_scheduled_slots(self):
        now = scheduler.now_utc()
        manual = scheduler.create_job(self.payload, now=now)
        scheduled = scheduler.create_job({**self.payload, 'every_seconds': 60}, now=now)
        with patch.object(scheduler.threading.Thread, 'start', side_effect=RuntimeError('no thread available')):
            with self.assertRaises(scheduler.SchedulerError):
                scheduler.run_now(manual['id'], now=now)
            report = scheduler.tick(now=now + timedelta(seconds=60))
        self.assertEqual(report.started, [])
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(scheduler._running, {})
        for job in (manual, scheduled):
            view = scheduler.get_job(job['id'])
            self.assertFalse(view['running'])
            self.assertIsNone(view['running_since'])
            self.assertIsNone(view['last_result'])
            self.assertEqual(scheduler.list_runs(job['id']), [])
        scheduler.run_now(manual['id'])
        scheduler._running[manual['id']].thread.join(5)
        self.assertEqual(scheduler.get_job(manual['id'])['last_result']['status'], 'ok')

    def test_read_recovers_restart_state_once_without_the_watcher(self):
        job = scheduler.create_job(self.payload)
        started = '2026-09-11T10:00:05Z'
        state = json.loads(scheduler.state_file().read_text())
        state['jobs'][job['id']]['running_since'] = started
        scheduler._write_doc(scheduler.state_file(), state)
        with patch.dict(os.environ, {'XO_SCHEDULER_ENABLED': '0'}), \
                patch.object(scheduler, 'run_spec_sync') as executor:
            view = self.client.get(f"/api/schedules/{job['id']}").json()
            self.assertFalse(view['running'])
            self.assertIsNone(view['running_since'])
            self.assertEqual(view['last_run'], started)
            self.assertEqual(view['last_result']['status'], 'lost')
            self.assertEqual(view['last_result']['started_at'], started)
            self.client.get('/api/schedules')
            runs = self.client.get(f"/api/schedules/{job['id']}/runs").json()['runs']
            self.assertEqual(len(runs), 1)
            executor.assert_not_called()

    def test_deleting_a_completed_job_harvests_before_removing_state(self):
        job = scheduler.create_job(self.payload)
        scheduler.run_now(job['id'])
        scheduler._running[job['id']].thread.join(5)
        # No tick or GET between completion and deletion.
        self.assertEqual(self.client.delete(f"/api/schedules/{job['id']}").status_code, 200)
        scheduler.reset_state()
        runs = [json.loads(line) for line in scheduler.runs_file(job['id']).read_text().splitlines()]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['status'], 'ok')
        self.assertEqual(runs[0]['output_tail'], 'result\n')
        self.assertEqual(scheduler.list_jobs(), [])
        self.assertNotIn(job['id'], json.loads(scheduler.state_file().read_text())['jobs'])

    def test_manual_run_and_scheduled_tick_share_the_concurrency_limit(self):
        now = scheduler.now_utc()
        release = threading.Event()
        def blocking_run(spec):
            release.wait(5)
            return CommandResult(argv=spec.argv, returncode=0, output='done', duration_seconds=0.1)
        manual = scheduler.create_job(self.payload, now=now)
        scheduled = scheduler.create_job({**self.payload, 'every_seconds': 60}, now=now)
        with patch.object(scheduler, 'run_spec_sync', side_effect=blocking_run):
            scheduler.run_now(manual['id'], now=now)
            try:
                report = scheduler.tick(now=now + timedelta(seconds=60))
                self.assertEqual(report.started, [])
                self.assertEqual(report.deferred, [scheduled['id']])
            finally:
                release.set()
                scheduler._running[manual['id']].thread.join(5)
            report = scheduler.tick(now=now + timedelta(seconds=61))
            self.assertEqual(report.finished, [manual['id']])
            self.assertEqual(report.started, [scheduled['id']])
            scheduler._running[scheduled['id']].thread.join(5)
            self.assertEqual(scheduler.get_job(scheduled['id'])['last_result']['trigger'], 'schedule')

    def test_setup_ui_pins(self):
        root = Path(__file__).resolve().parents[1] / 'space_ui'
        setup = (root / 'js/views/setup.js').read_text()
        card = (root / 'js/views/setup-commands.js').read_text()
        results = (root / 'js/core/command-results.js').read_text()
        self.assertIn("mountCommands(root.querySelector('#setup-commands'))", setup)
        self.assertIn("'/space/server/restart'", setup)
        self.assertIn('location.reload()', setup)
        self.assertIn('probe.data.instance_id!==', setup)
        self.assertNotIn("apiFetch('/health", setup)
        self.assertIn('setTimeout(pollRunning,3000)', card)
        self.assertIn('/runs?limit=20', results)
        self.assertIn('esc(run.output_tail', results)
        self.assertIn('openCommandResults', card)
        self.assertIn('No jobs yet', card)
        self.assertIn("from '../core/jobs.js?v=", card)
        for action in ('run', 'runs', 'edit', 'delete'):
            self.assertIn(f'data-command-action="{action}"', card)
