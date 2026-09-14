"""Inbox project activity retains the backend todo lifecycle vocabulary."""
from __future__ import annotations

import re
import runpy
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TodoStatusTests(unittest.TestCase):
    def test_inbox_display_order_covers_every_backend_status_once(self):
        vocabulary = runpy.run_path(str(
            ROOT / "services/cowork_agent/visualizer/todo_status.py"
        ))["TODO_STATUSES"]
        source = (ROOT / "space_ui/js/views/inbox-activity.js").read_text()
        match = re.search(r"const ST_ORDER\s*=\s*\{([^}]+)\}", source)
        self.assertIsNotNone(match, "Inbox owns project todo display order")
        order = {name: int(value) for name, value in re.findall(
            r"([a-z_]+)\s*:\s*(\d+)", match.group(1)
        )}
        self.assertEqual(set(order), set(vocabulary))
        self.assertEqual(len(set(order.values())), len(vocabulary))
        self.assertEqual(sorted(order, key=order.get), [
            "in_progress", "pending", "blocked", "completed", "cancelled",
        ])


if __name__ == "__main__":
    unittest.main()
