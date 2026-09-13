"""The collectors catalog and the payload mapping into events.jsonl lines.

Pure functions over fixture payloads: nothing here touches the filesystem
or the network. Every tool slug the catalog names is checked against the
Composio categories table so a write tool can never sneak into a poll."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from services.connections import collectors
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
                    # a url source is always declared, even when a toolkit has none to offer
                    self.assertTrue("url_keys" in spec or "url_template" in spec)
                    self.assertIsInstance(spec["args"], dict)
            identity = collectors.identity_spec(toolkit)
            if identity is not None:
                with self.subTest(toolkit=toolkit, identity=identity["tool"]):
                    self.assertEqual(set(identity), {"tool", "args", "keys"})
                    self.assertEqual(categories.classify(toolkit, identity["tool"]), "read")
                    self.assertIsInstance(identity["args"], dict)
                    self.assertTrue(identity["keys"])

    def test_catalog_contents(self) -> None:
        self.assertEqual([s["id"] for s in collectors.catalog("gmail")], ["unread", "inbox"])
        self.assertEqual(collectors.default_ids("gmail"), ["unread"])
        self.assertEqual(gmail_spec("unread")["tool"], "GMAIL_FETCH_EMAILS")
        self.assertEqual(gmail_spec("unread")["args"], {"query": "is:unread", "max_results": 20})
        self.assertEqual(gmail_spec("inbox")["args"], {"query": "in:inbox newer_than:1d", "max_results": 20})
        self.assertEqual(gmail_spec("inbox")["label"], "New mail in Inbox (last day)")
        self.assertEqual([s["id"] for s in collectors.catalog("googlecalendar")], ["upcoming"])
        self.assertEqual(collectors.collector("googlecalendar", "upcoming")["tool"], "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS")
        self.assertEqual(collectors.default_ids("googlecalendar"), ["upcoming"])
        notion = collectors.collector("notion", "recent_pages")
        self.assertEqual(notion["tool"], "NOTION_SEARCH_NOTION_PAGE")
        self.assertEqual(categories.classify("notion", "NOTION_SEARCH_NOTION_PAGE"), "read")
        self.assertEqual(notion["args"]["sort"], {"direction": "descending", "timestamp": "last_edited_time"})
        self.assertEqual(collectors.default_ids("notion"), ["recent_pages"])
        slack = collectors.collector("slack", "recent")
        self.assertEqual(slack["tool"], "SLACK_SEARCH_MESSAGES")
        self.assertEqual(slack["args"]["query"], "after:{yesterday_date}")
        self.assertEqual(collectors.default_ids("slack"), ["recent"])
        telegram = collectors.collector("telegram", "updates")
        self.assertEqual(telegram["tool"], "TELEGRAM_GET_UPDATES")
        self.assertEqual(telegram["args"], {"limit": 50, "allowed_updates": ["message", "channel_post"]})
        self.assertEqual(telegram["url_keys"], [])
        self.assertEqual(collectors.default_ids("telegram"), ["updates"])
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


class IdentityTests(unittest.TestCase):
    """The identity catalog and the label read out of each tool's answer,
    in the shapes the live Composio session answers with."""

    GMAIL = {"successful": True, "error": None,
             "data": {"emailAddress": "ana@example.com", "historyId": "12345", "messagesTotal": 10,
                      "threadsTotal": 8, "display_url": "https://mail.google.com/"}}
    CALENDAR = {"successful": True, "error": None,
                "data": {"calendar_data": {"id": "ana@example.com", "summary": "ana@example.com",
                                           "timeZone": "Europe/Lisbon"},
                         "display_url": "https://calendar.google.com/"}}

    def test_spec_lookups(self) -> None:
        gmail = collectors.identity_spec("gmail")
        self.assertEqual(gmail, {"tool": "GMAIL_GET_PROFILE", "args": {"user_id": "me"},
                                 "keys": ["emailAddress", "data.emailAddress"]})
        calendar = collectors.identity_spec("googlecalendar")
        self.assertEqual((calendar["tool"], calendar["args"]), ("GOOGLECALENDAR_GET_CALENDAR", {"calendar_id": "primary"}))
        self.assertEqual(calendar["keys"][:2], ["calendar_data.id", "data.calendar_data.id"])
        for toolkit in ("notion", "slack", "telegram", "figma", "googledocs", "not_a_toolkit"):
            self.assertIsNone(collectors.identity_spec(toolkit), toolkit)
        gmail["args"]["user_id"] = "changed"
        gmail["keys"].append("x")
        self.assertEqual(collectors.identity_spec("gmail")["args"], {"user_id": "me"}, "a copy every time")
        self.assertNotIn("x", collectors.identity_spec("gmail")["keys"])

    def test_extracts_the_email_from_both_real_shapes(self) -> None:
        self.assertEqual(collectors.extract_identity(collectors.identity_spec("gmail"), self.GMAIL), "ana@example.com")
        self.assertEqual(collectors.extract_identity(collectors.identity_spec("googlecalendar"), self.CALENDAR),
                         "ana@example.com")
        # the unwrapped payloads (a plain MCP server answering without the envelope)
        self.assertEqual(collectors.extract_identity(collectors.identity_spec("gmail"), self.GMAIL["data"]),
                         "ana@example.com")
        self.assertEqual(collectors.extract_identity(collectors.identity_spec("googlecalendar"), self.CALENDAR["data"]),
                         "ana@example.com")
        # a calendar whose id is not an address falls through to its summary
        payload = {"data": {"calendar_data": {"id": "", "summary": "  Ana Lima  "}}}
        self.assertEqual(collectors.extract_identity(collectors.identity_spec("googlecalendar"), payload), "Ana Lima")

    def test_missing_blank_or_non_string_values_read_as_none(self) -> None:
        spec = collectors.identity_spec("gmail")
        for payload in ({"data": {"emailAddress": ""}}, {"data": {"emailAddress": "   "}},
                        {"data": {"emailAddress": 7}}, {"data": {"emailAddress": ["a@b"]}},
                        {"data": {}}, {"successful": False, "data": None, "error": "nope"}, {}, None, "junk", []):
            with self.subTest(payload=payload):
                self.assertIsNone(collectors.extract_identity(spec, payload))
        self.assertIsNone(collectors.extract_identity(None, self.GMAIL), "no spec, no label")
        self.assertIsNone(collectors.extract_identity({"keys": []}, self.GMAIL))

    def test_label_is_stripped_and_capped(self) -> None:
        spec = collectors.identity_spec("gmail")
        self.assertEqual(collectors.extract_identity(spec, {"emailAddress": "  a@b.c \n"}), "a@b.c")
        long = collectors.extract_identity(spec, {"emailAddress": "x" * 500})
        self.assertEqual(len(long), collectors.IDENTITY_LABEL_MAX)
        self.assertEqual(collectors.IDENTITY_LABEL_MAX, 200)


class RenderArgsTests(unittest.TestCase):
    def test_calendar_placeholders(self) -> None:
        spec = collectors.collector("googlecalendar", "upcoming")
        before = copy.deepcopy(spec)
        args = collectors.render_args(spec, NOW)
        self.assertEqual(args["time_min"], NOW_ISO)
        self.assertEqual(args["time_max"], "2026-09-18T12:00:00Z")
        self.assertIs(args["single_events"], True)
        self.assertEqual(args["max_results_per_calendar"], 25)
        self.assertEqual(args["response_detail"], "full", "the events array, not only the summary view")
        self.assertNotIn("calendarId", args, "every calendar in the account's list, not primary alone")
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
    """The all-calendars tool wraps each event as ``{event, source_calendar_id,
    source_calendar_summary}``; the mapping reads through ``event.`` and falls
    back to the calendar's name for the body."""

    def spec(self) -> dict:
        return collectors.collector("googlecalendar", "upcoming")

    @staticmethod
    def wrap(event: dict, calendar: str = "Team") -> dict:
        return {"event": event, "source_calendar_id": calendar.lower() + "@group.calendar.google.com",
                "source_calendar_summary": calendar}

    def test_events_across_calendars_with_html_link(self) -> None:
        payload = {"successful": True, "data": {"events": [
            self.wrap({"id": "ev1", "summary": "Standup", "description": "daily", "location": "Room 1",
                       "start": {"dateTime": "2026-09-12T09:00:00+02:00"},
                       "htmlLink": "https://calendar.google.com/event?eid=1", "updated": "2026-09-01T00:00:00Z"}),
            self.wrap({"id": "ev2", "summary": "Offsite", "location": "Lisbon", "start": {"date": "2026-09-13"},
                       "htmlLink": "http://calendar.google.com/event?eid=2"}, calendar="Personal"),
            self.wrap({"id": "ev3", "start": {}, "updated": "2026-09-10T00:00:00Z", "htmlLink": "ftp://nope"}),
        ], "summary_view": [], "calendars_queried": [{"id": "primary"}], "errors_by_calendar": {}}}
        items = collectors.extract_items(self.spec(), payload, toolkit="googlecalendar", now=NOW)
        self.assertEqual([it["key"] for it in items], ["ev2", "ev1", "ev3"])
        ev1, ev2, ev3 = items[1], items[0], items[2]
        self.assertEqual((ev1["title"], ev1["body"], ev1["ts"], ev1["url"], ev1["type"], ev1["toolkit"]),
                         ("Standup", "daily", "2026-09-12T07:00:00Z", "https://calendar.google.com/event?eid=1",
                          "upcoming", "googlecalendar"))
        self.assertEqual((ev2["body"], ev2["ts"], ev2["url"]), ("Lisbon", "2026-09-13T00:00:00Z", "http://calendar.google.com/event?eid=2"))
        # no summary, description or location: the calendar's name is the body and the label the title
        self.assertEqual((ev3["title"], ev3["body"], ev3["ts"], ev3["url"]),
                         ("Upcoming events (next 7 days): ev3", "Team", "2026-09-10T00:00:00Z", None))

    def test_a_primary_only_shape_maps_nothing(self) -> None:
        """The old single-calendar answer (``data.items``) is not read any
        more: pinned so a silent regression to it cannot look like success."""
        payload = {"successful": True, "data": {"items": [{"id": "x", "summary": "S"}]}}
        self.assertEqual(collectors.extract_items(self.spec(), payload, toolkit="googlecalendar", now=NOW), [])

    def test_summary_view_is_the_fallback_for_a_minimal_answer(self) -> None:
        payload = {"successful": True, "data": {"summary_view": [
            {"event_id": "sv1", "title": "Review", "start": "2026-09-12T09:00:00Z", "end": "2026-09-12T10:00:00Z",
             "calendar": "Team", "is_all_day": False, "display_url": "https://calendar.google.com/event?eid=sv1"}]}}
        items = collectors.extract_items(self.spec(), payload, toolkit="googlecalendar", now=NOW)
        self.assertEqual([(it["key"], it["title"], it["body"], it["ts"], it["url"]) for it in items],
                         [("sv1", "Review", "Team", "2026-09-12T09:00:00Z", "https://calendar.google.com/event?eid=sv1")])

    def test_top_level_events_list_key(self) -> None:
        items = collectors.extract_items(self.spec(), {"events": [self.wrap({"id": "x", "summary": "S"})]},
                                         toolkit="googlecalendar", now=NOW)
        self.assertEqual([it["key"] for it in items], ["x"])

    def test_dedupe_key_is_the_id_only_so_a_reschedule_is_not_surfaced_again(self) -> None:
        # Pinned on purpose: a rescheduled event keeps its id and therefore its key; the
        # poller's seen set drops it. A recurring instance carries its own id and is new.
        before = {"events": [self.wrap({"id": "abc", "summary": "Sync", "start": {"dateTime": "2026-09-12T09:00:00Z"}})]}
        after = {"events": [self.wrap({"id": "abc", "summary": "Sync", "start": {"dateTime": "2026-09-14T09:00:00Z"}}),
                            self.wrap({"id": "abc_20260915T090000Z", "summary": "Sync",
                                       "start": {"dateTime": "2026-09-15T09:00:00Z"}})]}
        first = collectors.extract_items(self.spec(), before, toolkit="googlecalendar", now=NOW)
        second = collectors.extract_items(self.spec(), after, toolkit="googlecalendar", now=NOW)
        self.assertEqual([it["key"] for it in first], ["abc"])
        self.assertEqual(sorted(it["key"] for it in second), ["abc", "abc_20260915T090000Z"])
        self.assertNotEqual(first[0]["ts"], [it for it in second if it["key"] == "abc"][0]["ts"])


