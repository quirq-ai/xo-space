import io
import re
import sys
import threading
import unittest
from unittest import mock

from utils.stamped_output import StampedLines, stamp_lines

STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z "


class StampedLinesTests(unittest.TestCase):
    def test_each_line_starts_with_its_utc_time_once(self) -> None:
        out = io.StringIO()
        stamped = StampedLines(out)
        print("one", file=stamped)                  # print writes the text, then the newline
        stamped.write("two\nthree\n")
        stamped.write("par")
        stamped.write("tial\n")
        lines = out.getvalue().split("\n")
        self.assertEqual(lines[-1], "")
        self.assertEqual([re.sub(f"^{STAMP}", "", line) for line in lines[:-1]], ["one", "two", "three", "partial"])
        for line in lines[:-1]:
            self.assertRegex(line, f"^{STAMP}[a-z]+$")

    def test_the_stream_s_own_attributes_pass_through(self) -> None:
        out = io.StringIO()
        stamped = StampedLines(out)
        self.assertFalse(stamped.isatty())
        self.assertEqual(stamped.write(""), 0)
        self.assertEqual(stamped.write("x\n"), 2)
        stamped.flush()

    def test_lines_from_threads_keep_whole_stamps(self) -> None:
        out = io.StringIO()
        stamped = StampedLines(out)
        threads = [threading.Thread(target=lambda: [stamped.write(f"line {n}\n") for n in range(200)])
                   for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 800)
        for line in lines:
            self.assertRegex(line, f"^{STAMP}line \\d+$")

    def test_stamp_lines_wraps_stdout_and_stderr_once(self) -> None:
        with mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", io.StringIO()):
            stamp_lines()
            stamp_lines()
            self.assertIsInstance(sys.stdout, StampedLines)
            self.assertNotIsInstance(sys.stdout._stream, StampedLines)
            self.assertIsInstance(sys.stderr, StampedLines)


if __name__ == "__main__":
    unittest.main()
