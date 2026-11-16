"""POST /api/xo-projects: name and brief, typed clone errors, and clone jobs
(wait:false -> 202, GET to poll, DELETE to cancel)."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from routers.cowork_agent.bff import bff_routers
from routers.cowork_agent.bff.project_clones import router as clones_router
from routers.cowork_agent.bff.project_management import router as management_router
from services import project_clones
from services import project_management as service
from services.git_clone import CloneProgress, GitCloneResult


class CloneJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "projects"
        self.root.mkdir()
        env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.root), "QUIRQ_STATE_ROOT": str(self.base / "state"),
                                      "XO_SPACE_ID": "ws-own"})
        env.start()
        self.addCleanup(env.stop)
        self.release = asyncio.Event()
        self.argv = None
        self.outcome = GitCloneResult(returncode=0)
        runner = patch.object(service, "run_git_clone", self.fake_git)
        runner.start()
        self.addCleanup(runner.stop)
        self.addCleanup(project_clones._jobs.clear)
        self.addAsyncCleanup(self.stop_jobs)
        app = FastAPI()
        app.include_router(management_router)
        app.include_router(clones_router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def stop_jobs(self):
        tasks = [job.task for job in project_clones._jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def fake_git(self, argv, *, cwd, env, on_progress=None, idle_timeout=None):
        self.argv = argv
        destination = Path(argv[-1])
        destination.mkdir()
        (destination / "README.md").write_text("cloned")
        progress = CloneProgress()
        progress.feed("Receiving objects:  50% (5/10), 2.00 MiB | 1.00 MiB/s")
        if on_progress:
            on_progress(progress)
        await self.release.wait()
        return self.outcome

    def meta(self, project_id):
        return json.loads((self.root / project_id / ".xo" / "project.json").read_text())

    async def wait_state(self, clone_id, state):
        for _ in range(200):
            body = (await self.client.get(f"/api/xo-projects/clones/{clone_id}")).json()
            if body["state"] == state:
                return body
            await asyncio.sleep(0.01)
        self.fail(f"clone never reached {state}: {body}")

    async def test_blocking_form_records_name_and_brief(self):
        self.release.set()
        response = await self.client.post("/api/xo-projects", json={
            "project_id": "billing", "repository_url": "https://example.com/org/billing.git",
            "display_name": "  Billing API ", "description": "Invoices and payouts.\n"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"project_id": "billing", "created": True})
        meta = self.meta("billing")
        self.assertEqual((meta["display_name"], meta["description"]), ("Billing API", "Invoices and payouts."))
        self.assertTrue(meta["pid"])
        self.assertIn("--progress", self.argv)
        # Without them, the same defaults as a scaffolded project.
        response = await self.client.post("/api/xo-projects", json={
            "project_id": "plain", "repository_url": "https://example.com/org/plain.git", "display_name": "  "})
        self.assertEqual(response.status_code, 201)
        self.assertEqual((self.meta("plain")["display_name"], self.meta("plain")["description"]), ("plain", ""))

    async def test_bad_details_are_rejected_before_git(self):
        for body in ({"display_name": 5}, {"description": "x" * 5000}, {"display_name": "a\x00b"}):
            response = await self.client.post("/api/xo-projects", json={
                "project_id": "new", "repository_url": "https://example.com/o/r", **body})
            self.assertIn(response.status_code, (400, 422), body)
        self.assertIsNone(self.argv)

    async def test_failure_codes_and_detail_reach_the_client(self):
        self.release.set()
        self.outcome = GitCloneResult(returncode=128, stderr_tail=[
            "remote: Repository not found.", "fatal: repository 'https://github.com/o/r.git/' not found"])
        response = await self.client.post("/api/xo-projects", json={
            "project_id": "new", "repository_url": "https://example.com/o/r"})
        self.assertEqual(response.status_code, 404)
        error = response.json()["detail"]
        self.assertEqual(error["code"], "repo_not_found_or_private")
        self.assertEqual(error["detail"], "fatal: repository 'https://github.com/o/r.git/' not found")
        self.outcome = GitCloneResult(returncode=-1, timed_out=True)
        response = await self.client.post("/api/xo-projects", json={
            "project_id": "new", "repository_url": "https://example.com/o/r"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (504, "timed_out"))
        self.assertFalse((self.root / "new").exists())
        self.assertFalse(list(self.root.glob(".space-clone-*")))

    async def test_async_clone_progress_then_done(self):
        response = await self.client.post("/api/xo-projects", json={
            "project_id": "big", "repository_url": "https://example.com/o/big", "wait": False,
            "display_name": "Big"})
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        self.assertTrue(job["clone_id"].startswith("cl_"))
        self.assertEqual((job["state"], job["project_id"]), ("cloning", "big"))
        running = await self.wait_state(job["clone_id"], "cloning")
        for _ in range(100):
            running = (await self.client.get(f"/api/xo-projects/clones/{job['clone_id']}")).json()
            if running["phase"] == "receiving":
                break
            await asyncio.sleep(0.01)
        self.assertEqual((running["phase"], running["phase_percent"]), ("receiving", 50))
        self.assertEqual(running["received_bytes"], 2 * 1024 * 1024)
        self.assertGreater(running["percent"], 0)
        self.assertIsInstance(running["idle_seconds"], int)
        # The same folder name can't be taken twice while it clones.
        again = await self.client.post("/api/xo-projects", json={
            "project_id": "big", "repository_url": "https://example.com/o/big"})
        self.assertEqual((again.status_code, again.json()["detail"]["code"]), (409, "clone_in_progress"))
        self.release.set()
        done = await self.wait_state(job["clone_id"], "done")
        self.assertEqual((done["percent"], done["error"], done["idle_seconds"]), (100, None, 0))
        self.assertIsNotNone(done["finished_at"])
        self.assertEqual(self.meta("big")["display_name"], "Big")
        cancel = await self.client.delete(f"/api/xo-projects/clones/{job['clone_id']}")
        self.assertEqual((cancel.status_code, cancel.json()["detail"]["code"]), (409, "clone_finished"))

    async def test_async_failure_is_reported_on_the_job(self):
        self.release.set()
        self.outcome = GitCloneResult(returncode=128, stderr_tail=[
            "fatal: unable to access 'https://x.invalid/o/r/': Could not resolve host: x.invalid"])
        job = (await self.client.post("/api/xo-projects", json={
            "project_id": "new", "repository_url": "https://x.invalid/o/r", "wait": False})).json()
        failed = await self.wait_state(job["clone_id"], "failed")
        self.assertEqual(failed["error"]["code"], "host_unreachable")
        self.assertIn("Could not resolve host", failed["error"]["detail"])
        self.assertNotIn("new", service._cloning)

    async def test_cancel_stops_and_cleans_up(self):
        job = (await self.client.post("/api/xo-projects", json={
            "project_id": "big", "repository_url": "https://example.com/o/big", "wait": False})).json()
        for _ in range(100):
            if list(self.root.glob(".space-clone-*/project")):
                break
            await asyncio.sleep(0.01)
        self.assertTrue(list(self.root.glob(".space-clone-*/project")))
        response = await self.client.delete(f"/api/xo-projects/clones/{job['clone_id']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "cancelled")
        self.assertFalse(list(self.root.glob(".space-clone-*")))
        self.assertFalse((self.root / "big").exists())
        self.assertNotIn("big", service._cloning)
        # Idempotent, and the name is free again.
        self.assertEqual((await self.client.delete(f"/api/xo-projects/clones/{job['clone_id']}")).json()["state"], "cancelled")
        self.release.set()
        self.assertEqual((await self.client.post("/api/xo-projects", json={
            "project_id": "big", "repository_url": "https://example.com/o/big"})).status_code, 201)

    async def test_cancel_before_start_and_unknown_and_cross_site(self):
        request = service.prepare_clone("early", "https://example.com/o/r")
        job = project_clones.CloneJob(clone_id="cl_early", project_id="early")
        job.task = asyncio.create_task(project_clones._run(job, request))
        job.task.add_done_callback(lambda _: request.release())
        project_clones._jobs[job.clone_id] = job
        self.assertEqual((await project_clones.cancel_clone("cl_early"))["state"], "cancelled")
        self.assertNotIn("early", service._cloning)
        self.assertEqual((await self.client.get("/api/xo-projects/clones/cl_missing")).status_code, 404)
        response = await self.client.delete("/api/xo-projects/clones/cl_early", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    async def test_running_clones_are_bounded(self):
        with patch.object(project_clones, "MAX_RUNNING", 1):
            first = await self.client.post("/api/xo-projects", json={
                "project_id": "one", "repository_url": "https://example.com/o/one", "wait": False})
            self.assertEqual(first.status_code, 202)
            second = await self.client.post("/api/xo-projects", json={
                "project_id": "two", "repository_url": "https://example.com/o/two", "wait": False})
            self.assertEqual((second.status_code, second.json()["detail"]["code"]), (429, "too_many_clones"))
        self.assertNotIn("two", service._cloning)
        self.release.set()
        await self.wait_state(first.json()["clone_id"], "done")

    async def test_stale_staging_folders_are_swept_and_fresh_ones_kept(self):
        stale, fresh = self.root / ".space-clone-old", self.root / ".space-clone-new"
        for path in (stale, fresh):
            (path / "project" / ".git").mkdir(parents=True)
            (path / "project" / ".git" / "pack").write_bytes(b"x" * 10)
        old = time.time() - service.STALE_STAGING_S - 60
        os.utime(stale, (old, old))
        self.release.set()
        response = await self.client.post("/api/xo-projects", json={
            "project_id": "new", "repository_url": "https://example.com/o/r"})
        self.assertEqual(response.status_code, 201)
        self.assertFalse(stale.exists())
        self.assertTrue((fresh / "project" / ".git" / "pack").exists())

    async def test_a_cancel_after_publish_still_reports_done(self):
        from services import project_management as pm
        self.release.set()
        entered, leave = asyncio.Event(), asyncio.Event()
        real = pm._discard_staging

        def slow_discard(*args):
            asyncio.run_coroutine_threadsafe(self._set(entered), loop)
            import time as _t
            while not leave.is_set():
                _t.sleep(0.01)
            real(*args)

        loop = asyncio.get_running_loop()
        with patch.object(pm, "_discard_staging", slow_discard):
            job = (await self.client.post("/api/xo-projects", json={
                "project_id": "late", "repository_url": "https://example.com/o/late", "wait": False})).json()
            await entered.wait()
            project_clones._jobs[job["clone_id"]].task.cancel()
            leave.set()
            done = await self.wait_state(job["clone_id"], "done")
        self.assertTrue((self.root / "late" / "README.md").exists())
        self.assertEqual(done["percent"], 100)
        self.assertFalse(list(self.root.glob(".space-clone-*")))

    @staticmethod
    async def _set(event):
        event.set()

    async def test_shutdown_cancels_running_clones_and_cleans_up(self):
        job = (await self.client.post("/api/xo-projects", json={
            "project_id": "big", "repository_url": "https://example.com/o/big", "wait": False})).json()
        for _ in range(100):
            if list(self.root.glob(".space-clone-*/project")):
                break
            await asyncio.sleep(0.01)
        await project_clones.shutdown()
        self.assertEqual(project_clones.clone_status(job["clone_id"])["state"], "cancelled")
        self.assertFalse(list(self.root.glob(".space-clone-*")))

    async def test_pasted_github_addresses_are_accepted(self):
        self.release.set()
        for pid, pasted in (("a", "octo/hello"), ("b", "github.com/octo/hello"),
                            ("c", " https://github.com/octo/hello/tree/main?tab=readme \n")):
            response = await self.client.post("/api/xo-projects", json={"project_id": pid, "repository_url": pasted})
            self.assertEqual(response.status_code, 201, (pasted, response.text))
            self.assertEqual(self.argv[-2], "https://github.com/octo/hello.git")
        response = await self.client.post("/api/xo-projects", json={
            "project_id": "d", "repository_url": "https://user:token@github.com/octo/hello"})
        self.assertEqual(response.json()["detail"]["code"], "invalid_repository_url")

    def test_clone_routes_are_mounted_after_project_routes(self):
        # A project named "clones" keeps GET /api/xo-projects/clones/tree etc.
        self.assertIs(bff_routers[-1], clones_router)


if __name__ == "__main__":
    unittest.main()