class ExtractWarningsTests(unittest.TestCase):
    """``warn_keys``: partial failures inside a successful envelope become one
    line for ``last_error``; nothing declared, or an empty container, is
    no warning."""

    def test_errors_by_calendar_become_one_line(self) -> None:
        spec = collectors.collector("googlecalendar", "upcoming")
        payload = {"successful": True, "data": {"events": [], "errors_by_calendar": {
            "a@group.calendar.google.com": "403 Quota exceeded for quota metric 'Queries'"}}}
        lines = collectors.extract_warnings(spec, payload)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("1 calendar(s) failed: a@group.calendar.google.com: 403 Quota exceeded"), lines[0])

    def test_many_failures_are_counted_not_listed(self) -> None:
        spec = collectors.collector("googlecalendar", "upcoming")
        failures = {f"c{i}@x": f"boom {i}" for i in range(5)}
        lines = collectors.extract_warnings(spec, {"successful": True, "data": {"errors_by_calendar": failures}})
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("5 calendar(s) failed: "))
        self.assertTrue(lines[0].endswith("; and 2 more"), lines[0])

    def test_empty_absent_or_undeclared_is_no_warning(self) -> None:
        spec = collectors.collector("googlecalendar", "upcoming")
        self.assertEqual(collectors.extract_warnings(spec, {"successful": True, "data": {"errors_by_calendar": {}}}), [])
        self.assertEqual(collectors.extract_warnings(spec, {"successful": True, "data": {"events": []}}), [])
        self.assertEqual(collectors.extract_warnings(spec, "not a dict"), [])
        gmail = collectors.collector("gmail", "unread")
        self.assertEqual(collectors.extract_warnings(gmail, {"successful": True, "data": {"errors_by_calendar": {"a": "b"}}}), [])

    def test_a_list_of_messages_is_accepted(self) -> None:
        spec = {"warn_keys": ["data.problems"], "warn_label": "page"}
        lines = collectors.extract_warnings(spec, {"data": {"problems": ["p1 failed", "p2 failed"]}})
        self.assertEqual(lines, ["2 page(s) failed: p1 failed; p2 failed"])


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


