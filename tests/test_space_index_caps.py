from __future__ import annotations

import unittest

from services.cowork_agent.visualizer.space_index import _trim_total


def _leaf(pid: str, rel: str, date: str | None) -> dict:
    return {
        "id": f"{pid}:{rel}",
        "group": f"g_{pid}_root",
        "date": date,
        "path": f"{pid}/{rel}",
    }


def _project(pid: str, count: int, dated: bool) -> list[dict]:
    # Dated leaves get distinct, ordered days so newest-first is observable.
    return [
        _leaf(pid, f"f{i}.txt", f"2026-08-{(i % 27) + 1:02d}" if dated else None)
        for i in range(count)
    ]


def _count_by_project(leaves: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for leaf in leaves:
        pid = leaf["path"].split("/", 1)[0]
        out[pid] = out.get(pid, 0) + 1
    return out


class TrimTotalTests(unittest.TestCase):
    def test_under_cap_is_untouched(self) -> None:
        leaves = _project("small", 10, dated=True)
        self.assertEqual(_trim_total(leaves, cap=100, floor=5), leaves)

    def test_non_git_project_keeps_its_floor(self) -> None:
        # The #36 shape: two big git repos hold every dated leaf, one non-git
        # project has only undated leaves. Old behaviour dropped all of the
        # non-git project's files; now it keeps them, and the repos absorb
        # the cuts.
        leaves = (
            _project("big-a", 400, dated=True)
            + _project("big-b", 300, dated=True)
            + _project("nogit", 5, dated=False)
        )
        kept = _count_by_project(_trim_total(leaves, cap=500, floor=40))

        self.assertEqual(kept["nogit"], 5)
        self.assertEqual(sum(kept.values()), 500)
        self.assertGreaterEqual(kept["big-a"], 40)
        self.assertGreaterEqual(kept["big-b"], 40)

    def test_floor_is_bounded_by_own_count(self) -> None:
        # A 3-file project is guaranteed 3, not 40 — the floor never invents
        # leaves, so the spare budget goes back to the bigger projects.
        leaves = _project("big", 600, dated=True) + _project("tiny", 3, dated=False)
        kept = _count_by_project(_trim_total(leaves, cap=100, floor=40))

        self.assertEqual(kept["tiny"], 3)
        self.assertEqual(kept["big"], 97)

    def test_overflow_budget_keeps_newest_leaves(self) -> None:
        old = [_leaf("a", f"old{i}.txt", "2020-01-01") for i in range(10)]
        new = [_leaf("a", f"new{i}.txt", "2026-08-27") for i in range(10)]
        kept = _trim_total(old + new, cap=12, floor=2)

        self.assertEqual(len(kept), 12)
        self.assertEqual(sum(1 for leaf in kept if leaf["date"] == "2026-08-27"), 10)

    def test_many_projects_shrink_the_floor_but_never_to_zero(self) -> None:
        # More projects than cap // floor: the guarantee shrinks rather than
        # zeroing anyone — every project stays on the map.
        leaves = [
            leaf
            for i in range(50)
            for leaf in _project(f"p{i:02d}", 4, dated=(i % 2 == 0))
        ]
        kept = _count_by_project(_trim_total(leaves, cap=60, floor=40))

        self.assertEqual(len(kept), 50)
        self.assertTrue(all(count >= 1 for count in kept.values()))


if __name__ == "__main__":
    unittest.main()
