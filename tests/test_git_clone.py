"""git clone progress, the no-progress timeout, cancellation and error codes."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from services import git_clone
from services.git_clone import CloneProgress, GitCloneResult, classify_failure, failure_detail, run_git_clone


class ProgressParsingTests(unittest.TestCase):
    def test_phases_percent_and_bytes(self):
        progress = CloneProgress()
        seen = []
        for line in ("Cloning into 'x'...",
                     "remote: Enumerating objects: 1234, done.",
                     "remote: Counting objects:  50% (5/10)",
                     "remote: Compressing objects: 100% (10/10), done.",
                     "Receiving objects:  40% (400/1000), 1.50 MiB | 3.00 MiB/s",
                     "remote: Total 1000 (delta 2), reused 0 (delta 0), pack-reused 0",
                     "Receiving objects: 100% (1000/1000), 3.25 MiB | 3.00 MiB/s, done.",
                     "Resolving deltas:  50% (1/2)",
                     "Updating files:  10% (1/10)"):
            if progress.feed(line):
                seen.append(dict(progress.as_dict()))
        self.assertEqual([s["phase"] for s in seen],
                         ["counting", "counting", "counting", "receiving", "receiving", "resolving", "checkout"])
        receiving = seen[3]
        self.assertEqual(receiving["phase_percent"], 40)
        self.assertEqual(receiving["received_bytes"], int(1.5 * 1024 * 1024))
        self.assertEqual(seen[4]["received_bytes"], int(3.25 * 1024 * 1024))
        percents = [s["percent"] for s in seen]
        self.assertEqual(percents, sorted(percents))
        self.assertLess(percents[-1], 100)
        progress.finishing()
        self.assertEqual(progress.phase, "finishing")

    def test_counting_climbs_once_across_the_servers_three_steps(self):
        progress = CloneProgress()
        seen = []
        for line in ("remote: Enumerating objects: 2564870, done.",
                     "remote: Counting objects:  50% (3996/7992)        ",
                     "remote: Counting objects: 100% (7992/7992), done.",
                     "remote: Compressing objects:   3% (74/2450)",
                     "remote: Compressing objects:  50% (1225/2450)",
                     "remote: Compressing objects: 100% (2450/2450), done.",
                     "Receiving objects:   0% (1/2564870)"):
            progress.feed(line)
            seen.append((progress.phase, progress.phase_percent, progress.percent))
        counting = [pct for phase, pct, _ in seen if phase == "counting"]
        self.assertEqual(counting, sorted(counting))
        self.assertEqual((counting[0], counting[-1]), (20, 100))
        overall = [pct for *_, pct in seen]
        self.assertEqual(overall, sorted(overall))
        self.assertLess(overall[0], overall[-2])  # the bar moves during counting

    def test_never_steps_back_and_ignores_other_lines(self):
        progress = CloneProgress()
        progress.feed("Resolving deltas:  50% (1/2)")
        before = progress.as_dict()
        self.assertTrue(progress.feed("remote: Counting objects:  10% (1/10)"))
        self.assertEqual(progress.as_dict(), before)
        self.assertFalse(progress.feed("fatal: repository 'x' not found"))
        self.assertFalse(progress.feed("warning: You appear to have cloned an empty repository."))
        self.assertEqual(progress.received_bytes, 0)
        progress = CloneProgress()
        progress.feed("Receiving objects:  1% (1/100), 541 bytes | 541.00 KiB/s")
        self.assertEqual(progress.received_bytes, 541)


def failed(*lines, timed_out=False):
    return GitCloneResult(returncode=128, stderr_tail=list(lines), timed_out=timed_out)


class ClassificationTests(unittest.TestCase):
    def code(self, result, credential_sent=False, ssh=False):
        return classify_failure(result, credential_sent=credential_sent, ssh=ssh)[0]

    def test_codes(self):
        cases = [
            (failed("remote: Repository not found.", "fatal: repository 'https://github.com/o/r.git/' not found"), {}, "repo_not_found_or_private"),
            (failed("fatal: could not read Username for 'https://github.com': terminal prompts disabled"), {}, "repo_not_found_or_private"),
            (failed("fatal: repository 'https://example.com/o/r/' not found"), {}, "repo_not_found_or_private"),
            (failed("ERROR: Repository not found.", "fatal: Could not read from remote repository."), {"ssh": True}, "repo_not_found_or_private"),
            (failed("remote: Invalid username or token. Password authentication is not supported for Git operations.",
                    "fatal: Authentication failed for 'https://github.com/o/r.git/'"), {"credential_sent": True}, "auth_failed"),
            (failed("fatal: unable to access 'https://github.com/o/r.git/': The requested URL returned error: 403"), {"credential_sent": True}, "auth_failed"),
            (failed("fatal: Authentication failed for 'https://example.com/o/r.git/'"), {}, "repo_not_found_or_private"),
            (failed("git@github.com: Permission denied (publickey).", "fatal: Could not read from remote repository."), {"ssh": True}, "auth_failed"),
            (failed("Host key verification failed.", "fatal: Could not read from remote repository."), {"ssh": True}, "clone_failed"),
            (failed("fatal: unable to access 'https://nohost.invalid/o/r/': Could not resolve host: nohost.invalid"), {}, "host_unreachable"),
            (failed("ssh: Could not resolve hostname nohost.invalid: Name or service not known"), {"ssh": True}, "host_unreachable"),
            (failed("fatal: unable to access 'https://h/o/r/': Failed to connect to h port 443 after 3 ms: Connection refused"), {}, "host_unreachable"),
            (failed("Receiving objects", timed_out=True), {}, "timed_out"),
            (failed("fatal: unable to access 'https://h/o/r/': SSL certificate problem: self-signed certificate"), {}, "clone_failed"),
            (failed(), {}, "clone_failed"),
        ]
        for result, options, expected in cases:
            with self.subTest(result.stderr_tail):
                self.assertEqual(self.code(result, **options), expected)
                self.assertIn(expected, git_clone.FAILURES)

    def test_detail_is_last_fatal_line_scrubbed(self):
        result = failed("Cloning into '/srv/.space-clone-abc/project'...",
                        "fatal: unable to access 'https://user:pw@h/o/r/': AUTHORIZATION: basic eDp5 ghp_abcdef and secret-token-value",
                        "some trailing note")
        detail = failure_detail(result, secrets=["secret-token-value"],
                                replace=[("/srv/.space-clone-abc/project", "new")])
        self.assertTrue(detail.startswith("fatal: unable to access"))
        for leaked in ("pw@", "eDp5", "ghp_abcdef", "secret-token-value"):
            self.assertNotIn(leaked, detail)
        self.assertIsNone(failure_detail(failed()))
        ssh = failed("Cloning into 'x'...", "Host key verification failed.", "fatal: Could not read from remote repository.",
                     "Please make sure you have the correct access rights", "and the repository exists.")
        self.assertEqual(failure_detail(ssh), "Host key verification failed.")
        self.assertLessEqual(len(failure_detail(failed("fatal: " + "x" * 1000))), 300)


@unittest.skipUnless(sys.platform.startswith("linux") or sys.platform == "darwin", "POSIX process groups")
class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    async def test_real_clone_reports_progress(self):
        source = self.base / "source"
        source.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", str(source)], check=True, env=env)
        for i in range(50):
            (source / f"file{i}.txt").write_bytes(os.urandom(2048))
        subprocess.run(["git", "-C", str(source), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "init"], check=True, env=env)
        seen = []
        result = await run_git_clone(
            ["git", "clone", "--progress", "--", f"file://{source}", str(self.base / "dest")],
            cwd=self.base, env=env, on_progress=lambda p: seen.append(p.as_dict()))
        self.assertTrue(result.ok, result.stderr_tail)
        self.assertTrue((self.base / "dest" / "file0.txt").exists())
        phases = {s["phase"] for s in seen}
        self.assertIn("receiving", phases)
        self.assertGreater(max(s["received_bytes"] for s in seen), 50 * 1024)

    async def test_failed_command_keeps_stderr_tail(self):
        result = await run_git_clone(
            ["sh", "-c", "printf 'Receiving objects:  10%% (1/10)\\r' >&2; echo 'fatal: nope' >&2; exit 128"],
            cwd=self.base, env=dict(os.environ))
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 128)
        self.assertEqual(result.stderr_tail, ["fatal: nope"])

    async def test_idle_timeout_stops_a_stalled_clone_but_not_a_moving_one(self):
        started = time.monotonic()
        result = await run_git_clone(["sh", "-c", "echo start >&2; exec sleep 30"], cwd=self.base,
                                     env=dict(os.environ), idle_timeout=0.5)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 10)
        # Output every 0.2 s for ~1.2 s outlives a 0.5 s idle bound.
        result = await run_git_clone(
            ["sh", "-c", "for i in 1 2 3 4 5 6; do echo tick >&2; sleep 0.2; done"],
            cwd=self.base, env=dict(os.environ), idle_timeout=0.5)
        self.assertTrue(result.ok, result)

    async def test_cancel_kills_the_process_group(self):
        marker = self.base / "pid"
        task = asyncio.create_task(run_git_clone(
            ["sh", "-c", f"sleep 30 & echo $! > {marker}; wait"], cwd=self.base, env=dict(os.environ)))
        for _ in range(100):
            if marker.exists() and marker.read_text().strip():
                break
            await asyncio.sleep(0.05)
        child = int(marker.read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    async def test_missing_binary(self):
        result = await run_git_clone(["definitely-not-git-xyz"], cwd=self.base, env=dict(os.environ))
        self.assertTrue(result.binary_missing)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