class SlackAndTelegramTests(unittest.TestCase):
    NOW = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)

    def test_yesterday_placeholder_renders_a_date(self) -> None:
        args = collectors.render_args(collectors.collector("slack", "recent"), self.NOW)
        self.assertEqual(args, {"query": "after:2026-09-10", "sort": "timestamp", "sort_dir": "desc", "count": 20})

    def test_slack_search_matches_map_to_events(self) -> None:
        payload = {"successful": True, "data": {"messages": {"matches": [
            {"iid": "a1", "ts": "1757584800.000100", "text": "deploy is green", "username": "ana",
             "channel": {"id": "C1", "name": "eng"},
             "permalink": "https://x.slack.com/archives/C1/p1757584800000100"},
            {"ts": "1757584700.000200", "text": "no iid, ts is the id"},
        ]}}}
        items = collectors.extract_items(collectors.collector("slack", "recent"), payload,
                                         toolkit="slack", now=self.NOW)
        self.assertEqual([(i["key"], i["title"], i["body"], i["url"]) for i in items], [
            ("a1", "deploy is green", "eng", "https://x.slack.com/archives/C1/p1757584800000100"),
            ("1757584700.000200", "no iid, ts is the id", "", None),
        ])
        self.assertEqual(items[0]["ts"], "2025-09-11T10:00:00Z")   # the decimal epoch ts parses
        self.assertEqual(items[0]["type"], "recent")

    def test_telegram_updates_map_to_events_without_urls(self) -> None:
        payload = {"successful": True, "data": {"result": [
            {"update_id": 900, "message": {"message_id": 5, "date": 1757584800, "text": "hello bot",
                                           "chat": {"id": 1, "title": "Ops room", "type": "group"},
                                           "from": {"first_name": "Ana", "username": "ana"}}},
            {"update_id": 901, "channel_post": {"message_id": 6, "date": 1757584860, "text": "release notes",
                                                "chat": {"id": 2, "title": "Announcements", "type": "channel"}}},
            {"update_id": 902, "message": {"message_id": 7, "date": 1757584900,
                                           "chat": {"id": 1, "type": "private"}}},
        ]}}
        items = collectors.extract_items(collectors.collector("telegram", "updates"), payload,
                                         toolkit="telegram", now=self.NOW)
        self.assertEqual([(i["key"], i["title"], i["body"], i["url"]) for i in items], [
            ("902", "New messages to the bot: 902", "", None),        # no text: the label fallback
            ("901", "release notes", "Announcements", None),
            ("900", "hello bot", "Ops room", None),
        ])
        self.assertEqual(items[2]["ts"], "2025-09-11T10:00:00Z")

    def test_slack_and_telegram_are_classified_for_per_action_control(self) -> None:
        self.assertEqual(categories.classify("slack", "SLACK_SEARCH_MESSAGES"), "read")
        self.assertEqual(categories.classify("slack", "SLACK_SEND_MESSAGE"), "write")
        self.assertEqual(categories.classify("telegram", "TELEGRAM_GET_UPDATES"), "read")
        self.assertEqual(categories.classify("telegram", "TELEGRAM_SEND_MESSAGE"), "write")
        self.assertEqual(TOOLKITS["slack"].schemes, ("OAUTH2",))
        self.assertEqual(TOOLKITS["telegram"].schemes, ("API_KEY",))
