"""The collectors catalog and the payload mapping into events.jsonl lines.

Pure functions over fixture payloads: nothing here touches the filesystem
or the network. Every tool slug the catalog names is checked against the
Composio categories table so a write tool can never sneak into a poll."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from services.cowork_agent.connections import collectors
from services.cowork_agent.connectors.composio import categories
from services.cowork_agent.connectors.composio.service import TOOLKITS

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-09-11T12:00:00Z"
SPEC_KEYS = {"id", "label", "tool", "args", "list_keys", "id_keys", "title_keys", "body_keys", "ts_keys"}


def gmail_spec(collector_id: str = "unread") -> dict:
    return collectors.collector("gmail", collector_id)


def message(i: int, **extra) -> dict:
    return {"messageId": f"m{i}", "subject": f"Subject {i}", "snippet": f"snippet {i}",
            "messageTimestamp": f"2026-09-11T1{i}:00:00Z", **extra}


class CatalogTests(unittest.TestCase):
    def test_every_toolkit_has_a_list_and_every_tool_is_read_only(self) -> None:
        for toolkit in TOOLKITS:
            specs = collectors.catalog(toolkit)
            self.assertIsInstance(specs, list)
            ids = [spec["id"] for spec in specs]
            self.assertEqual(len(ids), len(set(ids)), f"duplicate collector ids for {toolkit}")
            for spec in specs:
                with self.subTest(toolkit=toolkit, collector=spec["id"]):
                    self.assertTrue(SPEC_KEYS <= set(spec), f"missing keys: {SPEC_KEYS - set(spec)}")
                    self.assertEqual(categories.classify(toolkit, spec["tool"]), "read")
                    self.assertTrue(spec.get("url_keys") or spec.get("url_template"))
                    self.assertIsInstance(spec["args"], dict)

    def test_catalog_contents(self) -> None:
        self.assertEqual([s["id"] for s in collectors.catalog("gmail")], ["unread", "inbox"])
        self.assertEqual(collectors.default_ids("gmail"), ["unread"])
        self.assertEqual(gmail_spec("unread")["tool"], "GMAIL_FETCH_EMAILS")
        self.assertEqual(gmail_spec("unread")["args"], {"query": "is:unread", "max_results": 20})
        self.assertEqual(gmail_spec("inbox")["args"], {"query": "in:inbox newer_than:1d", "max_results": 20})
        self.assertEqual(gmail_spec("inbox")["label"], "New mail in Inbox (last day)")
        self.assertEqual([s["id"] for s in collectors.catalog("googlecalendar")], ["upcoming"])
        self.assertEqual(collectors.collector("googlecalendar", "upcoming")["tool"], "GOOGLECALENDAR_EVENTS_LIST")
        self.assertEqual(collectors.default_ids("googlecalendar"), ["upcoming"])
        notion = collectors.collector("notion", "recent_pages")
        self.assertEqual(notion["tool"], "NOTION_SEARCH_NOTION_PAGE")
        self.assertEqual(categories.classify("notion", "NOTION_SEARCH_NOTION_PAGE"), "read")
        self.assertEqual(notion["args"]["sort"], {"direction": "descending", "timestamp": "last_edited_time"})
        self.assertEqual(collectors.default_ids("notion"), ["recent_pages"])
        for toolkit in ("googlesheets", "googledocs", "googleslides", "googlemeet", "figma"):
            self.assertEqual(collectors.catalog(toolkit), [])
            self.assertEqual(collectors.default_ids(toolkit), [])
        self.assertEqual(collectors.catalog("not_a_toolkit"), [])
        self.assertIsNone(collectors.collector("gmail", "nope"))
        self.assertIsNone(collectors.collector("figma", "unread"))

    def test_catalog_hands_out_copies(self) -> None:
        spec = gmail_spec()
        spec["args"]["query"] = "changed"
        spec["id_keys"].append("x")
        self.assertEqual(gmail_spec()["args"]["query"], "is:unread")
        self.assertNotIn("x", gmail_spec()["id_keys"])


class RenderArgsTests(unittest.TestCase):
    def test_calendar_placeholders(self) -> None:
        spec = collectors.collector("googlecalendar", "upcoming")
        before = copy.deepcopy(spec)
        args = collectors.render_args(spec, NOW)
        self.assertEqual(args["timeMin"], NOW_ISO)
        self.assertEqual(args["timeMax"], "2026-09-18T12:00:00Z")
        self.assertIs(args["singleEvents"], True)
        self.assertEqual(args["maxResults"], 25)
        self.assertEqual(args["calendarId"], "primary")
        self.assertEqual(spec, before, "rendering never mutates the spec")

    def test_naive_now_is_utc_and_offsets_convert(self) -> None:
        spec = {"args": {"a": "{now_iso}"}}
        self.assertEqual(collectors.render_args(spec, NOW.replace(tzinfo=None))["a"], NOW_ISO)
        from datetime import timedelta
        plus_two = NOW.astimezone(timezone(timedelta(hours=2)))
        self.assertEqual(collectors.render_args(spec, plus_two)["a"], NOW_ISO)

    def test_only_string_leaves_change_and_braces_are_safe(self) -> None:
        spec = {"args": {"q": "label:{weird} {now_iso}", "n": 3, "flag": False, "none": None,
                         "nested": {"list": ["{now_plus_7d_iso}", 1, {"deep": "{now_iso}"}]}}}
        args = collectors.render_args(spec, NOW)
        self.assertEqual(args["q"], f"label:{{weird}} {NOW_ISO}")
        self.assertEqual((args["n"], args["flag"], args["none"]), (3, False, None))
        self.assertEqual(args["nested"]["list"], ["2026-09-18T12:00:00Z", 1, {"deep": NOW_ISO}])
        self.assertEqual(collectors.render_args({}, NOW), {})
        self.assertEqual(collectors.render_args({"args": "junk"}, NOW), {})


class LookupAndTimeTests(unittest.TestCase):
    def test_lookup_dotted_paths_with_indexes(self) -> None:
        obj = {"a": {"b": [{"c": 1}, {"c": 2}]}, "0": "zero", "s": "text", "n": None}
        self.assertEqual(collectors.lookup(obj, "a.b.0.c"), 1)
        self.assertEqual(collectors.lookup(obj, "a.b.1.c"), 2)
        self.assertIsNone(collectors.lookup(obj, "a.b.2.c"))
        self.assertIsNone(collectors.lookup(obj, "a.b.x"))
        self.assertIsNone(collectors.lookup(obj, "a.b.-1"))
        self.assertEqual(collectors.lookup(obj, "0"), "zero", "a dict keyed '0' is a dict lookup")
        self.assertIsNone(collectors.lookup(obj, "s.0"), "a string is not a container")
        self.assertIsNone(collectors.lookup(obj, "n.x"))
        self.assertIsNone(collectors.lookup(obj, "missing"))
        self.assertEqual(collectors.lookup([{"x": 1}], "0.x"), 1)
        self.assertEqual(collectors.lookup(obj, "a"), obj["a"])
        self.assertIsNone(collectors.lookup(None, "a"))
        self.assertIsNone(collectors.lookup(obj, "a.b.0.c.d"))

    def test_parse_any_ts_variants(self) -> None:
        epoch = datetime(2024, 8, 30, 6, 40, 0, tzinfo=timezone.utc)
        self.assertEqual(collectors.parse_any_ts("2026-09-11T10:00:00Z"), datetime(2026, 9, 11, 10, tzinfo=timezone.utc))
        self.assertEqual(collectors.parse_any_ts("2026-09-11T12:00:00+02:00"), datetime(2026, 9, 11, 10, tzinfo=timezone.utc))
        self.assertEqual(collectors.parse_any_ts("2026-09-11T10:00:00.250Z"),
                         datetime(2026, 9, 11, 10, 0, 0, 250000, tzinfo=timezone.utc))
        self.assertEqual(collectors.parse_any_ts(1725000000), epoch)
        self.assertEqual(collectors.parse_any_ts(1725000000000), epoch, "milliseconds by magnitude")
        self.assertEqual(collectors.parse_any_ts("1725000000"), epoch)
        self.assertEqual(collectors.parse_any_ts(" 1725000000000 "), epoch)
        self.assertEqual(collectors.parse_any_ts(1725000000.0), epoch)
        self.assertEqual(collectors.parse_any_ts("2026-09-12"), datetime(2026, 9, 12, tzinfo=timezone.utc))
        for junk in (True, False, None, "", "   ", "nope", "2026-13-40", [], {}, 1e30):
            with self.subTest(value=junk):
                self.assertIsNone(collectors.parse_any_ts(junk))

    def test_iso_serialises_to_z(self) -> None:
        self.assertEqual(collectors.iso(NOW), NOW_ISO)
        self.assertEqual(collectors.iso(NOW.replace(tzinfo=None)), NOW_ISO)


class ExtractGmailTests(unittest.TestCase):
    def test_direct_list_and_data_wrapped_payloads(self) -> None:
        spec = gmail_spec()
        direct = [message(1), message(3), message(2)]
        wrapped = {"successful": True, "error": None, "data": {"messages": copy.deepcopy(direct)}}
        top = {"messages": copy.deepcopy(direct)}
        for payload in (direct, wrapped, top):
            with self.subTest(payload=type(payload).__name__):
                items = collectors.extract_items(spec, payload, toolkit="gmail", now=NOW)
                self.assertEqual([it["key"] for it in items], ["m3", "m2", "m1"], "newest-first by ts")
                self.assertEqual(items[0], {
                    "ts": "2026-09-11T13:00:00Z", "type": "unread", "key": "m3", "title": "Subject 3",
                    "body": "snippet 3", "url": "https://mail.google.com/mail/u/0/#all/m3", "toolkit": "gmail",
                })
        self.assertEqual(collectors.extract_items(spec, {"text": "not json"}, toolkit="gmail", now=NOW), [])
        self.assertEqual(collectors.extract_items(spec, {"data": {"messages": "x"}}, toolkit="gmail", now=NOW), [])
        self.assertEqual(collectors.extract_items(spec, None, toolkit="gmail", now=NOW), [])
        self.assertEqual(collectors.extract_items(spec, "junk", toolkit="gmail", now=NOW), [])

    def test_type_is_the_collector_id(self) -> None:
        items = collectors.extract_items(gmail_spec("inbox"), [message(1)], toolkit="gmail", now=NOW)
        self.assertEqual(items[0]["type"], "inbox")

    def test_ids_are_coerced_or_skipped(self) -> None:
        payload = [
            {"subject": "no id at all"},
            "not a dict",
            None,
            {"id": 42, "subject": "int id"},
            {"id": 4.2, "threadId": "t1", "subject": "float id falls to the next key"},
            {"id": True, "subject": "bool id skipped"},
            {"id": "  ", "subject": "blank id skipped"},
            {"threadId": "t2", "subject": "thread id"},
        ]
        items = collectors.extract_items(gmail_spec(), payload, toolkit="gmail", now=NOW)
        self.assertEqual(sorted(it["key"] for it in items), ["42", "t1", "t2"])
        self.assertTrue(all(it["ts"] == NOW_ISO for it in items), "no ts key means now")

    def test_titles_bodies_and_urls_are_coerced(self) -> None:
        payload = [
            {"messageId": "a", "subject": ["rich", "text"], "snippet": "from snippet"},
            {"messageId": "b", "subject": 7, "snippet": {"nested": 1}},
            {"messageId": "c"},
            {"messageId": "d", "subject": "", "snippet": "empty subject falls through"},
            {"messageId": "e", "subject": "  many   \n\t spaces  ", "snippet": "s"},
        ]
        by_key = {it["key"]: it for it in collectors.extract_items(gmail_spec(), payload, toolkit="gmail", now=NOW)}
        self.assertEqual(by_key["a"]["title"], "from snippet", "a list title is skipped, the next key wins")
        self.assertEqual((by_key["b"]["title"], by_key["b"]["body"]), ("7", ""))
        self.assertEqual(by_key["c"]["title"], "Unread mail: c", "fallback is '<label>: <key>'")
        self.assertEqual(by_key["c"]["body"], "")
        self.assertEqual(by_key["d"]["title"], "empty subject falls through")
        self.assertEqual(by_key["e"]["title"], "many spaces")
        self.assertEqual(by_key["e"]["url"], "https://mail.google.com/mail/u/0/#all/e")

    def test_truncation(self) -> None:
        payload = [{"messageId": "a", "subject": "x" * 500, "snippet": "y" * 5000}]
        item = collectors.extract_items(gmail_spec(), payload, toolkit="gmail", now=NOW)[0]
        self.assertEqual((len(item["title"]), len(item["body"])), (300, 4000))

    def test_timestamp_keys_and_forms(self) -> None:
        payload = [
            {"messageId": "iso", "messageTimestamp": "2026-09-11T10:00:00+02:00"},
            {"messageId": "ms", "internalDate": "1725000000000"},
            {"messageId": "s", "internalDate": 1725000000},
            {"messageId": "date", "date": "2026-09-12"},
            {"messageId": "bad", "messageTimestamp": "junk", "internalDate": "also junk"},
            {"messageId": "first_bad", "messageTimestamp": "junk", "internalDate": "1725000000"},
        ]
        by_key = {it["key"]: it["ts"] for it in collectors.extract_items(gmail_spec(), payload, toolkit="gmail", now=NOW)}
        self.assertEqual(by_key, {
            "iso": "2026-09-11T08:00:00Z", "ms": "2024-08-30T06:40:00Z", "s": "2024-08-30T06:40:00Z",
            "date": "2026-09-12T00:00:00Z", "bad": NOW_ISO, "first_bad": "2024-08-30T06:40:00Z",
        })


class ExtractCalendarTests(unittest.TestCase):
    def spec(self) -> dict:
        return collectors.collector("googlecalendar", "upcoming")

    def test_events_under_data_items_with_html_link(self) -> None:
        payload = {"successful": True, "data": {"items": [
            {"id": "ev1", "summary": "Standup", "description": "daily", "location": "Room 1",
             "start": {"dateTime": "2026-09-12T09:00:00+02:00"}, "htmlLink": "https://calendar.google.com/event?eid=1",
             "updated": "2026-09-01T00:00:00Z"},
            {"id": "ev2", "summary": "Offsite", "location": "Lisbon", "start": {"date": "2026-09-13"},
             "htmlLink": "http://calendar.google.com/event?eid=2"},
            {"id": "ev3", "start": {}, "updated": "2026-09-10T00:00:00Z", "htmlLink": "ftp://nope"},
        ]}}
        items = collectors.extract_items(self.spec(), payload, toolkit="googlecalendar", now=NOW)
        self.assertEqual([it["key"] for it in items], ["ev2", "ev1", "ev3"])
        ev1, ev2, ev3 = items[1], items[0], items[2]
        self.assertEqual((ev1["title"], ev1["body"], ev1["ts"], ev1["url"], ev1["type"], ev1["toolkit"]),
                         ("Standup", "daily", "2026-09-12T07:00:00Z", "https://calendar.google.com/event?eid=1",
                          "upcoming", "googlecalendar"))
        self.assertEqual((ev2["body"], ev2["ts"], ev2["url"]), ("Lisbon", "2026-09-13T00:00:00Z", "http://calendar.google.com/event?eid=2"))
        self.assertEqual((ev3["title"], ev3["ts"], ev3["url"]), ("Upcoming events (next 7 days): ev3", "2026-09-10T00:00:00Z", None))

    def test_top_level_events_list_key(self) -> None:
        items = collectors.extract_items(self.spec(), {"events": [{"id": "x", "summary": "S"}]},
                                         toolkit="googlecalendar", now=NOW)
        self.assertEqual([it["key"] for it in items], ["x"])

    def test_dedupe_key_is_the_id_only_so_a_reschedule_is_not_surfaced_again(self) -> None:
        # Pinned on purpose: a rescheduled event keeps its id and therefore its key; the
        # poller's seen set drops it. A recurring instance carries its own id and is new.
        before = [{"id": "abc", "summary": "Sync", "start": {"dateTime": "2026-09-12T09:00:00Z"}}]
        after = [{"id": "abc", "summary": "Sync", "start": {"dateTime": "2026-09-14T09:00:00Z"}},
                 {"id": "abc_20260915T090000Z", "summary": "Sync", "start": {"dateTime": "2026-09-15T09:00:00Z"}}]
        first = collectors.extract_items(self.spec(), before, toolkit="googlecalendar", now=NOW)
        second = collectors.extract_items(self.spec(), after, toolkit="googlecalendar", now=NOW)
        self.assertEqual([it["key"] for it in first], ["abc"])
        self.assertEqual(sorted(it["key"] for it in second), ["abc", "abc_20260915T090000Z"])
        self.assertNotEqual(first[0]["ts"], [it for it in second if it["key"] == "abc"][0]["ts"])


class ExtractNotionTests(unittest.TestCase):
    def spec(self) -> dict:
        return collectors.collector("notion", "recent_pages")

    def test_pages_under_data_results(self) -> None:
        payload = {"successful": True, "data": {"results": [
            {"object": "page", "id": "p1", "url": "https://www.notion.so/p1", "last_edited_time": "2026-09-11T09:00:00.000Z",
             "properties": {"title": {"type": "title", "title": [{"plain_text": "Roadmap"}]}}},
            {"object": "page", "id": "p2", "url": "https://www.notion.so/p2", "last_edited_time": "2026-09-11T10:00:00.000Z",
             "properties": {"Name": {"type": "title", "title": [{"plain_text": "Task two"}, {"plain_text": " (ignored)"}]}}},
            {"object": "page", "id": "p3", "url": "https://www.notion.so/p3", "created_time": "2026-09-10T10:00:00Z",
             "properties": {"Status": {"type": "select"},
                            "Task name": {"type": "title", "title": [{"plain_text": "Custom "}, {"plain_text": "prop"}]}}},
            {"object": "database", "id": "d1", "url": "notion://d1", "last_edited_time": "2026-09-09T10:00:00Z",
             "title": [{"plain_text": "a rich text list, not a string"}]},
            {"object": "page", "id": "p4", "properties": {"Name": {"type": "title", "title": []}}},
        ]}}
        items = collectors.extract_items(self.spec(), payload, toolkit="notion", now=NOW)
        by_key = {it["key"]: it for it in items}
        self.assertEqual([it["key"] for it in items], ["p4", "p2", "p1", "p3", "d1"])
        self.assertEqual((by_key["p1"]["title"], by_key["p1"]["url"], by_key["p1"]["ts"], by_key["p1"]["body"]),
                         ("Roadmap", "https://www.notion.so/p1", "2026-09-11T09:00:00Z", ""))
        self.assertEqual(by_key["p2"]["title"], "Task two", "the spec's key names the first rich-text part only")
        self.assertEqual(by_key["p3"]["title"], "Custom prop", "no title key hit: the property of type title, parts joined")
        self.assertEqual(by_key["p3"]["ts"], "2026-09-10T10:00:00Z")
        self.assertEqual((by_key["d1"]["title"], by_key["d1"]["url"]), ("Recently edited pages: d1", None))
        self.assertEqual((by_key["p4"]["title"], by_key["p4"]["ts"], by_key["p4"]["type"]),
                         ("Recently edited pages: p4", NOW_ISO, "recent_pages"))

    def test_response_data_and_direct_shapes(self) -> None:
        row = {"id": "p9", "title": "plain title string", "url": "https://www.notion.so/p9"}
        for payload in ({"data": {"response_data": {"results": [row]}}}, {"results": [row]}, [row]):
            items = collectors.extract_items(self.spec(), payload, toolkit="notion", now=NOW)
            self.assertEqual([(it["key"], it["title"]) for it in items], [("p9", "plain title string")])


if __name__ == "__main__":
    unittest.main()
