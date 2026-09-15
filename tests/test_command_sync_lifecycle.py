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
