"""The per-connection files under ``~/.quirq/connections/<toolkit>/``.

Hermetic: QUIRQ_STATE_ROOT (and XO_PROJECTS_ROOT) point into a temp dir, so
the files and the flock sentinels (``quirq_state_dir()/watcher/locks``) both
land there. Rotation is exercised by patching ``store._ROTATE_BYTES``, never
by writing 2 MB."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.connections import store
from services.cowork_agent.connections.store import ConnectionsError

BAD_IDS = ["..", ".", "", "Gmail", "a/b", "a-b", "x" * 41, "g mail", None, 7, "../gmail"]


def ev(i: int, ts: str, kind: str = "unread", toolkit: str = "gmail") -> dict:
    return {"ts": ts, "type": kind, "key": f"k{i}", "title": f"title {i}", "body": "", "url": None,
            "toolkit": toolkit}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.root / ".quirq"),
                                            "XO_PROJECTS_ROOT": str(self.root / "projects")})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def conns(self) -> Path:
        return self.root / ".quirq" / "connections"

    def write_file(self, toolkit: str, name: str, text: str) -> Path:
        path = self.conns() / toolkit / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def read_file(self, toolkit: str, name: str) -> dict:
        return json.loads((self.conns() / toolkit / name).read_text(encoding="utf-8"))


class PathsAndValidationTests(_Base):
    def test_paths_follow_quirq_state_root(self) -> None:
        self.assertEqual(store.connections_dir(), self.conns())
        self.assertEqual(store.connection_dir("gmail"), self.conns() / "gmail")
        self.assertEqual(store.connection_dir("some_other_id"), self.conns() / "some_other_id")

    def test_bad_ids_are_404_before_any_filesystem_touch(self) -> None:
        for bad in BAD_IDS:
            for fn in (store.connection_dir, store.read_config, store.read_state, store.read_events,
                       store.remove, lambda t: store.write_config(t, enabled=True),
                       lambda t: store.update_state(t, last_error="x"),
                       lambda t: store.append_events(t, [ev(1, "2026-09-11T10:00:00Z")])):
                with self.subTest(toolkit=bad):
                    with self.assertRaises(ConnectionsError) as ctx:
                        fn(bad)
                    self.assertEqual((ctx.exception.code, ctx.exception.status), ("unknown_toolkit", 404))
        self.assertFalse(self.conns().exists())

    def test_writes_require_a_catalog_toolkit_but_reads_do_not(self) -> None:
        with self.assertRaises(ConnectionsError) as ctx:
            store.write_config("zzz_unknown", enabled=True)
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("unknown_toolkit", 404))
        with self.assertRaises(ConnectionsError):
            store.append_events("zzz_unknown", [ev(1, "2026-09-11T10:00:00Z")])
        self.assertIsNone(store.read_config("zzz_unknown"))
        self.assertEqual(store.read_state("zzz_unknown")["events_total"], 0)
        self.assertEqual(store.read_events("zzz_unknown"), [])
        self.assertFalse(self.conns().exists())

    def test_error_carries_code_message_and_status(self) -> None:
        exc = ConnectionsError("invalid_interval", "too small")
        self.assertEqual((exc.code, exc.message, exc.status, str(exc)), ("invalid_interval", "too small", 400, "too small"))


class ConfigTests(_Base):
    def test_first_write_creates_the_folder_with_defaults(self) -> None:
        self.assertIsNone(store.read_config("gmail"))
        doc = store.write_config("gmail")
        self.assertEqual({k: doc[k] for k in ("schema", "toolkit", "enabled", "interval_s", "collectors")},
                         {"schema": 1, "toolkit": "gmail", "enabled": True, "interval_s": 900, "collectors": ["unread"]})
        self.assertRegex(doc["updated_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        on_disk = self.read_file("gmail", "config.json")
        self.assertEqual(on_disk["collectors"], ["unread"])
        self.assertEqual(store.read_config("gmail"), doc)
        self.assertTrue((self.root / ".quirq" / "watcher" / "locks").is_dir(), "flock sentinel stayed in the temp root")

    def test_defaults_per_toolkit(self) -> None:
        self.assertEqual(store.write_config("notion")["collectors"], ["recent_pages"])
        self.assertEqual(store.write_config("googlecalendar")["collectors"], ["upcoming"])
        self.assertEqual(store.write_config("figma")["collectors"], [])

    def test_write_validation(self) -> None:
        cases = [
            ({"interval_s": 59}, "invalid_interval"), ({"interval_s": 86401}, "invalid_interval"),
            ({"interval_s": "900"}, "invalid_interval"), ({"interval_s": True}, "invalid_interval"),
            ({"interval_s": 900.0}, "invalid_interval"),
            ({"collectors": "unread"}, "invalid_collector"), ({"collectors": ["nope"]}, "invalid_collector"),
            ({"collectors": [1]}, "invalid_collector"), ({"collectors": None}, "invalid_collector"),
            ({"enabled": "true"}, "invalid_value"), ({"enabled": 1}, "invalid_value"),
            ({"bogus": 1}, "invalid_value"),
        ]
        for fields, code in cases:
            with self.subTest(fields=fields):
                with self.assertRaises(ConnectionsError) as ctx:
                    store.write_config("gmail", **fields)
                self.assertEqual((ctx.exception.code, ctx.exception.status), (code, 400))
        with self.assertRaises(ConnectionsError) as ctx:
            store.write_config("gmail", interval_s=5)
        self.assertIn("5", ctx.exception.message)
        with self.assertRaises(ConnectionsError) as ctx:
            store.write_config("gmail", collectors=["unread", "bogus_id"])
        self.assertIn("bogus_id", ctx.exception.message)
        self.assertFalse((self.conns() / "gmail").exists(), "a rejected write creates nothing")

    def test_valid_values_and_dedupe_on_write(self) -> None:
        doc = store.write_config("gmail", enabled=False, interval_s=60, collectors=["inbox", "unread", "inbox"])
        self.assertEqual((doc["enabled"], doc["interval_s"], doc["collectors"]), (False, 60, ["inbox", "unread"]))
        doc = store.write_config("gmail", interval_s=86400, collectors=[])
        self.assertEqual((doc["enabled"], doc["interval_s"], doc["collectors"]), (False, 86400, []))

    def test_unknown_keys_survive_a_rewrite(self) -> None:
        self.write_file("gmail", "config.json", json.dumps({"interval_s": 300, "note": "keep me", "schema": 1}))
        store.write_config("gmail", enabled=False)
        on_disk = self.read_file("gmail", "config.json")
        self.assertEqual(on_disk["note"], "keep me")
        self.assertEqual((on_disk["enabled"], on_disk["interval_s"], on_disk["collectors"], on_disk["toolkit"]),
                         (False, 300, ["unread"], "gmail"))
        self.assertIn("updated_at", on_disk)

    def test_lenient_read_of_hand_edits(self) -> None:
        raw = {"enabled": "false", "interval_s": "300", "collectors": ["unread", "unread", "nope", 3],
               "toolkit": "someone_else", "schema": 2}
        self.write_file("gmail", "config.json", json.dumps(raw))
        with self.assertLogs("services.cowork_agent.connections.store", level="WARNING") as logs:
            doc = store.read_config("gmail")
        self.assertEqual((doc["enabled"], doc["interval_s"], doc["collectors"], doc["toolkit"]),
                         (False, 300, ["unread"], "gmail"))
        self.assertFalse(any("enabled" in line for line in logs.output), "a readable spelling needs no WARN")
        self.assertTrue(any("nope" in line for line in logs.output))

    def test_enabled_hand_edits_read_the_usual_spellings_and_fail_closed(self) -> None:
        for raw, expected in [("true", True), ("TRUE", True), ("yes", True), ("on", True), (1, True), ("1", True),
                              ("false", False), ("no", False), ("off", False), (0, False), ("0", False)]:
            with self.subTest(raw=raw):
                self.write_file("gmail", "config.json", json.dumps({"enabled": raw}))
                self.assertEqual(store.read_config("gmail")["enabled"], expected)
        for raw in ("maybe", 2, None, [], {}, 1.0):
            with self.subTest(raw=raw), \
                 self.assertLogs("services.cowork_agent.connections.store", level="WARNING") as logs:
                self.write_file("gmail", "config.json", json.dumps({"enabled": raw}))
                self.assertFalse(store.read_config("gmail")["enabled"], "an unreadable intent never polls")
                self.assertTrue(any("enabled" in line for line in logs.output))

    def test_a_rewrite_repairs_what_the_lenient_read_replaced(self) -> None:
        self.write_file("gmail", "config.json",
                        json.dumps({"interval_s": 5, "collectors": "unread", "note": "keep me"}))
        with self.assertLogs("services.cowork_agent.connections.store", level="WARNING"):
            store.write_config("gmail", enabled=False)
        on_disk = self.read_file("gmail", "config.json")
        self.assertEqual((on_disk["enabled"], on_disk["interval_s"], on_disk["collectors"], on_disk["note"]),
                         (False, 900, ["unread"], "keep me"))
        with self.assertNoLogs("services.cowork_agent.connections.store", level="WARNING"):
            self.assertEqual(store.read_config("gmail")["interval_s"], 900)

    def test_lenient_read_falls_back_for_bad_interval_and_collectors(self) -> None:
        for raw, interval, chosen in [
            ({"interval_s": 5}, 900, ["unread"]),
            ({"interval_s": True}, 900, ["unread"]),
            ({"interval_s": 900.0}, 900, ["unread"]),
            ({"collectors": "unread"}, 900, ["unread"]),
            ({"collectors": None}, 900, ["unread"]),
            ({"collectors": []}, 900, []),
            ({"collectors": ["inbox"]}, 900, ["inbox"]),
        ]:
            with self.subTest(raw=raw):
                self.write_file("gmail", "config.json", json.dumps(raw))
                doc = store.read_config("gmail")
                self.assertEqual((doc["interval_s"], doc["collectors"]), (interval, chosen))

    def test_unreadable_config_reads_as_none_and_is_never_overwritten(self) -> None:
        path = self.write_file("gmail", "config.json", "not json")
        self.assertIsNone(store.read_config("gmail"))
        with self.assertRaises(ConnectionsError) as ctx:
            store.write_config("gmail", enabled=True)
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("config_unreadable", 500))
        self.assertEqual(path.read_text(encoding="utf-8"), "not json")
        self.write_file("gmail", "config.json", "[1, 2]")
        self.assertIsNone(store.read_config("gmail"))

    def test_list_configured_only_sees_real_folders_with_a_config(self) -> None:
        self.assertEqual(store.list_configured(), [])
        store.write_config("notion")
        store.write_config("gmail")
        (self.conns() / "googledocs").mkdir()                                   # no config.json
        self.write_file("figma", "state.json", "{}")                            # state only
        (self.conns() / "stray.txt").write_text("x", encoding="utf-8")          # a file
        self.write_file("Bad-Name", "config.json", "{}")                        # outside the regex
        (self.conns() / "googlecalendar").symlink_to(self.conns() / "gmail")   # a symlink
        self.assertEqual(store.list_configured(), ["gmail", "notion"])


class StateTests(_Base):
    def test_defaults_when_missing(self) -> None:
        self.assertEqual(store.read_state("gmail"), {"schema": 1, "last_poll_at": None, "last_ok_at": None,
                                                     "last_error": None, "cursors": {}, "events_total": 0})

    def test_update_merges_and_normalises(self) -> None:
        store.write_config("gmail")
        doc = store.update_state("gmail", last_poll_at="2026-09-11T10:00:00Z", last_error="e" * 400)
        self.assertEqual((doc["last_poll_at"], len(doc["last_error"]), doc["last_ok_at"]), ("2026-09-11T10:00:00Z", 300, None))
        doc = store.update_state("gmail", cursors={"unread": {"seen": ["a", 1, "b"]}, 5: {"seen": ["x"]}, "bad": "no"},
                                 events_total=7)
        self.assertEqual(doc["cursors"], {"unread": {"seen": ["a", "b"]}, "bad": {"seen": []}})
        self.assertEqual(doc["events_total"], 7)
        self.assertEqual(self.read_file("gmail", "state.json")["last_poll_at"], "2026-09-11T10:00:00Z")
        doc = store.update_state("gmail", last_error=None, events_total=-5, last_ok_at="")
        self.assertEqual((doc["last_error"], doc["events_total"], doc["last_ok_at"]), (None, 0, None))
        self.assertEqual(store.read_state("gmail"), doc)

    def test_unknown_keys_in_state_survive(self) -> None:
        store.write_config("gmail")
        self.write_file("gmail", "state.json", json.dumps({"events_total": 2, "note": "keep", "cursors": "junk"}))
        doc = store.update_state("gmail", last_error="x")
        self.assertEqual((doc["note"], doc["events_total"], doc["cursors"]), ("keep", 2, {}))
        self.assertEqual(self.read_file("gmail", "state.json")["note"], "keep")

    def test_update_rejects_unknown_fields(self) -> None:
        store.write_config("gmail")
        with self.assertRaises(ConnectionsError) as ctx:
            store.update_state("gmail", bogus=1)
        self.assertEqual(ctx.exception.code, "invalid_value")

    def test_update_refuses_to_resurrect_a_removed_folder(self) -> None:
        doc = store.update_state("gmail", last_error="x")
        self.assertEqual(doc["last_error"], None)
        self.assertFalse((self.conns() / "gmail").exists())
        store.write_config("gmail")
        store.remove("gmail")
        store.update_state("gmail", last_poll_at="2026-09-11T10:00:00Z")
        self.assertFalse((self.conns() / "gmail").exists())

    def test_remember_seen_caps_at_500_and_keeps_order(self) -> None:
        doc = store.read_state("gmail")
        keys = [f"k{i}" for i in range(501)]
        seen = store.remember_seen(doc, "unread", keys)
        self.assertEqual(len(seen), 500)
        self.assertEqual((seen[0], seen[-1]), ("k1", "k500"))
        self.assertNotIn("k0", seen)
        self.assertIs(seen, doc["cursors"]["unread"]["seen"])
        seen = store.remember_seen(doc, "unread", ["k1", "k1", 7, "new"])
        self.assertEqual(seen[-2:], ["k1", "new"])
        self.assertEqual(seen.count("k1"), 1)
        self.assertEqual(len(seen), 500)
        self.assertEqual(store.remember_seen(doc, "inbox", None), [])
        self.assertEqual(store.remember_seen({"cursors": {"unread": "junk"}}, "unread", ["a"]), ["a"])


class EventsTests(_Base):
    T = ["2026-09-11T10:00:00Z", "2026-09-11T11:00:00Z", "2026-09-11T12:00:00Z"]

    def test_append_newest_first_batch_reads_back_newest_first(self) -> None:
        store.write_config("gmail")
        batch = [ev(3, self.T[2]), ev(2, self.T[1]), ev(1, self.T[0])]     # newest-first, as collectors return
        self.assertEqual(store.append_events("gmail", batch), 3)
        lines = [json.loads(l) for l in (self.conns() / "gmail" / "events.jsonl").read_text().splitlines()]
        self.assertEqual([l["key"] for l in lines], ["k1", "k2", "k3"], "chronological on disk")
        self.assertEqual([e["key"] for e in store.read_events("gmail")], ["k3", "k2", "k1"])
        self.assertEqual(store.read_events("gmail")[0], ev(3, self.T[2]))

    def test_append_skips_junk_and_empty_batches(self) -> None:
        store.write_config("gmail")
        self.assertEqual(store.append_events("gmail", []), 0)
        self.assertEqual(store.append_events("gmail", None), 0)
        self.assertEqual(store.append_events("gmail", ["x", None, ev(1, self.T[0])]), 1)
        self.assertEqual(len(store.read_events("gmail")), 1)

    def test_append_refuses_without_a_config(self) -> None:
        self.assertEqual(store.append_events("gmail", [ev(1, self.T[0])]), 0)
        self.assertFalse((self.conns() / "gmail").exists())

    def test_read_limit_and_types(self) -> None:
        store.write_config("gmail")
        store.append_events("gmail", [ev(1, self.T[0], "unread"), ev(2, self.T[1], "inbox"), ev(3, self.T[2], "unread")])
        self.assertEqual([e["key"] for e in store.read_events("gmail", limit=2)], ["k3", "k2"])
        self.assertEqual([e["key"] for e in store.read_events("gmail", types=["inbox"])], ["k2"])
        self.assertEqual([e["key"] for e in store.read_events("gmail", types=("unread", 5))], ["k3", "k1"])
        self.assertEqual(store.read_events("gmail", types=[]), [])
        self.assertEqual(len(store.read_events("gmail", limit=0)), 1, "limit is clamped to at least 1")

    def test_read_resorts_interleaved_writers(self) -> None:
        store.write_config("gmail")
        path = self.conns() / "gmail" / "events.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in [ev(2, self.T[1]), ev(3, self.T[2]), ev(1, self.T[0])]) + "\n",
                        encoding="utf-8")
        self.assertEqual([e["key"] for e in store.read_events("gmail")], ["k3", "k2", "k1"])
        path.write_text('{"ts": "junk", "key": "z"}\nnot json\n' + json.dumps(ev(1, self.T[0])) + "\n", encoding="utf-8")
        self.assertEqual([e["key"] for e in store.read_events("gmail")], ["k1", "z"])

    def test_rotation_at_the_threshold_keeps_the_newest_three(self) -> None:
        store.write_config("gmail")
        folder = self.conns() / "gmail"
        for stamp in ("20200101T000000Z", "20200102T000000Z", "20200103T000000Z"):
            (folder / f"events.{stamp}.jsonl").write_text("old\n", encoding="utf-8")
        (folder / "events.backup.jsonl").write_text("mine\n", encoding="utf-8")
        store.append_events("gmail", [ev(1, self.T[0])])
        self.assertEqual(len(list(folder.glob("events.*.jsonl"))), 4, "below the threshold nothing rotates")
        with patch.object(store, "_ROTATE_BYTES", 1):
            self.assertEqual(store.append_events("gmail", [ev(2, self.T[1])]), 1)
        self.assertEqual([e["key"] for e in store.read_events("gmail")], ["k2"], "only the live file is read")
        stamped = sorted(p.name for p in folder.iterdir() if store._ROTATION_RE.fullmatch(p.name))
        self.assertEqual(len(stamped), 3)
        self.assertNotIn("events.20200101T000000Z.jsonl", stamped)
        self.assertIn("events.20200102T000000Z.jsonl", stamped)
        self.assertTrue((folder / "events.backup.jsonl").is_file(), "hand-named files are never pruned")
        rotated = [p for p in folder.iterdir() if p.name not in {"events.backup.jsonl", "config.json"}
                   and p.name.startswith("events.") and "2020" not in p.name and p.name != "events.jsonl"]
        self.assertEqual(len(rotated), 1)
        self.assertIn('"k1"', rotated[0].read_text(encoding="utf-8"))


class RemoveTests(_Base):
    def test_remove_deletes_only_that_folder(self) -> None:
        store.write_config("gmail")
        store.write_config("notion")
        store.append_events("gmail", [ev(1, "2026-09-11T10:00:00Z")])
        self.assertTrue(store.remove("gmail"))
        self.assertFalse((self.conns() / "gmail").exists())
        self.assertTrue((self.conns() / "notion" / "config.json").is_file())
        self.assertFalse(store.remove("gmail"))
        self.assertFalse(store.remove("figma"))
        self.assertEqual(store.list_configured(), ["notion"])

    def test_remove_refuses_symlinks_and_files(self) -> None:
        outside = self.root / "elsewhere"
        outside.mkdir()
        (outside / "keep.txt").write_text("x", encoding="utf-8")
        self.conns().mkdir(parents=True)
        (self.conns() / "gmail").symlink_to(outside)
        self.assertFalse(store.remove("gmail"))
        self.assertTrue((outside / "keep.txt").is_file())
        self.assertTrue((self.conns() / "gmail").is_symlink())
        (self.conns() / "notion").write_text("a file, not a folder", encoding="utf-8")
        self.assertFalse(store.remove("notion"))
        self.assertTrue((self.conns() / "notion").is_file())

    def test_remove_never_touches_outside_connections_dir(self) -> None:
        for bad in ("..", "../..", "/", ".quirq"):
            with self.assertRaises(ConnectionsError):
                store.remove(bad)
        self.assertTrue(self.root.is_dir())
        self.assertTrue((self.root / ".quirq").is_dir() or not self.conns().exists())


if __name__ == "__main__":
    unittest.main()
