"""Synchronous command timeouts clean up helpers, not just their parent."""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from utils.commands import run_sync


@unittest.skipUnless(os.name == 'posix', 'process groups require POSIX')
class SyncCommandLifecycleTests(unittest.TestCase):
    def test_timeout_kills_pipe_holding_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            child_pid_file = Path(tmp) / 'child.pid'
            marker = Path(tmp) / 'escaped-timeout'
            child = (f'import os,time;from pathlib import Path;'
                     f'Path({str(child_pid_file)!r}).write_text(str(os.getpid()));'
                     f'time.sleep(1.2);Path({str(marker)!r}).write_text("still running");'
                     'time.sleep(2)')
            parent = ('import subprocess,sys,time;'
                      'print("before timeout",flush=True);'
                      f'subprocess.Popen([sys.executable,"-c",{child!r}]);'
                      'time.sleep(10)')
            try:
                started = time.monotonic()
                result = run_sync([sys.executable, '-c', parent], timeout=0.5)
                self.assertLess(time.monotonic() - started, 2, result.output)
                self.assertTrue(result.timed_out)
                self.assertIn('before timeout', result.output)
                self.assertIn('timed out after 0.5s', result.output)
                self.assertTrue(child_pid_file.exists(), 'the descendant must have actually started')
                time.sleep(1.0)
                self.assertFalse(marker.exists(), 'a descendant survived the command timeout')
            finally:
                # Exact PID of this test's child only; never a name/port sweep.
                if child_pid_file.exists():
                    try:
                        os.kill(int(child_pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass


class _Interrupted(BaseException):
    """What a script's own signal handler raises (the Launchpad deploy's Stop)."""


@unittest.skipUnless(os.name == 'posix', 'process groups require POSIX')
class SyncCommandInterruptTests(unittest.TestCase):
    """An interrupted run_sync stops its command as the async runner does:
    SIGTERM first, so a program that cleans up on exit can (git removes
    .git/index.lock), SIGKILL only after CANCEL_GRACE_S."""

    def interrupt_after(self, seconds: float) -> None:
        def handler(*_):
            raise _Interrupted()
        previous = signal.signal(signal.SIGALRM, handler)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)
        signal.setitimer(signal.ITIMER_REAL, seconds)

    def test_an_interrupted_command_gets_to_clean_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock, ready = Path(tmp) / 'index.lock', Path(tmp) / 'ready'
            script = (f'trap \'rm -f "{lock}"; exit 143\' TERM; : > "{lock}"; : > "{ready}"; '
                      'sleep 10 & wait')
            self.interrupt_after(0.5)
            started = time.monotonic()
            with self.assertRaises(_Interrupted):
                run_sync(['sh', '-c', script], timeout=20)
            self.assertTrue(ready.exists(), 'the command must have started')
            self.assertFalse(lock.exists(), 'the command was killed before it could remove its lock')
            self.assertLess(time.monotonic() - started, 5)

    def test_a_command_that_ignores_sigterm_is_still_killed(self):
        from utils import commands
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / 'pid'
            script = f'trap "" TERM; echo $$ > "{pid_file}"; sleep 30'
            original = commands.CANCEL_GRACE_S
            commands.CANCEL_GRACE_S = 0.3
            self.addCleanup(setattr, commands, 'CANCEL_GRACE_S', original)
            self.interrupt_after(0.5)
            started = time.monotonic()
            with self.assertRaises(_Interrupted):
                run_sync(['sh', '-c', script], timeout=60)
            self.assertLess(time.monotonic() - started, 5)
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
