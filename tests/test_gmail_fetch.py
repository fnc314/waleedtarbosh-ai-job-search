"""Offline tests for the scheduled Gmail fetcher.

These cover the parts that decide *what* is fetched and how a Composio message
becomes an inbox payload. They never call Composio and never touch Firestore;
the CLI round trip is exercised by running the wrapper by hand.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import gmail_fetch  # noqa: E402


LABELS = [
    {"id": "CATEGORY_PERSONAL", "name": "CATEGORY_PERSONAL", "type": "system"},
    {"id": "Label_1", "name": "JOBS", "type": "user"},
    {"id": "Label_2", "name": "JOBS/Applications", "type": "user"},
    {"id": "Label_3", "name": "JOBS/Recruiters", "type": "user"},
]


class TestQuery(unittest.TestCase):
    def test_default_scope_includes_categories_jobs_and_children(self):
        query = gmail_fetch.buildQuery(LABELS, None, None)
        self.assertIn("category:primary", query)
        self.assertIn("category:updates", query)
        self.assertIn('label:"JOBS"', query)
        self.assertIn('label:"JOBS/Applications"', query)
        self.assertIn('label:"JOBS/Recruiters"', query)
        self.assertIn("newer_than:30d", query)

    def test_an_explicit_since_replaces_the_lookback(self):
        query = gmail_fetch.buildQuery(LABELS, None, "2026-10-01")
        self.assertIn("after:2026/10/01", query)
        self.assertNotIn("newer_than:", query)

    def test_a_single_label_drops_the_default_scope(self):
        label = gmail_fetch.selectLabel(LABELS, "jobs/applications")
        query = gmail_fetch.buildQuery(LABELS, label, None)
        self.assertEqual(query, 'newer_than:30d label:"JOBS/Applications"')

    def test_an_id_is_accepted_as_a_label_selector(self):
        self.assertEqual(
            gmail_fetch.selectLabel(LABELS, "Label_2")["name"], "JOBS/Applications"
        )

    def test_an_unknown_label_lists_the_real_choices(self):
        with self.assertRaises(gmail_fetch.FetchError) as caught:
            gmail_fetch.selectLabel(LABELS, "Jobz")
        self.assertIn("Unknown label", str(caught.exception))
        self.assertIn("JOBS/Applications", str(caught.exception))

    def test_an_invalid_since_is_rejected_before_fetching(self):
        with self.assertRaises(gmail_fetch.FetchError):
            gmail_fetch.normalizeSince("10/01/2026")


class TestMessageMapping(unittest.TestCase):
    def test_body_text_is_preferred_and_truncated_into_the_snippet(self):
        message = gmail_fetch.toInboxMessage(
            {
                "messageId": "m1",
                "subject": "Update on your application",
                "sender": "recruiter@example.com",
                "messageTimestamp": "2026-10-30T10:00:00Z",
                "messageText": "Unfortunately we are not moving forward.",
            }
        )
        self.assertEqual(message["messageId"], "m1")
        self.assertEqual(message["sender"], "recruiter@example.com")
        self.assertEqual(message["receivedAt"], "2026-10-30T10:00:00+00:00")
        self.assertIn("Unfortunately", message["snippet"])
        self.assertLessEqual(len(message["snippet"]), 800)

    def test_preview_body_is_the_fallback_when_message_text_is_empty(self):
        message = gmail_fetch.toInboxMessage(
            {
                "messageId": "m2",
                "subject": "Hello",
                "sender": "a@b.c",
                "messageTimestamp": "2026-10-30T10:00:00Z",
                "messageText": "",
                "preview": {"body": "Preview text"},
            }
        )
        self.assertIn("Preview text", message["snippet"])

    def test_messages_without_an_id_are_dropped_by_the_caller(self):
        message = gmail_fetch.toInboxMessage(
            {"messageId": "", "subject": "x", "sender": "a@b.c"}
        )
        self.assertEqual(message["messageId"], "")


class TestPaging(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.original = gmail_fetch.runTool

    def tearDown(self):
        gmail_fetch.runTool = self.original

    def stub(self, pages):
        """Serve `pages` in order and record each request."""
        remaining = list(pages)

        def fakeRunTool(slug, arguments, **kwargs):
            self.calls.append(arguments)
            return remaining.pop(0)

        gmail_fetch.runTool = fakeRunTool

    def test_messages_are_collected_across_pages(self):
        self.stub(
            [
                {"messages": [{"messageId": "a"}], "nextPageToken": "t1"},
                {"messages": [{"messageId": "b"}], "nextPageToken": "t2"},
                {"messages": [{"messageId": "c"}]},
            ]
        )
        messages = gmail_fetch.fetchMessages("q", 10, pageSize=1)
        self.assertEqual([m["messageId"] for m in messages], ["a", "b", "c"])
        self.assertNotIn("page_token", self.calls[0])
        self.assertEqual(self.calls[1]["page_token"], "t1")
        self.assertEqual(self.calls[2]["page_token"], "t2")

    def test_no_page_is_ever_larger_than_the_payload_cap(self):
        # Composio fails above ~50 full messages; the page size must protect
        # the fetch even when a caller asks for more.
        self.stub([{"messages": [], "nextPageToken": None}])
        gmail_fetch.fetchMessages("q", 500, pageSize=500)
        self.assertEqual(self.calls[0]["max_results"], 50)

    def test_paging_stops_at_the_requested_total(self):
        self.stub(
            [
                {"messages": [{"messageId": "a"}, {"messageId": "b"}], "nextPageToken": "t1"},
            ]
        )
        messages = gmail_fetch.fetchMessages("q", 2, pageSize=2)
        self.assertEqual(len(messages), 2)
        self.assertEqual(len(self.calls), 1)

    def test_an_exhausted_mailbox_ends_paging(self):
        self.stub([{"messages": [{"messageId": "a"}], "nextPageToken": None}])
        self.assertEqual(len(gmail_fetch.fetchMessages("q", 10)), 1)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
